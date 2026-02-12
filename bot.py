import asyncio
import logging
import os
import random
import textwrap
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "")

if not all([BOT_TOKEN, JAMENDO_CLIENT_ID, UNSPLASH_ACCESS_KEY, ADMIN_ID, CHANNEL_ID]):
    raise RuntimeError(
        "Missing required environment variables: BOT_TOKEN, JAMENDO_CLIENT_ID, "
        "UNSPLASH_ACCESS_KEY, ADMIN_ID, CHANNEL_ID"
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music_channel_bot")

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
JAMENDO_BASE_URL = "https://api.jamendo.com/v3.0/tracks/"
UNSPLASH_URL = "https://api.unsplash.com/search/photos"
QUOTES_RSS_URL = "https://www.brainyquote.com/link/quotebr.rss"

POLL_TEMPLATES = [
    {
        "id": "poll_1",
        "title": "Опитування 1",
        "question": "Який настрій для сьогоднішнього вечора?",
        "options": ["Спокійний", "Енергійний", "Романтичний", "Мікс"],
    },
    {
        "id": "poll_2",
        "title": "Опитування 2",
        "question": "Який жанр хочете почути наступним?",
        "options": ["Pop", "Lo-fi", "Indie", "Deep House"],
    },
    {
        "id": "poll_3",
        "title": "Опитування 3",
        "question": "Коли краще публікувати музичні добірки?",
        "options": ["Ранок", "День", "Вечір", "Ніч"],
    },
]


@dataclass
class Track:
    name: str
    artist: str
    audio_url: str
    track_url: str


@dataclass
class PreparedPost:
    caption: str
    photo_bytes: bytes
    photo_name: str
    tracks: list[Track]
    audio_payloads: list[tuple[Track, bytes, str]]


prepared_posts: dict[int, PreparedPost] = {}


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Новий пост", callback_data="new_post")],
            [InlineKeyboardButton(text="2️⃣ Опитування", callback_data="polls")],
            [InlineKeyboardButton(text="3️⃣ Скасувати", callback_data="cancel")],
        ]
    )


def post_preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Опублікувати", callback_data="publish_post")],
            [InlineKeyboardButton(text="Скасувати", callback_data="cancel")],
        ]
    )


