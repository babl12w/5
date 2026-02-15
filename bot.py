import asyncio
import logging
import os
import random
import re
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
SPOTIFY_CLIENT_ID = os.getenv("spotify_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("spotify_CLIENT_SECRET", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not UNSPLASH_ACCESS_KEY:
    raise RuntimeError("UNSPLASH_ACCESS_KEY is required")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is required")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required")
if not SPOTIFY_CLIENT_ID:
    raise RuntimeError("spotify_CLIENT_ID is required")
if not SPOTIFY_CLIENT_SECRET:
    raise RuntimeError("spotify_CLIENT_SECRET is required")

GENRES = ["Pop", "Rock", "Rap", "Electronic"]
LANGUAGES = ["Українська", "Російська", "Польська"]
POLL_TEMPLATES: dict[str, dict[str, list[str] | str]] = {
    "Опитування 1": {
        "question": "Який жанр сьогодні в твоєму плейлисті?",
        "options": ["Pop", "Rock", "Rap", "Electronic"],
    },
    "Опитування 2": {
        "question": "Коли найчастіше слухаєш музику?",
        "options": ["Вранці", "Вдень", "Увечері", "Вночі"],
    },
    "Опитування 3": {
        "question": "Який настрій має твоя улюблена пісня?",
        "options": ["Енергійний", "Романтичний", "Меланхолійний", "Надихаючий"],
    },
}

RSS_SOURCES = [
    "https://www.ukrinform.ua/rss/block-lastnews",
    "https://www.radiosvoboda.org/api/zrqiteuuir",
]

LANGUAGE_HINTS = {
    "Українська": "ukrainian",
    "Російська": "russian",
    "Польська": "polish",
}


class NewPostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    confirm = State()


class PollStates(StatesGroup):
    choosing_poll = State()
    confirm = State()


def kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
        resize_keyboard=True,
    )


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return kb([["Новий пост"], ["Опитування"], ["Скасувати"]])


def genres_keyboard() -> ReplyKeyboardMarkup:
    return kb([["Pop", "Rock"], ["Rap", "Electronic"], ["Скасувати"]])


def languages_keyboard() -> ReplyKeyboardMarkup:
    return kb([["Українська", "Російська"], ["Польська"], ["Скасувати"]])


def publish_cancel_keyboard() -> ReplyKeyboardMarkup:
    return kb([["Опублікувати"], ["Скасувати"]])


def polls_keyboard() -> ReplyKeyboardMarkup:
    return kb([["Опитування 1"], ["Опитування 2"], ["Опитування 3"], ["Скасувати"]])


def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", "", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def fetch_spotify_token(session: aiohttp.ClientSession) -> str:
    auth = aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        payload = await response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Spotify token is missing")
    return token


