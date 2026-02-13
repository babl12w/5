import asyncio
import html
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")

RSS_FEEDS = [
    "https://archive.org/services/collection-rss.php?collection=opensource_audio",
    "https://freemusicarchive.org/playlist/rss",
]

GENRES = ["Pop", "Rock", "Electronic", "Hip-Hop"]
LANG_BUTTONS = {
    "🇺🇦 Українська": "uk",
    "🇷🇺 Російська": "ru",
    "🇵🇱 Польська": "pl",
}
LANG_LABELS = {"uk": "українською", "ru": "російською", "pl": "польською"}

LANGUAGE_KEYWORDS = {
    "uk": {
        "ukrainian",
        "україн",
        "uk",
        "ua",
        "україна",
    },
    "ru": {
        "russian",
        "русск",
        "рос",
        "ru",
    },
    "pl": {
        "polish",
        "polski",
        "polska",
        "поль",
        "pl",
    },
}

POLL_TEMPLATES = [
    {
        "question": "Який жанр сьогодні найкраще пасує до настрою?",
        "options": ["Pop", "Rock", "Electronic", "Hip-Hop"],
    },
    {
        "question": "Що публікувати частіше?",
        "options": ["Нові релізи", "Інді-артисти", "Саундтреки", "Ремікси"],
    },
    {
        "question": "Яка мова треків вам ближча?",
        "options": ["Українська", "Російська", "Польська", "Змішано"],
    },
    {
        "question": "Коли вам зручніше читати пости?",
        "options": ["Ранок", "День", "Вечір", "Ніч"],
    },
    {
        "question": "Скільки треків оптимально в одному пості?",
        "options": ["1", "2", "3", "4+"],
    },
]

UK_QUOTES_SOURCES = {
    "ukrainianpoetry": [
        "І все на світі треба пережити,\nІ кожен фініш — це, по суті, старт.\nІ наперед не треба ворожити,\nІ за минулим плакати не варт.",
        "Нації вмирають не від інфаркту.\nСпочатку їм відбирає мову.",
        "Людина нібито не літає...\nА крила має. А крила має!",
    ],
    "ukrclassic": [
        "Світ ловив мене, та не спіймав.",
        "Як добре те, що смерті не боюсь я\nі не питаю, чи тяжкий мій хрест.",
        "Борітеся — поборете,\nвам Бог помагає!",
    ],
}


class CreatePostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    confirming_post = State()
    choosing_poll = State()
    confirming_poll = State()


@dataclass
class Track:
    title: str
    artist: str
    url: str


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎵 Новий пост"), KeyboardButton(text="📊 Опитування")],
            [KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Оберіть дію",
    )


def genre_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Pop"), KeyboardButton(text="Rock")],
            [KeyboardButton(text="Electronic"), KeyboardButton(text="Hip-Hop")],
            [KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
    )


def language_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🇺🇦 Українська"), KeyboardButton(text="🇷🇺 Російська")],
            [KeyboardButton(text="🇵🇱 Польська")],
            [KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
    )


def poll_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"Опитування {i}")] for i in range(1, 6)
        ] + [[KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )


def confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Опублікувати"), KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
    )


def normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"\s+", " ", value)
    return value


def language_match(entry_data: str, lang: str) -> bool:
    data = normalize_text(entry_data)
    for kw in LANGUAGE_KEYWORDS[lang]:
        if re.search(rf"(^|[^a-zа-яіїєґ]){re.escape(kw)}([^a-zа-яіїєґ]|$)", data):
            return True
    return False


