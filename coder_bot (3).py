import os
import sys
import re
import json
import sqlite3
import asyncio
import logging
import subprocess

def ensure_packages():
    required = {"aiogram": "aiogram>=3.18.0", "aiohttp": "aiohttp"}
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

ensure_packages()

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

DB_PATH = "coder_assistant.db"

DEFAULT_STACK_MEMORY = (
    "Ты — персональный ИИ-напарник и старший разработчик на Python и веб-технологиях.\n"
    "Рабочий стек и привычки пользователя:\n"
    "1. Языки и фреймворки: Python 3, aiogram 3.x (асинхронные Telegram-боты), Telethon (юзерботы), SQLite, HTML5 + CSS3 + Vanilla JavaScript (сайты, лендинги, Telegram Mini Apps).\n"
    "2. Хостинг и деплой:\n"
    "   - Telegram-боты запускаются через связку GitHub + облачный хостинг Bothost.ru (бесплатный тариф).\n"
    "   - Секреты (BOT_TOKEN, ADMIN_ID и др.) читаются через os.getenv(...).\n"
    "   - Сайты и Mini Apps публикуются на GitHub Pages.\n"
    "3. Правила выдачи ответов:\n"
    "   - Пиши на русском языке, чётко, дружелюбно и по делу.\n"
    "   - Всегда выдавай ПОЛНЫЙ, рабочий код без заглушек и сокращений.\n"
    "   - Оформляй каждый файл в отдельный блок кода с указанием имени файла прямо перед блоком (например: `main.py` или `index.html`), чтобы бот автоматически прикреплял готовые файлы пользователю.\n"
    "   - Всегда добавляй файл `requirements.txt`, если используются сторонние библиотеки Python.\n"
    "   - В конце давай короткую пошаговую инструкцию по запуску через GitHub и Bothost."
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT)")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('provider', 'gemini')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gemini_key', '')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gemini_model', 'gemini-3.8-flash')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('openrouter_key', '')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('openrouter_model', 'qwen/qwen-2.5-coder-32b-instruct:free')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('stack_memory', ?)", (DEFAULT_STACK_MEMORY,))
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def add_history(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
    cur.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 16)")
    conn.commit()
    conn.close()

def get_history() -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role, content FROM history ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in rows]

def clear_history():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM history")
    conn.commit()
    conn.close()

class SetupStates(StatesGroup):
    waiting_gemini_key = State()
    waiting_gemini_model = State()
    waiting_openrouter_key = State()
    waiting_memory = State()

router = Router()

def main_menu_kb() -> InlineKeyboardMarkup:
    provider = get_setting("provider", "gemini")
    p_label = "Google Gemini" if provider == "gemini" else "OpenRouter"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔑 API-ключи ИИ ({p_label})", callback_data="menu_keys")],
        [InlineKeyboardButton(text="🧠 Память и мой стек", callback_data="menu_memory")],
        [InlineKeyboardButton(text="🧹 Новый проект (сброс диалога)", callback_data="clear_chat")],
    ])

