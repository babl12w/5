import asyncio
import logging
import os
import random
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

if not BOT_TOKEN or not UNSPLASH_ACCESS_KEY or not ADMIN_ID or not CHANNEL_ID:
    raise RuntimeError("BOT_TOKEN, UNSPLASH_ACCESS_KEY, ADMIN_ID, CHANNEL_ID must be set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music-channel-bot")

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🆕 Новий пост")],
        [KeyboardButton(text="📊 Опитування")],
        [KeyboardButton(text="❌ Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

GENRE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Pop"), KeyboardButton(text="Rock")],
        [KeyboardButton(text="Hip-Hop"), KeyboardButton(text="Electronic")],
        [KeyboardButton(text="❌ Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POLL_PREVIEW_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опублікувати", callback_data="poll_publish")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="poll_cancel")],
    ]
)

POLL_SELECT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Опитування 1")],
        [KeyboardButton(text="Опитування 2")],
        [KeyboardButton(text="Опитування 3")],
        [KeyboardButton(text="❌ Скасувати")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

POLL_BANK: dict[str, dict[str, Any]] = {
    "Опитування 1": {
        "question": "Який музичний жанр вам ближчий сьогодні?",
        "options": ["Pop", "Rock", "Hip-Hop", "Electronic"],
        "is_anonymous": True,
        "allows_multiple_answers": False,
    },
    "Опитування 2": {
        "question": "Коли ви найчастіше слухаєте музику?",
        "options": ["Зранку", "Вдень", "Увечері", "Вночі"],
        "is_anonymous": True,
        "allows_multiple_answers": False,
    },
    "Опитування 3": {
        "question": "Що важливіше у треку?",
        "options": ["Біт", "Текст", "Вокал", "Атмосфера"],
        "is_anonymous": True,
        "allows_multiple_answers": False,
    },
}

MUSIC_FEEDS = [
    "https://freemusicarchive.org/recent.rss",
    "https://ccmixter.org/api/query?f=rss&tags=instrumental",
    "https://archive.org/services/collection-rss.php?collection=opensource_audio",
]
QUOTE_FEEDS = [
    "https://www.brainyquote.com/link/quotebr.rss",
]

LANG_PATTERN = re.compile(r"[іїєґІЇЄҐыэёЁА-Яа-яąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
TRACK_SPLIT_PATTERN = re.compile(r"\s[-–—|:]\s")


class BotStates(StatesGroup):
    waiting_genre = State()
    waiting_poll_choice = State()


@dataclass
class AudioTrack:
    title: str
    artist: str
    file_path: Path


@dataclass
class PreparedPost:
    genre: str
    quote: str
    photo_path: Path
    tracks: list[AudioTrack] = field(default_factory=list)
    fallback_messages: list[str] = field(default_factory=list)


TEMP_DIR = Path(tempfile.gettempdir()) / "music_channel_bot"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^\w\-. ]+", "_", name, flags=re.UNICODE).strip()
    return safe[:80] or f"file_{uuid.uuid4().hex}"


async def is_admin(message: Message) -> bool:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("Доступ дозволено лише адміністратору.")
        return False
    return True


async def fetch_bytes(session: aiohttp.ClientSession, url: str, timeout: int = 20) -> bytes:
    async with session.get(url, timeout=timeout) as resp:
        resp.raise_for_status()
        return await resp.read()


async def fetch_text(session: aiohttp.ClientSession, url: str, timeout: int = 20) -> str:
    async with session.get(url, timeout=timeout) as resp:
        resp.raise_for_status()
        return await resp.text()


def detect_lang_compatible(text: str) -> bool:
    if not text:
        return False
    return bool(LANG_PATTERN.search(text))


def parse_track_metadata(entry: Any) -> tuple[str, str]:
    title_raw = str(entry.get("title", "")).strip() or "Unknown title"
    artist = "Unknown artist"
    title = title_raw

    for field in ("author", "artist", "itunes_author"):
        value = str(entry.get(field, "")).strip()
        if value:
            artist = value
            break

    if artist == "Unknown artist":
        parts = TRACK_SPLIT_PATTERN.split(title_raw, maxsplit=1)
        if len(parts) == 2 and all(parts):
            artist, title = parts[0].strip(), parts[1].strip()

    return title[:120], artist[:120]


def extract_audio_url(entry: Any) -> str | None:
    links = entry.get("links", []) or []
    for link in links:
        href = str(link.get("href", ""))
        type_ = str(link.get("type", "")).lower()
        rel = str(link.get("rel", "")).lower()
        if href and ("audio" in type_ or href.lower().endswith(".mp3") or rel == "enclosure"):
            return href
    enclosure = entry.get("enclosures", []) or []
    for enc in enclosure:
        href = str(enc.get("href", ""))
        if href and href.lower().endswith(".mp3"):
            return href
    return None


async def collect_tracks(session: aiohttp.ClientSession, query: str | None, limit: int = 2) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for feed_url in MUSIC_FEEDS:
        try:
            xml = await fetch_text(session, feed_url)
            parsed = feedparser.parse(xml)
            entries = parsed.entries[:50]
            random.shuffle(entries)

            for entry in entries:
                audio_url = extract_audio_url(entry)
                if not audio_url:
                    continue

                title, artist = parse_track_metadata(entry)
                text = f"{title} {artist}".strip()
                if query and query.lower() not in text.lower():
                    continue
                if not detect_lang_compatible(text):
                    continue

                found.append((title, artist, audio_url))
                if len(found) >= limit:
                    return found
        except Exception as exc:
            logger.warning("Feed parse failed for %s: %s", feed_url, exc)
    return found


async def download_audio_tracks(
    session: aiohttp.ClientSession,
    candidates: list[tuple[str, str, str]],
) -> list[AudioTrack]:
    tracks: list[AudioTrack] = []
    for title, artist, url in candidates:
        if len(tracks) >= 2:
            break
        try:
            data = await fetch_bytes(session, url, timeout=45)
            if not data:
                continue
            filename = sanitize_filename(f"{artist} - {title}.mp3")
            file_path = TEMP_DIR / f"{uuid.uuid4().hex}_{filename}"
            file_path.write_bytes(data)
            tracks.append(AudioTrack(title=title, artist=artist, file_path=file_path))
        except Exception as exc:
            logger.warning("Audio download failed (%s): %s", url, exc)
    return tracks


async def fetch_unsplash_photo(session: aiohttp.ClientSession, genre: str) -> Path:
    query = random.choice(["music vibe", "aesthetic music mood", f"{genre} music vibe"])
    url = "https://api.unsplash.com/photos/random"
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {"query": query, "orientation": "portrait", "content_filter": "low"}

    async with session.get(url, headers=headers, params=params, timeout=30) as resp:
        resp.raise_for_status()
        payload = await resp.json()

    image_url = payload.get("urls", {}).get("small") or payload.get("urls", {}).get("thumb")
    if not image_url:
        raise RuntimeError("Unsplash response missing image URL")

    image_data = await fetch_bytes(session, image_url, timeout=30)
    image_path = TEMP_DIR / f"photo_{uuid.uuid4().hex}.jpg"
    image_path.write_bytes(image_data)
    return image_path


async def fetch_ukrainian_quote(session: aiohttp.ClientSession) -> str:
    for feed_url in QUOTE_FEEDS:
        try:
            xml = await fetch_text(session, feed_url)
            parsed = feedparser.parse(xml)
            for entry in parsed.entries:
                content = str(entry.get("title", "")).strip()
                if not content:
                    continue
                cleaned = re.sub(r"\s+", " ", content)
                if len(cleaned) < 40:
                    continue
                if not detect_lang_compatible(cleaned):
                    continue
                text = cleaned[:220]
                lines = [
                    text[i : i + 55].strip()
                    for i in range(0, len(text), 55)
                    if text[i : i + 55].strip()
                ][:4]
                if len(lines) >= 2:
                    return "\n".join(lines)
        except Exception as exc:
            logger.warning("Quote source failed %s: %s", feed_url, exc)

    fallback_quotes = [
        "Музика лікує мовчанням\nі повертає сенс кожному подиху.",
        "Коли слова закінчуються,\nнароджується мелодія серця.",
        "У ритмі дня знайди свою хвилю,\nа в пісні — опору і світло.",
    ]
    return random.choice(fallback_quotes)


async def prepare_music_post(genre: str) -> PreparedPost:
    fallback_msgs: list[str] = []
    async with aiohttp.ClientSession() as session:
        quote = await fetch_ukrainian_quote(session)
        photo_path = await fetch_unsplash_photo(session, genre)

        candidates = await collect_tracks(session, query=genre, limit=2)
        if len(candidates) < 2:
            fallback_msgs.append("Не вистачило треків за жанром, використано пошук без жанру.")
            candidates = await collect_tracks(session, query=None, limit=2)

        if len(candidates) < 2:
            fallback_msgs.append("Недостатньо треків без жанру, використано random popular.")
            candidates = await collect_tracks(session, query="popular", limit=2)

        if len(candidates) < 2:
            fallback_msgs.append("Недостатньо popular треків, шукаю хоча б один трек.")
            one_track = await collect_tracks(session, query=None, limit=1)
            if one_track:
                candidates = one_track

        downloaded_tracks = await download_audio_tracks(session, candidates)
        if not downloaded_tracks:
            raise RuntimeError("Не вдалося завантажити жодного аудіо-файлу")

    return PreparedPost(
        genre=genre,
        quote=quote,
        photo_path=photo_path,
        tracks=downloaded_tracks,
        fallback_messages=fallback_msgs,
    )


async def send_main_menu(message: Message, text: str = "Головне меню") -> None:
    await message.answer(text, reply_markup=MAIN_KB)


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if not await is_admin(message):
        return
    await state.clear()
    await send_main_menu(message, "Вітаю! Оберіть дію:")


@dp.message(F.text == "❌ Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not await is_admin(message):
        return
    await state.clear()
    await send_main_menu(message, "Дію скасовано.")


@dp.message(F.text == "🆕 Новий пост")
async def new_post_handler(message: Message, state: FSMContext) -> None:
    if not await is_admin(message):
        return
    await state.set_state(BotStates.waiting_genre)
    await message.answer("Оберіть жанр:", reply_markup=GENRE_KB)


@dp.message(BotStates.waiting_genre)
async def genre_chosen_handler(message: Message, state: FSMContext) -> None:
    if not await is_admin(message):
        return
    genre = (message.text or "").strip()
    if genre not in {"Pop", "Rock", "Hip-Hop", "Electronic"}:
        await message.answer("Будь ласка, оберіть жанр кнопкою.", reply_markup=GENRE_KB)
        return

    await message.answer("Готую пост, зачекайте...", reply_markup=ReplyKeyboardRemove())
    await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)

    try:
        prepared = await prepare_music_post(genre)
        for info in prepared.fallback_messages:
            await message.answer(f"ℹ️ {info}")

        await bot.send_photo(
            chat_id=message.chat.id,
            photo=FSInputFile(prepared.photo_path),
            caption=prepared.quote,
        )

        for track in prepared.tracks[:2]:
            await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_AUDIO)
            await bot.send_audio(
                chat_id=message.chat.id,
                audio=FSInputFile(track.file_path),
                title=track.title,
                performer=track.artist,
            )

        if len(prepared.tracks) < 2:
            await message.answer("ℹ️ Вдалося знайти лише 1 трек. Пост буде опубліковано з доступною кількістю аудіо.")

        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=FSInputFile(prepared.photo_path),
            caption=prepared.quote,
        )
        for track in prepared.tracks[:2]:
            await bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=FSInputFile(track.file_path),
                title=track.title,
                performer=track.artist,
            )

        await send_main_menu(message, "✅ Пост опубліковано в канал.")
    except Exception as exc:
        logger.exception("Failed to generate post: %s", exc)
        await send_main_menu(message, "❌ Не вдалося створити пост. Спробуйте ще раз.")
    finally:
        await state.clear()


