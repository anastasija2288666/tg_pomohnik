import os
import re
import sys
import json
import sqlite3
import asyncio
import subprocess
from datetime import datetime

def ensure_packages():
    required = {"aiogram": "aiogram>=3.18.0", "aiohttp": "aiohttp"}
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

ensure_packages()

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

DB_PATH = "coder_assistant.db"

DEFAULT_PERSONA = (
    "Ты мой универсальный личный ИИ-помощник и напарник по работе и жизни. "
    "Отвечай живо, простым, понятным и дружелюбным языком на русском. "
    "Помогай с любыми вопросами: придумывай тексты, объявления, идеи для продвижения и продаж, отвечай на обычные и рабочие вопросы. "
    "Когда я прошу написать сайт, скрипт или Telegram-бота — выдавай полный готовый к запуску код без заглушек (мой стек: Python 3, aiogram 3, Telethon, SQLite, HTML/CSS/JS; "
    "хостинг ботов — GitHub + Bothost.ru с переменными BOT_TOKEN и ADMIN_ID, сайтов — GitHub Pages). "
    "Если в ответе есть код, оформляй его в блок с указанием имени файла в первой строке (например, # main.py), чтобы я могла сразу скачать его файлом."
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, created_at TEXT)")
    cur.execute("CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, message_id INTEGER)")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('provider', 'gemini')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('gemini_model', 'gemini-3.8-flash-lite')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('openrouter_model', 'qwen/qwen-2.5-coder-32b-instruct:free')")
    cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('persona', ?)", (DEFAULT_PERSONA,))
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else default

def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def add_history(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO history (role, content, created_at) VALUES (?, ?, ?)", (role, content, datetime.now().isoformat()))
    cur.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 20)")
    conn.commit()
    conn.close()

def get_history() -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role, content FROM history ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def clear_history():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM history")
    conn.commit()
    conn.close()

def track_msg(chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO chat_messages (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
    conn.commit()
    conn.close()

def pop_tracked_msgs(chat_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT message_id FROM chat_messages WHERE chat_id = ?", (chat_id,))
    ids = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return ids

class AssistantStates(StatesGroup):
    waiting_gemini_key = State()
    waiting_gemini_model = State()
    waiting_openrouter_key = State()
    waiting_persona = State()

def main_menu_kb() -> ReplyKeyboardMarkup:
    provider = get_setting("provider", "gemini")
    prov_label = "Google Gemini" if provider == "gemini" else "OpenRouter"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"🔑 API-ключи ИИ ({prov_label})")],
            [KeyboardButton(text="🧠 Память и характер бота")],
            [KeyboardButton(text="🧹 Новый проект (сброс диалога)")]
        ],
        resize_keyboard=True
    )

def api_inline_kb() -> InlineKeyboardMarkup:
    provider = get_setting("provider", "gemini")
    g_mark = "✅ " if provider == "gemini" else ""
    o_mark = "✅ " if provider == "openrouter" else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{g_mark}Использовать Google Gemini", callback_data="set_prov_gemini")],
            [InlineKeyboardButton(text="💎 Ввести ключ Google Gemini", callback_data="enter_gemini_key")],
            [InlineKeyboardButton(text="⚙️ Сменить модель Gemini", callback_data="enter_gemini_model")],
            [InlineKeyboardButton(text=f"{o_mark}Использовать OpenRouter", callback_data="set_prov_openrouter")],
            [InlineKeyboardButton(text="🌐 Ввести ключ OpenRouter", callback_data="enter_or_key")]
        ]
    )

def persona_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить память и характер", callback_data="edit_persona")],
            [InlineKeyboardButton(text="🔄 Вернуть стандартный характер", callback_data="reset_persona")]
        ]
    )

def is_allowed(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID

async def send_tracked(bot: Bot, chat_id: int, text: str, reply_markup=None, parse_mode=None):
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    track_msg(chat_id, msg.message_id)
    return msg

async def ask_gemini(api_key: str, model: str, system_prompt: str, messages: list) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6}
    }
    try:
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
    except Exception as e:
        return f"❌ Ошибка соединения с Gemini: {e}"

