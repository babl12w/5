import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

from userbot import TgSoundUserbot

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

required = {
    "BOT_TOKEN": BOT_TOKEN,
    "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
    "ADMIN_ID": ADMIN_ID_RAW,
    "CHANNEL_ID": CHANNEL_ID,
    "SPOTIFY_CLIENT_ID": SPOTIFY_CLIENT_ID,
    "SPOTIFY_CLIENT_SECRET": SPOTIFY_CLIENT_SECRET,
    "API_ID": os.getenv("API_ID"),
    "API_HASH": os.getenv("API_HASH"),
    "SESSION_STRING": os.getenv("SESSION_STRING"),
}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing env vars: {', '.join(missing)}")

ADMIN_ID = int(ADMIN_ID_RAW)

GENRES = ["Поп", "Рок", "Хіп-хоп", "Електронна"]
LANGUAGES = ["Українська", "Рос", "Польська"]

POLL_TEMPLATES = [
    {
        "question": "Який настрій сьогодні у плейлисті?",
        "options": ["Спокій", "Енергія", "Романтика", "Ностальгія"],
    },
    {
        "question": "Що ставимо наступним у каналі?",
        "options": ["Новинки", "Класика", "Інді", "Ремікси"],
    },
    {
        "question": "Який жанр слухаємо ввечері?",
        "options": ["Поп", "Рок", "Хіп-хоп", "Електронна"],
    },
]

rss_sources = [
    "https://www.ukrinform.ua/rss/block-lastnews",
    "https://nv.ua/ukr/rss/all.xml",
]


class CreatePostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    preview_post = State()
    choosing_poll = State()
    preview_poll = State()


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Новий пост")],
            [KeyboardButton(text="Опитування")],
            [KeyboardButton(text="Скасувати")],
        ],
        resize_keyboard=True,
    )


def genre_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=g)] for g in GENRES] + [[KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )


def language_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=l)] for l in LANGUAGES] + [[KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )


def publish_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Опублікувати")],
            [KeyboardButton(text="Скасувати")],
        ],
        resize_keyboard=True,
    )


def polls_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Опитування 1")],
            [KeyboardButton(text="Опитування 2")],
            [KeyboardButton(text="Опитування 3")],
            [KeyboardButton(text="Скасувати")],
        ],
        resize_keyboard=True,
    )


async def get_spotify_token(session: aiohttp.ClientSession) -> str:
    auth = aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        data = await response.json()
        return data["access_token"]


