import os
import sys
import subprocess
import asyncio
import sqlite3
import base64
import re
from typing import List, Dict, Optional, Tuple

def ensure_packages():
    required = {"aiogram": "aiogram>=3.15.0", "aiohttp": "aiohttp"}
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

ensure_packages()

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")

DB_PATH = "coder_bot_vision.db"

DEFAULT_SYSTEM_PROMPT = (
    "Ты мой универсальный личный ИИ-помощник и напарник по работе и жизни. "
    "Отвечай живо, простым, дружелюбным и понятным языком без лишней воды. "
    "Помогай с любыми вопросами: придумывай тексты, объявления, идеи для продвижения, "
    "помогай в переговорах и продажах, разбирай ошибки на скриншотах и фото. "
    "Когда я прошу написать сайт, скрипт или Telegram-бота — пиши полный готовый к запуску код "
    "без сокращений с учётом моего рабочего стека: Python 3, aiogram 3, Telethon, SQLite, HTML/CSS/JS, "
    "запуск ботов на GitHub и Bothost.ru (с переменными BOT_TOKEN и ADMIN_ID), а сайты — на GitHub Pages. "
    "Каждый файл кода оформляй в блок с указанием точного имени файла перед ним (например: `main.py`)."
)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute(
        "CREATE TABLE IF NOT EXISTS history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "role TEXT, "
        "text TEXT)"
    )
    cur.execute(
        "CREATE TABLE IF NOT EXISTS chat_messages ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "chat_id INTEGER, "
        "message_id INTEGER)"
    )
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

def add_history(role: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO history (role, text) VALUES (?, ?)", (role, text))
    cur.execute("DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 16)")
    conn.commit()
    conn.close()

def get_history() -> List[Tuple[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role, text FROM history ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return rows

def clear_history():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM history")
    conn.commit()
    conn.close()

def track_message(chat_id: int, message_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO chat_messages (chat_id, message_id) VALUES (?, ?)", (chat_id, message_id))
    conn.commit()
    conn.close()

def pop_tracked_messages(chat_id: int) -> List[int]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT message_id FROM chat_messages WHERE chat_id = ?", (chat_id,))
    ids = [r[0] for r in cur.fetchall()]
    cur.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return ids

USER_STATES: Dict[int, str] = {}

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 API-ключи и модели", callback_data="menu_api")],
        [InlineKeyboardButton(text="🧠 Память и характер бота", callback_data="menu_memory")],
        [InlineKeyboardButton(text="🧹 Новый проект (сброс диалога)", callback_data="reset_chat")],
    ])

def api_menu_kb() -> InlineKeyboardMarkup:
    provider = get_setting("provider", "gemini")
    g_mark = "✅ " if provider == "gemini" else ""
    o_mark = "✅ " if provider == "openrouter" else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{g_mark}Использовать Google Gemini", callback_data="use_gemini")],
        [InlineKeyboardButton(text="💎 Ввести ключ Google Gemini", callback_data="set_gemini_key")],
        [InlineKeyboardButton(text="🔍 Найти доступные модели для моего ключа", callback_data="list_gemini_models")],
        [InlineKeyboardButton(text="⚙️ Ввести название модели Gemini вручную", callback_data="set_gemini_model")],
        [InlineKeyboardButton(text=f"{o_mark}Использовать OpenRouter", callback_data="use_openrouter")],
        [InlineKeyboardButton(text="🌐 Ввести ключ OpenRouter", callback_data="set_openrouter_key")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])

def memory_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить память и характер", callback_data="edit_memory")],
        [InlineKeyboardButton(text="🔄 Вернуть стандартный характер", callback_data="reset_memory")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ])

async def send_tracked(bot: Bot, chat_id: int, text: str, reply_markup=None, parse_mode=None):
    msg = await bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    track_message(chat_id, msg.message_id)
    return msg

async def typing_loop(bot: Bot, chat_id: int, stop_event: asyncio.Event):
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=4.0)
        except asyncio.TimeoutError:
            pass

async def fetch_gemini_models(api_key: str) -> List[str]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=20) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                raise RuntimeError(err)
            models = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                name = m.get("name", "").replace("models/", "")
                if "generateContent" in methods and "gemini" in name:
                    models.append(name)
            return models

