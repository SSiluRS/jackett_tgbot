import os
import uuid
import logging
import html
import asyncio
import math
from collections import OrderedDict
import aiohttp
import aiofiles
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)

# Переменные окружения
TOKEN = os.getenv("TG_TOKEN")
ALLOWED_IDS_RAW = os.getenv("USERS") or os.getenv("ALLOWED_ID") or ""
ALLOWED_IDS = set()
for item in ALLOWED_IDS_RAW.replace(",", " ").split():
    if item.strip().isdigit():
        ALLOWED_IDS.add(int(item.strip()))

JACKETT_URL = os.getenv("JACKETT_URL", "http://jackett:9117").rstrip("/")
JACKETT_API = os.getenv("JACKETT_API", "")
PROXY = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
WATCH_DIR = os.getenv("WATCH_DIR", "/watch")

# Настройки qBittorrent Web API
QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://qbittorrent:8083").rstrip("/")
QBITTORRENT_USER = os.getenv("QBITTORRENT_USER", "admin")
QBITTORRENT_PASS = os.getenv("QBITTORRENT_PASS", "adminadmin")

from aiogram.client.session.aiohttp import AiohttpSession

if not TOKEN:
    logging.warning("Внимание: TG_TOKEN не установлен в переменных окружения!")

# Использование Xray VLESS прокси для работы с Telegram API (api.telegram.org)
bot_session = AiohttpSession(proxy=PROXY) if (TOKEN and PROXY) else (AiohttpSession() if TOKEN else None)
bot = Bot(token=TOKEN, session=bot_session) if TOKEN else None
dp = Dispatcher()

class LRUDict(OrderedDict):
    def __init__(self, maxsize=500, *args, **kwargs):
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)

links_db = LRUDict(maxsize=500)

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        logging.info(f"ALLOWED_IDS is empty -> access GRANTED for user {user_id}")
        return True
    allowed = user_id in ALLOWED_IDS
    if not allowed:
        logging.warning(f"Access DENIED for user {user_id}. Allowed IDs: {ALLOWED_IDS}")
    return allowed

def format_size(size_bytes: float) -> str:
    if size_bytes <= 0:
        return "0 B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def format_speed(speed_bytes: float) -> str:
    return f"{format_size(speed_bytes)}/s"

def format_eta(seconds: int) -> str:
    if seconds >= 8640000 or seconds < 0:
        return "∞"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}ч {minutes}м"
    if minutes > 0:
        return f"{minutes}м {secs}с"
    return f"{secs}с"

def make_progress_bar(progress: float, length: int = 10) -> str:
    filled = int(round(length * progress))
    bar = "█" * filled + "░" * (length - filled)
    percent = int(progress * 100)
    return f"[{bar}] {percent}%"

async def get_qbittorrent_torrents():
    """Авторизация и получение списка торрентов из qBittorrent Web API"""
    login_url = f"{QBITTORRENT_URL}/api/v2/auth/login"
    info_url = f"{QBITTORRENT_URL}/api/v2/torrents/info"

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            # 1. Авторизация (qBittorrent возвращает 200 или 204 "No Content" при успешном входе)
            async with session.post(login_url, data={"username": QBITTORRENT_USER, "password": QBITTORRENT_PASS}) as resp:
                if resp.status not in (200, 204):
                    logging.error(f"qBittorrent auth failed: {resp.status}")
                    return None, "❌ Ошибка авторизации в qBittorrent."
                cookie = resp.cookies.get("SID")
                if not cookie:
                    return None, "❌ Не удалось получить cookie сессии qBittorrent."

            # 2. Запрос списка торрентов
            cookies = {"SID": cookie.value}
            async with session.get(info_url, cookies=cookies) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data, None
                return None, f"❌ Ошибка qBittorrent API ({resp.status})"
        except Exception as e:
            logging.error(f"qBittorrent connection error: {e}")
            return None, f"❌ Ошибка подключения к qBittorrent: {e}"

@dp.message(Command("start"))
async def start(message: types.Message):
    logging.info(f"Received /start from user_id={message.from_user.id}")
    if not is_allowed(message.from_user.id):
        return
    text = (
        "Привет! Я бот для управления торрентами.\n\n"
        "🔍 Напиши мне название фильма или сериала для поиска.\n"
        "📊 Используй команду /downloads или /status для просмотра текущих загрузок."
    )
    await message.reply(text)

