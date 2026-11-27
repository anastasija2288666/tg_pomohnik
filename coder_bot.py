import os
import re
import sys
import json
import sqlite3
import asyncio
import logging
import subprocess
from io import BytesIO
from typing import Optional

def ensure_packages():
    required = {
        "aiogram": "aiogram>=3.18.0",
        "aiohttp": "aiohttp>=3.9.0",
    }
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

ensure_packages()

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or ""
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW.isdigit() else 0

DB_PATH = "coder_assistant.db"

DEFAULT_MEMORY = (
    "Пользователь — разработчик на Python (aiogram 3, Telethon, SQLite) и HTML/CSS/JS. "
    "Все Telegram-боты запускаются через GitHub и облачный хостинг Bothost.ru (бесплатный тариф). "
    "Переменные окружения на Bothost: BOT_TOKEN (или TOKEN) и ADMIN_ID. "
    "Сайты и Telegram Mini Apps размещаются на GitHub Pages. "
    "Всегда выдавай полный рабочий код без пропусков, с автоустановкой зависимостей и понятной пошаговой инструкцией под Bothost."
)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT,
                content TEXT
            )
        """)
        cur = conn.execute("SELECT value FROM settings WHERE key = 'memory'")
        if not cur.fetchone():
            conn.execute("INSERT INTO settings (key, value) VALUES ('memory', ?)", (DEFAULT_MEMORY,))
        conn.commit()

def get_setting(key: str, default: str = "") -> str:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

def set_setting(key: str, value: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()

def add_history(role: str, content: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
        conn.execute("""
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history ORDER BY id DESC LIMIT 12
            )
        """)
        conn.commit()

def get_history():
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT role, content FROM history ORDER BY id ASC")
        return [{"role": r, "content": c} for r, c in cur.fetchall()]

def clear_history():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM history")
        conn.commit()

class CoderStates(StatesGroup):
    waiting_gemini_key = State()
    waiting_openrouter_key = State()
    waiting_memory = State()

router = Router()

def is_admin(user_id: int) -> bool:
    if ADMIN_ID == 0:
        return True
    return user_id == ADMIN_ID

def main_menu_kb() -> InlineKeyboardMarkup:
    provider = get_setting("provider", "gemini")
    gemini_set = "✅" if get_setting("gemini_key") else "❌"
    or_set = "✅" if get_setting("openrouter_key") else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"{'🟢' if provider == 'gemini' else '⚪'} Google Gemini ({gemini_set})",
                callback_data="set_prov:gemini",
            ),
            InlineKeyboardButton(
                text=f"{'🟢' if provider == 'openrouter' else '⚪'} OpenRouter ({or_set})",
                callback_data="set_prov:openrouter",
            ),
        ],
        [
            InlineKeyboardButton(text="🔑 Ключ Gemini", callback_data="ask_gemini"),
            InlineKeyboardButton(text="🔑 Ключ OpenRouter", callback_data="ask_openrouter"),
        ],
        [
            InlineKeyboardButton(text="🧠 Память о моих проектах", callback_data="edit_memory"),
        ],
        [
            InlineKeyboardButton(text="🧹 Новый диалог (сбросить контекст)", callback_data="clear_ctx"),
        ],
    ])

def status_text() -> str:
    provider = get_setting("provider", "gemini")
    prov_name = "Google Gemini (бесплатный API)" if provider == "gemini" else "OpenRouter (бесплатные модели)"
    g_status = "подключён" if get_setting("gemini_key") else "не задан"
    o_status = "подключён" if get_setting("openrouter_key") else "не задан"
    return (
        "🤖 <b>Личный ИИ-напарник программиста</b>\n\n"
        f"⚙️ <b>Активный движок:</b> {prov_name}\n"
        f"• Google AI Studio (Gemini): <b>{g_status}</b>\n"
        f"• OpenRouter: <b>{o_status}</b>\n\n"
        "💡 <b>Как пользоваться:</b>\n"
        "Просто напиши текстом задачу — например: <i>«Сделай бота-визитку на aiogram»</i> или "
        "<i>«Сделай лендинг для маникюра»</i>. Я пришлю готовые файлы кода прямо в чат!"
    )

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(status_text(), reply_markup=main_menu_kb(), parse_mode="HTML")

@router.callback_query(F.data.startswith("set_prov:"))
async def cb_set_prov(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    prov = call.data.split(":")[1]
    set_setting("provider", prov)
    await call.answer("Движок переключён!")
    await call.message.edit_text(status_text(), reply_markup=main_menu_kb(), parse_mode="HTML")

@router.callback_query(F.data == "ask_gemini")
async def cb_ask_gemini(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(CoderStates.waiting_gemini_key)
    await call.message.answer(
        "🔑 Отправь бесплатный API-ключ из <b>Google AI Studio</b> (aistudio.google.com):",
        parse_mode="HTML",
    )
    await call.answer()

@router.message(CoderStates.waiting_gemini_key)
async def save_gemini_key(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    set_setting("gemini_key", message.text.strip())
    set_setting("provider", "gemini")
    await state.clear()
    await message.answer("✅ Ключ Google Gemini сохранён!", reply_markup=main_menu_kb())

@router.callback_query(F.data == "ask_openrouter")
async def cb_ask_or(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(CoderStates.waiting_openrouter_key)
    await call.message.answer(
        "🔑 Отправь бесплатный API-ключ из <b>OpenRouter</b> (openrouter.ai):",
        parse_mode="HTML",
    )
    await call.answer()

@router.message(CoderStates.waiting_openrouter_key)
async def save_or_key(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    set_setting("openrouter_key", message.text.strip())
    set_setting("provider", "openrouter")
    await state.clear()
    await message.answer("✅ Ключ OpenRouter сохранён!", reply_markup=main_menu_kb())

@router.callback_query(F.data == "edit_memory")
async def cb_edit_memory(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    mem = get_setting("memory", DEFAULT_MEMORY)
    await state.set_state(CoderStates.waiting_memory)
    await call.message.answer(
        f"🧠 <b>Текущая память бота:</b>\n<i>{mem}</i>\n\n"
        "Отправь новый текст памяти, если хочешь её обновить, или /start для отмены.",
        parse_mode="HTML",
    )
    await call.answer()

@router.message(CoderStates.waiting_memory)
async def save_memory(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    set_setting("memory", message.text.strip())
    await state.clear()
    await message.answer("✅ Память обновлена!", reply_markup=main_menu_kb())

@router.callback_query(F.data == "clear_ctx")
async def cb_clear_ctx(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    clear_history()
    await call.answer("Контекст диалога очищен! Начинаем новый проект.")

async def ask_gemini(api_key: str, system_prompt: str, messages: list) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": m["content"]}]})
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 8192},
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
        "Content-Type": "application/json",
    }
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": "qwen/qwen-2.5-coder-32b-instruct:free",
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
    # Ищем блоки кода с указанием имени файла перед блоком или внутри первой строки
    pattern = re.compile(r"(?:`([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))`\s*[:\-]?\s*)?```([a-zA-Z0-9_-]*)\n([\s\(\S\)]*?)```")
    idx = 1
    for match in pattern.finditer(text):
        fname, lang, code = match.group(1), (match.group(2) or "").lower(), match.group(3)
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
                }
                base = ext_map.get(lang, f"code_{idx}.txt")
                fname = base if idx == 1 else f"{idx}_{base}"
        files.append((fname, code.strip()))
        idx += 1
    return files

@router.message(F.text)
async def handle_prompt(message: Message):
    if not is_admin(message.from_user.id):
        return

    provider = get_setting("provider", "gemini")
    api_key = get_setting("gemini_key" if provider == "gemini" else "openrouter_key", "")
    if not api_key:
        await message.answer(
            "⚠️ Сначала добавь бесплатный API-ключ в настройках ниже:",
            reply_markup=main_menu_kb(),
        )
        return

    wait_msg = await message.answer("⏳ Пишу код и собираю файлы...")
    memory = get_setting("memory", DEFAULT_MEMORY)
    system_prompt = (
        f"{memory}\n\n"
        "Правила ответа:\n"
        "1. Отвечай на русском языке чётко и по делу.\n"
        "2. Перед каждым блоком кода обязательно пиши имя файла, например: main.py или index.html.\n"
        "3. Выдавай полный готовый код без сокращений и многоточий."
    )

    add_history("user", message.text)
    hist = get_history()

    try:
        if provider == "gemini":
            reply = await ask_gemini(api_key, system_prompt, hist)
        else:
            reply = await ask_openrouter(api_key, system_prompt, hist)
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка соединения с нейросетью: {e}")
        return

    add_history("assistant", reply)
    files = extract_code_files(reply)

    # Убираем длинные блоки кода из текстового сообщения, если прикрепляем их файлами
    clean_text = re.sub(r"```[\s\S]*?```", "
📄 <i>[Код прикреплён файлом ниже]</i>
", reply).strip()
    if len(clean_text) > 3900:
        clean_text = clean_text[:3900] + "..."

    try:
        await wait_msg.edit_text(clean_text or "✅ Готово! Файлы прикреплены ниже:", parse_mode="HTML")
    except Exception:
        await wait_msg.edit_text(clean_text or "✅ Готово! Файлы прикреплены ниже:")

    for fname, code_content in files:
        doc = BufferedInputFile(code_content.encode("utf-8"), filename=fname)
        await message.answer_document(doc, caption=f"📎 Готовый файл: {fname}")

async def main():
    if not BOT_TOKEN:
        logger.error("Не задан BOT_TOKEN!")
        return
    init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Личный ИИ-помощник запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
