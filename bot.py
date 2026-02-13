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
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

if not BOT_TOKEN or not UNSPLASH_ACCESS_KEY or not ADMIN_ID or not CHANNEL_ID:
    raise RuntimeError("Environment variables BOT_TOKEN, UNSPLASH_ACCESS_KEY, ADMIN_ID, CHANNEL_ID must be set")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music_channel_bot")

router = Router()


class PostStates(StatesGroup):
    choosing_genre = State()
    choosing_poll = State()


GENRES = ["Pop", "Rock", "Hip-Hop", "Electronic"]
POLL_TEMPLATES: list[dict[str, Any]] = [
    {
        "title": "Опитування 1",
        "question": "Який жанр сьогодні найбільше під настрій?",
        "options": ["Pop", "Rock", "Hip-Hop", "Electronic"],
    },
    {
        "title": "Опитування 2",
        "question": "Коли ти найчастіше слухаєш музику?",
        "options": ["Вранці", "Вдень", "Увечері", "Вночі"],
    },
    {
        "title": "Опитування 3",
        "question": "Що додати в наступний музичний пост?",
        "options": ["Більше новинок", "Локальних артистів", "Ретро-треків", "Електроніки"],
    },
]

MUSIC_FEEDS = [
    "https://freemusicarchive.org/genre/Pop.rss",
    "https://freemusicarchive.org/genre/Rock.rss",
    "https://freemusicarchive.org/genre/Hip-Hop.rss",
    "https://freemusicarchive.org/genre/Electronic.rss",
    "https://freemusicarchive.org/music.rss",
]

QUOTE_FEEDS = [
    "https://uk.wikiquote.org/w/api.php?action=query&list=random&rnnamespace=0&rnlimit=10&format=json",
    "https://api.allorigins.win/raw?url=https://www.brainyquote.com/quote_of_the_day",
]

STATIC_UA_QUOTES = [
    "Музика — це мова душі,\nяку неможливо підробити.\nВона лікує тишу.\nІ наповнює серце світлом.",
    "Коли слова закінчуються,\nпочинається мелодія.\nВона веде крізь темряву\nдо м’якого спокою.",
    "Справжній ритм життя\nчути не в годиннику,\nа в пульсі улюбленої пісні\nі теплій усмішці після неї.",
    "У кожній пісні є частинка дому,\nнавіть якщо ти далеко.\nМузика пам’ятає нас\nкраще за будь-які фото.",
]

FALLBACK_TRACKS = [
    {
        "artist": "Kalush Orchestra",
        "title": "Stefania (Instrumental Edit)",
        "url": "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Ketsa/Tides/Ketsa_-_01_-_Tides.mp3",
    },
    {
        "artist": "Daria Zawiałow",
        "title": "Polski Vibe Session",
        "url": "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Scott_Holmes_Music/Corporate__Motivational_Music/Scott_Holmes_Music_-_01_-_Our_Big_Adventure.mp3",
    },
    {
        "artist": "Miyagi & Andy Panda",
        "title": "Northern Lights (Remix)",
        "url": "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/BoxCat_Games/Nameless_the_Hackers_RPG_Soundtrack/BoxCat_Games_-_10_-_Battle_Boss.mp3",
    },
]

GENRE_HINTS = {
    "Pop": ["pop", "dance", "radio"],
    "Rock": ["rock", "indie", "alt"],
    "Hip-Hop": ["hip-hop", "rap", "trap"],
    "Electronic": ["electronic", "edm", "house"],
}

pending_posts: dict[int, dict[str, Any]] = {}
pending_polls: dict[int, dict[str, Any]] = {}


