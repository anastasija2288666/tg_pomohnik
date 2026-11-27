import os
import re
import sys
import json
import asyncio
import sqlite3
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
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

DB_PATH = "coder_assistant.db"

DEFAULT_MEMORY = (
    "Мой рабочий стек и правила разработки:\n"
    "- Пишу ботов на Python 3 (aiogram 3-й версии, Telethon, SQLite, aiohttp).\n"
    "- Хостинг ботов: GitHub + Bothost.ru (бесплатный тариф). Переменные окружения: BOT_TOKEN, ADMIN_ID.\n"
    "- Сайты и Mini Apps делаю на HTML + CSS + JS и размещаю на GitHub Pages.\n"
    "- Всегда выдавай полный рабочий код без сокращений и заглушек, с готовым списком библиотек для requirements.txt."
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT,
            content TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_setting(key: str, default: str = "") -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def set_setting(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def add_history(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
    c.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 12)")
    conn.commit()
    conn.close()

def get_history():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, content FROM history ORDER BY id ASC")
    rows = c.fetchall()
    conn.close()
    return [{"role": r, "content": cnt} for r, cnt in rows]

def clear_history():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM history")
    conn.commit()
    conn.close()

class Form(StatesGroup):
    waiting_gemini_key = State()
    waiting_openrouter_key = State()
    waiting_memory = State()

router = Router()

def is_owner(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔑 API-ключи ИИ", callback_data="menu_keys")
    kb.button(text="🧠 Память и мой стек", callback_data="menu_memory")
    kb.button(text="🧹 Новый проект (сброс диалога)", callback_data="clear_chat")
    kb.adjust(1)
    return kb.as_markup()

def keys_menu_kb():
    kb = InlineKeyboardBuilder()
    provider = get_setting("provider", "gemini")
    g_mark = "✅ " if provider == "gemini" else ""
    o_mark = "✅ " if provider == "openrouter" else ""
    kb.button(text=f"{g_mark}Выбрать Google Gemini (бесплатно)", callback_data="use_gemini")
    kb.button(text=f"{o_mark}Выбрать OpenRouter (бесплатно)", callback_data="use_openrouter")
    kb.button(text="✏️ Ввести ключ Google AI Studio", callback_data="set_gemini")
    kb.button(text="✏️ Ввести ключ OpenRouter", callback_data="set_openrouter")
    kb.button(text="⬅️ Назад в меню", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    await state.clear()
    provider = get_setting("provider", "gemini")
    has_g = "подключён ✅" if get_setting("gemini_key") else "не задан ❌"
    has_o = "подключён ✅" if get_setting("openrouter_key") else "не задан ❌"
    text = (
        "👋 <b>Привет! Я твой личный ИИ-напарник по разработке.</b>\n\n"
        "Я знаю твой стек (Python, aiogram 3, Telethon, SQLite, HTML/CSS/JS, GitHub + Bothost) "
        "и присылаю готовый код сразу файлами (<code>.py</code>, <code>.html</code>, <code>.txt</code>) в чат.\n\n"
        f"⚙️ <b>Активный движок:</b> {provider.upper()}\n"
        f"• Google Gemini: {has_g}\n"
        f"• OpenRouter: {has_o}\n\n"
        "✍️ <b>Просто напиши мне задачу</b> (например: <i>«Сделай бота для записи клиентов»</i> или <i>«Сверстай лендинг»</i>)."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.clear()
    await call.message.edit_text(
        "🛠 <b>Главное меню ИИ-напарника</b>\n\nНапиши любую задачу в чат или выбери настройку ниже:",
        parse_mode="HTML",
        reply_markup=main_menu_kb()
    )

@router.callback_query(F.data == "menu_keys")
async def cb_menu_keys(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    text = (
        "🔑 <b>Настройка бесплатного ИИ-движка</b>\n\n"
        "1. <b>Google AI Studio</b> (aistudio.google.com) — бесплатный ключ для Gemini Flash.\n"
        "2. <b>OpenRouter</b> (openrouter.ai) — бесплатный ключ для Qwen Coder / DeepSeek.\n\n"
        "Выбери активный движок или введи свой ключ:"
    )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=keys_menu_kb())

@router.callback_query(F.data == "use_gemini")
async def cb_use_gemini(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    set_setting("provider", "gemini")
    await call.answer("Выбран Google Gemini")
    await cb_menu_keys(call)

@router.callback_query(F.data == "use_openrouter")
async def cb_use_openrouter(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    set_setting("provider", "openrouter")
    await call.answer("Выбран OpenRouter")
    await cb_menu_keys(call)

@router.callback_query(F.data == "set_gemini")
async def cb_set_gemini(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.set_state(Form.waiting_gemini_key)
    await call.message.answer("🔑 Отправь свой API-ключ от <b>Google AI Studio</b> (начинается с <code>AIza...</code>):", parse_mode="HTML")

@router.message(Form.waiting_gemini_key)
async def save_gemini_key(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    set_setting("gemini_key", message.text.strip())
    set_setting("provider", "gemini")
    await state.clear()
    await message.answer("✅ Ключ <b>Google Gemini</b> сохранён и выбран основным!", parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "set_openrouter")
async def cb_set_openrouter(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    await state.set_state(Form.waiting_openrouter_key)
    await call.message.answer("🔑 Отправь свой API-ключ от <b>OpenRouter</b> (начинается с <code>sk-or-...</code>):", parse_mode="HTML")

@router.message(Form.waiting_openrouter_key)
async def save_openrouter_key(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    set_setting("openrouter_key", message.text.strip())
    set_setting("provider", "openrouter")
    await state.clear()
    await message.answer("✅ Ключ <b>OpenRouter</b> сохранён и выбран основным!", parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "menu_memory")
async def cb_menu_memory(call: CallbackQuery, state: FSMContext):
    if not is_owner(call.from_user.id):
        return
    mem = get_setting("memory", DEFAULT_MEMORY)
    await state.set_state(Form.waiting_memory)
    await call.message.answer(
        f"🧠 <b>Текущая память бота:</b>\n\n<pre>{mem}</pre>\n\n"
        "✏️ Отправь новый текст, если хочешь обновить правила или стек (или нажми /start для отмены):",
        parse_mode="HTML"
    )

@router.message(Form.waiting_memory)
async def save_memory(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    set_setting("memory", message.text.strip())
    await state.clear()
    await message.answer("✅ Память бота обновлена!", parse_mode="HTML", reply_markup=main_menu_kb())

@router.callback_query(F.data == "clear_chat")
async def cb_clear_chat(call: CallbackQuery):
    if not is_owner(call.from_user.id):
        return
    clear_history()
    await call.answer("История диалога очищена!")
    await call.message.answer("🧹 Память текущего проекта очищена. Можем начинать новую задачу!", reply_markup=main_menu_kb())

async def ask_gemini(api_key: str, system_prompt: str, messages: list) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4}
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=120) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                return f"❌ Ошибка Gemini API: {err}"
            candidates = data.get("candidates", [])
            if not candidates:
                return "❌ Gemini вернул пустой ответ."
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)

async def ask_openrouter(api_key: str, system_prompt: str, messages: list) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": "qwen/qwen-2.5-coder-32b-instruct:free",
        "messages": full_messages,
        "temperature": 0.4
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
    fence = chr(96) * 3
    single_tick = chr(96)
    pattern = re.compile(
        r"(?:" + single_tick + r"([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))" + single_tick + r"\s*[:\-]?\s*)?"
        + fence + r"([a-zA-Z0-9_-]*)\n([\s\S]*?)" + fence
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
                    "javascript": "script.js",
                    "js": "script.js",
                    "text": "requirements.txt",
                    "plaintext": "requirements.txt"
                }
                fname = ext_map.get(lang, f"code_{idx}.txt")
                idx += 1
        files.append((fname, code.strip() + "\n"))
    return files

@router.message(F.text)
async def handle_prompt(message: Message):
    if not is_owner(message.from_user.id):
        return
    provider = get_setting("provider", "gemini")
    api_key = get_setting("gemini_key") if provider == "gemini" else get_setting("openrouter_key")
    if not api_key:
        await message.answer(
            "⚠️ Сначала добавь бесплатный API-ключ в разделе <b>«🔑 API-ключи ИИ»</b>!",
            parse_mode="HTML",
            reply_markup=main_menu_kb()
        )
        return

    wait_msg = await message.answer("⏳ <b>Пишу код и собираю файлы...</b>", parse_mode="HTML")
    add_history("user", message.text)
    history = get_history()
    memory = get_setting("memory", DEFAULT_MEMORY)
    system_prompt = (
        "Ты опытный разработчик-напарник. Отвечай на русском языке чётко и по делу. "
        "Всегда выдавай полный готовый к запуску код без сокращений. "
        "Перед каждым блоком кода указывай имя файла в обратных кавычках, например `main.py` или `requirements.txt`.\n\n"
        + memory
    )

    if provider == "gemini":
        reply = await ask_gemini(api_key, system_prompt, history)
    else:
        reply = await ask_openrouter(api_key, system_prompt, history)

    add_history("assistant", reply)
    await wait_msg.delete()

    files = extract_code_files(reply)
    fence = chr(96) * 3
    clean_text = re.sub(fence + r"[\s\S]*?" + fence, "", reply).strip()
    if not clean_text:
        clean_text = "✅ Готово! Файлы с кодом прикреплены ниже."

    for i in range(0, len(clean_text), 4000):
        await message.answer(clean_text[i:i+4000])

    for fname, code_content in files:
        doc = BufferedInputFile(code_content.encode("utf-8"), filename=fname)
        await message.answer_document(doc, caption=f"📄 Готовый файл: {fname}")

async def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is empty")
        return
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
