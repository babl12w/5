import asyncio
import logging
import os
import random
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, Message, PollAnswer
from aiogram.utils.keyboard import ReplyKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

if not BOT_TOKEN or not UNSPLASH_ACCESS_KEY or not ADMIN_ID or not CHANNEL_ID:
    raise RuntimeError("Missing required environment variables: BOT_TOKEN, UNSPLASH_ACCESS_KEY, ADMIN_ID, CHANNEL_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music_channel_bot")

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

POLL_TEMPLATES: dict[str, dict[str, Any]] = {
    "Опитування 1": {
        "question": "Який жанр ви хочете почути наступним?",
        "options": ["Pop", "Rock", "Hip-Hop", "Electronic"],
    },
    "Опитування 2": {
        "question": "Коли вам зручно отримувати музичні пости?",
        "options": ["Ранок", "День", "Вечір", "Ніч"],
    },
    "Опитування 3": {
        "question": "Що важливіше у треку?",
        "options": ["Біт", "Вокал", "Текст", "Атмосфера"],
    },
}

GENRES = ["Pop", "Rock", "Hip-Hop", "Electronic"]

GENRE_FEEDS: dict[str, list[str]] = {
    "Pop": [
        "https://freemusicarchive.org/genre/Pop.rss",
        "https://ccmixter.org/api/query?f=rss&tags=pop&limit=30",
    ],
    "Rock": [
        "https://freemusicarchive.org/genre/Rock.rss",
        "https://ccmixter.org/api/query?f=rss&tags=rock&limit=30",
    ],
    "Hip-Hop": [
        "https://freemusicarchive.org/genre/Hip-Hop_Rap.rss",
        "https://ccmixter.org/api/query?f=rss&tags=hiphop&limit=30",
    ],
    "Electronic": [
        "https://freemusicarchive.org/genre/Electronic.rss",
        "https://ccmixter.org/api/query?f=rss&tags=electronic&limit=30",
    ],
}

GENERAL_MUSIC_FEEDS = [
    "https://freemusicarchive.org/recent.rss",
    "https://ccmixter.org/api/query?f=rss&limit=40",
]

POPULAR_MUSIC_FEEDS = [
    "https://ccmixter.org/api/query?f=rss&sort=rank&limit=40",
    "https://freemusicarchive.org/curators/all.rss",
]

QUOTE_RSS_SOURCES = [
    "https://www.brainyquote.com/link/quotebr.rss",
    "https://www.brainyquote.com/link/quotes/motivational.rss",
]


class BotStates(StatesGroup):
    waiting_genre = State()
    preview_post = State()
    waiting_poll_choice = State()
    preview_poll = State()


@dataclass
class TrackItem:
    title: str
    artist: str
    source_url: str
    local_path: str


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


def kb_main():
    kb = ReplyKeyboardBuilder()
    kb.button(text="🆕 Новий пост")
    kb.button(text="📊 Опитування")
    kb.button(text="❌ Скасувати")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def kb_genres():
    kb = ReplyKeyboardBuilder()
    for genre in GENRES:
        kb.button(text=genre)
    kb.button(text="❌ Скасувати")
    kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def kb_publish_cancel():
    kb = ReplyKeyboardBuilder()
    kb.button(text="✅ Опублікувати")
    kb.button(text="❌ Скасувати")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def kb_polls():
    kb = ReplyKeyboardBuilder()
    for poll_name in POLL_TEMPLATES:
        kb.button(text=poll_name)
    kb.button(text="❌ Скасувати")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True, is_persistent=True)


def sanitize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


async def fetch_feed_entries(session: aiohttp.ClientSession, url: str) -> list[Any]:
    try:
        async with session.get(url, timeout=20) as response:
            if response.status != 200:
                logger.warning("Feed response %s for %s", response.status, url)
                return []
            raw = await response.read()
        feed = feedparser.parse(raw)
        if getattr(feed, "bozo", False):
            logger.warning("Bozo feed for %s: %s", url, getattr(feed, "bozo_exception", "unknown"))
        return list(getattr(feed, "entries", []))
    except Exception as error:
        logger.exception("Failed to fetch feed %s: %s", url, error)
        return []


