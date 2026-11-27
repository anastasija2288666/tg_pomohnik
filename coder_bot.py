import asyncio
import logging
import os
import re
import sqlite3
from io import BytesIO
from typing import Dict, List

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

DEFAULT_SYSTEM_PROMPT = """Ты — персональный ИИ-напарник и разработчик для создания Telegram-ботов, парсеров, веб-приложений и сайтов.
Ты идеально знаешь рабочий стек и привычки пользователя:
1. Язык и библиотеки: Python 3, aiogram 3.18+, Telethon, aiohttp, sqlite3, HTML/CSS/JavaScript.
2. Инфраструктура и деплой:
   - Боты загружаются в публичные репозитории на GitHub и запускаются 24/7 на бесплатном тарифе облачного хостинга Bothost.ru.
   - На Bothost используются переменные окружения (BOT_TOKEN, ADMIN_ID и др.), а также указывается главный файл (main.py или другой .py) и requirements.txt.
   - Сайты и Telegram Mini Apps размещаются на GitHub Pages.
3. Правила написания кода:
   - Всегда пиши полный, рабочий, проверенный код без сокращений, заглушек и комментариев вида "# здесь ваш код".
   - Используй чистый русский язык в интерфейсах ботов, понятные кнопки, HTML-форматирование и защиту админ-функций через ADMIN_ID.
   - Если в ответе есть код файла, обязательно оформляй его в блок кода с указанием имени файла в первой строке или перед блоком (например, `main.py` или `requirements.txt`), чтобы система автоматически прикрепила его готовым документом.
4. Стиль общения:
   - Отвечай дружелюбно, чётко, по делу, простыми словами без сложных академических терминов.
   - После кода давай краткую пошаговую инструкцию из 3-4 пунктов для загрузки на GitHub и запуска на Bothost."""

DB_PATH = "coder_assistant.db"


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("system_prompt", DEFAULT_SYSTEM_PROMPT),
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("provider", "auto"),
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("openrouter_key", OPENROUTER_API_KEY),
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("gemini_key", GEMINI_API_KEY),
    )
    cur.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        ("openrouter_model", "qwen/qwen3-coder:free"),
    )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else default


def set_setting(key: str, value: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def add_history(role: str, content: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
    cur.execute(
        "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 16)"
    )
    conn.commit()
    conn.close()


def get_history() -> List[Dict[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role, content FROM history ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def clear_history() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM history")
    conn.commit()
    conn.close()


class SettingsStates(StatesGroup):
    waiting_api_key = State()
    waiting_extra_memory = State()


router = Router()


def is_owner(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧹 Новый проект (очистить чат)", callback_data="clear_chat"),
            ],
            [
                InlineKeyboardButton(text="🧠 Память и стек бота", callback_data="memory_menu"),
                InlineKeyboardButton(text="🔑 API-ключи ИИ", callback_data="keys_menu"),
            ],
            [
                InlineKeyboardButton(text="💡 Идеи промптов", callback_data="prompts_help"),
            ],
        ]
    )


def keys_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌐 Указать ключ OpenRouter (бесплатно)", callback_data="set_or_key"),
            ],
            [
                InlineKeyboardButton(text="✨ Указать ключ Google Gemini (бесплатно)", callback_data="set_gem_key"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main"),
            ],
        ]
    )


def memory_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Дополнить память бота", callback_data="add_memory"),
            ],
            [
                InlineKeyboardButton(text="🔄 Сбросить память к стандарту", callback_data="reset_memory"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main"),
            ],
        ]
    )


async def call_openrouter(messages: List[Dict[str, str]], api_key: str) -> str:
    model = get_setting("openrouter_model", "qwen/qwen3-coder:free")
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://bothost.ru",
        "X-Title": "Personal AI Coder Bot",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            data = await resp.json()
            if resp.status != 200:
                err_msg = data.get("error", {}).get("message", str(data))
                if model != "meta-llama/llama-3.3-70b-instruct:free":
                    payload["model"] = "meta-llama/llama-3.3-70b-instruct:free"
                    async with session.post(url, headers=headers, json=payload, timeout=120) as resp2:
                        data2 = await resp2.json()
                        if resp2.status == 200:
                            return data2["choices"][0]["message"]["content"]
                raise RuntimeError(f"OpenRouter ошибка: {err_msg}")
            return data["choices"][0]["message"]["content"]


async def call_gemini(system_prompt: str, history: List[Dict[str, str]], api_key: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.3},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=120) as resp:
            data = await resp.json()
            if resp.status != 200:
                err_msg = data.get("error", {}).get("message", str(data))
                raise RuntimeError(f"Gemini ошибка: {err_msg}")
            return data["candidates"][0]["content"]["parts"][0]["text"]