@dp.message(F.text == "📊 Опитування")
async def poll_entry_handler(message: Message, state: FSMContext) -> None:
    if not await is_admin(message):
        return
    await state.set_state(BotStates.waiting_poll_choice)
    await message.answer("Оберіть опитування:", reply_markup=POLL_SELECT_KB)


@dp.message(BotStates.waiting_poll_choice)
async def poll_choice_handler(message: Message, state: FSMContext) -> None:
    if not await is_admin(message):
        return
    choice = (message.text or "").strip()
    if choice not in POLL_BANK:
        await message.answer("Оберіть опитування кнопкою.", reply_markup=POLL_SELECT_KB)
        return

    payload = POLL_BANK[choice]
    await state.update_data(selected_poll=payload)
    await message.answer_poll(
        question=payload["question"],
        options=payload["options"],
        is_anonymous=payload["is_anonymous"],
        allows_multiple_answers=payload["allows_multiple_answers"],
    )
    await message.answer("Підтвердьте публікацію:", reply_markup=MAIN_KB)
    await message.answer("Керування опитуванням:", reply_markup=ReplyKeyboardRemove())
    await message.answer("Оберіть дію нижче:", reply_markup=POLL_PREVIEW_KB)


async def get_channel_avatar_file(bot_instance: Bot) -> BufferedInputFile | None:
    try:
        chat = await bot_instance.get_chat(CHANNEL_ID)
        if not chat.photo:
            return None
        file = await bot_instance.get_file(chat.photo.big_file_id)
        file_bytes = await bot_instance.download_file(file.file_path)
        data = file_bytes.read()
        return BufferedInputFile(data=data, filename="channel_avatar.jpg")
    except Exception as exc:
        logger.warning("Cannot fetch channel avatar: %s", exc)
        return None