def pick_audio_link(entry: Any) -> str | None:
    links = entry.get("links", []) if isinstance(entry, dict) else getattr(entry, "links", [])
    for link in links:
        href = link.get("href", "")
        mime = (link.get("type") or "").lower()
        if not href:
            continue
        if "audio" in mime and ".mp3" in href.lower():
            return href
    for link in links:
        href = link.get("href", "")
        if href.lower().endswith(".mp3"):
            return href
    for enclosure in entry.get("enclosures", []) if isinstance(entry, dict) else getattr(entry, "enclosures", []):
        href = enclosure.get("href", "")
        if href.lower().endswith(".mp3"):
            return href
    return None


async def download_file(session: aiohttp.ClientSession, url: str, suffix: str) -> str | None:
    try:
        async with session.get(url, timeout=40) as response:
            if response.status != 200:
                logger.warning("Download response %s for %s", response.status, url)
                return None
            data = await response.read()
        if not data:
            return None
        tmp_dir = Path(tempfile.gettempdir()) / "music_tg_bot"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        file_path = tmp_dir / f"{uuid.uuid4().hex}{suffix}"
        file_path.write_bytes(data)
        return str(file_path)
    except Exception as error:
        logger.exception("Failed to download file %s: %s", url, error)
        return None


async def collect_tracks_from_feeds(
    session: aiohttp.ClientSession,
    feed_urls: list[str],
    min_tracks: int,
) -> list[TrackItem]:
    tracks: list[TrackItem] = []
    seen_urls: set[str] = set()
    for feed_url in feed_urls:
        entries = await fetch_feed_entries(session, feed_url)
        random.shuffle(entries)
        for entry in entries:
            audio_url = pick_audio_link(entry)
            if not audio_url or audio_url in seen_urls:
                continue

            title_raw = entry.get("title", "Unknown track")
            author_raw = entry.get("author", "Unknown artist")
            title = sanitize_text(title_raw)
            artist = sanitize_text(author_raw)

            local_path = await download_file(session, audio_url, ".mp3")
            if not local_path:
                continue

            tracks.append(TrackItem(title=title, artist=artist, source_url=audio_url, local_path=local_path))
            seen_urls.add(audio_url)

            if len(tracks) >= min_tracks:
                return tracks
    return tracks


async def fetch_tracks_with_fallback(genre: str) -> tuple[list[TrackItem], str | None]:
    fallback_message = None
    async with aiohttp.ClientSession() as session:
        tracks = await collect_tracks_from_feeds(session, GENRE_FEEDS.get(genre, []), min_tracks=2)
        if len(tracks) >= 2:
            return tracks[:2], None

        tracks = await collect_tracks_from_feeds(session, GENERAL_MUSIC_FEEDS, min_tracks=2)
        if len(tracks) >= 2:
            fallback_message = "ℹ️ Не вистачило треків у вибраному жанрі. Використано загальний пошук."
            return tracks[:2], fallback_message

        tracks = await collect_tracks_from_feeds(session, POPULAR_MUSIC_FEEDS, min_tracks=2)
        if len(tracks) >= 2:
            fallback_message = "ℹ️ Використано fallback: random popular."
            return tracks[:2], fallback_message

        tracks = await collect_tracks_from_feeds(
            session,
            GENRE_FEEDS.get(genre, []) + GENERAL_MUSIC_FEEDS + POPULAR_MUSIC_FEEDS,
            min_tracks=1,
        )
        if tracks:
            fallback_message = "ℹ️ Знайдено лише 1 трек. Другий трек буде дубльовано для формату посту."
            tracks = [tracks[0], tracks[0]]
            return tracks, fallback_message

    raise RuntimeError("Не вдалося знайти жодного треку.")


async def fetch_unsplash_photo(genre: str) -> str:
    query = f"{genre} music vibe aesthetic mood"
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {
        "query": query,
        "orientation": "portrait",
        "content_filter": "high",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.unsplash.com/photos/random", headers=headers, params=params, timeout=20) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Unsplash API error: {resp.status}")
            payload = await resp.json()
        image_url = payload.get("urls", {}).get("regular")
        if not image_url:
            raise RuntimeError("Unsplash returned no image URL")
        image_path = await download_file(session, image_url, ".jpg")
        if not image_path:
            raise RuntimeError("Failed to download Unsplash photo")
        return image_path