def is_owner(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    await state.clear()
    provider = get_setting("provider", "gemini")
    g_key = get_setting("gemini_key", "")
    g_model = get_setting("gemini_model", "gemini-3.8-flash")
    or_key = get_setting("openrouter_key", "")

    status_g = f"✅ подключён ({g_model})" if g_key else "❌ не задан"
    status_or = "✅ подключён" if or_key else "❌ не задан"
    active_name = f"Google Gemini ({g_model})" if provider == "gemini" else "OpenRouter (Free Coder)"

    text = (
        "👋 <b>Привет! Я твой личный ИИ-напарник по разработке.</b>\n\n"
        "Я уже знаю твой рабочий стек: <b>Python, aiogram 3, Telethon, SQLite, HTML/CSS/JS, GitHub, Bothost.ru и GitHub Pages</b>.\n\n"
        f"⚡ <b>Активный движок:</b> {active_name}\n"
        f"• Google Gemini: {status_g}\n"
        f"• OpenRouter: {status_or}\n\n"
        "✍️ <b>Просто напиши мне любую задачу</b> (например: <i>«Сделай бота для записи клиентов»</i> или <i>«Сверстай лендинг»</i>), и я пришлю готовые файлы для скачивания!"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "🛠 <b>Главное меню ИИ-напарника</b>\n\nПиши любую задачу прямо в чат или выбери раздел ниже:",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

@router.callback_query(F.data == "menu_keys")
async def cb_menu_keys(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    provider = get_setting("provider", "gemini")
    g_model = get_setting("gemini_model", "gemini-3.8-flash")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Ввести ключ Google Gemini", callback_data="set_gemini")],
        [InlineKeyboardButton(text=f"⚙️ Модель Gemini ({g_model})", callback_data="set_gemini_model")],
        [InlineKeyboardButton(text="🌐 Ввести ключ OpenRouter", callback_data="set_openrouter")],
        [InlineKeyboardButton(
            text="🔄 Переключить на OpenRouter" if provider == "gemini" else "🔄 Переключить на Gemini",
            callback_data="toggle_provider"
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]
    ])
    await call.message.edit_text(
        "🔑 <b>Настройка бесплатных API-ключей ИИ</b>\n\n"
        "• Ключ <b>Google Gemini</b> можно бесплатно получить на <code>aistudio.google.com</code>\n"
        "• Ключ <b>OpenRouter</b> можно бесплатно получить на <code>openrouter.ai</code>\n\n"
        "Выбери действие:",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data == "toggle_provider")
async def cb_toggle_provider(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    cur_p = get_setting("provider", "gemini")
    new_p = "openrouter" if cur_p == "gemini" else "gemini"
    set_setting("provider", new_p)
    await call.answer(f"Переключено на {new_p.upper()}", show_alert=True)
    await cb_menu_keys(call)

@router.callback_query(F.data == "set_gemini")
async def cb_set_gemini(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.set_state(SetupStates.waiting_gemini_key)
    await call.message.edit_text(
        "💎 Отправь мне свой <b>API-ключ Google Gemini</b> (начинается с <code>AIza...</code>):",
        parse_mode="HTML"
    )

@router.message(SetupStates.waiting_gemini_key)
async def msg_gemini_key(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    key = message.text.strip()
    set_setting("gemini_key", key)
    set_setting("provider", "gemini")
    await state.clear()
    await message.answer("✅ Ключ <b>Google Gemini</b> сохранён и выбран основным!", parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "set_gemini_model")
async def cb_set_gemini_model(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.set_state(SetupStates.waiting_gemini_model)
    await call.message.edit_text(
        "⚙️ Отправь название модели Google Gemini (например: <code>gemini-3.8-flash</code> или <code>gemini-2.5-pro</code>):",
        parse_mode="HTML"
    )

@router.message(SetupStates.waiting_gemini_model)
async def msg_gemini_model(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    model = message.text.strip()
    set_setting("gemini_model", model)
    await state.clear()
    await message.answer(f"✅ Модель Gemini изменена на <b>{model}</b>!", parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "set_openrouter")
async def cb_set_openrouter(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.set_state(SetupStates.waiting_openrouter_key)
    await call.message.edit_text(
        "🌐 Отправь мне свой <b>API-ключ OpenRouter</b> (начинается с <code>sk-or-...</code>):",
        parse_mode="HTML"
    )

@router.message(SetupStates.waiting_openrouter_key)
async def msg_openrouter_key(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    key = message.text.strip()
    set_setting("openrouter_key", key)
    set_setting("provider", "openrouter")
    await state.clear()
    await message.answer("✅ Ключ <b>OpenRouter</b> сохранён и выбран основным!", parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "menu_memory")
async def cb_menu_memory(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    mem = get_setting("stack_memory", DEFAULT_STACK_MEMORY)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Дополнить / изменить память", callback_data="edit_memory")],
        [InlineKeyboardButton(text="🔄 Сбросить на стандартный стек", callback_data="reset_memory")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]
    ])
    preview = mem[:900] + ("..." if len(mem) > 900 else "")
    await call.message.edit_text(
        f"🧠 <b>Текущая постоянная память бота:</b>\n\n<code>{preview}</code>",
        parse_mode="HTML",
        reply_markup=kb
    )

@router.callback_query(F.data == "edit_memory")
async def cb_edit_memory(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.set_state(SetupStates.waiting_memory)
    await call.message.edit_text(
        "✏️ Напиши дополнение к памяти бота (например: <i>«Добавь, что я также делаю парсеры на BeautifulSoup»</i>), и я добавлю это в постоянную инструкцию:"
    )

@router.message(SetupStates.waiting_memory)
async def msg_edit_memory(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    cur = get_setting("stack_memory", DEFAULT_STACK_MEMORY)
    updated = cur + "\n- " + message.text.strip()
    set_setting("stack_memory", updated)
    await state.clear()
    await message.answer("✅ Память бота успешно дополнена!", reply_markup=main_menu_kb())

@router.callback_query(F.data == "reset_memory")
async def cb_reset_memory(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    set_setting("stack_memory", DEFAULT_STACK_MEMORY)
    await call.answer("Память сброшена на стандартную!", show_alert=True)
    await cb_menu_memory(call)

@router.callback_query(F.data == "clear_chat")
async def cb_clear_chat(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    clear_history()
    await call.answer("🧹 История диалога очищена! Можешь начинать новый проект.", show_alert=True)

async def ask_gemini(api_key: str, model: str, system_prompt: str, messages: list) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})

    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192}
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=120) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                return f"❌ Ошибка Gemini API ({model}): {err}"
            candidates = data.get("candidates", [])
            if not candidates:
                return "❌ Gemini вернул пустой ответ."
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)

async def ask_openrouter(api_key: str, system_prompt: str, messages: list) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": get_setting("openrouter_model", "qwen/qwen-2.5-coder-32b-instruct:free"),
        "messages": full_messages,
        "temperature": 0.4,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                return f"❌ Ошибка OpenRouter API: {err}"
            choices = data.get("choices", [])
            if not choices:
                return "❌ OpenRouter вернул пустой ответ."
            return choices[0].get("message", {}).get("content", "")

def extract_code_files(text: str):
    files = []
    tick3 = chr(96) * 3
    tick1 = chr(96)
    pattern = re.compile(
        r"(?:" + tick1 + r"([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))" + tick1 + r"\s*[:\-]?\s*)?"
        + tick3 + r"([a-zA-Z0-9_-]*)\n([\s\S]*?)" + tick3
    )
    idx = 1
    for match in pattern.finditer(text):
        fname = match.group(1)
        lang = (match.group(2) or "").lower()
        code = match.group(3)
        if not fname:
            first_line = code.splitlines()[0].strip() if code.splitlines() else ""
            m_first = re.search(r"([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))", first_line)
            if m_first:
                fname = m_first.group(1)
            else:
                ext_map = {
                    "python": "main.py",
                    "py": "main.py",
                    "html": "index.html",
                    "css": "style.css",
                    "js": "script.js",
                    "javascript": "script.js",
                    "json": "data.json",
                    "text": "requirements.txt",
                    "txt": "requirements.txt",
                }
                base = ext_map.get(lang, f"code_{idx}.txt")
                if any(f[0] == base for f in files):
                    name_part, ext_part = os.path.splitext(base)
                    fname = f"{name_part}_{idx}{ext_part}"
                else:
                    fname = base
        files.append((fname, code.strip() + "\n"))
        idx += 1
    return files

@router.message(F.text)
async def handle_task(message: Message):
    if not is_owner(message.from_user.id):
        return

    provider = get_setting("provider", "gemini")
    g_key = get_setting("gemini_key", "")
    g_model = get_setting("gemini_model", "gemini-3.8-flash")
    or_key = get_setting("openrouter_key", "")

    if provider == "gemini" and not g_key:
        await message.answer("⚠️ Сначала добавь бесплатный API-ключ <b>Google Gemini</b> через кнопку меню ниже!", parse_mode="HTML", reply_markup=main_menu_kb())
        return
    if provider == "openrouter" and not or_key:
        await message.answer("⚠️ Сначала добавь бесплатный API-ключ <b>OpenRouter</b> через кнопку меню ниже!", parse_mode="HTML", reply_markup=main_menu_kb())
        return

    wait_msg = await message.answer("⏳ <b>Пишу код и собираю файлы...</b>", parse_mode="HTML")

    user_text = message.text.strip()
    add_history("user", user_text)
    history = get_history()
    sys_prompt = get_setting("stack_memory", DEFAULT_STACK_MEMORY)

    try:
        if provider == "gemini":
            reply = await ask_gemini(g_key, g_model, sys_prompt, history)
        else:
            reply = await ask_openrouter(or_key, sys_prompt, history)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка соединения с ИИ: {e}")
        return

    add_history("assistant", reply)
    await wait_msg.delete()

    code_files = extract_code_files(reply)
    tick3 = chr(96) * 3
    clean_text = re.sub(tick3 + r"[\s\S]*?" + tick3, "", reply).strip()
    if not clean_text:
        clean_text = "✅ Готово! Файлы с кодом прикреплены ниже."

    for i in range(0, len(clean_text), 3800):
        chunk = clean_text[i:i + 3800]
        try:
            await message.answer(chunk, parse_mode="Markdown")
        except Exception:
            await message.answer(chunk)

    for fname, fcontent in code_files:
        doc = BufferedInputFile(fcontent.encode("utf-8"), filename=fname)
        await message.answer_document(doc, caption=f"📄 Готовый файл: {fname}")

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения!")
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
