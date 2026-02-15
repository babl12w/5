import asyncio
import logging
import os
import contextlib
import random
import re
import textwrap
from dataclasses import dataclass
from html import unescape
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
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

REQUIRED_VARS = {
    "BOT_TOKEN": BOT_TOKEN,
    "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
    "ADMIN_ID": ADMIN_ID_RAW,
    "CHANNEL_ID": CHANNEL_ID_RAW,
    "SPOTIFY_CLIENT_ID": SPOTIFY_CLIENT_ID,
    "SPOTIFY_CLIENT_SECRET": SPOTIFY_CLIENT_SECRET,
}

missing = [name for name, value in REQUIRED_VARS.items() if not value]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be int") from exc

CHANNEL_ID: int | str
if CHANNEL_ID_RAW.lstrip("-").isdigit():
    CHANNEL_ID = int(CHANNEL_ID_RAW)
else:
    CHANNEL_ID = CHANNEL_ID_RAW

GENRES = ["Pop", "Rock", "Electronic", "Hip-Hop"]
LANGUAGES = ["Українська", "Російська", "Польська"]
LANG_QUERY = {
    "Українська": "ukrainian",
    "Російська": "russian",
    "Польська": "polish",
}

POLL_TEMPLATES = [
    {
        "title": "Опитування 1",
        "question": "Який жанр сьогодні в топі?",
        "options": ["Pop", "Rock", "Electronic", "Hip-Hop"],
    },
    {
        "title": "Опитування 2",
        "question": "Коли найкраще слухати музику?",
        "options": ["Зранку", "Вдень", "Увечері", "Вночі"],
    },
    {
        "title": "Опитування 3",
        "question": "Що обереш прямо зараз?",
        "options": ["Новий реліз", "Плейлист", "Ретро-хіти", "Лайв"],
    },
]

RSS_SOURCES = [
    "https://citaty.info/rss.xml",
    "https://quote-citation.com/topic/music/feed",
]

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="1️⃣ Новий пост")],
        [KeyboardButton(text="2️⃣ Опитування")],
        [KeyboardButton(text="3️⃣ Скасувати")],
    ],
    resize_keyboard=True,
)

