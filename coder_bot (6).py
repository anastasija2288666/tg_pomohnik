import os
import sys
import subprocess
import importlib.util

def ensure_packages():
    required = {"aiogram": "aiogram>=3.18.0", "aiohttp": "aiohttp"}
    missing = [pkg for mod, pkg in required.items() if importlib.util.find_spec(mod) is None]
    if missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])

ensure_packages()

import re
import json
import sqlite3
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)

if not BOT_TOKEN:
    raise RuntimeError("Не задана переменная окружения BOT_TOKEN!")

DB_PATH = "coder_assistant.db"

DEFAULT_SYSTEM_PROMPT = (
    "Ты мой универсальный личный ИИ-помощник и напарник по работе и жизни. "
    "Отвечай живо, простым, дружелюбным и понятным языком без лишней воды. "
    "Помогай с любыми вопросами: придумывай тексты, объявления, идеи для продвижения, "
    "помогай в переговорах и продажах. "
    "Когда я прошу написать сайт, скрипт или Telegram-бота — пиши полный готовый к запуску код "
    "без сокращений с учётом моего рабочего стека: Python 3, aiogram 3, Telethon, SQLite, "
    "HTML/CSS/JS, запуск на GitHub и Bothost.ru (с переменными BOT_TOKEN и ADMIN_ID), "
    "а сайты — на GitHub Pages. Каждый файл кода оформляй в блок с указанием точного имени файла перед ним."
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        )
    """)
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
    cur.execute("""
        DELETE FROM history WHERE id NOT IN (
            SELECT id FROM history ORDER BY id DESC LIMIT 20
        )
    """)
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

def track_msg(chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO tracked_messages (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
    conn.commit()
    conn.close()

def get_and_clear_tracked(chat_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT message_id FROM tracked_messages WHERE chat_id = ?", (chat_id,))
    ids = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM tracked_messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return ids

init_db()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
user_states = {}

def is_admin(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    model = get_setting("gemini_model", "gemini-2.5-flash")
    kb.button(text=f"🔑 API-ключи и модели ({model})", callback_data="menu_keys")
    kb.button(text="🧠 Память и характер бота", callback_data="menu_memory")
    kb.button(text="🧹 Новый проект (сброс диалога)", callback_data="reset_dialog")
    kb.adjust(1)
    return kb.as_markup()

def keys_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Ввести ключ Google Gemini", callback_data="set_gemini_key")
    kb.button(text="🔍 Найти доступные модели для моего ключа", callback_data="fetch_models")
    kb.button(text="⚙️ Ввести название модели вручную", callback_data="set_gemini_model")
    kb.button(text="🌐 Ввести ключ OpenRouter (резерв)", callback_data="set_or_key")
    kb.button(text="◀️ Назад в меню", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

def memory_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить память и характер", callback_data="edit_memory")
    kb.button(text="🔄 Вернуть стандартный характер", callback_data="reset_memory")
    kb.button(text="◀️ Назад в меню", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

async def send_tracked(chat_id: int, text: str, reply_markup=None, parse_mode=None):
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    track_msg(chat_id, msg.message_id)
    return msg

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id):
        return
    user_states.pop(message.from_user.id, None)
    track_msg(message.chat.id, message.message_id)
    has_key = bool(get_setting("gemini_key") or get_setting("openrouter_key"))
    status = "✅ API-ключ подключён и готов к работе!" if has_key else "⚠️ Сначала добавь бесплатный ключ Google Gemini в настройках ниже."
    await send_tracked(
        message.chat.id,
        f"👋 Привет! Я твой личный ИИ-напарник.\n\n"
        f"{status}\n\n"
        f"Задавай любой вопрос текстом или попроси написать сайт / Telegram-бота — я соберу готовые файлы!",
        reply_markup=main_menu_kb()
    )

@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states.pop(call.from_user.id, None)
    await call.message.edit_text("🏠 Главное меню твоего ИИ-помощника:", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "menu_keys")
async def cb_menu_keys(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states.pop(call.from_user.id, None)
    g_key = get_setting("gemini_key")
    or_key = get_setting("openrouter_key")
    active = get_setting("active_provider", "gemini")
    model = get_setting("gemini_model", "gemini-2.5-flash")
    g_status = f"установлен (...{g_key[-6:]})" if len(g_key) > 6 else "не задан"
    or_status = f"установлен (...{or_key[-6:]})" if len(or_key) > 6 else "не задан"
    await call.message.edit_text(
        f"🔑 Настройки ИИ-движка\n\n"
        f"• Активный сервис: {active.upper()}\n"
        f"• Ключ Google Gemini: {g_status}\n"
        f"• Модель Gemini: {model}\n"
        f"• Ключ OpenRouter: {or_status}\n\n"
        f"Нажми «🔍 Найти доступные модели», чтобы бот сам показал список рабочих моделей для твоего ключа Google!",
        reply_markup=keys_menu_kb()
    )
    await call.answer()

@dp.callback_query(F.data == "set_gemini_key")
async def cb_set_gemini_key(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states[call.from_user.id] = "wait_gemini_key"
    await send_tracked(call.message.chat.id, "💎 Отправь свой API-ключ Google Gemini:")
    await call.answer()

@dp.callback_query(F.data == "set_gemini_model")
async def cb_set_gemini_model(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states[call.from_user.id] = "wait_gemini_model"
    await send_tracked(call.message.chat.id, "⚙️ Отправь точное название модели Google Gemini (например: gemini-2.5-flash):")
    await call.answer()

@dp.callback_query(F.data == "set_or_key")
async def cb_set_or_key(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states[call.from_user.id] = "wait_or_key"
    await send_tracked(call.message.chat.id, "🌐 Отправь API-ключ OpenRouter (начинается с sk-or-...):")
    await call.answer()

@dp.callback_query(F.data == "fetch_models")
async def cb_fetch_models(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    api_key = get_setting("gemini_key")
    if not api_key:
        await call.answer("Сначала введи ключ Google Gemini!", show_alert=True)
        return
    await call.answer("Проверяю доступные модели...")
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if resp.status != 200:
                    err = data.get("error", {}).get("message", str(data))
                    await send_tracked(call.message.chat.id, f"❌ Ошибка при проверке моделей: {err}")
                    return
                models = data.get("models", [])
                valid = []
                for m in models:
                    methods = m.get("supportedGenerationMethods", [])
                    name = m.get("name", "").replace("models/", "")
                    if "generateContent" in methods and "gemini" in name.lower():
                        valid.append(name)
                if not valid:
                    await send_tracked(call.message.chat.id, "⚠️ Для этого ключа не найдено текстовых моделей Gemini.")
                    return
                kb = InlineKeyboardBuilder()
                for m_name in valid[:12]:
                    kb.button(text=f"✅ {m_name}", callback_data=f"pick_model:{m_name}")
                kb.button(text="◀️ Назад", callback_data="menu_keys")
                kb.adjust(1)
                await send_tracked(
                    call.message.chat.id,
                    "📋 Вот все модели, которые точно открыты для твоего ключа Google прямо сейчас.\n"
                    "Выбери любую (лучше всего с пометкой flash):",
                    reply_markup=kb.as_markup()
                )
    except Exception as e:
        await send_tracked(call.message.chat.id, f"❌ Ошибка сети: {e}")

@dp.callback_query(F.data.startswith("pick_model:"))
async def cb_pick_model(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    chosen = call.data.split(":", 1)[1]
    set_setting("gemini_model", chosen)
    set_setting("active_provider", "gemini")
    await call.message.edit_text(
        f"✅ Выбрана рабочая модель: {chosen}\nМожешь сразу писать свой вопрос!",
        reply_markup=main_menu_kb()
    )
    await call.answer("Модель сохранена!")

@dp.callback_query(F.data == "menu_memory")
async def cb_menu_memory(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states.pop(call.from_user.id, None)
    current = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    await call.message.edit_text(
        f"🧠 Текущая память и характер бота:\n\n{current}",
        reply_markup=memory_menu_kb()
    )
    await call.answer()

@dp.callback_query(F.data == "edit_memory")
async def cb_edit_memory(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states[call.from_user.id] = "wait_memory"
    await send_tracked(
        call.message.chat.id,
        "✏️ Отправь новый текст памяти и характера бота одним сообщением (кем он должен быть, как отвечать и что знать о твоих проектах):"
    )
    await call.answer()

@dp.callback_query(F.data == "reset_memory")
async def cb_reset_memory(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states.pop(call.from_user.id, None)
    set_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    await call.message.edit_text(
        f"🔄 Возвращён стандартный характер универсального помощника:\n\n{DEFAULT_SYSTEM_PROMPT}",
        reply_markup=memory_menu_kb()
    )
    await call.answer("Сброшено!")

@dp.callback_query(F.data == "reset_dialog")
async def cb_reset_dialog(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    user_states.pop(call.from_user.id, None)
    clear_history()
    chat_id = call.message.chat.id
    msg_ids = get_and_clear_tracked(chat_id)
    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass
    try:
        await call.message.delete()
    except Exception:
        pass
    await send_tracked(
        chat_id,
        "🧹 Чат и память диалога полностью очищены! Начинаем с чистого листа.",
        reply_markup=main_menu_kb()
    )
    await call.answer()

async def ask_gemini(api_key: str, model: str, system_prompt: str, history: list) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    contents = []
    for item in history:
        role = "user" if item["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": item["content"]}]})
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6}
    }
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                data = await resp.json()
                if resp.status != 200:
                    err = data.get("error", {}).get("message", str(data))
                    return f"❌ Ошибка Gemini API ({model}): {err}\n\n💡 Нажми «🔑 API-ключи и модели -> 🔍 Найти доступные модели», чтобы выбрать другую рабочую модель."
                candidates = data.get("candidates", [])
                if not candidates:
                    return "⚠️ Модель вернула пустой ответ. Попробуй переформулировать запрос."
                parts = candidates[0].get("content", {}).get("parts", [])
                return "".join(p.get("text", "") for p in parts)
    except asyncio.TimeoutError:
        return f"⏳ Сервер Google ({model}) не ответил за 45 секунд (временная перегрузка). Попробуй повторить запрос или выбери другую модель в меню «🔑 API-ключи и модели»."
    except Exception as e:
        return f"❌ Ошибка соединения с Google: {e}"

async def ask_openrouter(api_key: str, system_prompt: str, history: list) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    messages = [{"role": "system", "content": system_prompt}] + history
    payload = {
        "model": "qwen/qwen-2.5-coder-32b-instruct:free",
        "messages": messages,
        "temperature": 0.6
    }
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                data = await resp.json()
                if resp.status != 200:
                    err = data.get("error", {}).get("message", str(data))
                    return f"❌ Ошибка OpenRouter: {err}"
                choices = data.get("choices", [])
                if not choices:
                    return "⚠️ OpenRouter вернул пустой ответ."
                return choices[0].get("message", {}).get("content", "")
    except Exception as e:
        return f"❌ Ошибка OpenRouter: {e}"

def extract_code_files(text: str):
    fence = chr(96) * 3
    pattern = re.compile(re.escape(fence) + r"([a-zA-Z0-9_-]*)\n([\s\S]*?)" + re.escape(fence))
    files = []
    ext_map = {
        "python": "main.py",
        "py": "main.py",
        "html": "index.html",
        "css": "style.css",
        "js": "script.js",
        "javascript": "script.js",
        "json": "data.json",
        "txt": "requirements.txt"
    }
    idx = 1
    for m in pattern.finditer(text):
        lang = (m.group(1) or "").lower().strip()
        code = m.group(2).strip()
        if not code:
            continue
        before = text[:m.start()].strip().splitlines()
        fname = ""
        if before:
            last_line = before[-1]
            fn_match = re.search(r"([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))", last_line)
            if fn_match:
                fname = fn_match.group(1)
        if not fname:
            fname = ext_map.get(lang, f"code_{idx}.txt")
            idx += 1
        files.append((fname, code))
    clean_text = pattern.sub("", text).strip()
    return clean_text, files

async def keep_typing(chat_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)

@dp.message(F.text)
async def handle_text(message: Message):
    if not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    chat_id = message.chat.id
    text = message.text.strip()
    track_msg(chat_id, message.message_id)

    state = user_states.get(uid)
    if state == "wait_gemini_key":
        set_setting("gemini_key", text)
        set_setting("active_provider", "gemini")
        user_states.pop(uid, None)
        await send_tracked(
            chat_id,
            "✅ Ключ Google Gemini сохранён!\n"
            "Теперь нажми «🔍 Найти доступные модели для моего ключа», чтобы выбрать самую быструю рабочую модель.",
            reply_markup=keys_menu_kb()
        )
        return

    if state == "wait_gemini_model":
        set_setting("gemini_model", text)
        set_setting("active_provider", "gemini")
        user_states.pop(uid, None)
        await send_tracked(chat_id, f"✅ Модель Gemini изменена на: {text}", reply_markup=main_menu_kb())
        return

    if state == "wait_or_key":
        set_setting("openrouter_key", text)
        set_setting("active_provider", "openrouter")
        user_states.pop(uid, None)
        await send_tracked(chat_id, "✅ Ключ OpenRouter сохранён и выбран основным!", reply_markup=main_menu_kb())
        return

    if state == "wait_memory":
        set_setting("system_prompt", text)
        user_states.pop(uid, None)
        await send_tracked(chat_id, "✅ Новая память и характер бота сохранены!", reply_markup=main_menu_kb())
        return

    provider = get_setting("active_provider", "gemini")
    g_key = get_setting("gemini_key")
    or_key = get_setting("openrouter_key")

    if provider == "gemini" and not g_key:
        await send_tracked(chat_id, "⚠️ Сначала введи API-ключ Google Gemini в меню ниже:", reply_markup=keys_menu_kb())
        return
    if provider == "openrouter" and not or_key:
        await send_tracked(chat_id, "⚠️ Сначала введи API-ключ OpenRouter в меню ниже:", reply_markup=keys_menu_kb())
        return

    add_history("user", text)
    history = get_history()
    sys_prompt = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, stop_typing))

    try:
        if provider == "openrouter":
            reply = await ask_openrouter(or_key, sys_prompt, history)
        else:
            model = get_setting("gemini_model", "gemini-2.5-flash")
            reply = await ask_gemini(g_key, model, sys_prompt, history)
    finally:
        stop_typing.set()
        typing_task.cancel()

    add_history("assistant", reply)
    clean_text, files = extract_code_files(reply)

    if clean_text:
        for i in range(0, len(clean_text), 4000):
            await send_tracked(chat_id, clean_text[i:i+4000])

    for fname, code_content in files:
        doc = BufferedInputFile(code_content.encode("utf-8"), filename=fname)
        sent_doc = await bot.send_document(chat_id=chat_id, document=doc, caption=f"📄 Готовый файл: {fname}")
        track_msg(chat_id, sent_doc.message_id)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