def parse_rss_entries(raw: bytes, lang: str, genre: str | None = None) -> list[Track]:
    parsed = feedparser.parse(raw)
    tracks: list[Track] = []
    genre_norm = normalize_text(genre) if genre else None

    for entry in parsed.entries:
        title = str(entry.get("title", "")).strip()
        if not title:
            continue

        artist = (
            str(entry.get("author", "")).strip()
            or str(entry.get("artist", "")).strip()
            or "Невідомий виконавець"
        )
        link = str(entry.get("link", "")).strip() or ""

        tags = " ".join([str(t.get("term", "")) for t in entry.get("tags", [])])
        pool = " ".join(
            [
                title,
                artist,
                str(entry.get("summary", "")),
                str(entry.get("description", "")),
                tags,
                link,
            ]
        )
        if not language_match(pool, lang):
            continue
        if genre_norm and genre_norm not in normalize_text(pool):
            continue

        tracks.append(Track(title=title[:120], artist=artist[:120], url=link))
    return tracks


async def fetch_rss_tracks(
    session: aiohttp.ClientSession,
    lang: str,
    genre: str | None,
    limit: int = 2,
) -> list[Track]:
    result: list[Track] = []
    seen: set[tuple[str, str]] = set()

    for feed_url in RSS_FEEDS:
        try:
            async with session.get(feed_url, timeout=20) as resp:
                if resp.status != 200:
                    continue
                raw = await resp.read()
            entries = parse_rss_entries(raw, lang=lang, genre=genre)
            for track in entries:
                key = (track.title.lower(), track.artist.lower())
                if key in seen:
                    continue
                seen.add(key)
                result.append(track)
                if len(result) >= limit:
                    return result
        except Exception:
            logging.exception("RSS feed read failed: %s", feed_url)
    return result


async def fetch_jamendo_tracks(
    session: aiohttp.ClientSession,
    lang: str,
    genre: str | None,
    limit: int = 2,
) -> list[Track]:
    if not JAMENDO_CLIENT_ID:
        return []

    language_map = {"uk": "ukrainian", "ru": "russian", "pl": "polish"}
    language_query = language_map[lang]
    base_url = "https://api.jamendo.com/v3.0/tracks/"

    params: dict[str, Any] = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": max(10, limit * 5),
        "include": "musicinfo",
        "order": "popularity_total",
        "audioformat": "mp31",
        "search": language_query,
    }
    if genre:
        params["tags"] = genre.lower()

    try:
        async with session.get(base_url, params=params, timeout=20) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json()
    except Exception:
        logging.exception("Jamendo request failed")
        return []

    data = payload.get("results", [])
    tracks: list[Track] = []
    for item in data:
        title = str(item.get("name", "")).strip()
        artist = str(item.get("artist_name", "")).strip() or "Невідомий виконавець"
        track_url = str(item.get("audio", "")).strip() or str(item.get("shareurl", "")).strip()
        pool = " ".join([title, artist, str(item.get("tags", "")), str(item.get("license_ccurl", ""))])
        if not language_match(pool + f" {language_query}", lang):
            continue
        tracks.append(Track(title=title[:120], artist=artist[:120], url=track_url))
        if len(tracks) >= limit:
            break
    return tracks


async def search_tracks(session: aiohttp.ClientSession, genre: str, lang: str) -> list[Track]:
    steps = [
        {"genre": genre},
        {"genre": None},
        {"genre": None},
        {"genre": None, "limit": 1},
    ]

    tracks: list[Track] = []
    for idx, step in enumerate(steps, start=1):
        needed = 2 if idx < 4 else 1
        if len(tracks) >= needed:
            break

        fetch_limit = step.get("limit", 2)
        found = await fetch_rss_tracks(session, lang=lang, genre=step.get("genre"), limit=fetch_limit)

        if idx == 3 and len(found) < 2:
            found = await fetch_jamendo_tracks(session, lang=lang, genre=None, limit=2)
        elif idx == 4 and len(found) < 1:
            found = await fetch_jamendo_tracks(session, lang=lang, genre=genre, limit=1)

        uniq: dict[tuple[str, str], Track] = {(t.title.lower(), t.artist.lower()): t for t in tracks}
        for item in found:
            key = (item.title.lower(), item.artist.lower())
            if key not in uniq:
                uniq[key] = item
        tracks = list(uniq.values())

        if idx < 4 and len(tracks) >= 2:
            return tracks[:2]
        if idx == 4 and tracks:
            return tracks[:2]

    return tracks[:2]