@dp.callback_query(F.data == "poll_publish")
async def poll_publish_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостатньо прав.", show_alert=True)
        return

    data = await state.get_data()
    selected = data.get("selected_poll")
    if not selected:
        await callback.answer("Опитування не вибрано.", show_alert=True)
        return

    try:
        avatar = await get_channel_avatar_file(bot)
        if avatar:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=avatar,
                caption="🎵 Нове опитування для спільноти",
            )

        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=selected["question"],
            options=selected["options"],
            is_anonymous=selected["is_anonymous"],
            allows_multiple_answers=selected["allows_multiple_answers"],
        )
        await callback.answer("✅ Опитування опубліковано")
        await callback.message.answer("✅ Опитування опубліковано.", reply_markup=MAIN_KB)
    except TelegramAPIError as exc:
        logger.exception("Poll publish failed: %s", exc)
        await callback.answer("❌ Помилка публікації", show_alert=True)
    finally:
        await state.clear()


@dp.callback_query(F.data == "poll_cancel")
async def poll_cancel_handler(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостатньо прав.", show_alert=True)
        return
    await state.clear()
    await callback.answer("Скасовано")
    await callback.message.answer("Дію скасовано.", reply_markup=MAIN_KB)


@dp.errors()
async def error_handler(event: Any) -> bool:
    logger.exception("Unhandled error: %s", event)
    return True


async def main() -> None:
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
