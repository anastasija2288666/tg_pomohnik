import os
import re
import sys
import json
import asyncio
import sqlite3
import subprocess

def ensure_packages():
    required = ["aiogram", "aiohttp"]
    for pkg in required:
        try:
            __import__(pkg)
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

DB_PATH = "coder_assistant.db"

DEFAULT_MEMORY = (
    "Ты мой универсальный личный ИИ-помощник и напарник по работе и жизни. "
    "Отвечай живо, простым, дружелюбным и понятным языком без лишней воды. "
    "Помогай с любыми вопросами: придумывай тексты, объявления, идеи для продвижения, "
    "помогай в переговорах и продажах. "
    "Когда я прошу написать сайт, скрипт или Telegram-бота — пиши полный готовый к запуску код "
    "без сокращений с учётом моего рабочего стека: Python 3, aiogram 3, Telethon, SQLite, HTML/CSS/JS, "
    "запуск на GitHub и Bothost.ru (с переменными BOT_TOKEN и ADMIN_ID), а сайты — на GitHub Pages. "
    "Каждый файл кода оформляй в блок с указанием точного имени файла перед ним."
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS msg_log (msg_id INTEGER PRIMARY KEY)")
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('provider', 'gemini')")
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('gemini_key', '')")
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('gemini_model', 'gemini-3.8-flash-lite')")
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('openrouter_key', '')")
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('openrouter_model', 'qwen/qwen-2.5-coder-32b-instruct:free')")
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('memory', ?)", (DEFAULT_MEMORY,))
    c.execute("INSERT OR IGNORE INTO settings (k, v) VALUES ('state', 'idle')")
    conn.commit()
    conn.close()

def get_setting(k: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT v FROM settings WHERE k = ?", (k,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(k: str, v: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (k, v) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def log_msg_id(msg_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO msg_log (msg_id) VALUES (?)", (msg_id,))
    conn.commit()
    conn.close()

def get_and_clear_msg_ids() -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT msg_id FROM msg_log ORDER BY msg_id DESC")
    ids = [r[0] for r in c.fetchall()]
    c.execute("DELETE FROM msg_log")
    conn.commit()
    conn.close()
    return ids

def add_history(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
    c.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 16)")
    conn.commit()
    conn.close()

def get_history() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def clear_history():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM history")
    conn.commit()
    conn.close()

def main_kb() -> ReplyKeyboardMarkup:
    provider = "Google Gemini" if get_setting("provider") == "gemini" else "OpenRouter"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"🔑 API-ключи ИИ ({provider})")],
            [KeyboardButton(text="🧠 Память и характер бота")],
            [KeyboardButton(text="🧹 Новый проект (сброс диалога)")],
        ],
        resize_keyboard=True,
    )

def api_inline_kb() -> InlineKeyboardMarkup:
    prov = get_setting("provider", "gemini")
    g_mark = "✅ " if prov == "gemini" else ""
    o_mark = "✅ " if prov == "openrouter" else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{g_mark}Использовать Google Gemini", callback_data="use_gemini")],
            [InlineKeyboardButton(text="💎 Ввести ключ Google Gemini", callback_data="set_gemini_key")],
            [InlineKeyboardButton(text="⚙️ Сменить модель Gemini", callback_data="set_gemini_model")],
            [InlineKeyboardButton(text=f"{o_mark}Использовать OpenRouter", callback_data="use_openrouter")],
            [InlineKeyboardButton(text="🌐 Ввести ключ OpenRouter", callback_data="set_or_key")],
        ]
    )

def memory_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить память и характер", callback_data="edit_memory")],
            [InlineKeyboardButton(text="🔄 Вернуть стандартный характер", callback_data="reset_memory")],
        ]
    )

def is_admin(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID

async def track_and_answer(message: types.Message, text: str, reply_markup=None, parse_mode=None):
    log_msg_id(message.message_id)
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
    log_msg_id(sent.message_id)
    return sent

async def ask_gemini(api_key: str, model: str, system_prompt: str, messages: list[dict]) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6},
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

async def ask_openrouter(api_key: str, model: str, system_prompt: str, messages: list[dict]) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": model,
        "messages": full_messages,
        "temperature": 0.6,
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

