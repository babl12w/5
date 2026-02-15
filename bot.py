import asyncio
import logging
import os
import random
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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
from dotenv import load_dotenv

from userbot import DownloadedTrack, TgSoundUserbot

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

required = {
    "BOT_TOKEN": BOT_TOKEN,
    "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
    "ADMIN_ID": str(ADMIN_ID) if ADMIN_ID else None,
    "CHANNEL_ID": CHANNEL_ID,
    "SPOTIFY_CLIENT_ID": SPOTIFY_CLIENT_ID,
    "SPOTIFY_CLIENT_SECRET": SPOTIFY_CLIENT_SECRET,
    "API_ID": os.getenv("API_ID"),
    "API_HASH": os.getenv("API_HASH"),
}
missing = [k for k, v in required.items() if not v]
if missing:
    raise RuntimeError(f"Missing required ENV keys: {', '.join(missing)}")

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Новий пост")],
        [KeyboardButton(text="Опитування")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
)

GENRES = ["Pop", "Rock", "Hip-Hop", "Electronic"]
LANGUAGES = {"Українська": "uk", "Рос": "ru", "Польська": "pl"}
RSS_URLS = [
    "https://www.ukrinform.ua/rss/block-lastnews",
    "https://www.radiosvoboda.org/api/epiqq",
]
POLL_TEMPLATES = [
    {"title": "Опитування 1", "question": "Який трек хочете почути наступним?", "options": ["Більше Pop", "Більше Rock", "Більше Electronic"]},
    {"title": "Опитування 2", "question": "Коли вам зручніше слухати музику?", "options": ["Зранку", "Вдень", "Вночі"]},
    {"title": "Опитування 3", "question": "Який вайб сьогодні?", "options": ["Спокійний", "Енергійний", "Ліричний"]},
]


class PostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    choosing_poll = State()


@dataclass
class PendingPost:
    image_path: str
    quote: str
    tracks: List[DownloadedTrack]
    temp_dir: str


@dataclass
class PendingPoll:
    question: str
    options: List[str]


pending_posts: Dict[int, PendingPost] = {}
pending_polls: Dict[int, PendingPoll] = {}
userbot = TgSoundUserbot()


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def fetch_spotify_token(session: aiohttp.ClientSession) -> str:
    auth = aiohttp.BasicAuth(login=SPOTIFY_CLIENT_ID, password=SPOTIFY_CLIENT_SECRET)
    async with session.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["access_token"]


async def spotify_search_tracks(session: aiohttp.ClientSession, token: str, query: str, limit: int = 10) -> List[Dict[str, str]]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {"q": query, "type": "track", "limit": str(limit), "market": "UA"}
    async with session.get(
        "https://api.spotify.com/v1/search",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    items = data.get("tracks", {}).get("items", [])
    result: List[Dict[str, str]] = []
    for item in items:
        name = item.get("name")
        artists = item.get("artists", [])
        if not name or not artists:
            continue
        artist_name = artists[0].get("name", "Unknown")
        result.append({"title": name, "artist": artist_name})
    return result


async def fetch_unsplash_image(session: aiohttp.ClientSession, genre: str) -> bytes:
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    params = {"query": f"music {genre} vibe", "orientation": "landscape", "content_filter": "high"}
    async with session.get(
        "https://api.unsplash.com/photos/random",
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=25),
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()

    image_url = data.get("urls", {}).get("small") or data.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash image URL is missing")

    async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        resp.raise_for_status()
        return await resp.read()


def quote_from_rss() -> str:
    phrases: List[str] = []
    for url in RSS_URLS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            text = " ".join(filter(None, [getattr(entry, "title", ""), getattr(entry, "summary", "")]))
            text = " ".join(text.split())
            if 40 <= len(text) <= 240:
                phrases.append(text)

    if not phrases:
        return "Музика лікує втому, коли мовчать слова.\nДихай глибше — і натискай play."

    source = random.choice(phrases)
    chunks = [c.strip() for c in source.replace("!", ".").split(".") if c.strip()]
    chosen = chunks[:4] if len(chunks) >= 2 else [source]
    return "\n".join(chosen[:4])


def inline_publish_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Опублікувати", callback_data=f"publish:{kind}")],
            [InlineKeyboardButton(text="Скасувати", callback_data=f"cancel:{kind}")],
        ]
    )