@dataclass
class Track:
    artist: str
    title: str
    url: str
    source: str


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🆕 Новий пост")],
        [KeyboardButton(text="📊 Опитування")],
        [KeyboardButton(text="❌ Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

GENRE_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Pop"), KeyboardButton(text="Rock")],
        [KeyboardButton(text="Hip-Hop"), KeyboardButton(text="Electronic")],
        [KeyboardButton(text="❌ Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POLL_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Опитування 1")],
        [KeyboardButton(text="Опитування 2")],
        [KeyboardButton(text="Опитування 3")],
        [KeyboardButton(text="❌ Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


async def safe_feed_parse(url: str) -> feedparser.FeedParserDict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: feedparser.parse(url))


def has_target_language(text: str) -> bool:
    lower = text.lower()
    if re.search(r"[іїєґ]", lower):
        return True
    if re.search(r"[а-яё]", lower):
        return True
    if re.search(r"[ąćęłńóśźż]", lower):
        return True
    return False


def parse_artist_title(raw_title: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", raw_title or "").strip()
    if not cleaned:
        return "Unknown Artist", "Unknown Track"
    for sep in (" - ", " — ", " – ", " | ", ": "):
        if sep in cleaned:
            left, right = cleaned.split(sep, 1)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    return "Unknown Artist", cleaned


def extract_audio_url(entry: Any) -> str:
    links = entry.get("links", []) if isinstance(entry, dict) else getattr(entry, "links", [])
    for link in links:
        href = link.get("href", "")
        mime = link.get("type", "")
        if href and ("audio" in mime or href.lower().endswith(".mp3")):
            return href
    enclosures = entry.get("enclosures", []) if isinstance(entry, dict) else getattr(entry, "enclosures", [])
    for enclosure in enclosures:
        href = enclosure.get("href", "")
        if href and href.lower().endswith(".mp3"):
            return href
    possible = entry.get("link", "") if isinstance(entry, dict) else getattr(entry, "link", "")
    if possible and possible.lower().endswith(".mp3"):
        return possible
    return ""


async def collect_tracks(genre: str) -> tuple[list[Track], str | None]:
    attempts = [
        ("genre", GENRE_HINTS.get(genre, [genre.lower()])),
        ("no_genre", []),
        ("random_popular", ["popular", "top", "hit", "music"]),
        ("at_least_one", []),
    ]

    for attempt_name, keywords in attempts:
        tracks: list[Track] = []
        seen_urls: set[str] = set()
        random.shuffle(MUSIC_FEEDS)

        for feed_url in MUSIC_FEEDS:
            try:
                parsed = await safe_feed_parse(feed_url)
            except Exception:
                logger.exception("Failed to parse RSS feed: %s", feed_url)
                continue

            entries = parsed.entries[:20]
            for entry in entries:
                title = entry.get("title", "")
                summary = (entry.get("summary", "") or "")
                combined = f"{title} {summary}".lower()

                if attempt_name == "genre" and keywords:
                    if not any(keyword in combined for keyword in keywords):
                        continue

                artist, track_title = parse_artist_title(title)
                if attempt_name != "at_least_one":
                    if not has_target_language(f"{artist} {track_title}"):
                        continue

                audio_url = extract_audio_url(entry)
                if not audio_url or audio_url in seen_urls:
                    continue

                track = Track(artist=artist, title=track_title, url=audio_url, source=feed_url)
                tracks.append(track)
                seen_urls.add(audio_url)

                if len(tracks) >= 3:
                    break
            if len(tracks) >= 3:
                break

        if len(tracks) >= 2:
            if attempt_name != "genre":
                return tracks[:3], attempt_name
            return tracks[:3], None

        if attempt_name == "at_least_one" and len(tracks) >= 1:
            return tracks[:3], attempt_name

    fallback_objs = [Track(**item, source="static_fallback") for item in FALLBACK_TRACKS]
    return fallback_objs[:2], "random_popular"


async def fetch_quotes_ua(count: int = 2) -> list[str]:
    quotes: list[str] = []

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for url in QUOTE_FEEDS:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
            except Exception:
                logger.exception("Failed to get quote source: %s", url)
                continue

            chunks = re.split(r"[\n\r]+", re.sub(r"<[^>]+>", "\n", text))
            for chunk in chunks:
                line = re.sub(r"\s+", " ", chunk).strip(" .•\t")
                if not line:
                    continue
                if not has_target_language(line):
                    continue
                if len(line) < 35:
                    continue
                lines = [seg.strip() for seg in re.split(r"(?<=[,.!?:;])\s+", line) if seg.strip()]
                if len(lines) < 2:
                    continue
                selected = "\n".join(lines[:4])
                if 2 <= selected.count("\n") + 1 <= 4:
                    quotes.append(selected)
                if len(quotes) >= count:
                    return quotes[:count]

    while len(quotes) < count:
        quotes.append(random.choice(STATIC_UA_QUOTES))
    return quotes[:count]


async def download_file(url: str, suffix: str) -> str:
    filename = f"{uuid.uuid4().hex}{suffix}"
    path = str(Path(tempfile.gettempdir()) / filename)
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Download failed with status {resp.status} for {url}")
            data = await resp.read()
            if not data:
                raise RuntimeError(f"Empty response while downloading {url}")
    with open(path, "wb") as f:
        f.write(data)
    return path


async def fetch_unsplash_photo() -> str:
    endpoint = "https://api.unsplash.com/photos/random"
    params = {
        "query": "music mood vibe aesthetic",
        "orientation": "portrait",
        "content_filter": "high",
    }
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(endpoint, params=params, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Unsplash API error {resp.status}: {text}")
            payload = await resp.json()
            image_url = payload.get("urls", {}).get("small") or payload.get("urls", {}).get("regular")
            if not image_url:
                raise RuntimeError("Unsplash response has no image URL")

    return await download_file(image_url, ".jpg")


async def fetch_channel_avatar(bot: Bot) -> str | None:
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        if not chat.photo:
            return None
        file_id = chat.photo.big_file_id
        file = await bot.get_file(file_id)
        path = str(Path(tempfile.gettempdir()) / f"channel_avatar_{uuid.uuid4().hex}.jpg")
        await bot.download_file(file.file_path, destination=path)
        return path
    except TelegramAPIError:
        logger.exception("Unable to fetch channel avatar")
        return None
    except Exception:
        logger.exception("Unexpected error while fetching channel avatar")
        return None


def ensure_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if not ensure_admin(message):
        await message.answer("Доступ обмежено.")
        return
    await state.clear()
    await message.answer("Головне меню.", reply_markup=MAIN_MENU)


@router.message(F.text == "❌ Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not ensure_admin(message):
        return
    await state.clear()
    pending_posts.pop(message.from_user.id, None)
    pending_polls.pop(message.from_user.id, None)
    await message.answer("Скасовано. Повернення в головне меню.", reply_markup=MAIN_MENU)


@router.message(F.text == "🆕 Новий пост")
async def new_post_handler(message: Message, state: FSMContext) -> None:
    if not ensure_admin(message):
        return
    await state.set_state(PostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=GENRE_MENU)


@router.message(PostStates.choosing_genre, F.text.in_(GENRES))
async def genre_selected_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if not ensure_admin(message):
        return

    genre = message.text
    await message.answer("Формую пост, зачекайте...", reply_markup=MAIN_MENU)

    photo_path = None
    quote = None
    audio_paths: list[str] = []
    fallback_note = None

    try:
        tracks, fallback_mode = await collect_tracks(genre)
        if fallback_mode:
            fallback_note = {
                "no_genre": "Використано fallback: пошук без жанру.",
                "random_popular": "Використано fallback: random popular джерела.",
                "at_least_one": "Використано fallback: знайдено мінімально доступні треки.",
            }.get(fallback_mode, "Використано fallback для пошуку треків.")

        if len(tracks) == 1:
            tracks = [tracks[0], tracks[0]]
        elif len(tracks) == 0:
            tracks = [Track(**FALLBACK_TRACKS[0], source="static_fallback"), Track(**FALLBACK_TRACKS[1], source="static_fallback")]
            fallback_note = "Використано аварійний fallback для треків."

        tracks = tracks[:2]

        quotes = await fetch_quotes_ua(count=2)
        quote = random.choice(quotes)

        photo_path = await fetch_unsplash_photo()

        for track in tracks:
            path = await download_file(track.url, ".mp3")
            audio_paths.append(path)

        post_id = message.from_user.id
        pending_posts[post_id] = {
            "photo": photo_path,
            "quote": quote,
            "audio": audio_paths,
            "genre": genre,
            "tracks": tracks,
        }

        publish_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data="publish_post")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_post")],
            ]
        )

        with open(photo_path, "rb"):
            pass
        await bot.send_photo(chat_id=ADMIN_ID, photo=FSInputFile(photo_path), caption=quote)

        for track, path in zip(tracks, audio_paths, strict=False):
            caption = f"<b>{track.title}</b>\n<i>{track.artist}</i>"
            await bot.send_audio(chat_id=ADMIN_ID, audio=FSInputFile(path), caption=caption)

        if fallback_note:
            await message.answer(fallback_note)

        await message.answer("Пост готовий. Опублікувати?", reply_markup=publish_kb)

    except Exception as exc:
        logger.exception("Failed to build post")
        for p in [photo_path, *audio_paths]:
            if p and os.path.exists(p):
                os.remove(p)
        await message.answer(f"Сталася помилка під час формування посту: {exc}", reply_markup=MAIN_MENU)
    finally:
        await state.clear()


@router.callback_query(F.data == "cancel_post")
async def cancel_post_callback(callback: Any, state: FSMContext) -> None:
    user_id = callback.from_user.id
    post = pending_posts.pop(user_id, None)
    if post:
        for p in [post.get("photo"), *(post.get("audio", []))]:
            if p and os.path.exists(p):
                os.remove(p)
    await state.clear()
    await callback.message.answer("Скасовано. Повернення в головне меню.", reply_markup=MAIN_MENU)
    await callback.answer()


@router.callback_query(F.data == "publish_post")
async def publish_post_callback(callback: Any, bot: Bot, state: FSMContext) -> None:
    user_id = callback.from_user.id
    post = pending_posts.pop(user_id, None)
    if not post:
        await callback.answer("Пост не знайдено", show_alert=True)
        return

    try:
        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(post["photo"]), caption=post["quote"])
        for track, path in zip(post["tracks"], post["audio"], strict=False):
            caption = f"<b>{track.title}</b>\n<i>{track.artist}</i>"
            await bot.send_audio(CHANNEL_ID, audio=FSInputFile(path), caption=caption)
        await callback.message.answer("Опубліковано в канал.", reply_markup=MAIN_MENU)
        await callback.answer("Готово")
    except Exception:
        logger.exception("Failed to publish post")
        await callback.answer("Помилка публікації", show_alert=True)
    finally:
        for p in [post.get("photo"), *(post.get("audio", []))]:
            if p and os.path.exists(p):
                os.remove(p)
        await state.clear()


@router.message(F.text == "📊 Опитування")
async def poll_menu_handler(message: Message, state: FSMContext) -> None:
    if not ensure_admin(message):
        return
    await state.set_state(PostStates.choosing_poll)
    await message.answer("Оберіть шаблон опитування:", reply_markup=POLL_MENU)


@router.message(PostStates.choosing_poll, F.text.in_([p["title"] for p in POLL_TEMPLATES]))
async def poll_selected_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    if not ensure_admin(message):
        return

    selected = next((p for p in POLL_TEMPLATES if p["title"] == message.text), None)
    if not selected:
        await message.answer("Шаблон не знайдено.", reply_markup=MAIN_MENU)
        await state.clear()
        return

    try:
        await bot.send_poll(
            chat_id=ADMIN_ID,
            question=selected["question"],
            options=selected["options"],
            is_anonymous=False,
        )

        avatar_path = await fetch_channel_avatar(bot)
        play_button = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Програти пісню", url="https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Ketsa/Tides/Ketsa_-_01_-_Tides.mp3")]
            ]
        )

        if avatar_path and os.path.exists(avatar_path):
            await bot.send_photo(ADMIN_ID, photo=FSInputFile(avatar_path), caption="Кнопка програвання треку:", reply_markup=play_button)
            os.remove(avatar_path)
        else:
            await message.answer("Кнопка програвання треку:", reply_markup=play_button)

        control = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data="publish_poll")],
                [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_poll")],
            ]
        )

        pending_polls[message.from_user.id] = selected
        await message.answer("Опитування готове до публікації.", reply_markup=control)
    except Exception:
        logger.exception("Failed to prepare poll")
        await message.answer("Не вдалося створити опитування.", reply_markup=MAIN_MENU)
    finally:
        await state.clear()