async def fetch_quote() -> str:
    async with aiohttp.ClientSession() as session:
        for rss_url in QUOTE_RSS_SOURCES:
            entries = await fetch_feed_entries(session, rss_url)
            for entry in entries:
                title = sanitize_text(entry.get("title", ""))
                summary = sanitize_text(re.sub(r"<[^>]+>", " ", entry.get("summary", "")))
                if title and summary:
                    quote = f"“{title}”\n— {summary}"
                elif title:
                    quote = f"“{title}”"
                else:
                    continue

                lines = [line for line in quote.split("\n") if line.strip()]
                if 2 <= len(lines) <= 4:
                    return "\n".join(lines)
                if len(lines) < 2:
                    return quote + "\n🎵"

        try:
            async with session.get("https://zenquotes.io/api/random", timeout=20) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and data:
                        q = sanitize_text(data[0].get("q", ""))
                        a = sanitize_text(data[0].get("a", ""))
                        if q:
                            return f"“{q}”\n— {a or 'Unknown'}"
        except Exception as error:
            logger.warning("Quote API fallback failed: %s", error)

    return "“Music is the shorthand of emotion.”\n— Leo Tolstoy"


def cleanup_paths(paths: list[str]) -> None:
    for path in paths:
        try:
            if path and Path(path).exists():
                Path(path).unlink(missing_ok=True)
        except Exception as error:
            logger.warning("Failed to cleanup %s: %s", path, error)


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("⛔ Доступ заборонено.")
        return
    await state.clear()
    await message.answer("Головне меню:", reply_markup=kb_main())