async def fetch_unsplash_photo(session: aiohttp.ClientSession, genre: str) -> str | None:
    if not UNSPLASH_ACCESS_KEY:
        return None
    url = "https://api.unsplash.com/photos/random"
    params = {
        "query": f"moody {genre} music aesthetic",
        "orientation": "portrait",
        "content_filter": "high",
    }
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    try:
        async with session.get(url, params=params, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
        return (
            data.get("urls", {}).get("small")
            or data.get("urls", {}).get("regular")
            or data.get("urls", {}).get("thumb")
        )
    except Exception:
        logging.exception("Unsplash request failed")
        return None


def get_ukrainian_quote() -> str | None:
    candidates: list[str] = []
    for source_quotes in UK_QUOTES_SOURCES.values():
        candidates.extend(source_quotes)
    if not candidates:
        return None
    selected = random.choice(candidates)
    lines = [line.strip() for line in selected.splitlines() if line.strip()]
    if len(lines) < 2:
        lines = [selected.strip(), ""]
    if len(lines) > 4:
        lines = lines[:4]
    return "\n".join(lines).strip()


async def notify_admin(bot: Bot, text: str) -> None:
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, text)
        except TelegramBadRequest:
            logging.exception("Failed to notify admin")


async def build_post_data(bot: Bot, genre: str, lang: str) -> dict[str, Any] | None:
    async with aiohttp.ClientSession() as session:
        tracks = await search_tracks(session=session, genre=genre, lang=lang)
        if not tracks:
            await notify_admin(
                bot,
                f"⚠️ Не вдалося знайти треки {LANG_LABELS[lang]} через RSS/Jamendo.",
            )
            return None

        photo_url = await fetch_unsplash_photo(session=session, genre=genre)
        quote = get_ukrainian_quote()

        if not quote:
            await notify_admin(bot, "⚠️ Джерела цитат недоступні.")
            return None

    tracks_text = "\n".join(
        [
            f"{idx}. 🎵 <b>{html.escape(track.title)}</b> — {html.escape(track.artist)}"
            for idx, track in enumerate(tracks[:2], start=1)
        ]
    )
    caption = f"{html.escape(quote)}\n\n{tracks_text}"

    return {
        "photo_url": photo_url,
        "caption": caption,
        "tracks_count": len(tracks[:2]),
    }


async def ensure_admin(message: Message) -> bool:
    if message.from_user and message.from_user.id == ADMIN_ID:
        return True
    await message.answer("Ця команда доступна лише адміну.")
    return False