def cleanup_post(post: PendingPost) -> None:
    shutil.rmtree(post.temp_dir, ignore_errors=True)


async def build_post_data(genre: str, language_label: str) -> PendingPost:
    lang_code = LANGUAGES[language_label]
    temp_dir = tempfile.mkdtemp(prefix="music_post_")
    async with aiohttp.ClientSession() as session:
        token = await fetch_spotify_token(session)

        q1 = f'genre:"{genre}" {lang_code}'
        tracks = await spotify_search_tracks(session, token, q1)
        if len(tracks) < 2:
            q2 = f"{lang_code} music"
            tracks = await spotify_search_tracks(session, token, q2)
        if len(tracks) < 2:
            q3 = "popular hits"
            tracks = await spotify_search_tracks(session, token, q3)

        image_bytes = await fetch_unsplash_image(session, genre)

    quote = await asyncio.to_thread(quote_from_rss)

    image_path = str(Path(temp_dir) / "cover.jpg")
    Path(image_path).write_bytes(image_bytes)

    downloaded: List[DownloadedTrack] = []
    for tr in tracks:
        query = f"{tr['artist']} - {tr['title']}"
        found = await userbot.search_and_download(query=query, destination_dir=temp_dir, limit=2, timeout_sec=35)
        for item in found:
            if len(downloaded) < 2:
                downloaded.append(item)
        if len(downloaded) >= 2:
            break

    if len(downloaded) < 2:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError("Не вдалося знайти достатньо треків.")

    return PendingPost(image_path=image_path, quote=quote, tracks=downloaded[:2], temp_dir=temp_dir)


async def show_main_menu(message: Message, text: str = "Головне меню") -> None:
    await message.answer(text, reply_markup=MAIN_MENU)


async def start_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        await message.answer("Доступ заборонено.")
        return
    await state.clear()
    await show_main_menu(message, "Вітаю! Оберіть дію:")


async def cancel_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if is_admin(message):
        post = pending_posts.pop(ADMIN_ID, None)
        poll = pending_polls.pop(ADMIN_ID, None)
        if post:
            cleanup_post(post)
        _ = poll
    await show_main_menu(message, "Скасовано. Повернення у головне меню.")


async def new_post_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    genre_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=g)] for g in GENRES] + [[KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )
    await state.set_state(PostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_keyboard)


async def choose_genre_handler(message: Message, state: FSMContext) -> None:
    if message.text not in GENRES:
        await message.answer("Оберіть жанр кнопкою.")
        return
    await state.update_data(genre=message.text)
    lang_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Українська")], [KeyboardButton(text="Рос")], [KeyboardButton(text="Польська")], [KeyboardButton(text="Скасувати")]],
        resize_keyboard=True,
    )
    await state.set_state(PostStates.choosing_language)
    await message.answer("Оберіть мову:", reply_markup=lang_keyboard)


async def choose_language_handler(message: Message, state: FSMContext) -> None:
    if message.text not in LANGUAGES:
        await message.answer("Оберіть мову кнопкою.")
        return

    data = await state.get_data()
    genre = data.get("genre")
    if not genre:
        await state.clear()
        await show_main_menu(message, "Сталася помилка стану. Спробуйте знову.")
        return

    await message.answer("Генерую прев'ю поста, зачекайте...")
    try:
        post = await build_post_data(genre=genre, language_label=message.text)
    except Exception as exc:
        await message.answer(str(exc))
        await message.answer("Не вдалося знайти достатньо треків.")
        await state.clear()
        await show_main_menu(message)
        return

    old = pending_posts.pop(ADMIN_ID, None)
    if old:
        cleanup_post(old)
    pending_posts[ADMIN_ID] = post

    await message.answer_photo(photo=FSInputFile(post.image_path), caption=post.quote)
    for tr in post.tracks:
        await message.answer_audio(audio=FSInputFile(tr.file_path), title=tr.title, performer=tr.performer)

    await message.answer("Прев'ю готове.", reply_markup=MAIN_MENU)
    await message.answer("Підтвердьте дію:", reply_markup=inline_publish_keyboard("post"))
    await state.clear()