@dp.message(Command("downloads", "status"))
async def show_downloads(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    text, reply_markup = await generate_downloads_status()
    await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")

@dp.callback_query(F.data == "refresh_downloads")
async def refresh_downloads(call: types.CallbackQuery):
    if not is_allowed(call.from_user.id):
        return

    text, reply_markup = await generate_downloads_status()
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await call.answer("Обновлено")
    except Exception:
        await call.answer("Без изменений")

async def generate_downloads_status():
    torrents, err = await get_qbittorrent_torrents()
    if err:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_downloads")
        ]])
        return err, kb

    if not torrents:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_downloads")
        ]])
        return "📂 Список загрузок пуст.", kb

    # Сортируем: сначала качающиеся, потом остальное (не больше 10 раздач)
    torrents.sort(key=lambda x: (x.get("state") != "downloading", x.get("added_on", 0)), reverse=False)
    torrents_slice = torrents[:10]

    lines = ["<b>📊 Статус загрузок qBittorrent:</b>\n"]

    state_map = {
        "downloading": "⬇️ Скачивание",
        "stalledDL": "⏳ Ожидание сидов",
        "uploading": "⬆️ Раздача",
        "stalledUP": "🟢 Завершено (раздача)",
        "pausedDL": "⏸ На паузе",
        "pausedUP": "⏸ Пауза (раздача)",
        "queuedDL": "🕒 В очереди",
        "checkingDL": "🔍 Проверка",
        "checkingUP": "🔍 Проверка",
        "error": "❌ Ошибка"
    }

    for t in torrents_slice:
        name = html.escape(t.get("name", "Без имени"))
        progress = t.get("progress", 0.0)
        dlspeed = t.get("dlspeed", 0)
        upspeed = t.get("upspeed", 0)
        eta = t.get("eta", 8640000)
        state_raw = t.get("state", "")
        state_str = state_map.get(state_raw, state_raw)

        prog_bar = make_progress_bar(progress)
        size_str = format_size(t.get("size", 0))

        item_text = (
            f"🎬 <b>{name}</b>\n"
            f"{prog_bar} | {size_str}\n"
            f"Статус: {state_str}\n"
        )
        if state_raw in ["downloading", "stalledDL"]:
            item_text += f"🚀 ⬇️ {format_speed(dlspeed)} | ETA: {format_eta(eta)}\n"
        elif state_raw in ["uploading", "stalledUP"]:
            item_text += f"🚀 ⬆️ {format_speed(upspeed)}\n"

        lines.append(item_text)

    if len(torrents) > 10:
        lines.append(f"<i>...и еще {len(torrents) - 10} торрент(ов)</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_downloads")
    ]])

    return "\n".join(lines), kb

@dp.message(F.text)
async def search(message: types.Message):
    logging.info(f"Received text '{message.text}' from user_id={message.from_user.id}")
    if not is_allowed(message.from_user.id):
        return

    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    api_url = f"{JACKETT_URL}/api/v2.0/indexers/all/results"
    params = {"apikey": JACKETT_API, "Query": query}

    msg = await message.answer(f"🔍 Ищу «<b>{html.escape(query)}</b>»...", parse_mode="HTML")

    timeout = aiohttp.ClientTimeout(total=90)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        try:
            # trust_env=False гарантирует прямой запрос к локальному Jackett в сети Docker без Xray прокси
            async with session.get(api_url, params=params) as resp:
                if resp.status != 200:
                    text_err = await resp.text()
                    logging.error(f"Jackett API status {resp.status}: {text_err[:200]}")
                    await msg.edit_text("❌ Ошибка при обращении к API Jackett.")
                    return
                data = await resp.json()
        except Exception as e:
            logging.error(f"Error querying Jackett API: {e}", exc_info=True)
            await msg.edit_text(f"❌ Ошибка соединения: {html.escape(str(e))}", parse_mode="HTML")
            return

    results = data.get("Results", [])[:7]
    if not results:
        await msg.edit_text("🤷‍♂️ Ничего не найдено.")
        return

    try:
        await msg.delete()
    except Exception:
        pass

    for res in results:
        title = res.get("Title", "Без названия")
        size_bytes = res.get("Size", 0)
        size_gb = round(size_bytes / (1024**3), 2)
        seeders = res.get("Seeders", 0)
        download_link = res.get("Link") or res.get("MagnetUri")

        if not download_link:
            continue

        link_id = str(uuid.uuid4())[:8]
        links_db[link_id] = download_link

        safe_title = html.escape(title)
        text = f"🎬 <b>{safe_title}</b>\n💾 Размер: {size_gb} GB | 🟢 Сиды: {seeders}"

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📥 Скачать", callback_data=f"dl_{link_id}")
        ]])
        await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("dl_"))
async def download_torrent(call: types.CallbackQuery):
    if not is_allowed(call.from_user.id):
        return

    link_id = call.data.split("_")[1]
    download_link = links_db.get(link_id)

    if not download_link:
        await call.answer("Ссылка устарела или не найдена. Повторите поиск.", show_alert=True)
        return

    await call.answer("Отправляю в Jackett...")

    # Вызываем скачивание через Jackett (Jackett сам поместит файл в watch/qBittorrent)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
        try:
            async with session.get(download_link, allow_redirects=True) as resp:
                if resp.status in (200, 204, 302):
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="📊 Прогресс закачек", callback_data="refresh_downloads")
                    ]])
                    await call.message.edit_text(
                        f"{call.message.html_text}\n\n✅ <b>Отправлено в загрузки!</b>",
                        reply_markup=kb,
                        parse_mode="HTML"
                    )
                else:
                    logging.error(f"Jackett trigger status {resp.status} for link {download_link}")
                    await call.message.edit_text(f"❌ Jackett вернул статус {resp.status}.")
        except Exception as e:
            logging.error(f"Download trigger error: {e}")
            await call.message.edit_text("❌ Ошибка соединения с Jackett.")

async def main():
    if not bot:
        logging.error("TG_TOKEN не задан. Завершение работы.")
        return
    logging.info("Запуск бота Jackett_TGBot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