async def ask_gemini(
    api_key: str,
    model: str,
    system_prompt: str,
    history: List[Tuple[str, str]],
    user_text: str,
    image_b64: Optional[str] = None,
    image_mime: str = "image/jpeg",
) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    contents = []
    for role, txt in history:
        g_role = "user" if role == "user" else "model"
        contents.append({"role": g_role, "parts": [{"text": txt}]})

    user_parts = []
    if image_b64:
        user_parts.append({
            "inline_data": {
                "mime_type": image_mime,
                "data": image_b64,
            }
        })
    user_parts.append({"text": user_text})
    contents.append({"role": "user", "parts": user_parts})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.5},
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=90) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                return f"❌ Ошибка Gemini API ({model}): {err}"
            candidates = data.get("candidates", [])
            if not candidates:
                return "⚠️ Gemini вернул пустой ответ. Попробуй переформулировать запрос."
            parts = candidates[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()

async def ask_openrouter(
    api_key: str,
    system_prompt: str,
    history: List[Tuple[str, str]],
    user_text: str,
    image_b64: Optional[str] = None,
    image_mime: str = "image/jpeg",
) -> str:
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    messages = [{"role": "system", "content": system_prompt}]
    for role, txt in history:
        messages.append({"role": role, "content": txt})

    if image_b64:
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}},
            ],
        })
        model_name = "google/gemini-2.0-flash-exp:free"
    else:
        messages.append({"role": "user", "content": user_text})
        model_name = "qwen/qwen-2.5-coder-32b-instruct:free"

    payload = {"model": model_name, "messages": messages, "temperature": 0.5}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=90) as resp:
            data = await resp.json()
            if resp.status != 200:
                err = data.get("error", {}).get("message", str(data))
                return f"❌ Ошибка OpenRouter API: {err}"
            choices = data.get("choices", [])
            if not choices:
                return "⚠️ OpenRouter вернул пустой ответ."
            return choices[0].get("message", {}).get("content", "").strip()

def extract_code_files(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    fence = chr(96) * 3
    pattern = re.compile(
        r"(?:`([a-zA-Z0-9_.-]+\.(?:py|txt|html|css|js|json|md))`\s*[:\-]?\s*)?"
        + fence + r"([a-zA-Z0-9_-]*)\n([\s\S]*?)" + fence
    )
    files: List[Tuple[str, str]] = []
    idx = 1
    for m in pattern.finditer(text):
        fname = m.group(1) or ""
        lang = (m.group(2) or "").lower()
        code = m.group(3).strip()
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
                    "json": "data.json",
                }
                fname = ext_map.get(lang, f"code_{idx}.txt")
                idx += 1
        files.append((fname, code))

    clean_text = pattern.sub("", text).strip()
    return clean_text, files

def is_admin(user_id: int) -> bool:
    return ADMIN_ID == 0 or user_id == ADMIN_ID