async def generate_ai_reply(user_text: str) -> str:
    system_prompt = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    add_history("user", user_text)
    history = get_history()

    or_key = get_setting("openrouter_key", "").strip() or OPENROUTER_API_KEY.strip()
    gem_key = get_setting("gemini_key", "").strip() or GEMINI_API_KEY.strip()

    if gem_key:
        reply = await call_gemini(system_prompt, history, gem_key)
    elif or_key:
        messages = [{"role": "system", "content": system_prompt}] + history
        reply = await call_openrouter(messages, or_key)
    else:
        raise RuntimeError(
            "Не указан API-ключ нейросети. Нажмите «🔑 API-ключи ИИ» в меню и вставьте бесплатный ключ от Google AI Studio (Gemini) или OpenRouter."
        )

    add_history("assistant", reply)
    return reply


def extract_code_files(text: str) -> List[tuple[str, str]]:
    files: List[tuple[str, str]] = []
    pattern = re.compile(
        r"(?:`([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))`[^
]*
+)?```([a-zA-Z0-9_-]*)
(.*?)```",
        re.DOTALL,
    )
    counters = {"python": 0, "html": 0, "css": 0, "js": 0, "text": 0}

    for match in pattern.finditer(text):
        explicit_name = match.group(1)
        lang = (match.group(2) or "").lower().strip()
        code = match.group(3).strip()
        if not code:
            continue

        if explicit_name:
            filename = explicit_name
        elif lang in ("python", "py"):
            counters["python"] += 1
            filename = "main.py" if counters["python"] == 1 else f"script_{counters['python']}.py"
        elif lang == "html":
            counters["html"] += 1
            filename = "index.html" if counters["html"] == 1 else f"page_{counters['html']}.html"
        elif lang == "css":
            counters["css"] += 1
            filename = "style.css" if counters["css"] == 1 else f"style_{counters['css']}.css"
        elif lang in ("js", "javascript"):
            counters["js"] += 1
            filename = "script.js" if counters["js"] == 1 else f"script_{counters['js']}.js"
        else:
            if "aiogram" in code or "telethon" in code:
                filename = "requirements.txt"
            else:
                counters["text"] += 1
                filename = f"file_{counters['text']}.txt"

        files.append((filename, code))
    return files


