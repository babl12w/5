import asyncio
import html
import logging
import os
import random
import re
import tempfile
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
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from imageio_ffmpeg import get_ffmpeg_exe
from yt_dlp import YoutubeDL

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

REQUIRED_ENVS = {
    "BOT_TOKEN": BOT_TOKEN,
    "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
    "ADMIN_ID": str(ADMIN_ID) if ADMIN_ID else "",
    "CHANNEL_ID": CHANNEL_ID,
    "SPOTIFY_CLIENT_ID": SPOTIFY_CLIENT_ID,
    "SPOTIFY_CLIENT_SECRET": SPOTIFY_CLIENT_SECRET,
}

missing = [name for name, value in REQUIRED_ENVS.items() if not value]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music-bot")

router = Router()

GENRES = ["Pop", "Rok", "Rap", "Electronic"]
LANGUAGES = ["ukrainian", "russian", "polish"]

POLL_TEMPLATES: dict[str, dict[str, list[str] | str]] = {
    "Опитування 1": {
        "question": "Який вайб сьогодні вмикаємо?",
        "options": ["Chill", "Dance", "Rock", "Lo-fi"],
    },
    "Опитування 2": {
        "question": "Що хочете почути у наступному пості?",
        "options": ["Свіжі новинки", "Класика", "Інді", "Мікс"],
    },
    "Опитування 3": {
        "question": "Якою мовою додати більше треків?",
        "options": ["Українською", "Англійською", "Польською", "Без різниці"],
    },
}

QUOTE_RSS_SOURCES = [
    "https://maximum.fm/rss",
    "https://www.radiosvoboda.org/api/z$pryqqp$r",
    "https://www.ukrinform.ua/rss/block-lastnews",
    "https://life.pravda.com.ua/rss/",
]


class PostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    preview_post = State()


class PollStates(StatesGroup):
    choosing_template = State()
    preview_poll = State()


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1️⃣ Новий пост")],
            [KeyboardButton(text="2️⃣ Опитування")],
            [KeyboardButton(text="3️⃣ Скасувати")],
        ],
        resize_keyboard=True,
    )


def genre_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=GENRES[0]), KeyboardButton(text=GENRES[1])],
            [KeyboardButton(text=GENRES[2]), KeyboardButton(text=GENRES[3])],
            [KeyboardButton(text="3️⃣ Скасувати")],
        ],
        resize_keyboard=True,
    )


def language_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=LANGUAGES[0])],
            [KeyboardButton(text=LANGUAGES[1])],
            [KeyboardButton(text=LANGUAGES[2])],
            [KeyboardButton(text="3️⃣ Скасувати")],
        ],
        resize_keyboard=True,
    )


def publish_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Опублікувати")],
            [KeyboardButton(text="3️⃣ Скасувати")],
        ],
        resize_keyboard=True,
    )


def polls_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Опитування 1")],
            [KeyboardButton(text="Опитування 2")],
            [KeyboardButton(text="Опитування 3")],
            [KeyboardButton(text="3️⃣ Скасувати")],
        ],
        resize_keyboard=True,
    )


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def cleanup_paths(paths: list[str]) -> None:
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        try:
            if path.exists():
                path.unlink()
        except Exception as exc:
            logger.warning("Failed to delete temp file %s: %s", path, exc)


def sanitize_html_text(text: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def text_to_quote(text: str) -> str | None:
    raw = sanitize_html_text(text)
    if len(raw) < 60:
        return None
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw) if p.strip()]
    if not parts:
        return None
    lines = parts[:4]
    if len(lines) < 2 and len(raw) > 80:
        mid = len(raw) // 2
        lines = [raw[:mid].strip(), raw[mid:].strip()]
    lines = [line[:140].strip() for line in lines if line.strip()]
    if not (2 <= len(lines) <= 4):
        return None
    return "\n".join(lines)


async def fetch_quote(session: aiohttp.ClientSession) -> str:
    random.shuffle(QUOTE_RSS_SOURCES)
    for url in QUOTE_RSS_SOURCES:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    continue
                xml = await resp.text()
            parsed = feedparser.parse(xml)
            entries = parsed.entries or []
            random.shuffle(entries)
            for entry in entries[:12]:
                text = f"{entry.get('title', '')}. {entry.get('summary', '')}"
                quote = text_to_quote(text)
                if quote:
                    return quote
        except Exception as exc:
            logger.warning("Quote RSS source failed (%s): %s", url, exc)
    return "Музика збирає думки в ритм.\nНехай цей настрій тримає день."


async def get_spotify_token(session: aiohttp.ClientSession) -> str:
    payload = {"grant_type": "client_credentials"}
    auth = aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data=payload,
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Spotify token was not received")
    return token


