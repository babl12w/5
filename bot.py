import asyncio
import html
import logging
import os
import random
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
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
    CallbackQuery,
)
from yt_dlp import YoutubeDL

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip())
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
SPOTIFY_CLIENT_ID = os.getenv("spotify_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("spotify_CLIENT_SECRET", "").strip()
YOUTUBE_API_KEY = os.getenv("YouTube_API_KEY", "").strip()

REQUIRED_ENV = {
    "BOT_TOKEN": BOT_TOKEN,
    "ADMIN_ID": str(ADMIN_ID),
    "CHANNEL_ID": CHANNEL_ID,
    "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
    "spotify_CLIENT_ID": SPOTIFY_CLIENT_ID,
    "spotify_CLIENT_SECRET": SPOTIFY_CLIENT_SECRET,
    "YouTube_API_KEY": YOUTUBE_API_KEY,
}

missing = [name for name, value in REQUIRED_ENV.items() if not value or value == "0"]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music_bot")

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Новий пост")],
        [KeyboardButton(text="Опитування")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
)

GENRES = ["Поп", "Рок", "Хіп-хоп", "Електронна"]
LANGUAGES = ["Українська", "Російська", "Польська"]

POLL_TEMPLATES = [
    {
        "name": "Опитування 1",
        "question": "Що зараз найкраще під ваш настрій?",
        "options": ["Спокійний вайб", "Танцювальний біт", "Рок-енергія", "Щось нове"],
    },
    {
        "name": "Опитування 2",
        "question": "Коли вам найкраще слухається музика?",
        "options": ["Зранку", "Вдень", "Увечері", "Пізно вночі"],
    },
    {
        "name": "Опитування 3",
        "question": "Яку мову треків обираєте сьогодні?",
        "options": ["Українську", "Російську", "Польську", "Без різниці"],
    },
]

LANGUAGE_QUERY = {
    "Українська": "ukrainian",
    "Російська": "russian",
    "Польська": "polish",
}

GENRE_QUERY = {
    "Поп": "pop",
    "Рок": "rock",
    "Хіп-хоп": "hip hop",
    "Електронна": "electronic",
}

QUOTE_RSS_URL = "https://www.ukrlib.com.ua/rss/index.xml"


class FlowState(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    post_preview_ready = State()
    choosing_poll = State()
    poll_preview_ready = State()


@dataclass
class PreparedTrack:
    title: str
    artist: str
    genre: str
    mp3_path: Path


@dataclass
class PreparedPost:
    genre: str
    language: str
    quote: str
    image_path: Path
    tracks: list[PreparedTrack]


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    async with session.get(
        url,
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        response.raise_for_status()
        return await response.json()


async def fetch_text(session: aiohttp.ClientSession, url: str, timeout: int = 30) -> str:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
        response.raise_for_status()
        return await response.text()


async def download_file(session: aiohttp.ClientSession, url: str, suffix: str) -> Path:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as response:
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            while True:
                chunk = await response.content.read(65536)
                if not chunk:
                    break
                temp.write(chunk)
            return Path(temp.name)


def genre_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=genre, callback_data=f"genre:{genre}")] for genre in GENRES
        ]
    )


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=lang, callback_data=f"lang:{lang}")] for lang in LANGUAGES
        ]
    )


def publish_cancel_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Опублікувати", callback_data=f"{prefix}:publish"),
                InlineKeyboardButton(text="Скасувати", callback_data=f"{prefix}:cancel"),
            ]
        ]
    )


def poll_templates_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item["name"], callback_data=f"poll:{idx}")]
            for idx, item in enumerate(POLL_TEMPLATES)
        ]
    )


async def get_spotify_token(session: aiohttp.ClientSession) -> str:
    auth = aiohttp.BasicAuth(login=SPOTIFY_CLIENT_ID, password=SPOTIFY_CLIENT_SECRET)
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        data = await response.json()
        token = data.get("access_token", "")
        if not token:
            raise RuntimeError("Spotify token was not returned")
        return token


async def spotify_search_tracks(
    session: aiohttp.ClientSession,
    token: str,
    query: str,
    limit: int = 8,
) -> list[dict[str, str]]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": query,
        "type": "track",
        "limit": str(limit),
        "market": "PL",
    }
    data = await fetch_json(session, "https://api.spotify.com/v1/search", params=params, headers=headers)
    items = data.get("tracks", {}).get("items", [])
    prepared: list[dict[str, str]] = []
    for item in items:
        artists = item.get("artists", [])
        artist_name = artists[0].get("name", "Unknown") if artists else "Unknown"
        prepared.append(
            {
                "title": item.get("name", "Unknown"),
                "artist": artist_name,
                "genre": "mood",
            }
        )
    return prepared