def strip_code_blocks(text: str) -> str:
    cleaned = re.sub(r"```[a-zA-Z0-9_-]*
.*?```", "
📎 <i>(Код прикреплён готовым файлом ниже)</i>
", text, flags=re.DOTALL)
    cleaned = re.sub(r"
{3,}", "

", cleaned).strip()
    return cleaned


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Это приватный бот-помощник разработчика.")
        return

    await state.clear()
    or_key = get_setting("openrouter_key", "").strip() or OPENROUTER_API_KEY.strip()
    gem_key = get_setting("gemini_key", "").strip() or GEMINI_API_KEY.strip()
    status_ai = "✅ Подключён" if (or_key or gem_key) else "⚠️ Нужен бесплатный ключ"

    text = (
        "👩‍💻 <b>Твой личный ИИ-напарник по коду на связи!</b>

"
        "Я уже знаю твой рабочий стек:
"
        "• <b>Боты и парсеры:</b> Python, aiogram 3, Telethon, SQLite
"
        "• <b>Сайты и Mini Apps:</b> HTML, CSS, JS, GitHub Pages
"
        "• <b>Деплой:</b> GitHub + бесплатный хостинг Bothost.ru

"
        f"🧠 <b>Статус нейросети:</b> {status_ai}

"
        "Просто напиши, какого бота, парсер или сайт нужно собрать — я напишу полный код, пришлю готовые файлы для скачивания и дам инструкцию по запуску!"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        return
    await state.clear()
    or_key = get_setting("openrouter_key", "").strip() or OPENROUTER_API_KEY.strip()
    gem_key = get_setting("gemini_key", "").strip() or GEMINI_API_KEY.strip()
    status_ai = "✅ Подключён" if (or_key or gem_key) else "⚠️ Нужен бесплатный ключ"

    await callback.message.edit_text(
        f"👩‍💻 <b>Главное меню ИИ-напарника</b>

"
        f"🧠 <b>Статус нейросети:</b> {status_ai}

"
        "Напиши задачу прямо в чат или выбери раздел ниже:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "clear_chat")
async def cb_clear_chat(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return
    clear_history()
    await callback.answer("Контекст диалога очищен! Начинаем новый проект.", show_alert=True)


@router.callback_query(F.data == "keys_menu")
async def cb_keys_menu(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return
    or_key = get_setting("openrouter_key", "").strip() or OPENROUTER_API_KEY.strip()
    gem_key = get_setting("gemini_key", "").strip() or GEMINI_API_KEY.strip()

    text = (
        "🔑 <b>Настройка бесплатного API-ключа</b>

"
        f"• <b>Google Gemini:</b> {'✅ установлен' if gem_key else '❌ не задан'}
"
        f"• <b>OpenRouter:</b> {'✅ установлен' if or_key else '❌ не задан'}

"
        "<b>Где взять ключ бесплатно за 1 минуту:</b>
"
        "1. <b>Google AI Studio</b> (aistudio.google.com) -> Create API key (работает без привязки карты).
"
        "2. <b>OpenRouter</b> (openrouter.ai) -> Keys -> Create Key (бесплатные модели для кода Qwen/Llama)."
    )
    await callback.message.edit_text(text, reply_markup=keys_menu_kb())
    await callback.answer()


@router.callback_query(F.data.in_({"set_or_key", "set_gem_key"}))
async def cb_ask_key(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        return
    key_type = "openrouter_key" if callback.data == "set_or_key" else "gemini_key"
    label = "OpenRouter (начинается с sk-or-...)" if key_type == "openrouter_key" else "Google Gemini (начинается с AIza...)"
    await state.update_data(key_type=key_type)
    await state.set_state(SettingsStates.waiting_api_key)
    await callback.message.edit_text(
        f"🔑 Отправь в чат свой API-ключ <b>{label}</b>:

"
        "<i>Ключ сохранится в базе бота и сразу начнёт работать.</i>"
    )
    await callback.answer()


@router.message(SettingsStates.waiting_api_key)
async def process_api_key(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    data = await state.get_data()
    key_type = data.get("key_type", "gemini_key")
    key_val = (message.text or "").strip()
    if len(key_val) < 15:
        await message.answer("⚠️ Ключ слишком короткий. Проверь и отправь его целиком.")
        return

    set_setting(key_type, key_val)
    if key_type == "gemini_key":
        set_setting("openrouter_key", "")
    else:
        set_setting("gemini_key", "")

    await state.clear()
    await message.answer(
        "✅ <b>API-ключ успешно сохранён!</b>
Теперь можешь сразу писать любую задачу на создание бота или сайта.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "memory_menu")
async def cb_memory_menu(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return
    current_prompt = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    preview = current_prompt[:600] + ("..." if len(current_prompt) > 600 else "")
    await callback.message.edit_text(
        "🧠 <b>Память и знания бота о твоей работе:</b>

"
        f"<code>{preview}</code>

"
        "Ты можешь дописать сюда любые новые правила, реквизиты или любимые фишки.",
        reply_markup=memory_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "add_memory")
async def cb_add_memory(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_owner(callback.from_user.id):
        return
    await state.set_state(SettingsStates.waiting_extra_memory)
    await callback.message.edit_text(
        "✍️ Напиши, что добавить в постоянную память бота:

"
        "<i>Например: «Всегда добавляй в ботов кнопку связи с менеджером @my_nick и используй зелёные эмодзи в меню».</i>"
    )
    await callback.answer()


@router.message(SettingsStates.waiting_extra_memory)
async def process_extra_memory(message: Message, state: FSMContext) -> None:
    if not is_owner(message.from_user.id):
        return
    extra = (message.text or "").strip()
    if extra:
        current = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
        set_setting("system_prompt", f"{current}

Дополнительное правило пользователя:
{extra}")
    await state.clear()
    await message.answer("✅ Новое правило добавлено в память бота!", reply_markup=main_menu_kb())


@router.callback_query(F.data == "reset_memory")
async def cb_reset_memory(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return
    set_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    await callback.answer("Память сброшена к стандартным настройкам!", show_alert=True)


@router.callback_query(F.data == "prompts_help")
async def cb_prompts_help(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        return
    text = (
        "💡 <b>Примеры запросов, которые можно писать боту:</b>

"
        "1️⃣ <i>«Сделай Telegram-бота для записи клиентов на наращивание ресниц с выбором даты, времени и уведомлением админу»</i>
"
        "2️⃣ <i>«Напиши одностраничный сайт-лендинг для мастера маникюра с прайсом и кнопкой записи в Telegram»</i>
"
        "3️⃣ <i>«Сделай бота-воронку, который проверяет подписку на канал @mychannel и после этого выдаёт PDF-гайд»</i>
"
        "4️⃣ <i>«Вот ошибка при запуске на Bothost: [вставь текст ошибки] — исправь код и пришли новый файл»</i>"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.message(F.text)
async def handle_user_prompt(message: Message) -> None:
    if not is_owner(message.from_user.id):
        return

    wait_msg = await message.answer("⏳ <i>Думаю и собираю решение...</i>")

    try:
        reply = await generate_ai_reply(message.text)
        code_files = extract_code_files(reply)
        clean_text = strip_code_blocks(reply)

        await wait_msg.delete()

        if clean_text:
            for i in range(0, len(clean_text), 3800):
                chunk = clean_text[i : i + 3800]
                try:
                    await message.answer(chunk, parse_mode=ParseMode.HTML)
                except Exception:
                    await message.answer(chunk, parse_mode=None)

        for filename, code_content in code_files:
            file_bytes = BytesIO(code_content.encode("utf-8"))
            doc = BufferedInputFile(file_bytes.getvalue(), filename=filename)
            await message.answer_document(doc, caption=f"📄 Готовый файл: <b>{filename}</b>")

    except Exception as exc:
        logger.exception("Ошибка генерации")
        await wait_msg.edit_text(
            f"❌ <b>Не удалось выполнить запрос:</b>
<code>{exc}</code>

"
            "Проверь API-ключ в меню «🔑 API-ключи ИИ» или попробуй ещё раз.",
            reply_markup=main_menu_kb(),
        )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения!")

    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