async def spotify_search_tracks(
    session: aiohttp.ClientSession,
    token: str,
    query: str,
    market: str,
    limit: int = 10,
) -> list[dict[str, str]]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": query,
        "type": "track",
        "market": market,
        "limit": str(limit),
    }
    async with session.get(
        "https://api.spotify.com/v1/search",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        data = await response.json()

    tracks: list[dict[str, str]] = []
    for item in data.get("tracks", {}).get("items", []):
        name = item.get("name")
        artists = item.get("artists", [])
        if not name or not artists:
            continue
        artist_name = artists[0].get("name")
        if not artist_name:
            continue
        tracks.append({"title": name, "artist": artist_name})
    return tracks


def unique_tracks(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result: list[dict[str, str]] = []
    for tr in items:
        key = (tr["title"].strip().lower(), tr["artist"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        result.append(tr)
    return result


async def pick_two_tracks(session: aiohttp.ClientSession, genre: str, language: str) -> list[dict[str, str]]:
    market_map = {
        "Українська": "UA",
        "Рос": "KZ",
        "Польська": "PL",
    }
    market = market_map.get(language, "UA")
    genre_en_map = {
        "Поп": "pop",
        "Рок": "rock",
        "Хіп-хоп": "hip hop",
        "Електронна": "electronic",
    }
    genre_query = genre_en_map.get(genre, "pop")

    token = await get_spotify_token(session)
    attempts = [
        f"genre:{genre_query}",
        "top hits",
        "popular",
    ]

    for query in attempts:
        found = await spotify_search_tracks(session, token, query, market, limit=20)
        deduped = unique_tracks(found)
        if len(deduped) >= 2:
            return deduped[:2]

    return []


async def fetch_unsplash_photo(session: aiohttp.ClientSession, genre: str) -> str:
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {
        "query": f"{genre} music mood",
        "orientation": "landscape",
    }
    async with session.get(
        "https://api.unsplash.com/photos/random",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        data = await response.json()

    image_url = data.get("urls", {}).get("small")
    if not image_url:
        raise RuntimeError("Unsplash image URL not found")

    async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        response.raise_for_status()
        content = await response.read()

    path = Path("/tmp") / f"preview_{asyncio.get_running_loop().time()}.jpg"
    path.write_bytes(content)
    return str(path)


def get_quote_from_rss() -> str:
    for source in rss_sources:
        feed = feedparser.parse(source)
        entries = feed.get("entries", [])
        if not entries:
            continue

        lines = []
        for item in entries[:4]:
            title = (item.get("title") or "").strip()
            if title:
                lines.append(title)
            if len(lines) == 4:
                break

        if len(lines) >= 2:
            return "\n".join(lines[:4])

    return "Музика тримає ритм серця.\nКожна нота — ковток повітря."


async def cleanup_files(paths: list[str]) -> None:
    for p in paths:
        try:
            path = Path(p)
            if path.exists():
                path.unlink()
        except Exception:
            continue


router = Router()


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ заборонено.")
        return
    await state.clear()
    await message.answer("Оберіть дію:", reply_markup=main_menu_keyboard())


@router.message(F.text == "Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    files = data.get("temp_files", [])
    if isinstance(files, list):
        await cleanup_files(files)
    await state.clear()
    await message.answer("Скасовано.", reply_markup=main_menu_keyboard())


@router.message(F.text == "Новий пост")
async def new_post_handler(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await state.set_state(CreatePostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_keyboard())


@router.message(CreatePostStates.choosing_genre, F.text.in_(GENRES))
async def choose_genre_handler(message: Message, state: FSMContext) -> None:
    await state.update_data(genre=message.text)
    await state.set_state(CreatePostStates.choosing_language)
    await message.answer("Оберіть мову:", reply_markup=language_keyboard())


@router.message(CreatePostStates.choosing_language, F.text.in_(LANGUAGES))
async def choose_language_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    genre = data["genre"]
    language = message.text

    await message.answer("Генерую прев'ю поста...")

    userbot: TgSoundUserbot = bot["userbot"]

    async with aiohttp.ClientSession() as session:
        tracks = await pick_two_tracks(session, genre, language)
        if len(tracks) < 2:
            await bot.send_message(ADMIN_ID, "Не вдалося знайти достатньо треків.")
            await state.clear()
            await message.answer("Спробуйте ще раз.", reply_markup=main_menu_keyboard())
            return

        photo_path = await fetch_unsplash_photo(session, genre)

    quote = await asyncio.to_thread(get_quote_from_rss)

    mp3_paths: list[str] = []
    for track in tracks:
        mp3_path = await userbot.find_track_mp3(track["artist"], track["title"])
        mp3_paths.append(mp3_path)

    await message.answer_photo(
        photo=FSInputFile(photo_path),
        caption=quote,
    )

    for idx, track in enumerate(tracks):
        await message.answer_audio(
            audio=FSInputFile(mp3_paths[idx]),
            title=track["title"],
            performer=track["artist"],
        )

    await state.update_data(
        post_preview={
            "genre": genre,
            "language": language,
            "quote": quote,
            "photo_path": photo_path,
            "tracks": tracks,
            "mp3_paths": mp3_paths,
        },
        temp_files=[photo_path, *mp3_paths],
    )
    await state.set_state(CreatePostStates.preview_post)
    await message.answer("Прев'ю готове.", reply_markup=publish_keyboard())


@router.message(F.text == "Опублікувати", CreatePostStates.preview_post)
async def publish_post_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    preview = data.get("post_preview")
    if not preview:
        await message.answer("Немає підготовленого поста.", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    await bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=FSInputFile(preview["photo_path"]),
        caption=preview["quote"],
    )

    for idx, track in enumerate(preview["tracks"]):
        await bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=FSInputFile(preview["mp3_paths"][idx]),
            title=track["title"],
            performer=track["artist"],
        )

    await cleanup_files(preview["mp3_paths"] + [preview["photo_path"]])
    await state.clear()
    await message.answer("Опубліковано.", reply_markup=main_menu_keyboard())


@router.message(F.text == "Опитування")
async def poll_menu_handler(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await state.set_state(CreatePostStates.choosing_poll)
    await message.answer("Оберіть опитування:", reply_markup=polls_keyboard())


@router.message(CreatePostStates.choosing_poll, F.text.in_(["Опитування 1", "Опитування 2", "Опитування 3"]))
async def choose_poll_handler(message: Message, state: FSMContext) -> None:
    idx = int(message.text.split()[-1]) - 1
    await state.update_data(chosen_poll=idx)
    await state.set_state(CreatePostStates.preview_poll)

    poll = POLL_TEMPLATES[idx]
    preview_text = f"Прев'ю:\n{poll['question']}\n- " + "\n- ".join(poll["options"])
    await message.answer(preview_text, reply_markup=publish_keyboard())


@router.message(F.text == "Опублікувати", CreatePostStates.preview_poll)
async def publish_poll_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    idx = data.get("chosen_poll")
    if idx is None:
        await state.clear()
        await message.answer("Немає вибраного опитування.", reply_markup=main_menu_keyboard())
        return

    poll = POLL_TEMPLATES[idx]
    await bot.send_poll(
        chat_id=CHANNEL_ID,
        question=poll["question"],
        options=poll["options"],
        is_anonymous=True,
    )

    await state.clear()
    await message.answer("Опитування опубліковано.", reply_markup=main_menu_keyboard())


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    userbot = TgSoundUserbot()
    bot["userbot"] = userbot

    await userbot.start()
    try:
        await dp.start_polling(bot)
    finally:
        await userbot.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
