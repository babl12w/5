import asyncio
import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urljoin

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.enums import PollType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001234567890"))

GENRES = ["Pop", "Rock", "Rap", "Electronic"]
POLL_OPTIONS = {
    "Опитування 1": ("Який жанр сьогодні?", ["Pop", "Rock", "Rap", "Electronic"]),
    "Опитування 2": ("Коли слухаєш музику найчастіше?", ["Зранку", "Вдень", "Увечері", "Вночі"]),
    "Опитування 3": ("Що сьогодні вмикаємо?", ["Хіти", "Новинки", "Ретро", "Мікс"]),
}
RSS_SOURCES = [
    "https://www.brainyquote.com/link/quotebr.rss",
    "https://www.inc.com/rss",
    "https://feeds.feedburner.com/quotationspage/qotd",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("music_bot")


@dataclass
class Track:
    title: str
    artist: str
    source_url: str
    file_path: Path


class UserStates(StatesGroup):
    choosing_genre = State()
    choosing_poll = State()
    confirm_publish = State()


storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(BOT_TOKEN)


main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Новий пост"), KeyboardButton(text="Опитування")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
)

genre_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=g)] for g in GENRES] + [[KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

polls_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=p)] for p in POLL_OPTIONS.keys()] + [[KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

publish_menu = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Опублікувати"), KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, timeout=20) as response:
            if response.status == 200:
                return await response.text()
    except Exception as e:
        logger.warning("Fetch text failed for %s: %s", url, e)
    return ""


def normalize_title(raw: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", raw).strip(" -_\n\t")
    if " - " in cleaned:
        artist, title = cleaned.split(" - ", 1)
        return title.strip()[:100], artist.strip()[:100]
    return cleaned[:100] or "Unknown Track", "Unknown Artist"


def collect_mp3_candidates(html: str, base_url: str) -> list[tuple[str, str]]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[str, str]] = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".mp3" in href.lower():
            full_url = urljoin(base_url, href)
            text = a.get_text(" ", strip=True) or Path(href).stem
            title, artist = normalize_title(text)
            key = (full_url, title, artist)
            if key not in seen:
                seen.add(key)
                candidates.append((full_url, f"{artist} - {title}"))

    script_matches = re.findall(r"https?://[^\"'\s]+\.mp3[^\"'\s]*", html, flags=re.IGNORECASE)
    for link in script_matches:
        if link not in [c[0] for c in candidates]:
            stem = Path(link.split("?")[0]).stem.replace("_", " ").replace("-", " ")
            title, artist = normalize_title(stem)
            candidates.append((link, f"{artist} - {title}"))

    return candidates


async def download_track(session: aiohttp.ClientSession, url: str, title_hint: str) -> Optional[Track]:
    try:
        async with session.get(url, timeout=30) as response:
            if response.status != 200:
                return None
            content = await response.read()
            if len(content) < 10240:
                return None
            temp_dir = Path(tempfile.gettempdir()) / "music_bot_tracks"
            temp_dir.mkdir(parents=True, exist_ok=True)
            filename = re.sub(r"[^a-zA-Z0-9а-яА-ЯіїєІЇЄ_-]+", "_", title_hint)[:70] or "track"
            file_path = temp_dir / f"{filename}_{random.randint(1000,9999)}.mp3"
            file_path.write_bytes(content)
            title, artist = normalize_title(title_hint)
            return Track(title=title, artist=artist, source_url=url, file_path=file_path)
    except Exception as e:
        logger.warning("Download track failed for %s: %s", url, e)
        return None


async def fetch_tracks_for_genre(genre: str) -> list[Track]:
    queries = [
        f"https://z3.fm/search?keywords={quote_plus(genre)}",
        f"https://sefon.pro/search/{quote_plus(genre)}",
        "https://muzcore.online/top-100.html",
        "https://file-examples.com/index.php/sample-audio-files/",
    ]
    fallback_mp3 = [
        ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3", "SoundHelix - Song 1"),
        ("https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3", "SoundHelix - Song 2"),
    ]
    downloaded: list[Track] = []

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        for idx, search_url in enumerate(queries, start=1):
            if len(downloaded) >= 2:
                break
            html = await fetch_text(session, search_url)
            candidates = collect_mp3_candidates(html, search_url)
            if idx < 4 and len(candidates) < 2:
                continue
            if idx == 4 and not candidates:
                candidates = fallback_mp3
            for mp3_url, title_hint in candidates:
                if len(downloaded) >= 2:
                    break
                if any(t.source_url == mp3_url for t in downloaded):
                    continue
                track = await download_track(session, mp3_url, title_hint)
                if track:
                    downloaded.append(track)

        if len(downloaded) < 2:
            for mp3_url, title_hint in fallback_mp3:
                if len(downloaded) >= 2:
                    break
                if any(t.source_url == mp3_url for t in downloaded):
                    continue
                track = await download_track(session, mp3_url, title_hint)
                if track:
                    downloaded.append(track)
    return downloaded


async def fetch_unsplash_photo(genre: str) -> Optional[Path]:
    if not UNSPLASH_ACCESS_KEY:
        return None
    url = "https://api.unsplash.com/photos/random"
    params = {
        "query": f"{genre} music vibe",
        "orientation": "portrait",
        "content_filter": "high",
        "client_id": UNSPLASH_ACCESS_KEY,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=25) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                image_url = data.get("urls", {}).get("regular") or data.get("urls", {}).get("full")
                if not image_url:
                    return None
            async with session.get(image_url, timeout=25) as img_response:
                if img_response.status != 200:
                    return None
                content = await img_response.read()
                temp_dir = Path(tempfile.gettempdir()) / "music_bot_images"
                temp_dir.mkdir(parents=True, exist_ok=True)
                file_path = temp_dir / f"{genre.lower()}_{random.randint(1000,9999)}.jpg"
                file_path.write_bytes(content)
                return file_path
    except Exception as e:
        logger.warning("Unsplash fetch failed: %s", e)
        return None


def shorten_lines(text: str) -> str:
    fragments = [x.strip() for x in re.split(r"[\n\.\!\?]+", text) if x.strip()]
    if not fragments:
        return "Музика — це мова емоцій, яка не потребує перекладу."
    max_lines = random.randint(2, 4)
    chosen = fragments[:max_lines]
    return "\n".join(chosen)


async def fetch_quote() -> str:
    random.shuffle(RSS_SOURCES)
    loop = asyncio.get_running_loop()
    for source in RSS_SOURCES:
        try:
            feed = await loop.run_in_executor(None, lambda: feedparser.parse(source))
            if not feed.entries:
                continue
            entry = random.choice(feed.entries)
            raw_text = f"{entry.get('title', '')}. {entry.get('summary', '')}"
            raw_text = BeautifulSoup(raw_text, "html.parser").get_text(" ", strip=True)
            quote = shorten_lines(raw_text)
            if len(quote) >= 30:
                return quote
        except Exception as e:
            logger.warning("Quote fetch failed for %s: %s", source, e)
    return "Музика говорить там, де слова закінчуються.\nСлухай серцем."


def build_caption(quote: str, tracks: list[Track]) -> str:
    lines = [f"📝 {quote}", "", "🎵 Треки дня:"]
    for idx, track in enumerate(tracks, start=1):
        lines.append(f"{idx}. {track.artist} — {track.title}")
    return "\n".join(lines)


async def cleanup_files(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path and path.exists():
                path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning("Cleanup failed for %s: %s", path, e)


@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Головне меню", reply_markup=main_menu)


@dp.message(F.text == "Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    files_to_remove: list[Path] = []
    for key in ["photo", "tracks"]:
        item = data.get(key)
        if isinstance(item, str):
            files_to_remove.append(Path(item))
        if isinstance(item, list):
            files_to_remove.extend(Path(x.get("file_path")) for x in item if x.get("file_path"))
    await cleanup_files(files_to_remove)
    await state.clear()
    await message.answer("Скасовано. Головне меню", reply_markup=main_menu)


@dp.message(F.text == "Новий пост")
async def new_post_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(UserStates.choosing_genre)
    await message.answer("Оберіть жанр", reply_markup=genre_menu)


@dp.message(UserStates.choosing_genre, F.text.in_(GENRES))
async def genre_handler(message: Message, state: FSMContext) -> None:
    genre = message.text
    await message.answer("Готую пост, зачекай...", reply_markup=main_menu)
    tracks = await fetch_tracks_for_genre(genre)
    quote = await fetch_quote()
    photo_path = await fetch_unsplash_photo(genre)

    serialized_tracks = [
        {
            "title": t.title,
            "artist": t.artist,
            "source_url": t.source_url,
            "file_path": str(t.file_path),
        }
        for t in tracks
    ]

    await state.update_data(
        genre=genre,
        quote=quote,
        tracks=serialized_tracks,
        photo=str(photo_path) if photo_path else "",
        poll_title="",
    )
    await state.set_state(UserStates.confirm_publish)
    await message.answer("Пост підготовлено. Натисни «Опублікувати» або «Скасувати».", reply_markup=publish_menu)


@dp.message(F.text == "Опитування")
async def poll_select_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(UserStates.choosing_poll)
    await message.answer("Обери опитування", reply_markup=polls_menu)


@dp.message(UserStates.choosing_poll, F.text.in_(list(POLL_OPTIONS.keys())))
async def poll_chosen_handler(message: Message, state: FSMContext) -> None:
    await state.update_data(poll_title=message.text)
    await state.set_state(UserStates.confirm_publish)
    await message.answer("Опитування вибрано. Натисни «Опублікувати» або «Скасувати».", reply_markup=publish_menu)


@dp.message(F.text == "Опублікувати")
async def publish_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    tracks_data = data.get("tracks", [])
    quote = data.get("quote", "")
    poll_title = data.get("poll_title", "")
    photo = data.get("photo", "")

    if not tracks_data and not poll_title:
        await message.answer("Спочатку створи пост або обери опитування.", reply_markup=main_menu)
        await state.clear()
        return

    tracks = [Track(t["title"], t["artist"], t["source_url"], Path(t["file_path"])) for t in tracks_data]
    sent_files: list[Path] = []

    try:
        if tracks:
            caption = build_caption(quote, tracks)
            if photo and Path(photo).exists():
                await bot.send_photo(CHANNEL_ID, photo=FSInputFile(photo), caption=caption)
                sent_files.append(Path(photo))
            else:
                await bot.send_message(CHANNEL_ID, caption)

            for track in tracks:
                if track.file_path.exists():
                    await bot.send_audio(
                        CHANNEL_ID,
                        audio=FSInputFile(track.file_path),
                        title=track.title,
                        performer=track.artist,
                    )
                    sent_files.append(track.file_path)

            if len(tracks) == 1:
                await bot.send_message(ADMIN_ID, "Не вдалося знайти 2 треки, опубліковано 1.")

        if poll_title:
            q, options = POLL_OPTIONS[poll_title]
            await bot.send_poll(CHANNEL_ID, question=q, options=options, is_anonymous=False, type=PollType.REGULAR)

        await message.answer("Опубліковано.", reply_markup=main_menu)
    except Exception as e:
        logger.exception("Publish failed: %s", e)
        await message.answer("Помилка під час публікації.", reply_markup=main_menu)
    finally:
        await cleanup_files(sent_files)
        await state.clear()


@dp.message()
async def fallback_handler(message: Message) -> None:
    await message.answer("Оберіть дію з меню", reply_markup=main_menu)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
