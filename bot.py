import asyncio
import html
import logging
import os
import random
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
SPOTIFY_CLIENT_ID = os.getenv("spotify_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("spotify_CLIENT_SECRET", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not ADMIN_ID_RAW:
    raise RuntimeError("ADMIN_ID is not set")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is not set")
if not UNSPLASH_ACCESS_KEY:
    raise RuntimeError("UNSPLASH_ACCESS_KEY is not set")
if not SPOTIFY_CLIENT_ID:
    raise RuntimeError("spotify_CLIENT_ID is not set")
if not SPOTIFY_CLIENT_SECRET:
    raise RuntimeError("spotify_CLIENT_SECRET is not set")

ADMIN_ID = int(ADMIN_ID_RAW)

GENRES = ["Pop", "Rock", "Hip-Hop", "Electronic"]
LANGUAGES = ["Українська", "Російська", "Польська"]
POLL_LIBRARY = [
    {
        "label": "Опитування 1",
        "question": "Який настрій сьогодні пасує найбільше?",
        "options": ["Спокійний", "Енергійний", "Романтичний", "Мрійливий"],
    },
    {
        "label": "Опитування 2",
        "question": "Що хочете почути наступним постом?",
        "options": ["Інді", "Поп", "Лоуфай", "Рок"],
    },
    {
        "label": "Опитування 3",
        "question": "Коли вам зручно слухати музику?",
        "options": ["Вранці", "Вдень", "Увечері", "Вночі"],
    },
]

QUOTE_RSS_URL = "https://www.ukrinform.ua/rss/block-lastnews"


class PostFlow(StatesGroup):
    choose_genre = State()
    choose_language = State()
    confirm_post = State()


class PollFlow(StatesGroup):
    choose_poll = State()
    confirm_poll = State()


@dataclass
class TrackCandidate:
    title: str
    artist: str
    search_query: str


@dataclass
class PreparedTrack:
    title: str
    artist: str
    mp3_path: Path


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Новий пост")],
            [KeyboardButton(text="Опитування")],
            [KeyboardButton(text="Скасувати")],
        ],
        resize_keyboard=True,
    )


def genres_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=genre)] for genre in GENRES] + [[KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )


def languages_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=lang)] for lang in LANGUAGES] + [[KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )


def publish_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Опублікувати")], [KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )


def polls_keyboard() -> ReplyKeyboardMarkup:
    poll_buttons = [[KeyboardButton(text=item["label"])] for item in POLL_LIBRARY]
    poll_buttons.append([KeyboardButton(text="Скасувати")])
    return ReplyKeyboardMarkup(keyboard=poll_buttons, resize_keyboard=True)


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=40)
    async with session.request(
        method,
        url,
        params=params,
        headers=headers,
        data=data,
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        return await response.json()


async def get_spotify_token(session: aiohttp.ClientSession) -> str:
    payload = {"grant_type": "client_credentials"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data=payload,
        auth=aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        timeout=timeout,
    ) as response:
        response.raise_for_status()
        token_data = await response.json()
    token = token_data.get("access_token")
    if not token:
        raise RuntimeError("Spotify token was not returned")
    return token


async def spotify_search_tracks(
    session: aiohttp.ClientSession,
    token: str,
    query: str,
    limit: int = 8,
) -> list[TrackCandidate]:
    headers = {"Authorization": f"Bearer {token}"}
    data = await fetch_json(
        session,
        "https://api.spotify.com/v1/search",
        params={"q": query, "type": "track", "limit": str(limit), "market": "UA"},
        headers=headers,
    )
    items = data.get("tracks", {}).get("items", [])
    candidates: list[TrackCandidate] = []
    for item in items:
        name = item.get("name")
        artists = item.get("artists", [])
        if not name or not artists:
            continue
        artist_name = artists[0].get("name", "Unknown")
        candidates.append(TrackCandidate(title=name, artist=artist_name, search_query=f"{name} {artist_name} official audio"))
    return candidates


async def search_unsplash_photo(session: aiohttp.ClientSession, genre: str) -> Path:
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    data = await fetch_json(
        session,
        "https://api.unsplash.com/photos/random",
        params={"query": f"{genre} music vibe", "orientation": "landscape"},
        headers=headers,
    )
    image_url = data.get("urls", {}).get("small") or data.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash did not return image URL")

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    temp_path = Path(temp_file.name)
    temp_file.close()

    async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=45)) as response:
        response.raise_for_status()
        with temp_path.open("wb") as f:
            while True:
                chunk = await response.content.read(1024 * 64)
                if not chunk:
                    break
                f.write(chunk)
    return temp_path