@router.callback_query(F.data == "cancel_poll")
async def cancel_poll_callback(callback: Any, state: FSMContext) -> None:
    pending_polls.pop(callback.from_user.id, None)
    await state.clear()
    await callback.message.answer("Скасовано. Повернення в головне меню.", reply_markup=MAIN_MENU)
    await callback.answer()


@router.callback_query(F.data == "publish_poll")
async def publish_poll_callback(callback: Any, bot: Bot, state: FSMContext) -> None:
    selected = pending_polls.pop(callback.from_user.id, None)
    if not selected:
        await callback.answer("Опитування не знайдено", show_alert=True)
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=selected["question"],
            options=selected["options"],
            is_anonymous=False,
        )
        await callback.message.answer("Опитування опубліковано.", reply_markup=MAIN_MENU)
        await callback.answer("Готово")
    except Exception:
        logger.exception("Failed to publish poll")
        await callback.answer("Помилка публікації", show_alert=True)
    finally:
        await state.clear()


@router.message()
async def fallback_handler(message: Message, state: FSMContext) -> None:
    if not ensure_admin(message):
        return
    current_state = await state.get_state()
    if current_state == PostStates.choosing_genre.state:
        await message.answer("Оберіть один із жанрів: Pop, Rock, Hip-Hop, Electronic.", reply_markup=GENRE_MENU)
    elif current_state == PostStates.choosing_poll.state:
        await message.answer("Оберіть Опитування 1, Опитування 2 або Опитування 3.", reply_markup=POLL_MENU)
    else:
        await message.answer("Оберіть дію з меню.", reply_markup=MAIN_MENU)


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Bot started")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