def extract_code_files(text: str) -> list[tuple[str, str]]:
    files = []
    fence = chr(96) * 3
    pattern = re.compile(rf"{fence}([a-zA-Z0-9_-]*)\n([\s\S]*?){fence}")
    idx = 1
    for match in pattern.finditer(text):
        lang = (match.group(1) or "").lower()
        code = match.group(2).strip()
        if not code:
            continue
        before_text = text[:match.start()][-140:]
        m_name = re.search(r"([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))", before_text)
        if m_name:
            fname = m_name.group(1)
        else:
            first_line = code.splitlines()[0] if code.splitlines() else ""
            m_first = re.search(r"([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))", first_line)
            if m_first:
                fname = m_first.group(1)
            else:
                ext_map = {
                    "python": "main.py",
                    "py": "main.py",
                    "html": "index.html",
                    "css": "style.css",
                    "javascript": "script.js",
                    "js": "script.js",
                    "json": "data.json",
                }
                fname = ext_map.get(lang, f"file_{idx}.txt")
                idx += 1
        files.append((fname, code))
    return files

async def main():
    if not BOT_TOKEN:
        print("ОШИБКА: Не задан BOT_TOKEN в переменных окружения!")
        return

    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        set_setting("state", "idle")
        g_ok = "✅" if get_setting("gemini_key") else "❌"
        o_ok = "✅" if get_setting("openrouter_key") else "❌"
        text = (
            "👋 <b>Привет! Я твой личный ИИ-помощник.</b>\n\n"
            "Могу просто поболтать, придумать посты, помочь с продажами или написать готовый код для сайта и бота (пришлю сразу файлами).\n\n"
            f"💎 Google Gemini: {g_ok}\n"
            f"🌐 OpenRouter: {o_ok}\n\n"
            "Просто напиши свой вопрос или задачу в чат!"
        )
        await track_and_answer(message, text, reply_markup=main_kb(), parse_mode="HTML")

    @dp.message(F.text.startswith("🔑 API-ключи ИИ"))
    async def menu_keys(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        set_setting("state", "idle")
        model = get_setting("gemini_model", "gemini-3.8-flash-lite")
        await track_and_answer(
            message,
            f"Текущая модель Gemini: <code>{model}</code>\nВыбери активный сервис или введи API-ключ:",
            reply_markup=api_inline_kb(),
            parse_mode="HTML",
        )

    @dp.message(F.text == "🧠 Память и характер бота")
    async def menu_memory(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        set_setting("state", "idle")
        mem = get_setting("memory", DEFAULT_MEMORY)
        await track_and_answer(
            message,
            f"🧠 <b>Текущая память и характер бота:</b>\n\n<i>{mem}</i>",
            reply_markup=memory_inline_kb(),
            parse_mode="HTML",
        )

    @dp.message(F.text == "🧹 Новый проект (сброс диалога)")
    async def menu_clear(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        set_setting("state", "idle")
        clear_history()
        log_msg_id(message.message_id)
        msg_ids = get_and_clear_msg_ids()
        for mid in msg_ids:
            try:
                await bot.delete_message(chat_id=message.chat.id, message_id=mid)
            except Exception:
                pass
        sent = await message.answer(
            "🧹 Чат и память диалога полностью очищены! Начинаем с чистого листа — пиши новую задачу или вопрос.",
            reply_markup=main_kb(),
        )
        log_msg_id(sent.message_id)

    @dp.callback_query(F.data == "use_gemini")
    async def cb_use_gemini(call: types.CallbackQuery):
        set_setting("provider", "gemini")
        await call.message.edit_reply_markup(reply_markup=api_inline_kb())
        await call.answer("Выбран Google Gemini")

    @dp.callback_query(F.data == "use_openrouter")
    async def cb_use_or(call: types.CallbackQuery):
        set_setting("provider", "openrouter")
        await call.message.edit_reply_markup(reply_markup=api_inline_kb())
        await call.answer("Выбран OpenRouter")

    @dp.callback_query(F.data == "set_gemini_key")
    async def cb_set_gkey(call: types.CallbackQuery):
        set_setting("state", "wait_gemini_key")
        sent = await call.message.answer("💎 Отправь мне свой API-ключ Google Gemini:")
        log_msg_id(sent.message_id)
        await call.answer()

    @dp.callback_query(F.data == "set_gemini_model")
    async def cb_set_gmodel(call: types.CallbackQuery):
        set_setting("state", "wait_gemini_model")
        sent = await call.message.answer("⚙️ Отправь название модели Google Gemini (например: <code>gemini-3.8-flash-lite</code> или <code>gemini-3.8-flash</code>):", parse_mode="HTML")
        log_msg_id(sent.message_id)
        await call.answer()

    @dp.callback_query(F.data == "set_or_key")
    async def cb_set_orkey(call: types.CallbackQuery):
        set_setting("state", "wait_or_key")
        sent = await call.message.answer("🌐 Отправь мне свой бесплатный API-ключ OpenRouter:")
        log_msg_id(sent.message_id)
        await call.answer()

    @dp.callback_query(F.data == "edit_memory")
    async def cb_edit_mem(call: types.CallbackQuery):
        set_setting("state", "wait_memory")
        sent = await call.message.answer("✏️ Отправь новый текст памяти и характера, который бот должен запомнить навсегда:")
        log_msg_id(sent.message_id)
        await call.answer()

    @dp.callback_query(F.data == "reset_memory")
    async def cb_reset_mem(call: types.CallbackQuery):
        set_setting("memory", DEFAULT_MEMORY)
        set_setting("state", "idle")
        sent = await call.message.answer("🔄 Характер бота обновлён на стандартный (универсальный помощник).", reply_markup=main_kb())
        log_msg_id(sent.message_id)
        await call.answer()

    @dp.message(F.text)
    async def handle_text(message: types.Message):
        if not is_admin(message.from_user.id):
            return

        log_msg_id(message.message_id)
        state = get_setting("state", "idle")
        text = message.text.strip()

        if state == "wait_gemini_key":
            set_setting("gemini_key", text)
            set_setting("provider", "gemini")
            set_setting("state", "idle")
            sent = await message.answer("✅ Ключ Google Gemini сохранён и выбран основным!", reply_markup=main_kb())
            log_msg_id(sent.message_id)
            return

        if state == "wait_gemini_model":
            set_setting("gemini_model", text)
            set_setting("state", "idle")
            sent = await message.answer(f"✅ Модель Gemini изменена на <code>{text}</code>!", reply_markup=main_kb(), parse_mode="HTML")
            log_msg_id(sent.message_id)
            return

        if state == "wait_or_key":
            set_setting("openrouter_key", text)
            set_setting("provider", "openrouter")
            set_setting("state", "idle")
            sent = await message.answer("✅ Ключ OpenRouter сохранён и выбран основным!", reply_markup=main_kb())
            log_msg_id(sent.message_id)
            return

        if state == "wait_memory":
            set_setting("memory", text)
            set_setting("state", "idle")
            sent = await message.answer("✅ Новая память и характер бота сохранены! Теперь я буду общаться с учётом этих правил.", reply_markup=main_kb())
            log_msg_id(sent.message_id)
            return

        provider = get_setting("provider", "gemini")
        if provider == "gemini":
            api_key = get_setting("gemini_key", "")
            if not api_key:
                sent = await message.answer("⚠️ Сначала добавь API-ключ Google Gemini через кнопку <b>🔑 API-ключи ИИ</b>.", parse_mode="HTML")
                log_msg_id(sent.message_id)
                return
        else:
            api_key = get_setting("openrouter_key", "")
            if not api_key:
                sent = await message.answer("⚠️ Сначала добавь API-ключ OpenRouter через кнопку <b>🔑 API-ключи ИИ</b>.", parse_mode="HTML")
                log_msg_id(sent.message_id)
                return

        wait_msg = await message.answer("💭 Думаю над ответом...")
        add_history("user", text)
        history = get_history()
        system_prompt = get_setting("memory", DEFAULT_MEMORY)

        try:
            if provider == "gemini":
                model = get_setting("gemini_model", "gemini-3.8-flash-lite")
                reply_text = await ask_gemini(api_key, model, system_prompt, history)
            else:
                model = get_setting("openrouter_model", "qwen/qwen-2.5-coder-32b-instruct:free")
                reply_text = await ask_openrouter(api_key, model, system_prompt, history)
        except Exception as e:
            reply_text = f"❌ Ошибка соединения с нейросетью: {e}"

        try:
            await wait_msg.delete()
        except Exception:
            pass

        if reply_text.startswith("❌"):
            sent = await message.answer(reply_text)
            log_msg_id(sent.message_id)
            return

        add_history("assistant", reply_text)

        files = extract_code_files(reply_text)
        fence = chr(96) * 3
        clean_text = re.sub(rf"{fence}[\s\S]*?{fence}", "", reply_text).strip()

        if clean_text:
            for chunk in [clean_text[i:i+4000] for i in range(0, len(clean_text), 4000)]:
                sent = await message.answer(chunk)
                log_msg_id(sent.message_id)
        elif files:
            sent = await message.answer("✅ Готово! Собрал файлы по твоей задаче:")
            log_msg_id(sent.message_id)

        for fname, code_content in files:
            doc = BufferedInputFile(code_content.encode("utf-8"), filename=fname)
            sent_doc = await message.answer_document(doc, caption=f"📄 {fname}")
            log_msg_id(sent_doc.message_id)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