@router.message(F.text == "❌ Скасувати")
async def handle_cancel(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    data = await state.get_data()
    paths_to_remove = data.get("temp_paths", [])
    cleanup_paths(paths_to_remove)
    await state.clear()
    await message.answer("Дію скасовано. Головне меню:", reply_markup=kb_main())


@router.message(F.text == "🆕 Новий пост")
async def start_new_post(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(BotStates.waiting_genre)
    await message.answer("Оберіть жанр:", reply_markup=kb_genres())


@router.message(BotStates.waiting_genre)
async def process_genre_choice(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message):
        return
    genre = (message.text or "").strip()
    if genre not in GENRES:
        await message.answer("Оберіть жанр кнопкою.", reply_markup=kb_genres())
        return

    wait_msg = await message.answer("🔎 Шукаю контент, зачекайте...")
    temp_paths: list[str] = []

    try:
        tracks, fallback_note = await fetch_tracks_with_fallback(genre)
        quote = await fetch_quote()
        photo_path = await fetch_unsplash_photo(genre)

        temp_paths.extend([photo_path, tracks[0].local_path, tracks[1].local_path])

        if fallback_note:
            await bot.send_message(ADMIN_ID, fallback_note)

        caption = quote

        await bot.send_photo(chat_id=ADMIN_ID, photo=FSInputFile(photo_path), caption=caption)
        await bot.send_audio(
            chat_id=ADMIN_ID,
            audio=FSInputFile(tracks[0].local_path),
            title=tracks[0].title,
            performer=tracks[0].artist,
        )
        await bot.send_audio(
            chat_id=ADMIN_ID,
            audio=FSInputFile(tracks[1].local_path),
            title=tracks[1].title,
            performer=tracks[1].artist,
        )

        await state.update_data(
            pending_post={
                "caption": caption,
                "photo_path": photo_path,
                "tracks": [
                    {
                        "title": tracks[0].title,
                        "artist": tracks[0].artist,
                        "path": tracks[0].local_path,
                    },
                    {
                        "title": tracks[1].title,
                        "artist": tracks[1].artist,
                        "path": tracks[1].local_path,
                    },
                ],
            },
            temp_paths=temp_paths,
        )

        await state.set_state(BotStates.preview_post)
        await message.answer("Превʼю готове. Опублікувати в канал?", reply_markup=kb_publish_cancel())

    except Exception as error:
        logger.exception("Error while generating post content: %s", error)
        cleanup_paths(temp_paths)
        await state.clear()
        await message.answer(
            "⚠️ Не вдалося згенерувати пост. Спробуйте ще раз.",
            reply_markup=kb_main(),
        )
    finally:
        try:
            await wait_msg.delete()
        except TelegramBadRequest:
            pass


@router.message(F.text == "📊 Опитування")
async def start_poll_flow(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(BotStates.waiting_poll_choice)
    await message.answer("Оберіть опитування:", reply_markup=kb_polls())


@router.message(BotStates.waiting_poll_choice)
async def choose_poll(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message):
        return

    poll_key = (message.text or "").strip()
    if poll_key not in POLL_TEMPLATES:
        await message.answer("Оберіть опитування кнопкою.", reply_markup=kb_polls())
        return

    template = POLL_TEMPLATES[poll_key]
    await bot.send_poll(
        chat_id=ADMIN_ID,
        question=template["question"],
        options=template["options"],
        is_anonymous=False,
        allows_multiple_answers=False,
    )

    await state.update_data(
        pending_poll={
            "question": template["question"],
            "options": template["options"],
        }
    )
    await state.set_state(BotStates.preview_poll)
    await message.answer("Опитування готове. Опублікувати в канал?", reply_markup=kb_publish_cancel())


@router.message(BotStates.preview_post, F.text == "✅ Опублікувати")
async def publish_post(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message):
        return
    data = await state.get_data()
    pending_post = data.get("pending_post")
    if not pending_post:
        await message.answer("Немає посту для публікації.", reply_markup=kb_main())
        await state.clear()
        return

    try:
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=FSInputFile(pending_post["photo_path"]),
            caption=pending_post["caption"],
        )
        for track in pending_post["tracks"]:
            await bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=FSInputFile(track["path"]),
                title=track["title"],
                performer=track["artist"],
            )

        await message.answer("✅ Опубліковано в канал.", reply_markup=kb_main())
    except Exception as error:
        logger.exception("Failed to publish post: %s", error)
        await message.answer("❌ Помилка під час публікації.", reply_markup=kb_main())
    finally:
        cleanup_paths(data.get("temp_paths", []))
        await state.clear()


@router.message(BotStates.preview_poll, F.text == "✅ Опублікувати")
async def publish_poll(message: Message, state: FSMContext, bot: Bot) -> None:
    if not is_admin(message):
        return

    data = await state.get_data()
    pending_poll = data.get("pending_poll")
    if not pending_poll:
        await message.answer("Немає опитування для публікації.", reply_markup=kb_main())
        await state.clear()
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=pending_poll["question"],
            options=pending_poll["options"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        await message.answer("✅ Опитування опубліковано.", reply_markup=kb_main())
    except Exception as error:
        logger.exception("Failed to publish poll: %s", error)
        await message.answer("❌ Помилка під час публікації опитування.", reply_markup=kb_main())
    finally:
        await state.clear()


@router.poll_answer()
async def on_poll_answer(_: PollAnswer) -> None:
    return


@router.message()
async def fallback_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    current_state = await state.get_state()
    if current_state == BotStates.waiting_genre.state:
        await message.answer("Оберіть жанр кнопкою.", reply_markup=kb_genres())
        return
    if current_state == BotStates.waiting_poll_choice.state:
        await message.answer("Оберіть опитування кнопкою.", reply_markup=kb_polls())
        return
    if current_state in {BotStates.preview_post.state, BotStates.preview_poll.state}:
        await message.answer("Оберіть дію кнопкою.", reply_markup=kb_publish_cancel())
        return
    await message.answer("Оберіть дію з меню.", reply_markup=kb_main())


async def on_startup(bot: Bot) -> None:
    logger.info("Bot started successfully")
    try:
        await bot.send_message(ADMIN_ID, "✅ Бот запущено", reply_markup=kb_main())
    except Exception as error:
        logger.warning("Startup message failed: %s", error)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp.startup.register(on_startup)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception as error:
        logger.exception("Fatal polling error: %s", error)
        await asyncio.sleep(3)
        raise


if __name__ == "__main__":
    asyncio.run(main())
