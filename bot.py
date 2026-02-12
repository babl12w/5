import asyncio
import io
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urljoin

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not UNSPLASH_ACCESS_KEY:
    raise RuntimeError("UNSPLASH_ACCESS_KEY is required")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music_bot")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10, sock_read=20)
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024

GENRES = ["Pop", "Rock", "Rap", "Electronic"]
POLL_OPTIONS: dict[str, list[str]] = {
    "Опитування 1": ["🔥 Топ", "🎧 Норм", "⏭ Скіп"],
    "Опитування 2": ["Pop", "Rock", "Rap", "Electronic"],
    "Опитування 3": ["Ранок", "День", "Вечір", "Ніч"],
    "Опитування 4": ["Більше треків", "Більше цитат", "Більше фото"],
}

QUOTE_FEEDS = [
    "https://www.brainyquote.com/link/quotebr.rss",
    "https://feeds.feedburner.com/quotationspage/qotd",
]

FALLBACK_TRACKS = [
    {
        "title": "SoundHelix Song 1",
        "artist": "SoundHelix",
        "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
    },
    {
        "title": "SoundHelix Song 2",
        "artist": "SoundHelix",
        "url": "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
    },
    {
        "title": "Sample Audio",
        "artist": "Pixabay",
        "url": "https://cdn.pixabay.com/download/audio/2022/03/15/audio_c8f2f7f2f5.mp3",
    },
]

GENRE_KEYWORDS = {
    "Pop": "pop music vibes",
    "Rock": "rock concert vibes",
    "Rap": "hip hop street vibes",
    "Electronic": "electronic neon vibes",
}


@dataclass
class Track:
    title: str
    artist: str
    url: str


@dataclass
class PostPackage:
    genre: str
    quote: str
    image_bytes: bytes
    image_name: str
    tracks: list[Track]


class FlowState(StatesGroup):
    choosing_genre = State()
    choosing_poll = State()
    confirm_publish = State()


router = Router()

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Новий пост")],
        [KeyboardButton(text="Опитування")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
)