class FlowState(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    confirm_post = State()
    choosing_poll = State()
    confirm_poll = State()


@dataclass
class Track:
    name: str
    artist: str


def build_keyboard(options: list[str], include_cancel: bool = True) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=opt)] for opt in options]
    if include_cancel:
        rows.append([KeyboardButton(text="❌ Скасувати")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def extract_cdata_text(raw_text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", raw_text)
    no_entities = unescape(no_tags)
    return normalize_text(no_entities)


def is_cyrillic_line(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яІіЇїЄєЁё]", text))


def quote_to_multiline(quote: str) -> str:
    wrapped = textwrap.wrap(quote, width=46)
    if not wrapped:
        return quote
    if len(wrapped) < 2:
        wrapped = textwrap.wrap(quote, width=max(18, len(quote) // 2))
    if len(wrapped) > 4:
        wrapped = wrapped[:4]
        wrapped[-1] = wrapped[-1].rstrip(" ,.;:-") + "…"
    return "\n".join(wrapped)


async def fetch_json(session: aiohttp.ClientSession, url: str, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> Any:
    async with session.get(url, headers=headers, params=params) if data is None else session.post(url, headers=headers, data=data) as resp:
        resp.raise_for_status()
        return await resp.json()


async def spotify_token(session: aiohttp.ClientSession) -> str:
    response = await fetch_json(
        session,
        "https://accounts.spotify.com/api/token",
        data={
            "grant_type": "client_credentials",
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
    )
    token = response.get("access_token")
    if not token:
        raise RuntimeError("Spotify token not received")
    return token


async def search_tracks(session: aiohttp.ClientSession, token: str, query: str, limit: int = 8) -> list[Track]:
    response = await fetch_json(
        session,
        "https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "track", "limit": str(limit), "market": "UA"},
    )
    items = response.get("tracks", {}).get("items", [])
    tracks: list[Track] = []
    for item in items:
        name = normalize_text(item.get("name", ""))
        artists = item.get("artists") or []
        artist_name = normalize_text(artists[0].get("name", "")) if artists else "Unknown"
        if name and artist_name:
            tracks.append(Track(name=name, artist=artist_name))
    unique: list[Track] = []
    seen = set()
    for track in tracks:
        key = (track.name.lower(), track.artist.lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(track)
    return unique


async def get_two_tracks(session: aiohttp.ClientSession, genre: str, language: str) -> list[Track]:
    token = await spotify_token(session)
    lang = LANG_QUERY[language]
    queries = [
        f'genre:"{genre}" {lang}',
        f"{lang} music",
        "popular hits",
    ]
    pool: list[Track] = []
    for query in queries:
        tracks = await search_tracks(session, token, query)
        for track in tracks:
            if all(not (track.name == t.name and track.artist == t.artist) for t in pool):
                pool.append(track)
        if len(pool) >= 2:
            return pool[:2]
    return pool[:2]


async def get_unsplash_photo_url(session: aiohttp.ClientSession) -> str:
    data = await fetch_json(
        session,
        "https://api.unsplash.com/photos/random",
        headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
        params={
            "query": "music mood vibe",
            "orientation": "portrait",
            "content_filter": "low",
        },
    )
    urls = data.get("urls", {})
    return urls.get("small") or urls.get("regular") or urls.get("thumb") or ""


async def get_quote_candidates(session: aiohttp.ClientSession) -> list[str]:
    candidates: list[str] = []
    for url in RSS_SOURCES:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as response:
                if response.status != 200:
                    continue
                xml_text = await response.text()
        except Exception:
            continue

        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            continue

        items = root.findall(".//item")[:20]
        for item in items:
            title = item.findtext("title", default="")
            desc = item.findtext("description", default="")
            content = extract_cdata_text(f"{title}. {desc}")
            if len(content) < 35:
                continue
            if not is_cyrillic_line(content):
                continue
            content = re.sub(r"\s*\[[^\]]+\]\s*$", "", content).strip(" -—|•")
            if len(content) > 220:
                content = content[:220].rsplit(" ", 1)[0] + "…"
            multiline = quote_to_multiline(content)
            line_count = len(multiline.splitlines())
            if 2 <= line_count <= 4:
                candidates.append(multiline)

    unique: list[str] = []
    seen = set()
    for quote in candidates:
        key = quote.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(quote)
    return unique


def fallback_quotes() -> list[str]:
    return [
        "Музика не просто звучить —\nвона зшиває серце\nз тим, що неможливо\nсказати словами.",
        "Іноді одна пісня\nкаже про нас більше,\nніж сотні повідомлень\nу будь-якому чаті.",
    ]


def format_post_text(quote: str, tracks: list[Track]) -> str:
    return (
        f"{quote}\n\n"
        f"🎵 {tracks[0].name} — {tracks[0].artist}\n"
        f"🎵 {tracks[1].name} — {tracks[1].artist}"
    )


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def deny_if_not_admin(message: Message) -> bool:
    if is_admin(message):
        return False
    await message.answer("Доступ заборонено.")
    return True


async def to_main_menu(message: Message, state: FSMContext, text: str = "Головне меню") -> None:
    await state.clear()
    await message.answer(text, reply_markup=MAIN_MENU)


async def post_preview(message: Message, state: FSMContext, post_text: str, photo_url: str) -> None:
    confirm_keyboard = build_keyboard(["✅ Опублікувати", "❌ Скасувати"], include_cancel=False)
    await state.set_state(FlowState.confirm_post)
    await state.update_data(post_text=post_text, photo_url=photo_url)
    if photo_url:
        await message.answer_photo(photo=photo_url, caption=post_text, reply_markup=confirm_keyboard)
    else:
        await message.answer(post_text, reply_markup=confirm_keyboard)


async def poll_preview(message: Message, state: FSMContext, poll_data: dict[str, Any]) -> None:
    confirm_keyboard = build_keyboard(["✅ Опублікувати", "❌ Скасувати"], include_cancel=False)
    await state.set_state(FlowState.confirm_poll)
    await state.update_data(selected_poll=poll_data)
    preview = (
        f"{poll_data['title']}\n\n"
        f"Питання: {poll_data['question']}\n"
        f"Варіанти: {', '.join(poll_data['options'])}"
    )
    await message.answer(preview, reply_markup=confirm_keyboard)


bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await to_main_menu(message, state, "Вітаю! Обери дію.")


@dp.message(F.text == "3️⃣ Скасувати")
@dp.message(F.text == "❌ Скасувати")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await to_main_menu(message, state, "Скасовано. Повертаю в головне меню.")


@dp.message(F.text == "1️⃣ Новий пост")
async def new_post_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    await state.set_state(FlowState.choosing_genre)
    await message.answer("Обери жанр:", reply_markup=build_keyboard(GENRES))


@dp.message(FlowState.choosing_genre)
async def choose_genre_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    genre = (message.text or "").strip()
    if genre not in GENRES:
        await message.answer("Оберіть жанр кнопкою.")
        return
    await state.update_data(genre=genre)
    await state.set_state(FlowState.choosing_language)
    await message.answer("Обери мову:", reply_markup=build_keyboard(LANGUAGES))


@dp.message(FlowState.choosing_language)
async def choose_language_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    language = (message.text or "").strip()
    if language not in LANGUAGES:
        await message.answer("Оберіть мову кнопкою.")
        return

    data = await state.get_data()
    genre = data.get("genre")
    if not genre:
        await to_main_menu(message, state, "Сталася помилка стану. Спробуй ще раз.")
        return

    wait_msg = await message.answer("Генерую пост...")
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tracks = await get_two_tracks(session, genre, language)
            if len(tracks) < 2:
                await bot.send_message(ADMIN_ID, "Не вдалося знайти 2 треки. Спробуйте інший жанр або мову.")
                await to_main_menu(message, state, "Не вдалося підібрати 2 треки. Спробуй інший вибір.")
                return

            photo_url = await get_unsplash_photo_url(session)
            quotes = await get_quote_candidates(session)
            if len(quotes) < 2:
                quotes.extend(fallback_quotes())
            if len(quotes) < 2:
                await bot.send_message(ADMIN_ID, "Не вдалося знайти мінімум 2 цитати.")
                await to_main_menu(message, state, "Не вдалося отримати цитату. Спробуй ще раз.")
                return

            quote = random.choice(quotes)
            post_text = format_post_text(quote, tracks)
            await post_preview(message, state, post_text, photo_url)
    except Exception as exc:
        logging.exception("Post generation error: %s", exc)
        await to_main_menu(message, state, "Сталася помилка під час формування посту.")
    finally:
        with contextlib.suppress(TelegramBadRequest):
            await wait_msg.delete()


@dp.message(FlowState.confirm_post, F.text == "✅ Опублікувати")
async def publish_post_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    data = await state.get_data()
    post_text = data.get("post_text")
    photo_url = data.get("photo_url", "")
    if not post_text:
        await to_main_menu(message, state, "Немає підготовленого поста.")
        return

    try:
        if photo_url:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_url, caption=post_text)
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=post_text)
        await to_main_menu(message, state, "Пост опубліковано.")
    except Exception as exc:
        logging.exception("Publish post error: %s", exc)
        await to_main_menu(message, state, "Не вдалося опублікувати пост.")


@dp.message(F.text == "2️⃣ Опитування")
async def polls_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    options = [poll["title"] for poll in POLL_TEMPLATES]
    await state.set_state(FlowState.choosing_poll)
    await message.answer("Обери опитування:", reply_markup=build_keyboard(options))


@dp.message(FlowState.choosing_poll)
async def choose_poll_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    selected_title = (message.text or "").strip()
    selected = next((poll for poll in POLL_TEMPLATES if poll["title"] == selected_title), None)
    if not selected:
        await message.answer("Оберіть опитування кнопкою.")
        return
    await poll_preview(message, state, selected)


@dp.message(FlowState.confirm_poll, F.text == "✅ Опублікувати")
async def publish_poll_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    data = await state.get_data()
    poll_data = data.get("selected_poll")
    if not poll_data:
        await to_main_menu(message, state, "Немає підготовленого опитування.")
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=poll_data["question"],
            options=poll_data["options"],
            is_anonymous=True,
        )
        await to_main_menu(message, state, "Опитування опубліковано.")
    except Exception as exc:
        logging.exception("Publish poll error: %s", exc)
        await to_main_menu(message, state, "Не вдалося опублікувати опитування.")


@dp.message()
async def fallback_handler(message: Message, state: FSMContext) -> None:
    if await deny_if_not_admin(message):
        return
    current_state = await state.get_state()
    if current_state:
        await message.answer("Використай кнопки нижче.")
    else:
        await message.answer("Натисни /start для запуску.")


async def on_startup() -> None:
    logging.info("Bot started")
    await bot.send_message(ADMIN_ID, "Бот запущено та готовий до роботи.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