async def spotify_search_tracks(
    session: aiohttp.ClientSession,
    token: str,
    query: str,
    limit: int = 12,
    offset: int = 0,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": query,
        "type": "track",
        "limit": str(limit),
        "offset": str(offset),
    }
    async with session.get(
        "https://api.spotify.com/v1/search",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        payload = await response.json()
    return payload.get("tracks", {}).get("items", [])


async def fetch_artist_genres(
    session: aiohttp.ClientSession,
    token: str,
    artist_ids: list[str],
) -> dict[str, list[str]]:
    if not artist_ids:
        return {}
    headers = {"Authorization": f"Bearer {token}"}
    params = {"ids": ",".join(artist_ids[:50])}
    async with session.get(
        "https://api.spotify.com/v1/artists",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        payload = await response.json()
    result: dict[str, list[str]] = {}
    for artist in payload.get("artists", []):
        if artist and artist.get("id"):
            result[artist["id"]] = artist.get("genres", [])
    return result


async def gather_tracks_with_fallback(
    session: aiohttp.ClientSession,
    genre: str,
    language: str,
) -> list[dict[str, str]]:
    token = await fetch_spotify_token(session)
    lang_hint = LANGUAGE_HINTS.get(language, "music")

    queries = [
        f'{genre} {lang_hint} music',
        f'{lang_hint} music',
        "popular hits",
    ]
    offsets = [0, 0, random.randint(1, 30)]

    for query, offset in zip(queries, offsets):
        raw_tracks = await spotify_search_tracks(session, token, query, limit=20, offset=offset)
        if not raw_tracks:
            continue

        artist_ids: list[str] = []
        for item in raw_tracks:
            artists = item.get("artists", [])
            if artists and artists[0].get("id"):
                artist_ids.append(artists[0]["id"])
        genres_map = await fetch_artist_genres(session, token, artist_ids)

        collected: list[dict[str, str]] = []
        seen: set[str] = set()

        for item in raw_tracks:
            track_name = item.get("name")
            artists = item.get("artists", [])
            if not track_name or not artists:
                continue
            artist_name = artists[0].get("name") or "Unknown"
            artist_id = artists[0].get("id")
            track_genres = genres_map.get(artist_id, []) if artist_id else []
            mood_or_genre = track_genres[0] if track_genres else genre
            key = f"{track_name.lower()}::{artist_name.lower()}"
            if key in seen:
                continue
            seen.add(key)
            collected.append(
                {
                    "title": track_name,
                    "artist": artist_name,
                    "genre": mood_or_genre,
                }
            )
            if len(collected) == 2:
                return collected

        if len(collected) >= 2:
            return collected[:2]

    return []


async def download_unsplash_vertical_photo(session: aiohttp.ClientSession) -> Path:
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {
        "query": "music vibe aesthetic",
        "orientation": "portrait",
        "content_filter": "high",
    }
    async with session.get(
        "https://api.unsplash.com/photos/random",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        data = await response.json()

    image_url = data.get("urls", {}).get("small") or data.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash image not found")

    async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=60)) as response:
        response.raise_for_status()
        content = await response.read()

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    with temp_file:
        temp_file.write(content)
    return Path(temp_file.name)


async def fetch_ukrainian_quote(session: aiohttp.ClientSession) -> str:
    for source in RSS_SOURCES:
        try:
            async with session.get(source, timeout=aiohttp.ClientTimeout(total=20)) as response:
                response.raise_for_status()
                xml_text = await response.text()
            feed = feedparser.parse(xml_text)
            entries = feed.entries or []
            for entry in entries:
                candidate = clean_text(
                    getattr(entry, "summary", "") or getattr(entry, "description", "") or getattr(entry, "title", "")
                )
                if len(candidate) < 40:
                    continue
                candidate = candidate[:360].strip(" .,-")
                words = candidate.split()
                if len(words) < 8:
                    continue
                lines_count = random.randint(2, 4)
                chunk = max(4, len(words) // lines_count)
                lines: list[str] = []
                index = 0
                for _ in range(lines_count):
                    if index >= len(words):
                        break
                    lines.append(" ".join(words[index : index + chunk]))
                    index += chunk
                lines = [line for line in lines if line]
                if 2 <= len(lines) <= 4:
                    return "🎵 <b>Цитата дня</b>\n\n" + "\n".join(f"<i>{line}</i>" for line in lines)
        except Exception:
            continue

    return (
        "🎵 <b>Цитата дня</b>\n\n"
        "<i>Музика народжується в тиші.</i>\n"
        "<i>Вона проходить крізь серце.</i>\n"
        "<i>І залишає світло в думках.</i>"
    )


async def run_yt_dlp_extract(track_query: str, out_prefix: Path) -> Path | None:
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "192K",
        "-o",
        f"{out_prefix}.%(ext)s",
        f"ytsearch5:{track_query}",
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    if process.returncode != 0:
        logging.error("yt-dlp failed for '%s': %s", track_query, stderr.decode(errors="ignore"))
        return None

    matches = sorted(out_prefix.parent.glob(f"{out_prefix.name}*.mp3"))
    if matches:
        return matches[0]
    return None


async def get_two_mp3_tracks(tracks: list[dict[str, str]]) -> list[tuple[Path, dict[str, str]]]:
    temp_dir = Path(tempfile.mkdtemp(prefix="music_bot_"))
    result: list[tuple[Path, dict[str, str]]] = []

    for idx, track in enumerate(tracks, start=1):
        query = f"{track['title']} {track['artist']} {track['genre']} official audio"
        path = await run_yt_dlp_extract(query, temp_dir / f"track_{idx}")
        if path:
            result.append((path, track))

    return result


async def publish_music_post(bot: Bot, genre: str, language: str) -> bool:
    image_path: Path | None = None
    audio_items: list[tuple[Path, dict[str, str]]] = []

    try:
        async with aiohttp.ClientSession() as session:
            spotify_tracks = await gather_tracks_with_fallback(session, genre, language)
            if len(spotify_tracks) < 2:
                await bot.send_message(ADMIN_ID, "Не вдалося знайти достатню кількість треків.")
                return False

            image_path = await download_unsplash_vertical_photo(session)
            quote = await fetch_ukrainian_quote(session)

            audio_items = await get_two_mp3_tracks(spotify_tracks)
            if len(audio_items) < 2:
                await bot.send_message(ADMIN_ID, "Не вдалося знайти достатню кількість треків.")
                return False

            await bot.send_photo(CHANNEL_ID, FSInputFile(image_path), caption=quote)

            for audio_path, track in audio_items[:2]:
                await bot.send_audio(
                    CHANNEL_ID,
                    audio=FSInputFile(audio_path),
                    title=track["title"],
                    performer=track["artist"],
                )

        return True
    finally:
        for audio_path, _ in audio_items:
            try:
                if audio_path.exists():
                    audio_path.unlink()
            except Exception:
                pass

        if image_path:
            try:
                if image_path.exists():
                    image_path.unlink()
            except Exception:
                pass


def selected_poll_from_text(text: str) -> dict[str, Any] | None:
    return POLL_TEMPLATES.get(text)


async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Вітаю! Оберіть дію:", reply_markup=main_menu_keyboard())


async def action_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Скасовано. Повернув у головне меню.", reply_markup=main_menu_keyboard())


async def action_new_post(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewPostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genres_keyboard())


async def choose_genre(message: Message, state: FSMContext) -> None:
    if message.text not in GENRES:
        await message.answer("Оберіть жанр кнопкою нижче.", reply_markup=genres_keyboard())
        return

    await state.update_data(genre=message.text)
    await state.set_state(NewPostStates.choosing_language)
    await message.answer("Оберіть мову:", reply_markup=languages_keyboard())


async def choose_language(message: Message, state: FSMContext) -> None:
    if message.text not in LANGUAGES:
        await message.answer("Оберіть мову кнопкою нижче.", reply_markup=languages_keyboard())
        return

    await state.update_data(language=message.text)
    await state.set_state(NewPostStates.confirm)
    await message.answer("Готово до публікації.", reply_markup=publish_cancel_keyboard())


async def confirm_publish_post(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text != "Опублікувати":
        await message.answer("Натисніть «Опублікувати» або «Скасувати».", reply_markup=publish_cancel_keyboard())
        return

    data = await state.get_data()
    genre = data.get("genre")
    language = data.get("language")
    if not genre or not language:
        await state.clear()
        await message.answer("Сесію втрачено. Спробуйте ще раз.", reply_markup=main_menu_keyboard())
        return

    await message.answer("Публікую пост у канал...", reply_markup=main_menu_keyboard())
    await state.clear()

    ok = await publish_music_post(bot, genre, language)
    if ok:
        await message.answer("Пост успішно опубліковано ✅", reply_markup=main_menu_keyboard())
    else:
        await message.answer("Не вдалося опублікувати пост.", reply_markup=main_menu_keyboard())


async def action_poll(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PollStates.choosing_poll)
    await message.answer("Оберіть опитування:", reply_markup=polls_keyboard())


async def choose_poll(message: Message, state: FSMContext) -> None:
    poll = selected_poll_from_text(message.text or "")
    if not poll:
        await message.answer("Оберіть опитування кнопкою нижче.", reply_markup=polls_keyboard())
        return

    await state.update_data(poll_key=message.text)
    await state.set_state(PollStates.confirm)
    await message.answer("Опитування готове до публікації.", reply_markup=publish_cancel_keyboard())


async def confirm_publish_poll(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.text != "Опублікувати":
        await message.answer("Натисніть «Опублікувати» або «Скасувати».", reply_markup=publish_cancel_keyboard())
        return

    data = await state.get_data()
    poll_key = data.get("poll_key")
    poll = selected_poll_from_text(poll_key) if poll_key else None
    if not poll:
        await state.clear()
        await message.answer("Сесію втрачено. Спробуйте ще раз.", reply_markup=main_menu_keyboard())
        return

    await bot.send_poll(
        chat_id=CHANNEL_ID,
        question=str(poll["question"]),
        options=[str(item) for item in poll["options"]],
        type="regular",
        is_anonymous=False,
    )
    await state.clear()
    await message.answer("Опитування опубліковано ✅", reply_markup=main_menu_keyboard())


async def fallback_handler(message: Message) -> None:
    await message.answer("Оберіть дію з меню нижче.", reply_markup=main_menu_keyboard())


async def error_handler(event: Any, bot: Bot) -> None:
    logging.exception("Unhandled error: %s", getattr(event, "exception", event))
    try:
        await bot.send_message(ADMIN_ID, "Сталася помилка під час роботи бота.")
    except Exception:
        pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(action_cancel, F.text == "Скасувати")

    dp.message.register(action_new_post, F.text == "Новий пост")
    dp.message.register(choose_genre, NewPostStates.choosing_genre)
    dp.message.register(choose_language, NewPostStates.choosing_language)
    dp.message.register(confirm_publish_post, NewPostStates.confirm)

    dp.message.register(action_poll, F.text == "Опитування")
    dp.message.register(choose_poll, PollStates.choosing_poll)
    dp.message.register(confirm_publish_poll, PollStates.confirm)

    dp.message.register(fallback_handler)
    dp.errors.register(error_handler)

    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
