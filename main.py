import os
import uuid
import logging
import html
import asyncio
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
# Поддержка как ALLOWED_ID, так и USERS (через запятую или пробел)
ALLOWED_IDS_RAW = os.getenv("USERS") or os.getenv("ALLOWED_ID") or ""
ALLOWED_IDS = set()
for item in ALLOWED_IDS_RAW.replace(",", " ").split():
    if item.strip().isdigit():
        ALLOWED_IDS.add(int(item.strip()))

# URL Jackett (по умолчанию имя сервиса в docker-network: jackett)
JACKETT_URL = os.getenv("JACKETT_URL", "http://jackett:9117").rstrip("/")
JACKETT_API = os.getenv("JACKETT_API", "")
PROXY = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
WATCH_DIR = os.getenv("WATCH_DIR", "/watch")

if not TOKEN:
    logging.warning("Внимание: TG_TOKEN не установлен в переменных окружения!")

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()

# Ограниченное хранилище ссылок (LRU cache на 500 элементов)
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
        return True  # если список пуст, разрешаем доступ всем (или настройте по желанию)
    return user_id in ALLOWED_IDS

@dp.message(Command("start"))
async def start(message: types.Message):
    if not is_allowed(message.from_user.id):
        logging.warning(f"Unauthorized access attempt from user_id={message.from_user.id}")
        return
    await message.reply("Привет! Напиши мне название фильма или сериала, и я поищу его на трекерах.")

@dp.message(F.text)
async def search(message: types.Message):
    if not is_allowed(message.from_user.id):
        return

    query = message.text.strip()
    if not query:
        return

    api_url = f"{JACKETT_URL}/api/v2.0/indexers/all/results"
    params = {"apikey": JACKETT_API, "Query": query}

    msg = await message.answer(f"🔍 Ищу «<b>{html.escape(query)}</b>»...", parse_mode="HTML")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(api_url, params=params, proxy=PROXY) as resp:
                if resp.status != 200:
                    text_err = await resp.text()
                    logging.error(f"Jackett API status {resp.status}: {text_err[:200]}")
                    await msg.edit_text("❌ Ошибка при обращении к API Jackett.")
                    return
                data = await resp.json()
        except Exception as e:
            logging.error(f"Error querying Jackett API: {e}")
            await msg.edit_text(f"❌ Ошибка соединения: {html.escape(str(e))}", parse_mode="HTML")
            return

    results = data.get("Results", [])[:7]  # Берем топ-7 результатов
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

    await call.answer("Скачиваю торрент...")

    # Если это magnet-ссылка
    if download_link.startswith("magnet:"):
        try:
            os.makedirs(WATCH_DIR, exist_ok=True)
            fname = f"torrent_{link_id}.magnet"
            filepath = os.path.join(WATCH_DIR, fname)
            async with aiofiles.open(filepath, 'w', encoding='utf-8') as f:
                await f.write(download_link)

            await call.message.edit_text(
                f"{call.message.html_text}\n\n✅ <b>Magnet-ссылка отправлена в загрузки!</b>",
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Error writing magnet file: {e}")
            await call.message.edit_text("❌ Ошибка при сохранении magnet-файла.")
        return

    # Загрузка .torrent файла по HTTP/HTTPS
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(download_link, proxy=PROXY, allow_redirects=True) as resp:
                if resp.status == 200:
                    content = await resp.read()

                    # Определение имени файла
                    cd = resp.headers.get("Content-Disposition", "")
                    fname = f"torrent_{link_id}.torrent"
                    if "filename=" in cd:
                        extracted = cd.split("filename=")[-1].strip('"').strip("'")
                        if extracted:
                            fname = extracted

                    os.makedirs(WATCH_DIR, exist_ok=True)
                    filepath = os.path.join(WATCH_DIR, fname)
                    async with aiofiles.open(filepath, 'wb') as f:
                        await f.write(content)

                    await call.message.edit_text(
                        f"{call.message.html_text}\n\n✅ <b>Отправлено в загрузки!</b>",
                        parse_mode="HTML"
                    )
                else:
                    logging.error(f"Download status {resp.status} for link {download_link}")
                    await call.message.edit_text("❌ Ошибка при скачивании файла с трекера.")
        except Exception as e:
            logging.error(f"Download error: {e}")
            await call.message.edit_text("❌ Ошибка соединения при скачивании.")

async def main():
    if not bot:
        logging.error("TG_TOKEN не задан. Завершение работы.")
        return
    logging.info("Запуск бота Jackett2Telegram...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