async def poll_menu_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Опитування 1")],
            [KeyboardButton(text="Опитування 2")],
            [KeyboardButton(text="Опитування 3")],
            [KeyboardButton(text="Скасувати")],
        ],
        resize_keyboard=True,
    )
    await state.set_state(PostStates.choosing_poll)
    await message.answer("Оберіть шаблон опитування:", reply_markup=keyboard)


async def poll_select_handler(message: Message, state: FSMContext) -> None:
    titles = {p["title"]: p for p in POLL_TEMPLATES}
    selected = titles.get(message.text or "")
    if not selected:
        await message.answer("Оберіть варіант кнопкою.")
        return

    pending_polls[ADMIN_ID] = PendingPoll(question=selected["question"], options=selected["options"])
    await message.answer(
        f"Прев'ю опитування:\n<b>{selected['question']}</b>\n" + "\n".join(f"• {o}" for o in selected["options"]),
        reply_markup=MAIN_MENU,
    )
    await message.answer("Підтвердьте дію:", reply_markup=inline_publish_keyboard("poll"))
    await state.clear()


async def callback_handler(callback_query):
    if callback_query.from_user.id != ADMIN_ID:
        await callback_query.answer("Недостатньо прав", show_alert=True)
        return

    action, kind = (callback_query.data or "").split(":", maxsplit=1)

    if action == "cancel":
        if kind == "post":
            post = pending_posts.pop(ADMIN_ID, None)
            if post:
                cleanup_post(post)
        if kind == "poll":
            pending_polls.pop(ADMIN_ID, None)
        await callback_query.message.answer("Скасовано.", reply_markup=MAIN_MENU)
        await callback_query.answer()
        return

    if action == "publish" and kind == "post":
        post = pending_posts.pop(ADMIN_ID, None)
        if not post:
            await callback_query.answer("Немає готового поста", show_alert=True)
            return
        await callback_query.bot.send_photo(chat_id=CHANNEL_ID, photo=FSInputFile(post.image_path), caption=post.quote)
        for tr in post.tracks:
            await callback_query.bot.send_audio(chat_id=CHANNEL_ID, audio=FSInputFile(tr.file_path), title=tr.title, performer=tr.performer)
        cleanup_post(post)
        await callback_query.message.answer("Пост опубліковано.", reply_markup=MAIN_MENU)
        await callback_query.answer()
        return

    if action == "publish" and kind == "poll":
        poll = pending_polls.pop(ADMIN_ID, None)
        if not poll:
            await callback_query.answer("Немає готового опитування", show_alert=True)
            return
        await callback_query.bot.send_poll(
            chat_id=CHANNEL_ID,
            question=poll.question,
            options=poll.options,
            is_anonymous=True,
        )
        await callback_query.message.answer("Опитування опубліковано.", reply_markup=MAIN_MENU)
        await callback_query.answer()
        return

    await callback_query.answer("Невідома дія", show_alert=True)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(start_handler, CommandStart())
    dp.message.register(cancel_handler, F.text == "Скасувати")

    dp.message.register(new_post_handler, F.text == "Новий пост")
    dp.message.register(choose_genre_handler, PostStates.choosing_genre)
    dp.message.register(choose_language_handler, PostStates.choosing_language)

    dp.message.register(poll_menu_handler, F.text == "Опитування")
    dp.message.register(poll_select_handler, PostStates.choosing_poll)

    dp.callback_query.register(callback_handler, F.data.startswith(("publish:", "cancel:")))

    try:
        await dp.start_polling(bot)
    finally:
        await userbot.stop()


if __name__ == "__main__":
    asyncio.run(main())