async def youtube_search_video(
    session: aiohttp.ClientSession,
    query: str,
) -> str | None:
    params = {
        "key": YOUTUBE_API_KEY,
        "part": "snippet",
        "type": "video",
        "maxResults": "1",
        "q": query,
        "videoEmbeddable": "true",
        "safeSearch": "moderate",
    }
    data = await fetch_json(session, "https://www.googleapis.com/youtube/v3/search", params=params)
    items = data.get("items", [])
    if not items:
        return None
    video_id = items[0].get("id", {}).get("videoId")
    if not video_id:
        return None
    return f"https://www.youtube.com/watch?v={video_id}"


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^\w\-\.]+", "_", name, flags=re.UNICODE)


def download_youtube_audio_mp3(url: str, output_dir: Path, track_stub: str) -> Path | None:
    output_template = output_dir / f"{sanitize_filename(track_stub)}_%(id)s.%(ext)s"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_template),
        "quiet": True,
        "noprogress": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded = Path(ydl.prepare_filename(info))
            mp3_path = downloaded.with_suffix(".mp3")
            if mp3_path.exists():
                return mp3_path
            candidates = list(output_dir.glob(f"{sanitize_filename(track_stub)}_*.mp3"))
            if candidates:
                return candidates[0]
            return None
    except Exception:
        logger.exception("Failed to download audio from YouTube")
        return None


async def choose_tracks_with_fallback(
    session: aiohttp.ClientSession,
    language: str,
    genre: str,
) -> list[dict[str, str]]:
    token = await get_spotify_token(session)
    lang_query = LANGUAGE_QUERY.get(language, "")
    genre_query = GENRE_QUERY.get(genre, "")

    query_plan = [
        f"{genre_query} {lang_query}",
        f"{lang_query}",
        "top hits",
    ]

    best: list[dict[str, str]] = []
    for query in query_plan:
        tracks = await spotify_search_tracks(session, token, query)
        if len(tracks) >= 2:
            return tracks
        if len(tracks) > len(best):
            best = tracks
    return best


async def notify_admin_not_enough_tracks(bot: Bot, genre: str, language: str) -> None:
    try:
        await bot.send_message(
            ADMIN_ID,
            f"Вибачте, не вдалося знайти 2 треки для жанру <b>{html.escape(genre)}</b> і мови <b>{html.escape(language)}</b>.",
        )
    except Exception:
        logger.exception("Failed to notify admin")


async def fetch_unsplash_image(session: aiohttp.ClientSession, genre: str) -> Path:
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {
        "query": f"music {genre} mood",
        "orientation": "landscape",
        "content_filter": "high",
    }
    data = await fetch_json(session, "https://api.unsplash.com/photos/random", params=params, headers=headers)
    image_url = data.get("urls", {}).get("small") or data.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash did not return image URL")
    return await download_file(session, image_url, ".jpg")


def parse_quote_from_rss(xml_text: str) -> str | None:
    root = ElementTree.fromstring(xml_text)
    descriptions: list[str] = []
    for item in root.findall(".//item"):
        desc = item.findtext("description") or item.findtext("title")
        if desc:
            cleaned = re.sub(r"<[^>]+>", "", desc).strip()
            if len(cleaned) > 40:
                descriptions.append(cleaned)
    if not descriptions:
        return None
    source = random.choice(descriptions)
    pieces = [part.strip() for part in re.split(r"[.!?]\s+", source) if part.strip()]
    if len(pieces) < 2:
        pieces = [source]
    selected_lines = pieces[:4]
    if len(selected_lines) < 2:
        selected_lines = [source[:120], source[120:240]] if len(source) > 120 else [source, "Збережи цей момент у музиці."]
    return "\n".join(line[:120] for line in selected_lines[:4])


async def fetch_quote(session: aiohttp.ClientSession) -> str:
    try:
        xml_text = await fetch_text(session, QUOTE_RSS_URL)
        quote = parse_quote_from_rss(xml_text)
        if quote:
            lines = [line for line in quote.splitlines() if line.strip()]
            if 2 <= len(lines) <= 4:
                return quote
            if len(lines) > 4:
                return "\n".join(lines[:4])
            return "\n".join(lines + ["Нехай музика говорить за тебе."])
    except Exception:
        logger.exception("Failed to load quote from RSS")
    fallback_quotes = [
        "Іноді найкращі слова — це ноти.\nСлухай серцем,\nдихай ритмом.",
        "Тиша теж співає,\nколи в душі є музика.\nЗупинись і відчуй момент.",
        "Кожна мелодія — це шлях.\nЗроби крок назустріч світлу,\nі ритм підкаже напрям.",
    ]
    return random.choice(fallback_quotes)