GENRE_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=g)] for g in GENRES] + [[KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

PUBLISH_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Опублікувати")], [KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

POLL_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=name)] for name in POLL_OPTIONS] + [[KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)


async def http_get_text(session: aiohttp.ClientSession, url: str) -> str:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    async with session.get(url, headers=headers) as response:
        response.raise_for_status()
        return await response.text()


async def download_bytes(session: aiohttp.ClientSession, url: str, max_bytes: int) -> bytes | None:
    headers = {"User-Agent": USER_AGENT, "Referer": url}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                return None
            content = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                content.extend(chunk)
                if len(content) > max_bytes:
                    return None
            return bytes(content)
    except Exception:
        logger.exception("Download failed: %s", url)
        return None


def dedupe_tracks(items: list[Track]) -> list[Track]:
    seen: set[str] = set()
    result: list[Track] = []
    for item in items:
        key = item.url.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def parse_title_artist(raw: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", " ", raw).strip(" -\n\t")
    if " - " in clean:
        artist, title = clean.split(" - ", 1)
        return title.strip(), artist.strip()
    return clean[:100] or "Unknown track", "Unknown artist"


async def search_z3fm(session: aiohttp.ClientSession, genre: str) -> list[Track]:
    query = quote_plus(f"{genre} music")
    url = f"https://z3.fm/search?keywords={query}"
    tracks: list[Track] = []
    try:
        html = await http_get_text(session, url)
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.select("a[href$='.mp3'], a[data-url]"):
            track_url = tag.get("href") or tag.get("data-url")
            if not track_url:
                continue
            full_url = urljoin("https://z3.fm", track_url)
            text = tag.get_text(" ", strip=True) or tag.get("title", "")
            title, artist = parse_title_artist(text)
            tracks.append(Track(title=title, artist=artist, url=full_url))
        if not tracks:
            for mp3 in set(re.findall(r"https?://[^\"']+\.mp3[^\"']*", html)):
                tracks.append(Track(title="Unknown track", artist="Unknown artist", url=mp3))
    except Exception:
        logger.exception("z3.fm search failed")
    return dedupe_tracks(tracks)


async def search_sefon(session: aiohttp.ClientSession) -> list[Track]:
    url = "https://sefon.pro/"
    tracks: list[Track] = []
    try:
        html = await http_get_text(session, url)
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("div, li, article"):
            text = row.get_text(" ", strip=True)
            link = row.find("a", href=True)
            if link and ".mp3" in link["href"]:
                full_url = urljoin(url, link["href"])
                title, artist = parse_title_artist(text)
                tracks.append(Track(title=title, artist=artist, url=full_url))
        if not tracks:
            for mp3 in set(re.findall(r"https?://[^\"']+\.mp3[^\"']*", html)):
                tracks.append(Track(title="Unknown track", artist="Unknown artist", url=mp3))
    except Exception:
        logger.exception("sefon.pro search failed")
    return dedupe_tracks(tracks)


async def search_muzcore(session: aiohttp.ClientSession) -> list[Track]:
    url = "https://muzcore.online/top-100.html"
    tracks: list[Track] = []
    try:
        html = await http_get_text(session, url)
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("tr, li, div"):
            text = row.get_text(" ", strip=True)
            link = row.find("a", href=True)
            if link and ".mp3" in link["href"]:
                full_url = urljoin(url, link["href"])
                title, artist = parse_title_artist(text)
                tracks.append(Track(title=title, artist=artist, url=full_url))
        if not tracks:
            for mp3 in set(re.findall(r"https?://[^\"']+\.mp3[^\"']*", html)):
                tracks.append(Track(title="Unknown track", artist="Unknown artist", url=mp3))
    except Exception:
        logger.exception("muzcore search failed")
    return dedupe_tracks(tracks)


async def collect_tracks(session: aiohttp.ClientSession, genre: str) -> list[Track]:
    aggregated: list[Track] = []

    for provider in (
        lambda: search_z3fm(session, genre),
        lambda: search_sefon(session),
        lambda: search_muzcore(session),
    ):
        if len(aggregated) >= 2:
            break
        try:
            found = await provider()
            aggregated.extend(found)
            aggregated = dedupe_tracks(aggregated)
        except Exception:
            logger.exception("Provider failed")

    if len(aggregated) < 2:
        aggregated.extend(Track(**item) for item in FALLBACK_TRACKS)
        aggregated = dedupe_tracks(aggregated)

    if not aggregated:
        aggregated = [Track(**FALLBACK_TRACKS[0])]

    return aggregated[:2] if len(aggregated) >= 2 else aggregated[:1]


async def get_unsplash_image(session: aiohttp.ClientSession, genre: str) -> bytes:
    query = GENRE_KEYWORDS.get(genre, genre)
    url = "https://api.unsplash.com/photos/random"
    params = {"query": query, "orientation": "landscape", "content_filter": "high"}
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}", "Accept-Version": "v1"}

    try:
        async with session.get(url, params=params, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json()
        img_url = payload.get("urls", {}).get("regular")
        if img_url:
            image = await download_bytes(session, img_url, MAX_IMAGE_BYTES)
            if image:
                return image
    except Exception:
        logger.exception("Unsplash fetch failed")

    fallback = "https://images.unsplash.com/photo-1470225620780-dba8ba36b745?fit=crop&w=1400&q=80"
    image = await download_bytes(session, fallback, MAX_IMAGE_BYTES)
    if image:
        return image
    raise RuntimeError("Image download failed")


async def get_random_quote(session: aiohttp.ClientSession) -> str:
    entries: list[str] = []
    for feed_url in QUOTE_FEEDS:
        try:
            content = await http_get_text(session, feed_url)
            feed = feedparser.parse(content)
            for entry in feed.entries[:20]:
                text = re.sub(r"<[^>]+>", "", entry.get("summary", "") or entry.get("title", ""))
                text = re.sub(r"\s+", " ", text).strip()
                if len(text.split()) < 8:
                    continue
                parts = [p.strip() for p in re.split(r"[.!?]", text) if p.strip()]
                if not parts:
                    continue
                lines = parts[: min(4, max(2, len(parts)))]
                quote = "\n".join(lines[:4])
                entries.append(quote)
        except Exception:
            logger.exception("Quote feed failed: %s", feed_url)

    if not entries:
        return "Music gives a soul to the universe\nWings to the mind\nFlight to the imagination"
    return random.choice(entries)


async def build_package(session: aiohttp.ClientSession, genre: str, bot: Bot) -> PostPackage:
    quote_task = asyncio.create_task(get_random_quote(session))
    image_task = asyncio.create_task(get_unsplash_image(session, genre))
    tracks_task = asyncio.create_task(collect_tracks(session, genre))

    quote, image_bytes, tracks = await asyncio.gather(quote_task, image_task, tracks_task)

    if len(tracks) < 2:
        try:
            await bot.send_message(ADMIN_ID, "Не вдалося знайти 2 треки. Опубліковано 1.")
        except TelegramBadRequest:
            logger.warning("Could not notify admin")

    return PostPackage(
        genre=genre,
        quote=quote,
        image_bytes=image_bytes,
        image_name=f"{genre.lower()}_vibe.jpg",
        tracks=tracks,
    )


async def publish_package(bot: Bot, session: aiohttp.ClientSession, package: PostPackage, poll_name: str | None) -> None:
    photo = BufferedInputFile(package.image_bytes, filename=package.image_name)
    await bot.send_photo(chat_id=CHANNEL_ID, photo=photo, caption=package.quote)

    for idx, track in enumerate(package.tracks, start=1):
        audio_bytes = await download_bytes(session, track.url, MAX_AUDIO_BYTES)
        if not audio_bytes:
            logger.warning("Skip audio: %s", track.url)
            continue
        audio = BufferedInputFile(audio_bytes, filename=f"track_{idx}.mp3")
        await bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=audio,
            title=track.title,
            performer=track.artist,
        )

    if poll_name and poll_name in POLL_OPTIONS:
        options = POLL_OPTIONS[poll_name]
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=poll_name,
            options=options,
            is_anonymous=False,
        )