async def ask_openrouter(api_key: str, model: str, system_prompt: str, messages: list) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {"model": model, "messages": full_messages, "temperature": 0.6}
    try:
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
    except Exception as e:
        return f"❌ Ошибка соединения с OpenRouter: {e}"

def extract_code_files(text: str) -> list:
    files = []
    fence = chr(96) * 3
    nl = chr(10)
    pattern = re.compile(fence + r"([a-zA-Z0-9_-]*)" + nl + r"([\s\S]*?)" + fence)
    idx = 1
    for match in pattern.finditer(text):
        lang = (match.group(1) or "").lower()
        code = match.group(2)
        code_lines = code.splitlines()
        first_line = code_lines[0].strip() if code_lines else ""
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
                "txt": "requirements.txt"
            }
            base = ext_map.get(lang, f"code_{idx}.txt")
            if any(f[0] == base for f in files):
                name_part, ext_part = os.path.splitext(base)
                fname = f"{name_part}_{idx}{ext_part}"
            else:
                fname = base
        files.append((fname, code.strip() + nl))
        idx += 1
    return files

async def main():
    if not BOT_TOKEN:
        print("ОШИБКА: Не задан BOT_TOKEN в переменных окружения!")
        return

    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        await state.clear()
        provider = get_setting("provider", "gemini")
        g_key = get_setting("gemini_key", "")
        o_key = get_setting("openrouter_key", "")
        g_model = get_setting("gemini_model", "gemini-3.8-flash-lite")
        status_g = "✅ подключён" if g_key else "❌ не задан"
        status_o = "✅ подключён" if o_key else "❌ не задан"
        text = (
            "👋 <b>Привет! Я твой универсальный личный ИИ-помощник.</b>\n\n"
            "Можешь спрашивать что угодно: придумывать посты, идеи для бизнеса и продаж, "
            "разбирать любые вопросы или просить написать сайт и Telegram-бота — "
            "если в ответе будет код, я сразу пришлю его готовыми файлами!\n\n"
            f"🔹 <b>Активный движок:</b> {provider.upper()} (<code>{g_model}</code>)\n"
            f"💎 <b>Google Gemini:</b> {status_g}\n"
            f"🌐 <b>OpenRouter:</b> {status_o}\n\n"
            "Просто напиши мне свой вопрос или задачу в чат 👇"
        )
        await send_tracked(bot, message.chat.id, text, reply_markup=main_menu_kb(), parse_mode="HTML")

    @dp.message(F.text.startswith("🔑 API-ключи ИИ"))
    async def menu_api_keys(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        await state.clear()
        g_model = get_setting("gemini_model", "gemini-3.8-flash-lite")
        await send_tracked(
            bot,
            message.chat.id,
            f"⚙️ <b>Настройка ИИ-движка и ключей</b>\n\nТекущая модель Gemini: <code>{g_model}</code>\nВыбери активный сервис или введи бесплатный API-ключ:",
            reply_markup=api_inline_kb(),
            parse_mode="HTML"
        )

    @dp.callback_query(F.data == "set_prov_gemini")
    async def cb_prov_gemini(callback: types.CallbackQuery):
        if not is_allowed(callback.from_user.id):
            return
        set_setting("provider", "gemini")
        await callback.message.edit_reply_markup(reply_markup=api_inline_kb())
        await send_tracked(bot, callback.message.chat.id, "✅ Основным выбран <b>Google Gemini</b>.", reply_markup=main_menu_kb(), parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data == "set_prov_openrouter")
    async def cb_prov_or(callback: types.CallbackQuery):
        if not is_allowed(callback.from_user.id):
            return
        set_setting("provider", "openrouter")
        await callback.message.edit_reply_markup(reply_markup=api_inline_kb())
        await send_tracked(bot, callback.message.chat.id, "✅ Основным выбран <b>OpenRouter</b>.", reply_markup=main_menu_kb(), parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data == "enter_gemini_key")
    async def cb_enter_gemini(callback: types.CallbackQuery, state: FSMContext):
        if not is_allowed(callback.from_user.id):
            return
        await state.set_state(AssistantStates.waiting_gemini_key)
        await send_tracked(bot, callback.message.chat.id, "💎 Отправь мне свой API-ключ <b>Google Gemini</b> (начинается с <code>AIza...</code>):", parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data == "enter_gemini_model")
    async def cb_enter_gemini_model(callback: types.CallbackQuery, state: FSMContext):
        if not is_allowed(callback.from_user.id):
            return
        await state.set_state(AssistantStates.waiting_gemini_model)
        await send_tracked(bot, callback.message.chat.id, "⚙️ Отправь название модели Google Gemini (например: <code>gemini-3.8-flash-lite</code> или <code>gemini-3.8-flash</code>):", parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data == "enter_or_key")
    async def cb_enter_or(callback: types.CallbackQuery, state: FSMContext):
        if not is_allowed(callback.from_user.id):
            return
        await state.set_state(AssistantStates.waiting_openrouter_key)
        await send_tracked(bot, callback.message.chat.id, "🌐 Отправь мне свой API-ключ <b>OpenRouter</b> (начинается с <code>sk-or-...</code>):", parse_mode="HTML")
        await callback.answer()

    @dp.message(AssistantStates.waiting_gemini_key)
    async def save_gemini_key(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        key = (message.text or "").strip()
        if not key.startswith("AIza"):
            await send_tracked(bot, message.chat.id, "⚠️ Похоже, это не API-ключ Google (ключ должен начинаться с <code>AIza...</code>). Попробуй скопировать сам ключ на сайте Google AI Studio и отправь снова:", parse_mode="HTML")
            return
        set_setting("gemini_key", key)
        set_setting("provider", "gemini")
        await state.clear()
        await send_tracked(bot, message.chat.id, "✅ Ключ <b>Google Gemini</b> сохранён и выбран основным!", reply_markup=main_menu_kb(), parse_mode="HTML")

    @dp.message(AssistantStates.waiting_gemini_model)
    async def save_gemini_model(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        model = (message.text or "").strip()
        if not model or " " in model:
            await send_tracked(bot, message.chat.id, "⚠️ Введи корректное имя модели без пробелов (например: <code>gemini-3.8-flash-lite</code>):", parse_mode="HTML")
            return
        set_setting("gemini_model", model)
        await state.clear()
        await send_tracked(bot, message.chat.id, f"✅ Модель изменена на <code>{model}</code>!", reply_markup=main_menu_kb(), parse_mode="HTML")

    @dp.message(AssistantStates.waiting_openrouter_key)
    async def save_or_key(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        key = (message.text or "").strip()
        set_setting("openrouter_key", key)
        set_setting("provider", "openrouter")
        await state.clear()
        await send_tracked(bot, message.chat.id, "✅ Ключ <b>OpenRouter</b> сохранён и выбран основным!", reply_markup=main_menu_kb(), parse_mode="HTML")

    @dp.message(F.text == "🧠 Память и характер бота")
    async def menu_persona(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        await state.clear()
        current = get_setting("persona", DEFAULT_PERSONA)
        await send_tracked(bot, message.chat.id, f"🧠 <b>Текущая память и характер бота:</b>\n\n<i>{current}</i>", reply_markup=persona_inline_kb(), parse_mode="HTML")

    @dp.callback_query(F.data == "edit_persona")
    async def cb_edit_persona(callback: types.CallbackQuery, state: FSMContext):
        if not is_allowed(callback.from_user.id):
            return
        await state.set_state(AssistantStates.waiting_persona)
        await send_tracked(bot, callback.message.chat.id, "✏️ <b>Отправь новым сообщением описание характера и правил для бота</b> (что ему нужно знать о тебе и как отвечать):", parse_mode="HTML")
        await callback.answer()

    @dp.callback_query(F.data == "reset_persona")
    async def cb_reset_persona(callback: types.CallbackQuery, state: FSMContext):
        if not is_allowed(callback.from_user.id):
            return
        await state.clear()
        set_setting("persona", DEFAULT_PERSONA)
        await send_tracked(bot, callback.message.chat.id, "🔄 <b>Стандартный характер универсального помощника восстановлен!</b>", reply_markup=main_menu_kb(), parse_mode="HTML")
        await callback.answer()

    @dp.message(AssistantStates.waiting_persona)
    async def save_persona(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        track_msg(message.chat.id, message.message_id)
        new_text = (message.text or "").strip()
        if len(new_text) < 5:
            await send_tracked(bot, message.chat.id, "⚠️ Текст слишком короткий. Напиши подробнее или нажми отмену.")
            return
        set_setting("persona", new_text)
        await state.clear()
        await send_tracked(bot, message.chat.id, f"✅ <b>Новая память сохранена!</b> Теперь я буду отвечать с учётом этих правил:\n\n<i>{new_text}</i>", reply_markup=main_menu_kb(), parse_mode="HTML")

    @dp.message(F.text == "🧹 Новый проект (сброс диалога)")
    async def menu_clear_history(message: types.Message, state: FSMContext):
        if not is_allowed(message.from_user.id):
            return
        await state.clear()
        clear_history()

        chat_id = message.chat.id
        msg_ids = pop_tracked_msgs(chat_id)
        if message.message_id not in msg_ids:
            msg_ids.append(message.message_id)

        all_to_delete = set(msg_ids)
        for mid in range(max(1, message.message_id - 100), message.message_id + 1):
            all_to_delete.add(mid)

        for mid in sorted(all_to_delete, reverse=True):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

        await send_tracked(
            bot,
            chat_id,
            "🧹 <b>Чат полностью очищен и история диалога сброшена!</b>\nМожем начинать с чистого листа 👇",
            reply_markup=main_menu_kb(),
            parse_mode="HTML"
        )

    @dp.message(F.text)
    async def handle_prompt(message: types.Message):
        if not is_allowed(message.from_user.id):
            return

        chat_id = message.chat.id
        track_msg(chat_id, message.message_id)
        user_text = message.text.strip()
        if not user_text:
            return

        provider = get_setting("provider", "gemini")
        persona = get_setting("persona", DEFAULT_PERSONA)

        if provider == "gemini":
            api_key = get_setting("gemini_key", "")
            model = get_setting("gemini_model", "gemini-3.8-flash-lite")
            if not api_key:
                await send_tracked(bot, chat_id, "⚠️ Сначала добавь API-ключ <b>Google Gemini</b> через кнопку «🔑 API-ключи ИИ».", parse_mode="HTML")
                return
        else:
            api_key = get_setting("openrouter_key", "")
            model = get_setting("openrouter_model", "qwen/qwen-2.5-coder-32b-instruct:free")
            if not api_key:
                await send_tracked(bot, chat_id, "⚠️ Сначала добавь API-ключ <b>OpenRouter</b> через кнопку «🔑 API-ключи ИИ».", parse_mode="HTML")
                return

        wait_msg = await bot.send_message(chat_id, "💭 <i>Думаю над ответом...</i>", parse_mode="HTML")
        add_history("user", user_text)
        history = get_history()

        if provider == "gemini":
            reply_text = await ask_gemini(api_key, model, persona, history)
        else:
            reply_text = await ask_openrouter(api_key, model, persona, history)

        try:
            await wait_msg.delete()
        except Exception:
            pass

        if reply_text.startswith("❌"):
            await send_tracked(bot, chat_id, reply_text)
            return

        add_history("assistant", reply_text)
        code_files = extract_code_files(reply_text)

        fence = chr(96) * 3
        clean_text = re.sub(fence + r"[\s\S]*?" + fence, "", reply_text).strip()

        if clean_text:
            for i in range(0, len(clean_text), 4000):
                chunk = clean_text[i:i+4000]
                await send_tracked(bot, chat_id, chunk)

        for fname, content in code_files:
            doc = BufferedInputFile(content.encode("utf-8"), filename=fname)
            doc_msg = await bot.send_document(chat_id, document=doc, caption=f"📄 Готовый файл: {fname}")
            track_msg(chat_id, doc_msg.message_id)

    print("Личный ИИ-помощник успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