async def main():
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set!")
        return

    init_db()
    if not get_setting("system_prompt"):
        set_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def cmd_start(message: Message):
        if not is_admin(message.from_user.id):
            return
        USER_STATES.pop(message.from_user.id, None)
        track_message(message.chat.id, message.message_id)
        provider = get_setting("provider", "gemini")
        model = get_setting("gemini_model", "gemini-2.5-flash")
        info = f"Google Gemini ({model})" if provider == "gemini" else "OpenRouter"
        await send_tracked(
            bot,
            message.chat.id,
            f"👋 Привет! Я твой универсальный личный ИИ-помощник.\n\n"
            f"🔹 Активный движок: {info}\n"
            f"🔹 Умею отвечать на любые вопросы, придумывать тексты и идеи, писать код с выгрузкой файлов, "
            f"а также читать твои скриншоты и фото ошибок — просто отправь картинку в чат!",
            reply_markup=main_menu_kb(),
        )

    @dp.callback_query(F.data == "back_main")
    async def cb_back_main(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES.pop(call.from_user.id, None)
        await call.message.edit_text("🏠 Главное меню личного помощника:", reply_markup=main_menu_kb())
        await call.answer()

    @dp.callback_query(F.data == "menu_api")
    async def cb_menu_api(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        model = get_setting("gemini_model", "gemini-2.5-flash")
        g_key = "✅ задан" if get_setting("gemini_key") else "❌ не задан"
        o_key = "✅ задан" if get_setting("openrouter_key") else "❌ не задан"
        await call.message.edit_text(
            f"🔑 Настройки ИИ-движков:\n\n"
            f"• Ключ Gemini: {g_key}\n"
            f"• Модель Gemini: {model}\n"
            f"• Ключ OpenRouter: {o_key}",
            reply_markup=api_menu_kb(),
        )
        await call.answer()

    @dp.callback_query(F.data == "use_gemini")
    async def cb_use_gemini(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        set_setting("provider", "gemini")
        await call.message.edit_reply_markup(reply_markup=api_menu_kb())
        await call.answer("✅ Выбран Google Gemini")

    @dp.callback_query(F.data == "use_openrouter")
    async def cb_use_openrouter(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        set_setting("provider", "openrouter")
        await call.message.edit_reply_markup(reply_markup=api_menu_kb())
        await call.answer("✅ Выбран OpenRouter")

    @dp.callback_query(F.data == "set_gemini_key")
    async def cb_set_gemini_key(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES[call.from_user.id] = "wait_gemini_key"
        await send_tracked(bot, call.message.chat.id, "💎 Отправь свой API-ключ Google Gemini из Google AI Studio:")
        await call.answer()

    @dp.callback_query(F.data == "set_gemini_model")
    async def cb_set_gemini_model(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES[call.from_user.id] = "wait_gemini_model"
        await send_tracked(bot, call.message.chat.id, "⚙️ Отправь точное название модели Gemini (например: gemini-2.5-flash):")
        await call.answer()

    @dp.callback_query(F.data == "list_gemini_models")
    async def cb_list_gemini_models(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        g_key = get_setting("gemini_key")
        if not g_key:
            await call.answer("⚠️ Сначала введи ключ Google Gemini!", show_alert=True)
            return
        await call.answer("🔍 Запрашиваю список моделей у Google...")
        try:
            models = await fetch_gemini_models(g_key)
            if not models:
                await send_tracked(bot, call.message.chat.id, "⚠️ Для этого ключа не найдено доступных моделей.")
                return
            buttons = [
                [InlineKeyboardButton(text=f"🤖 {m}", callback_data=f"pick_model:{m}")]
                for m in models[:15]
            ]
            buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_api")])
            await send_tracked(
                bot,
                call.message.chat.id,
                "✅ Доступные модели для твоего ключа (нажми на любую для выбора):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
        except Exception as e:
            await send_tracked(bot, call.message.chat.id, f"❌ Не удалось получить список моделей: {e}")

    @dp.callback_query(F.data.startswith("pick_model:"))
    async def cb_pick_model(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        model_name = call.data.split(":", 1)[1]
        set_setting("gemini_model", model_name)
        set_setting("provider", "gemini")
        await send_tracked(bot, call.message.chat.id, f"✅ Выбрана модель: {model_name}", reply_markup=main_menu_kb())
        await call.answer()

    @dp.callback_query(F.data == "set_openrouter_key")
    async def cb_set_openrouter_key(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES[call.from_user.id] = "wait_openrouter_key"
        await send_tracked(bot, call.message.chat.id, "🌐 Отправь свой API-ключ OpenRouter (начинается с sk-or-...):")
        await call.answer()

    @dp.callback_query(F.data == "menu_memory")
    async def cb_menu_memory(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        prompt = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
        await call.message.edit_text(
            f"🧠 Текущая память и характер бота:\n\n{prompt}",
            reply_markup=memory_menu_kb(),
        )
        await call.answer()

    @dp.callback_query(F.data == "edit_memory")
    async def cb_edit_memory(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES[call.from_user.id] = "wait_memory"
        await send_tracked(
            bot,
            call.message.chat.id,
            "✏️ Отправь новый текст памяти/характера для бота одним сообщением:",
        )
        await call.answer()

    @dp.callback_query(F.data == "reset_memory")
    async def cb_reset_memory(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES.pop(call.from_user.id, None)
        set_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
        await call.message.edit_text(
            f"🔄 Стандартный характер восстановлен:\n\n{DEFAULT_SYSTEM_PROMPT}",
            reply_markup=memory_menu_kb(),
        )
        await call.answer("✅ Восстановлено")

    @dp.callback_query(F.data == "reset_chat")
    async def cb_reset_chat(call: CallbackQuery):
        if not is_admin(call.from_user.id):
            return
        USER_STATES.pop(call.from_user.id, None)
        clear_history()
        chat_id = call.message.chat.id
        msg_ids = pop_tracked_messages(chat_id)
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
            bot,
            chat_id,
            "🧹 Диалог сброшен, история чата очищена! Чем помочь дальше?",
            reply_markup=main_menu_kb(),
        )
        await call.answer()

    @dp.message(F.photo | F.document | F.text)
    async def handle_all(message: Message):
        if not is_admin(message.from_user.id):
            return

        uid = message.from_user.id
        chat_id = message.chat.id
        track_message(chat_id, message.message_id)

        state = USER_STATES.get(uid)
        text_in = (message.text or message.caption or "").strip()

        if state == "wait_gemini_key" and message.text:
            USER_STATES.pop(uid, None)
            set_setting("gemini_key", text_in)
            set_setting("provider", "gemini")
            await send_tracked(
                bot,
                chat_id,
                "✅ Ключ Google Gemini сохранён и выбран основным!",
                reply_markup=main_menu_kb(),
            )
            return

        if state == "wait_gemini_model" and message.text:
            USER_STATES.pop(uid, None)
            set_setting("gemini_model", text_in)
            set_setting("provider", "gemini")
            await send_tracked(
                bot,
                chat_id,
                f"✅ Модель Gemini изменена на: {text_in}",
                reply_markup=main_menu_kb(),
            )
            return

        if state == "wait_openrouter_key" and message.text:
            USER_STATES.pop(uid, None)
            set_setting("openrouter_key", text_in)
            set_setting("provider", "openrouter")
            await send_tracked(
                bot,
                chat_id,
                "✅ Ключ OpenRouter сохранён и выбран основным!",
                reply_markup=main_menu_kb(),
            )
            return

        if state == "wait_memory" and message.text:
            USER_STATES.pop(uid, None)
            set_setting("system_prompt", text_in)
            await send_tracked(
                bot,
                chat_id,
                "✅ Память и характер бота успешно обновлены!",
                reply_markup=main_menu_kb(),
            )
            return

        image_b64: Optional[str] = None
        image_mime = "image/jpeg"

        if message.photo:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            bio = await bot.download_file(file_info.file_path)
            image_b64 = base64.b64encode(bio.read()).decode("utf-8")
            image_mime = "image/jpeg"
        elif message.document and (message.document.mime_type or "").startswith("image/"):
            file_info = await bot.get_file(message.document.file_id)
            bio = await bot.download_file(file_info.file_path)
            image_b64 = base64.b64encode(bio.read()).decode("utf-8")
            image_mime = message.document.mime_type or "image/png"

        if not text_in and image_b64:
            text_in = (
                "Внимательно изучи этот скриншот или фотографию. "
                "Если на картинке есть ошибка кода, логов или настройки — объясни простым языком, "
                "в чём причина, и дай пошаговое решение."
            )

        if not text_in and not image_b64:
            return

        provider = get_setting("provider", "gemini")
        system_prompt = get_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
        history = get_history()

        if provider == "gemini":
            g_key = get_setting("gemini_key")
            if not g_key:
                await send_tracked(
                    bot,
                    chat_id,
                    "⚠️ Сначала добавь API-ключ Google Gemini в разделе «🔑 API-ключи и модели».",
                    reply_markup=main_menu_kb(),
                )
                return
            model = get_setting("gemini_model", "gemini-2.5-flash")
        else:
            o_key = get_setting("openrouter_key")
            if not o_key:
                await send_tracked(
                    bot,
                    chat_id,
                    "⚠️ Сначала добавь API-ключ OpenRouter в разделе «🔑 API-ключи и модели».",
                    reply_markup=main_menu_kb(),
                )
                return

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(typing_loop(bot, chat_id, stop_event))

        try:
            if provider == "gemini":
                reply = await ask_gemini(
                    g_key, model, system_prompt, history, text_in, image_b64=image_b64, image_mime=image_mime
                )
            else:
                reply = await ask_openrouter(
                    o_key, system_prompt, history, text_in, image_b64=image_b64, image_mime=image_mime
                )
        except asyncio.TimeoutError:
            reply = "⏳ Сервер ИИ слишком долго не отвечал (таймаут). Попробуй повторить запрос или выбери другую модель в меню."
        except Exception as e:
            reply = f"❌ Ошибка соединения: {e}"
        finally:
            stop_event.set()
            await typing_task

        hist_label = f"[Фото] {text_in}" if image_b64 else text_in
        add_history("user", hist_label)
        add_history("assistant", reply)

        clean_text, files = extract_code_files(reply)

        if clean_text:
            for chunk in [clean_text[i:i + 4000] for i in range(0, len(clean_text), 4000)]:
                await send_tracked(bot, chat_id, chunk)

        for fname, code_content in files:
            doc = BufferedInputFile(code_content.encode("utf-8"), filename=fname)
            sent_doc = await bot.send_document(chat_id, document=doc, caption=f"📄 Готовый файл: {fname}")
            track_message(chat_id, sent_doc.message_id)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