async def fetch_spotify_seed_data(
    session: aiohttp.ClientSession,
    genre: str,
    language: str,
) -> dict[str, str]:
    token = await get_spotify_token(session)
    query = f"{genre} {language} music"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": query, "type": "track", "limit": "20", "market": "UA"}
    async with session.get(
        "https://api.spotify.com/v1/search",
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    items = data.get("tracks", {}).get("items", [])
    if not items:
        raise RuntimeError("Spotify did not return tracks")
    item = random.choice(items)
    track_name = item.get("name", "")
    artists = item.get("artists") or []
    artist_name = artists[0].get("name", "") if artists else ""
    spotify_genre = genre

    artist_id = artists[0].get("id") if artists else None
    if artist_id:
        async with session.get(
            f"https://api.spotify.com/v1/artists/{artist_id}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                adata = await resp.json()
                genres = adata.get("genres") or []
                if genres:
                    spotify_genre = genres[0]

    mood_map = {
        "Поп": "energetic",
        "Рок": "powerful",
        "Хіп-хоп": "groovy",
        "Електроніка": "dreamy",
    }
    mood = mood_map.get(genre, "vibe")
    if track_name and artist_name:
        return {"track": track_name, "artist": artist_name, "genre": spotify_genre, "mood": mood}
    raise RuntimeError("Spotify result is incomplete")


def extract_youtube_candidates(query: str, limit: int = 8) -> list[dict[str, str]]:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    entries = info.get("entries") or []
    results: list[dict[str, str]] = []
    for entry in entries:
        video_id = entry.get("id")
        title = entry.get("title") or "Unknown"
        if not video_id:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        results.append({"title": title, "url": url})
    return results


def download_mp3_from_youtube(url: str, output_dir: str) -> str:
    ffmpeg_path = get_ffmpeg_exe()
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "ffmpeg_location": ffmpeg_path,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        downloaded = Path(ydl.prepare_filename(info))
    mp3_path = downloaded.with_suffix(".mp3")
    if not mp3_path.exists():
        candidates = sorted(Path(output_dir).glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError("MP3 file was not created")
        mp3_path = candidates[0]
    return str(mp3_path)


async def search_and_prepare_tracks(seed: dict[str, str]) -> list[dict[str, str]]:
    queries = [
        f"{seed['artist']} {seed['track']} {seed['genre']} official audio",
        f"{seed['artist']} {seed['track']} official audio",
        "popular music hits official audio",
    ]

    found: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for query in queries:
        candidates = await asyncio.to_thread(extract_youtube_candidates, query, 10)
        for item in candidates:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            found.append(item)
            if len(found) >= 2:
                return found[:2]
    return found[:2]


async def prepare_post_assets(genre: str, language: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        seed = await fetch_spotify_seed_data(session, genre, language)
        tracks = await search_and_prepare_tracks(seed)
        if len(tracks) < 2:
            raise RuntimeError("Не вдалося знайти 2 треки з YouTube.")

        img_query = f"music {genre} {seed['mood']}"
        headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
        params = {"query": img_query, "orientation": "landscape"}
        async with session.get(
            "https://api.unsplash.com/photos/random",
            params=params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            img_data = await resp.json()
        image_url = img_data.get("urls", {}).get("small") or img_data.get("urls", {}).get("regular")
        if not image_url:
            raise RuntimeError("Unsplash не повернув зображення.")

        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            image_bytes = await resp.read()

        quote = await fetch_quote(session)

    temp_dir = tempfile.mkdtemp(prefix="musicbot_")
    image_path = Path(temp_dir) / "cover.jpg"
    image_path.write_bytes(image_bytes)

    prepared_tracks: list[dict[str, str]] = []
    for idx, track in enumerate(tracks, start=1):
        mp3_path = await asyncio.to_thread(download_mp3_from_youtube, track["url"], temp_dir)
        prepared_tracks.append(
            {
                "title": track["title"],
                "url": track["url"],
                "file_path": mp3_path,
                "label": f"🎵 Трек {idx}",
            }
        )

    caption_tracks = "\n".join([item["label"] for item in prepared_tracks])
    caption = f"<blockquote>{html.escape(quote)}</blockquote>\n\n{caption_tracks}"

    return {
        "image_path": str(image_path),
        "tracks": prepared_tracks,
        "caption": caption,
        "temp_dir": temp_dir,
    }


async def send_post_preview(message: Message, payload: dict[str, Any]) -> None:
    await message.answer_photo(
        FSInputFile(payload["image_path"]),
        caption=payload["caption"],
        reply_markup=publish_menu(),
    )
    for track in payload["tracks"]:
        await message.answer_audio(
            audio=FSInputFile(track["file_path"]),
            title=track["title"][:64],
        )


async def publish_post(bot: Bot, payload: dict[str, Any]) -> None:
    await bot.send_photo(chat_id=CHANNEL_ID, photo=FSInputFile(payload["image_path"]), caption=payload["caption"])
    for track in payload["tracks"]:
        await bot.send_audio(chat_id=CHANNEL_ID, audio=FSInputFile(track["file_path"]), title=track["title"][:64])


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Доступ заборонено.", reply_markup=ReplyKeyboardRemove())
        return
    await state.clear()
    await message.answer("Вітаю! Оберіть дію:", reply_markup=main_menu())


@router.message(F.text == "1️⃣ Новий пост")
async def new_post(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.clear()
    await state.set_state(PostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_menu())


@router.message(PostStates.choosing_genre, F.text.in_(GENRES))
async def choose_genre(message: Message, state: FSMContext) -> None:
    await state.update_data(genre=message.text)
    await state.set_state(PostStates.choosing_language)
    await message.answer("Оберіть мову:", reply_markup=language_menu())


@router.message(PostStates.choosing_language, F.text.in_(LANGUAGES))
async def choose_language(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    genre = data.get("genre")
    language = message.text
    await message.answer("Готую превʼю поста, зачекайте...", reply_markup=ReplyKeyboardRemove())

    payload: dict[str, Any] | None = None
    try:
        payload = await prepare_post_assets(genre=genre, language=language)
        await send_post_preview(message, payload)
        await state.update_data(post_payload=payload)
        await state.set_state(PostStates.preview_post)
    except Exception as exc:
        logger.exception("Post preparation failed: %s", exc)
        if payload:
            await cleanup_paths(
                [payload.get("image_path", "")]
                + [track.get("file_path", "") for track in payload.get("tracks", [])]
            )
            temp_dir = payload.get("temp_dir")
            if temp_dir:
                Path(temp_dir).rmdir()
        await state.clear()
        await message.answer(
            "Не вдалося підготувати пост. Спробуйте ще раз пізніше.",
            reply_markup=main_menu(),
        )


@router.message(PostStates.preview_post, F.text == "Опублікувати")
async def publish_post_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    payload = data.get("post_payload")
    if not payload:
        await state.clear()
        await message.answer("Немає підготовленого поста.", reply_markup=main_menu())
        return

    try:
        await publish_post(bot, payload)
        await message.answer("Пост опубліковано ✅", reply_markup=main_menu())
    except TelegramAPIError as exc:
        logger.exception("Publishing post failed: %s", exc)
        await message.answer("Помилка публікації поста.", reply_markup=main_menu())
    finally:
        await cleanup_paths(
            [payload.get("image_path", "")]
            + [track.get("file_path", "") for track in payload.get("tracks", [])]
        )
        temp_dir = payload.get("temp_dir")
        if temp_dir:
            try:
                Path(temp_dir).rmdir()
            except Exception:
                pass
        await state.clear()


@router.message(F.text == "2️⃣ Опитування")
async def polls_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.clear()
    await state.set_state(PollStates.choosing_template)
    await message.answer("Оберіть шаблон опитування:", reply_markup=polls_menu())


@router.message(PollStates.choosing_template, F.text.in_(list(POLL_TEMPLATES.keys())))
async def poll_template_selected(message: Message, state: FSMContext) -> None:
    template = POLL_TEMPLATES[message.text]
    await state.update_data(selected_poll=template)
    await state.set_state(PollStates.preview_poll)
    options_text = "\n".join([f"• {opt}" for opt in template["options"]])
    await message.answer(
        f"Превʼю опитування:\n\n{template['question']}\n{options_text}",
        reply_markup=publish_menu(),
    )


@router.message(PollStates.preview_poll, F.text == "Опублікувати")
async def publish_poll_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    selected_poll = data.get("selected_poll")
    if not selected_poll:
        await state.clear()
        await message.answer("Немає підготовленого опитування.", reply_markup=main_menu())
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=selected_poll["question"],
            options=selected_poll["options"],
            is_anonymous=True,
        )
        await message.answer("Опитування опубліковано ✅", reply_markup=main_menu())
    except TelegramAPIError as exc:
        logger.exception("Publishing poll failed: %s", exc)
        await message.answer("Помилка публікації опитування.", reply_markup=main_menu())
    finally:
        await state.clear()


@router.message(F.text == "3️⃣ Скасувати")
@router.message(F.text == "Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    payload = data.get("post_payload")
    if payload:
        await cleanup_paths(
            [payload.get("image_path", "")]
            + [track.get("file_path", "") for track in payload.get("tracks", [])]
        )
        temp_dir = payload.get("temp_dir")
        if temp_dir:
            try:
                Path(temp_dir).rmdir()
            except Exception:
                pass

    await state.clear()
    await message.answer("Скасовано.", reply_markup=main_menu())


@router.message()
async def fallback(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current in {PostStates.choosing_genre.state, PostStates.choosing_language.state}:
        await message.answer("Будь ласка, використайте кнопки для вибору.")
        return
    if current in {PostStates.preview_post.state, PollStates.preview_poll.state}:
        await message.answer("Натисніть 'Опублікувати' або '3️⃣ Скасувати'.")
        return
    await message.answer("Оберіть дію з меню.", reply_markup=main_menu())


async def on_startup(bot: Bot) -> None:
    logger.info("Bot started as @%s", (await bot.get_me()).username)


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.startup.register(on_startup)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