async def get_session(state: FSMContext) -> dict[str, Any]:
    data = await state.get_data()
    return data


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Головне меню", reply_markup=MAIN_MENU)


@router.message(F.text == "Скасувати")
async def cancel_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Скасовано. Повертаю в меню.", reply_markup=MAIN_MENU)


@router.message(F.text == "Новий пост")
async def new_post(message: Message, state: FSMContext) -> None:
    await state.set_state(FlowState.choosing_genre)
    await message.answer("Обери жанр:", reply_markup=GENRE_MENU)


@router.message(FlowState.choosing_genre, F.text.in_(GENRES))
async def choose_genre(message: Message, state: FSMContext, bot: Bot) -> None:
    genre = message.text
    await message.answer("Формую пост, зачекай кілька секунд...", reply_markup=ReplyKeyboardRemove())

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        package = await build_package(session, genre, bot)

    await state.update_data(package=package, genre=genre)
    await state.set_state(FlowState.choosing_poll)
    await message.answer("Пост готовий. Тепер обери опитування:", reply_markup=POLL_MENU)


@router.message(F.text == "Опитування")
async def poll_entry(message: Message, state: FSMContext) -> None:
    await state.set_state(FlowState.choosing_poll)
    await message.answer("Обери опитування:", reply_markup=POLL_MENU)


@router.message(FlowState.choosing_poll, F.text.in_(list(POLL_OPTIONS.keys())))
async def choose_poll(message: Message, state: FSMContext) -> None:
    await state.update_data(selected_poll=message.text)
    await state.set_state(FlowState.confirm_publish)
    await message.answer("Натисни 'Опублікувати' або 'Скасувати'.", reply_markup=PUBLISH_MENU)


@router.message(FlowState.confirm_publish, F.text == "Опублікувати")
async def publish_now(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await get_session(state)
    poll_name = data.get("selected_poll")
    package: PostPackage | None = data.get("package")
    genre = data.get("genre", "Pop")

    await message.answer("Публікую...", reply_markup=ReplyKeyboardRemove())

    async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
        if package is None:
            package = await build_package(session, genre, bot)
        await publish_package(bot, session, package, poll_name)

    await state.clear()
    await message.answer("Опубліковано ✅", reply_markup=MAIN_MENU)


@router.message()
async def fallback_handler(message: Message) -> None:
    await message.answer("Обери дію з меню.", reply_markup=MAIN_MENU)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