async def reset_to_main(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Дію скасовано. Повертаю в головне меню.", reply_markup=main_menu_keyboard())


async def start_handler(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await state.clear()
    await message.answer(
        "Вітаю! Оберіть дію в меню.",
        reply_markup=main_menu_keyboard(),
    )


async def new_post_handler(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await state.set_state(CreatePostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_keyboard())


async def genre_chosen_handler(message: Message, state: FSMContext) -> None:
    genre = message.text or ""
    if genre not in GENRES:
        await message.answer("Будь ласка, оберіть жанр кнопками.")
        return
    await state.update_data(genre=genre)
    await state.set_state(CreatePostStates.choosing_language)
    await message.answer("Оберіть мову треків:", reply_markup=language_keyboard())


async def language_chosen_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    lang_key = message.text or ""
    if lang_key not in LANG_BUTTONS:
        await message.answer("Будь ласка, оберіть мову кнопками.")
        return

    await message.answer("Готую пост, зачекайте...", reply_markup=ReplyKeyboardRemove())
    data = await state.get_data()
    genre = data.get("genre")
    lang = LANG_BUTTONS[lang_key]

    post_data = await build_post_data(bot=bot, genre=genre, lang=lang)
    if not post_data:
        await state.clear()
        await message.answer("Не вдалося сформувати пост. Спробуйте пізніше.", reply_markup=main_menu_keyboard())
        return

    await state.update_data(
        pending_post=post_data,
        lang=lang,
    )
    await state.set_state(CreatePostStates.confirming_post)

    photo_url = post_data.get("photo_url")
    caption = post_data["caption"]
    if photo_url:
        try:
            await message.answer_photo(photo=photo_url, caption=caption, parse_mode=ParseMode.HTML)
        except Exception:
            logging.exception("Preview photo failed, sending text")
            await message.answer(caption, parse_mode=ParseMode.HTML)
    else:
        await notify_admin(bot, "⚠️ Unsplash недоступний, пост без фото в превʼю.")
        await message.answer(caption, parse_mode=ParseMode.HTML)

    await message.answer("Підтвердити публікацію?", reply_markup=confirm_keyboard())


async def publish_post_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    pending_post = data.get("pending_post")
    if not pending_post:
        await message.answer("Немає підготовленого поста.", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    photo_url = pending_post.get("photo_url")
    caption = pending_post.get("caption", "")

    try:
        if photo_url:
            await bot.send_photo(CHANNEL_ID, photo=photo_url, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(CHANNEL_ID, caption, parse_mode=ParseMode.HTML)
    except Exception:
        logging.exception("Failed to publish post")
        await message.answer("Не вдалося опублікувати пост.", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    await state.clear()
    await message.answer("✅ Пост опубліковано.", reply_markup=main_menu_keyboard())


async def polls_handler(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await state.set_state(CreatePostStates.choosing_poll)
    await message.answer("Оберіть опитування:", reply_markup=poll_keyboard())


async def poll_selected_handler(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    match = re.fullmatch(r"Опитування (\d)", text)
    if not match:
        await message.answer("Оберіть опитування кнопками.")
        return

    idx = int(match.group(1)) - 1
    if idx < 0 or idx >= len(POLL_TEMPLATES):
        await message.answer("Такого опитування немає.")
        return

    poll = POLL_TEMPLATES[idx]
    await state.update_data(selected_poll=poll)
    await state.set_state(CreatePostStates.confirming_poll)
    await message.answer(
        f"Обрано: {text}\n\nПитання: {poll['question']}",
        reply_markup=confirm_keyboard(),
    )


async def publish_poll_handler(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    poll = data.get("selected_poll")
    if not poll:
        await message.answer("Немає обраного опитування.", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    try:
        await bot.send_poll(
            chat_id=CHANNEL_ID,
            question=poll["question"],
            options=poll["options"],
            is_anonymous=False,
        )
    except Exception:
        logging.exception("Failed to publish poll")
        await message.answer("Не вдалося опублікувати опитування.", reply_markup=main_menu_keyboard())
        await state.clear()
        return

    await state.clear()
    await message.answer("✅ Опитування опубліковано.", reply_markup=main_menu_keyboard())


async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await reset_to_main(message, state)


def validate_env() -> None:
    missing = [
        name
        for name, value in {
            "BOT_TOKEN": BOT_TOKEN,
            "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
            "ADMIN_ID": ADMIN_ID,
            "CHANNEL_ID": CHANNEL_ID,
            "JAMENDO_CLIENT_ID": JAMENDO_CLIENT_ID,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


async def on_startup(bot: Bot) -> None:
    await notify_admin(bot, "✅ Бот запущено та готовий до роботи.")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    validate_env()

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.startup.register(on_startup)
    dp.message.register(start_handler, CommandStart())
    dp.message.register(cancel_handler, F.text == "❌ Скасувати")

    dp.message.register(new_post_handler, F.text == "🎵 Новий пост")
    dp.message.register(genre_chosen_handler, CreatePostStates.choosing_genre)
    dp.message.register(language_chosen_handler, CreatePostStates.choosing_language)
    dp.message.register(
        publish_post_handler,
        CreatePostStates.confirming_post,
        F.text == "✅ Опублікувати",
    )

    dp.message.register(polls_handler, F.text == "📊 Опитування")
    dp.message.register(poll_selected_handler, CreatePostStates.choosing_poll)
    dp.message.register(
        publish_poll_handler,
        CreatePostStates.confirming_poll,
        F.text == "✅ Опублікувати",
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")