async def prepare_post_assets(bot: Bot, genre: str, language: str) -> PreparedPost | None:
    async with aiohttp.ClientSession() as session:
        spotify_tracks = await choose_tracks_with_fallback(session, language, genre)
        if len(spotify_tracks) < 2:
            await notify_admin_not_enough_tracks(bot, genre, language)
            return None

        output_dir = Path(tempfile.mkdtemp(prefix="tg_music_"))
        prepared_tracks: list[PreparedTrack] = []
        used_titles: set[str] = set()

        for item in spotify_tracks:
            if len(prepared_tracks) >= 2:
                break
            title = item["title"].strip()
            artist = item["artist"].strip()
            unique_key = f"{title}-{artist}".lower()
            if unique_key in used_titles:
                continue
            used_titles.add(unique_key)
            query = f"{title} {artist} official audio"
            video_url = await youtube_search_video(session, query)
            if not video_url:
                continue
            track_stub = f"{uuid.uuid4().hex}_{title}_{artist}"[:70]
            mp3_path = await asyncio.to_thread(download_youtube_audio_mp3, video_url, output_dir, track_stub)
            if not mp3_path or not mp3_path.exists():
                continue
            prepared_tracks.append(PreparedTrack(title=title, artist=artist, genre=item.get("genre", "mood"), mp3_path=mp3_path))

        if len(prepared_tracks) < 2:
            await notify_admin_not_enough_tracks(bot, genre, language)
            for existing in output_dir.glob("*"):
                existing.unlink(missing_ok=True)
            output_dir.rmdir()
            return None

        image_path = await fetch_unsplash_image(session, genre)
        quote = await fetch_quote(session)

    return PreparedPost(genre=genre, language=language, quote=quote, image_path=image_path, tracks=prepared_tracks)


async def cleanup_post(post: PreparedPost | None) -> None:
    if not post:
        return
    try:
        if post.image_path.exists():
            post.image_path.unlink(missing_ok=True)
        parent = None
        for track in post.tracks:
            if track.mp3_path.exists():
                track.mp3_path.unlink(missing_ok=True)
            parent = track.mp3_path.parent
        if parent and parent.exists():
            for item in parent.glob("*"):
                item.unlink(missing_ok=True)
            parent.rmdir()
    except Exception:
        logger.exception("Failed to clean temporary files")


async def show_main_menu(message: Message, text: str = "Оберіть дію:") -> None:
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await show_main_menu(message, "Вітаю! Оберіть дію:")