def polls_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for poll in POLL_TEMPLATES:
        rows.append([InlineKeyboardButton(text=poll["title"], callback_data=f"send_{poll['id']}")])
    rows.append([InlineKeyboardButton(text="Скасувати", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        async with session.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            return await response.json()
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Timeout while requesting {url}") from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"HTTP error while requesting {url}: {exc}") from exc


async def fetch_bytes(session: aiohttp.ClientSession, url: str, headers: dict[str, str] | None = None) -> bytes:
    try:
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as response:
            response.raise_for_status()
            return await response.read()
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Timeout while downloading {url}") from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"HTTP error while downloading {url}: {exc}") from exc


async def jamendo_request(session: aiohttp.ClientSession, limit: int, genre: str | None = None, sort: str | None = None) -> list[Track]:
    params: dict[str, Any] = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": limit,
        "include": "musicinfo",
        "audioformat": "mp32",
    }
    if genre:
        params["tags"] = genre
    if sort:
        params["order"] = sort

    payload = await fetch_json(session, JAMENDO_BASE_URL, params=params)
    raw_results = payload.get("results", [])
    tracks: list[Track] = []

    for item in raw_results:
        name = (item.get("name") or "").strip()
        artist = (item.get("artist_name") or "").strip()
        audio_url = (item.get("audio") or "").strip()
        track_url = (item.get("shareurl") or item.get("url") or "").strip()

        if name and artist and audio_url and track_url:
            tracks.append(Track(name=name, artist=artist, audio_url=audio_url, track_url=track_url))

    return tracks


def dedupe_tracks(tracks: list[Track]) -> list[Track]:
    seen: set[str] = set()
    unique: list[Track] = []
    for track in tracks:
        key = f"{track.name.lower()}::{track.artist.lower()}::{track.audio_url}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(track)
    return unique


async def find_tracks(session: aiohttp.ClientSession) -> list[Track]:
    collected: list[Track] = []
    genre = random.choice(["pop", "chill"])

    attempts: list[dict[str, Any]] = [
        {"limit": 2, "genre": genre, "sort": None},
        {"limit": 2, "genre": None, "sort": None},
        {"limit": 2, "genre": None, "sort": "popularity_total"},
        {"limit": 1, "genre": None, "sort": "popularity_total"},
    ]

    for step, attempt in enumerate(attempts, start=1):
        try:
            tracks = await jamendo_request(session, attempt["limit"], attempt["genre"], attempt["sort"])
            collected.extend(tracks)
            collected = dedupe_tracks(collected)
            logger.info("Jamendo attempt %s found %s tracks (unique=%s)", step, len(tracks), len(collected))
            if len(collected) >= 2:
                return collected[:2]
        except Exception:
            logger.exception("Jamendo attempt %s failed", step)

    return collected[:2] if len(collected) >= 2 else collected[:1]


async def find_unsplash_photo(session: aiohttp.ClientSession) -> tuple[bytes, str]:
    params = {
        "query": "music vibe night aesthetic",
        "orientation": "portrait",
        "per_page": 30,
        "order_by": "relevant",
    }
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    payload = await fetch_json(session, UNSPLASH_URL, params=params, headers=headers)
    results = payload.get("results", [])
    if not results:
        raise RuntimeError("Unsplash returned no results")

    picked = random.choice(results)
    image_url = (picked.get("urls") or {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash returned result without image url")

    image_bytes = await fetch_bytes(session, image_url)
    return image_bytes, "cover.jpg"


async def find_quote(session: aiohttp.ClientSession) -> str:
    rss_bytes = await fetch_bytes(session, QUOTES_RSS_URL)
    root = ElementTree.fromstring(rss_bytes)

    items = root.findall("./channel/item")
    candidates: list[str] = []

    for item in items:
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        text = f"{title}\n{description}".strip()
        text = "\n".join(part.strip() for part in text.splitlines() if part.strip())

        if not text:
            continue

        wrapped = textwrap.wrap(text.replace("\n", " "), width=46)
        lines_count = len(wrapped)
        if 2 <= lines_count <= 4 and len(text) <= 240:
            candidates.append("\n".join(wrapped))

    if not candidates:
        return "Music can change the world\nbecause it can change people."

    return random.choice(candidates)


def build_caption(quote: str, tracks: list[Track]) -> str:
    first = tracks[0]
    second = tracks[1] if len(tracks) > 1 else tracks[0]
    return (
        f"{quote}\n\n"
        f"🎵 Трек 1\n"
        f"{first.name} — {first.artist}\n\n"
        f"🎵 Трек 2\n"
        f"{second.name} — {second.artist}"
    )


async def build_post() -> PreparedPost:
    async with aiohttp.ClientSession() as session:
        tracks = await find_tracks(session)
        if not tracks:
            raise RuntimeError("Не вдалося знайти жодного треку через Jamendo API")

        photo_bytes, photo_name = await find_unsplash_photo(session)
        quote = await find_quote(session)

        audio_payloads: list[tuple[Track, bytes, str]] = []
        for idx, track in enumerate(tracks, start=1):
            audio_bytes = await fetch_bytes(session, track.audio_url)
            safe_name = f"track_{idx}.mp3"
            audio_payloads.append((track, audio_bytes, safe_name))

        caption = build_caption(quote, tracks)
        return PreparedPost(
            caption=caption,
            photo_bytes=photo_bytes,
            photo_name=photo_name,
            tracks=tracks,
            audio_payloads=audio_payloads,
        )


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("Доступ заборонено.")
        return

    await message.answer("Оберіть дію:", reply_markup=main_menu_keyboard())


@dp.callback_query(F.data == "cancel")
async def cancel_handler(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return

    prepared_posts.pop(callback.from_user.id, None)
    await callback.message.edit_text("Операцію скасовано. Оберіть дію:", reply_markup=main_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "new_post")
async def new_post_handler(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_text("Генерую пост, зачекайте...")

    try:
        post = await build_post()
    except Exception as exc:
        logger.exception("Failed to generate post")
        await callback.message.answer(f"Помилка генерації поста: {exc}")
        await callback.message.answer("Оберіть дію:", reply_markup=main_menu_keyboard())
        return

    prepared_posts[callback.from_user.id] = post

    if len(post.tracks) == 1:
        await callback.message.answer(
            "Знайдено лише 1 трек. Пост сформовано та готовий до публікації."
        )

    photo_input = BufferedInputFile(post.photo_bytes, filename=post.photo_name)
    await callback.message.answer_photo(
        photo=photo_input,
        caption=post.caption,
        reply_markup=post_preview_keyboard(),
    )

    for track, audio_bytes, file_name in post.audio_payloads:
        audio_input = BufferedInputFile(audio_bytes, filename=file_name)
        await callback.message.answer_audio(
            audio=audio_input,
            title=track.name,
            performer=track.artist,
            caption=track.track_url,
        )


@dp.callback_query(F.data == "publish_post")
async def publish_post_handler(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return

    post = prepared_posts.get(callback.from_user.id)
    if not post:
        await callback.answer("Немає підготовленого поста", show_alert=True)
        return

    try:
        photo_input = BufferedInputFile(post.photo_bytes, filename=post.photo_name)
        await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_input, caption=post.caption)

        for track, audio_bytes, file_name in post.audio_payloads:
            audio_input = BufferedInputFile(audio_bytes, filename=file_name)
            await bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=audio_input,
                title=track.name,
                performer=track.artist,
                caption=track.track_url,
            )

        prepared_posts.pop(callback.from_user.id, None)
        await callback.message.answer("✅ Пост успішно опубліковано")
        await callback.message.answer("Оберіть дію:", reply_markup=main_menu_keyboard())
        await callback.answer()
    except Exception:
        logger.exception("Failed to publish post")
        await callback.answer("Помилка під час публікації", show_alert=True)


@dp.callback_query(F.data == "polls")
async def polls_handler(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return

    await callback.message.edit_text("Оберіть опитування:", reply_markup=polls_keyboard())
    await callback.answer()


@dp.callback_query(F.data.startswith("send_poll_"))
async def send_poll_handler(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Доступ заборонено", show_alert=True)
        return

    poll_id = callback.data.replace("send_", "", 1)
    poll = next((item for item in POLL_TEMPLATES if item["id"] == poll_id), None)
    if not poll:
        await callback.answer("Опитування не знайдено", show_alert=True)
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=poll["question"],
            options=poll["options"],
            is_anonymous=True,
            allows_multiple_answers=False,
        )
        await callback.message.answer("✅ Опитування опубліковано")
        await callback.message.answer("Оберіть дію:", reply_markup=main_menu_keyboard())
        await callback.answer()
    except TelegramBadRequest as exc:
        logger.exception("Telegram rejected poll publishing")
        await callback.answer("Помилка публікації опитування", show_alert=True)
        await callback.message.answer(f"Telegram API error: {exc.message}")
    except Exception:
        logger.exception("Unexpected poll publishing error")
        await callback.answer("Помилка публікації опитування", show_alert=True)


async def main() -> None:
    logger.info("Bot is starting")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