def extract_quote_from_rss(xml_text: str) -> str:
    root = ElementTree.fromstring(xml_text)
    texts: list[str] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        combined = " ".join(part for part in [title, description] if part)
        if len(combined) >= 60:
            texts.append(combined)

    if not texts:
        texts = [
            "Слухай серцем.\nКоли день шумить — музика збирає думки докупи.\nІ стає трохи тепліше.",
        ]

    raw = random.choice(texts)
    clean = " ".join(raw.replace("\xa0", " ").split())

    parts = [segment.strip() for segment in clean.replace("!", ".").split(".") if segment.strip()]
    if len(parts) >= 4:
        lines = parts[:4]
    elif len(parts) >= 2:
        lines = parts[:2]
    else:
        words = clean.split()
        chunk_size = max(4, len(words) // 3)
        lines = [" ".join(words[i : i + chunk_size]) for i in range(0, min(len(words), chunk_size * 3), chunk_size)]

    lines = lines[:4]
    if len(lines) < 2:
        lines = ["Музика нагадує дихати глибше", "і знаходити світло в деталях"]

    return "\n".join(html.escape(line) for line in lines)


async def fetch_ukrainian_quote(session: aiohttp.ClientSession) -> str:
    async with session.get(QUOTE_RSS_URL, timeout=aiohttp.ClientTimeout(total=25)) as response:
        response.raise_for_status()
        xml_text = await response.text()
    return extract_quote_from_rss(xml_text)


def yt_download_to_mp3(search_query: str, target_dir: Path) -> Path | None:
    output_template = str(target_dir / "%(title).60s-%(id)s.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "default_search": "ytsearch1",
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_query, download=True)
        if not info:
            return None
        entry = info["entries"][0] if "entries" in info else info
        downloaded = Path(ydl.prepare_filename(entry)).with_suffix(".mp3")
        if downloaded.exists():
            return downloaded

    found_mp3 = sorted(target_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found_mp3[0] if found_mp3 else None


async def prepare_tracks_with_fallback(
    session: aiohttp.ClientSession,
    genre: str,
    language: str,
) -> list[PreparedTrack]:
    token = await get_spotify_token(session)

    queries = [
        f"{genre} mood {language}",
        f"music mood {language}",
        "popular hits",
    ]

    tracks_dir = Path(tempfile.mkdtemp(prefix="tg_music_"))
    prepared: list[PreparedTrack] = []
    seen: set[str] = set()

    for query in queries:
        spotify_tracks = await spotify_search_tracks(session, token, query, limit=10)
        if not spotify_tracks:
            continue

        for candidate in spotify_tracks:
            key = f"{candidate.title.lower()}::{candidate.artist.lower()}"
            if key in seen:
                continue
            seen.add(key)

            try:
                mp3_path = await asyncio.to_thread(yt_download_to_mp3, candidate.search_query, tracks_dir)
            except Exception as err:  # noqa: BLE001
                logging.warning("YouTube extraction failed for %s: %s", candidate.search_query, err)
                continue

            if mp3_path and mp3_path.exists():
                prepared.append(PreparedTrack(title=candidate.title, artist=candidate.artist, mp3_path=mp3_path))

            if len(prepared) >= 2:
                return prepared

    return prepared


async def notify_admin_not_enough_tracks(bot: Bot, genre: str, language: str) -> None:
    text = (
        "Вітаю! Не вдалося підготувати 2 треки для поста.\n"
        f"Жанр: {genre}\n"
        f"Мова: {language}\n"
        "Спробовано fallback-запити Spotify/YouTube, але треків все ще недостатньо."
    )
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception as err:  # noqa: BLE001
        logging.error("Cannot notify admin: %s", err)


async def cleanup_temp_files(state: FSMContext) -> None:
    data = await state.get_data()
    photo_path = data.get("photo_path")
    track_paths = data.get("track_paths", [])
    temp_dir = data.get("temp_dir")

    for raw_path in track_paths:
        path = Path(raw_path)
        if path.exists():
            path.unlink(missing_ok=True)

    if photo_path:
        photo = Path(photo_path)
        if photo.exists():
            photo.unlink(missing_ok=True)

    if temp_dir:
        shutil.rmtree(temp_dir, ignore_errors=True)


async def show_main_menu(message: Message, text: str = "Вітаю! Оберіть дію:") -> None:
    await message.answer(text, reply_markup=menu_keyboard())


async def cmd_start(message: Message, state: FSMContext) -> None:
    await cleanup_temp_files(state)
    await state.clear()
    await show_main_menu(message)


async def cancel_action(message: Message, state: FSMContext) -> None:
    await cleanup_temp_files(state)
    await state.clear()
    await show_main_menu(message, "Дію скасовано. Оберіть нову дію:")


async def new_post_entry(message: Message, state: FSMContext) -> None:
    await cleanup_temp_files(state)
    await state.clear()
    await state.set_state(PostFlow.choose_genre)
    await message.answer("Оберіть жанр:", reply_markup=genres_keyboard())


async def choose_genre_handler(message: Message, state: FSMContext) -> None:
    genre = (message.text or "").strip()
    if genre not in GENRES:
        await message.answer("Будь ласка, оберіть жанр кнопкою.", reply_markup=genres_keyboard())
        return

    await state.update_data(genre=genre)
    await state.set_state(PostFlow.choose_language)
    await message.answer("Оберіть мову:", reply_markup=languages_keyboard())


async def choose_language_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    language = (message.text or "").strip()
    if language not in LANGUAGES:
        await message.answer("Будь ласка, оберіть мову кнопкою.", reply_markup=languages_keyboard())
        return

    data = await state.get_data()
    genre = data.get("genre", "Pop")

    await message.answer("Готую прев'ю поста, зачекайте кілька секунд...")

    try:
        async with aiohttp.ClientSession() as session:
            tracks = await prepare_tracks_with_fallback(session, genre=genre, language=language)
            if len(tracks) < 2:
                await notify_admin_not_enough_tracks(bot, genre=genre, language=language)
                for track in tracks:
                    track.mp3_path.unlink(missing_ok=True)
                await message.answer(
                    "На жаль, зараз не вдалося знайти 2 треки. Спробуйте ще раз трохи пізніше.",
                    reply_markup=menu_keyboard(),
                )
                await state.clear()
                return

            photo_path = await search_unsplash_photo(session, genre=genre)
            quote = await fetch_ukrainian_quote(session)
    except Exception as err:  # noqa: BLE001
        logging.exception("Error preparing post preview: %s", err)
        await message.answer("Сталася помилка під час підготовки поста. Спробуйте ще раз.", reply_markup=menu_keyboard())
        await state.clear()
        return

    temp_dir = str(Path(tracks[0].mp3_path).parent)
    caption = quote

    await state.update_data(
        language=language,
        quote=caption,
        photo_path=str(photo_path),
        track_paths=[str(track.mp3_path) for track in tracks],
        track_titles=[track.title for track in tracks],
        track_artists=[track.artist for track in tracks],
        temp_dir=temp_dir,
    )

    await message.answer("Прев'ю готове:")
    await message.answer_photo(photo=FSInputFile(photo_path), caption=caption)

    for track in tracks[:2]:
        await message.answer_audio(
            audio=FSInputFile(track.mp3_path),
            title=track.title,
            performer=track.artist,
        )

    await state.set_state(PostFlow.confirm_post)
    await message.answer("Опублікувати пост у канал?", reply_markup=publish_keyboard())


async def publish_post_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not data:
        await show_main_menu(message)
        return

    try:
        photo_path = Path(data["photo_path"])
        quote = data["quote"]
        track_paths = [Path(path) for path in data.get("track_paths", [])]
        titles = data.get("track_titles", [])
        artists = data.get("track_artists", [])

        await bot.send_photo(CHANNEL_ID, photo=FSInputFile(photo_path), caption=quote)
        for i, path in enumerate(track_paths[:2]):
            await bot.send_audio(
                CHANNEL_ID,
                audio=FSInputFile(path),
                title=titles[i] if i < len(titles) else "Track",
                performer=artists[i] if i < len(artists) else "Unknown",
            )
    except Exception as err:  # noqa: BLE001
        logging.exception("Error while publishing post: %s", err)
        await message.answer("Не вдалося опублікувати пост. Спробуйте ще раз.", reply_markup=menu_keyboard())
        await cleanup_temp_files(state)
        await state.clear()
        return

    await message.answer("Готово! Пост миттєво опубліковано ✅", reply_markup=menu_keyboard())
    await cleanup_temp_files(state)
    await state.clear()


async def poll_entry(message: Message, state: FSMContext) -> None:
    await cleanup_temp_files(state)
    await state.clear()
    await state.set_state(PollFlow.choose_poll)
    await message.answer("Оберіть заготовлене опитування:", reply_markup=polls_keyboard())


async def choose_poll_handler(message: Message, state: FSMContext) -> None:
    selected = (message.text or "").strip()
    poll = next((item for item in POLL_LIBRARY if item["label"] == selected), None)
    if not poll:
        await message.answer("Будь ласка, оберіть опитування кнопкою.", reply_markup=polls_keyboard())
        return

    await state.update_data(poll_label=poll["label"], poll_question=poll["question"], poll_options=poll["options"])
    await state.set_state(PollFlow.confirm_poll)

    options_preview = "\n".join(f"• {html.escape(opt)}" for opt in poll["options"])
    await message.answer(
        f"Прев'ю:\n<b>{html.escape(poll['question'])}</b>\n{options_preview}",
        reply_markup=publish_keyboard(),
    )


async def publish_poll_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not data:
        await show_main_menu(message)
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=data["poll_question"],
            options=data["poll_options"],
            is_anonymous=True,
        )
    except Exception as err:  # noqa: BLE001
        logging.exception("Error while publishing poll: %s", err)
        await message.answer("Не вдалося опублікувати опитування. Спробуйте ще раз.", reply_markup=menu_keyboard())
        await state.clear()
        return

    await message.answer("Опитування опубліковано ✅", reply_markup=menu_keyboard())
    await state.clear()


async def fallback_unhandled(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current in {PostFlow.choose_genre.state, PostFlow.choose_language.state, PostFlow.confirm_post.state}:
        await message.answer("Скористайтесь кнопками нижче для продовження або скасування.")
    elif current in {PollFlow.choose_poll.state, PollFlow.confirm_poll.state}:
        await message.answer("Скористайтесь кнопками нижче для вибору опитування або скасування.")
    else:
        await show_main_menu(message, "Оберіть дію через меню нижче:")


async def on_shutdown(dispatcher: Dispatcher, bot: Bot) -> None:
    logging.info("Shutting down bot...")
    await bot.session.close()


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cancel_action, F.text == "Скасувати")

    dp.message.register(new_post_entry, F.text == "Новий пост")
    dp.message.register(choose_genre_handler, PostFlow.choose_genre)
    dp.message.register(choose_language_handler, PostFlow.choose_language)
    dp.message.register(publish_post_handler, PostFlow.confirm_post, F.text == "Опублікувати")

    dp.message.register(poll_entry, F.text == "Опитування")
    dp.message.register(choose_poll_handler, PollFlow.choose_poll)
    dp.message.register(publish_poll_handler, PollFlow.confirm_poll, F.text == "Опублікувати")

    dp.message.register(fallback_unhandled)
    return dp


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = build_dispatcher()
    dp.shutdown.register(on_shutdown)

    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