async def cancel_everything(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    post = data.get("prepared_post")
    if isinstance(post, PreparedPost):
        await cleanup_post(post)
    await state.clear()
    await show_main_menu(message, "Скасовано. Повертаю в головне меню.")


async def new_post_entry(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FlowState.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_keyboard())


async def genre_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    genre = callback.data.split(":", 1)[1]
    await state.update_data(genre=genre)
    await state.set_state(FlowState.choosing_language)
    await callback.message.edit_text(f"Жанр: <b>{html.escape(genre)}</b>\nОберіть мову:", reply_markup=language_keyboard())
    await callback.answer()


async def language_chosen(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    language = callback.data.split(":", 1)[1]
    data = await state.get_data()
    genre = data.get("genre")
    if not genre:
        await callback.answer("Спочатку оберіть жанр", show_alert=True)
        return

    await callback.message.edit_text(
        f"Жанр: <b>{html.escape(genre)}</b>\nМова: <b>{html.escape(language)}</b>\nГотую превʼю...",
    )

    prepared_post = await prepare_post_assets(bot, genre, language)
    if not prepared_post:
        await state.clear()
        await callback.message.answer(
            "Не вдалося підготувати пост. Спробуйте інший жанр або мову.",
            reply_markup=MAIN_KEYBOARD,
        )
        await callback.answer()
        return

    await state.update_data(prepared_post=prepared_post)
    await state.set_state(FlowState.post_preview_ready)

    await callback.message.answer_photo(
        FSInputFile(prepared_post.image_path),
        caption=prepared_post.quote,
    )
    for track in prepared_post.tracks:
        await callback.message.answer_audio(
            audio=FSInputFile(track.mp3_path),
            title=track.title,
            performer=track.artist,
        )

    await callback.message.answer(
        "Превʼю готове. Опублікувати в канал?",
        reply_markup=publish_cancel_keyboard("post"),
    )
    await callback.answer()


async def publish_or_cancel_post(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    post = data.get("prepared_post")

    if not isinstance(post, PreparedPost):
        await state.clear()
        await callback.message.answer("Сесія завершена. Створіть новий пост.", reply_markup=MAIN_KEYBOARD)
        await callback.answer()
        return

    if action == "cancel":
        await cleanup_post(post)
        await state.clear()
        await callback.message.answer("Публікацію скасовано.", reply_markup=MAIN_KEYBOARD)
        await callback.answer()
        return

    try:
        await bot.send_photo(CHANNEL_ID, FSInputFile(post.image_path), caption=post.quote)
        for track in post.tracks:
            await bot.send_audio(
                CHANNEL_ID,
                audio=FSInputFile(track.mp3_path),
                title=track.title,
                performer=track.artist,
            )
        await callback.message.answer("Опубліковано.", reply_markup=MAIN_KEYBOARD)
    except TelegramBadRequest:
        logger.exception("Telegram rejected publish request")
        await callback.message.answer("Не вдалося опублікувати. Перевірте права бота в каналі.", reply_markup=MAIN_KEYBOARD)
    except Exception:
        logger.exception("Unexpected error during post publish")
        await callback.message.answer("Сталася помилка під час публікації.", reply_markup=MAIN_KEYBOARD)
    finally:
        await cleanup_post(post)
        await state.clear()
        await callback.answer()


async def poll_entry(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(FlowState.choosing_poll)
    await message.answer("Оберіть заготовку опитування:", reply_markup=poll_templates_keyboard())


async def poll_template_selected(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    template = POLL_TEMPLATES[idx]
    await state.update_data(poll_idx=idx)
    await state.set_state(FlowState.poll_preview_ready)
    options_preview = "\n".join([f"• {html.escape(opt)}" for opt in template["options"]])
    await callback.message.edit_text(
        f"<b>{html.escape(template['question'])}</b>\n{options_preview}\n\nОпублікувати?",
        reply_markup=publish_cancel_keyboard("poll"),
    )
    await callback.answer()


async def publish_or_cancel_poll(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    idx = data.get("poll_idx")
    if idx is None:
        await state.clear()
        await callback.message.answer("Сесія завершена. Створіть нове опитування.", reply_markup=MAIN_KEYBOARD)
        await callback.answer()
        return

    template = POLL_TEMPLATES[int(idx)]

    if action == "cancel":
        await state.clear()
        await callback.message.answer("Публікацію опитування скасовано.", reply_markup=MAIN_KEYBOARD)
        await callback.answer()
        return

    try:
        await bot.send_poll(
            CHANNEL_ID,
            question=template["question"],
            options=template["options"],
            is_anonymous=True,
            type="regular",
        )
        await callback.message.answer("Опитування опубліковано.", reply_markup=MAIN_KEYBOARD)
    except Exception:
        logger.exception("Failed to publish poll")
        await callback.message.answer("Не вдалося опублікувати опитування.", reply_markup=MAIN_KEYBOARD)
    finally:
        await state.clear()
        await callback.answer()


async def fallback_text_handler(message: Message) -> None:
    await show_main_menu(message, "Скористайтесь кнопками меню нижче.")


async def error_handler(event, exception: Exception) -> bool:
    logger.exception("Unhandled error: %s", exception)
    return True


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.errors.register(error_handler)

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cancel_everything, F.text == "Скасувати")
    dp.message.register(new_post_entry, F.text == "Новий пост")
    dp.message.register(poll_entry, F.text == "Опитування")

    dp.callback_query.register(genre_chosen, FlowState.choosing_genre, F.data.startswith("genre:"))
    dp.callback_query.register(language_chosen, FlowState.choosing_language, F.data.startswith("lang:"))
    dp.callback_query.register(publish_or_cancel_post, FlowState.post_preview_ready, F.data.startswith("post:"))

    dp.callback_query.register(poll_template_selected, FlowState.choosing_poll, F.data.startswith("poll:"))
    dp.callback_query.register(publish_or_cancel_poll, FlowState.poll_preview_ready, F.data.startswith("poll:"))

    dp.message.register(fallback_text_handler)

    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
