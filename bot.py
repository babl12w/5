import asyncio
import os
import random
import shutil
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
from aiogram.types import (
    FSInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from userbot import TgSoundUserbot

BOT_TOKEN = os.getenv("BOT_TOKEN")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

if not all(
    [
        BOT_TOKEN,
        UNSPLASH_ACCESS_KEY,
        ADMIN_ID,
        CHANNEL_ID,
        SPOTIFY_CLIENT_ID,
        SPOTIFY_CLIENT_SECRET,
    ]
):
    raise RuntimeError("Required environment variables are missing")

GENRES = ["Поп", "Рок", "Хіп-хоп", "Електроніка"]
LANGUAGES = ["Українська", "Рос", "Польська"]

POLL_TEMPLATES = [
    {
        "question": "Що зараз більше під ваш настрій?",
        "options": ["Спокійний чіл", "Енергійний драйв", "Легка ностальгія", "Щось нове"],
    },
    {
        "question": "Який вайб у вашому плейлисті сьогодні?",
        "options": ["Нічне місто", "Ранкова кава", "Дорога", "Домашній затишок"],
    },
    {
        "question": "Який жанр хочете чути частіше на каналі?",
        "options": ["Інді", "Поп", "Альтернатива", "Електроніка"],
    },
]

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Новий пост")],
        [KeyboardButton(text="Опитування")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
)

GENRE_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=genre)] for genre in GENRES] + [[KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

LANG_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=lang)] for lang in LANGUAGES] + [[KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

PREVIEW_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Опублікувати")], [KeyboardButton(text="Скасувати")]],
    resize_keyboard=True,
)

POLL_SELECT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Опитування 1")],
        [KeyboardButton(text="Опитування 2")],
        [KeyboardButton(text="Опитування 3")],
        [KeyboardButton(text="Скасувати")],
    ],
    resize_keyboard=True,
)


class PostFlow(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    preview_ready = State()


class PollFlow(StatesGroup):
    choosing_poll = State()
    preview_ready = State()


class SpotifyService:
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    SEARCH_URL = "https://api.spotify.com/v1/search"
    ARTIST_URL = "https://api.spotify.com/v1/artists/{artist_id}"

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
        async with session.post(
            self.TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=auth,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["access_token"]

    async def _search_tracks(
        self, session: aiohttp.ClientSession, token: str, query: str, market: str, limit: int
    ) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {token}"}
        params = {"q": query, "type": "track", "market": market, "limit": str(limit)}
        async with session.get(
            self.SEARCH_URL,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data.get("tracks", {}).get("items", [])

    async def _artist_genres(self, session: aiohttp.ClientSession, token: str, artist_id: str) -> str:
        headers = {"Authorization": f"Bearer {token}"}
        async with session.get(
            self.ARTIST_URL.format(artist_id=artist_id),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return ""
            data = await resp.json()
        genres = data.get("genres", [])
        return genres[0] if genres else ""

    async def get_two_tracks(self, genre: str, language: str) -> list[dict[str, str]]:
        market = {"Українська": "UA", "Рос": "RU", "Польська": "PL"}.get(language, "UA")
        async with aiohttp.ClientSession() as session:
            token = await self._get_token(session)
            attempts = [
                f'genre:"{genre.lower()}"',
                "music",
                "pop",
            ]

            chosen: list[dict[str, str]] = []
            for query in attempts:
                items = await self._search_tracks(session, token, query, market, 15)
                for item in items:
                    artists = item.get("artists", [])
                    if not artists:
                        continue
                    artist_name = artists[0].get("name", "Unknown")
                    artist_id = artists[0].get("id", "")
                    mood = await self._artist_genres(session, token, artist_id) if artist_id else ""
                    chosen.append(
                        {
                            "title": item.get("name", "Unknown"),
                            "artist": artist_name,
                            "mood": mood or genre,
                        }
                    )
                    if len(chosen) == 2:
                        return chosen
                if len(chosen) >= 2:
                    return chosen[:2]

            return chosen[:2]


class ContentService:
    def __init__(self, unsplash_key: str) -> None:
        self.unsplash_key = unsplash_key

    async def get_photo(self, genre: str) -> str:
        headers = {"Authorization": f"Client-ID {self.unsplash_key}"}
        params = {
            "query": f"{genre} music mood",
            "orientation": "landscape",
            "content_filter": "high",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.unsplash.com/photos/random",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data["urls"]["small"]

    async def make_quote(self) -> str:
        def parse_quote() -> str:
            feed = feedparser.parse("https://www.ukrinform.ua/rss/block-lastnews")
            titles = [entry.get("title", "").strip() for entry in feed.entries if entry.get("title")]
            random.shuffle(titles)
            selected = [t for t in titles[:3] if t]
            if not selected:
                return "Кожен звук — як подих надії.\nСлухай серцем."
            return "\n".join(selected[:4])

        return await asyncio.to_thread(parse_quote)

    async def download_binary(self, url: str, suffix: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                data = await resp.read()
        folder = Path(os.getenv("TMPDIR", "/tmp")) / f"music_post_{random.randint(1000, 999999)}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"file{suffix}"
        path.write_bytes(data)
        return str(path)


spotify_service = SpotifyService(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
content_service = ContentService(UNSPLASH_ACCESS_KEY)
userbot = TgSoundUserbot()


async def ensure_admin(message: Message) -> bool:
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("Доступ лише для адміністратора.")
        return False
    return True


async def clear_temp_files(state: FSMContext) -> None:
    data = await state.get_data()
    temp_files = data.get("temp_files", [])
    for file_path in temp_files:
        path = Path(file_path)
        if path.exists():
            parent = path.parent
            path.unlink(missing_ok=True)
            if parent.exists() and parent.name.startswith("music_post_"):
                shutil.rmtree(parent, ignore_errors=True)


async def cmd_start(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await state.clear()
    await message.answer("Оберіть дію:", reply_markup=MAIN_KB)


async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await clear_temp_files(state)
    await state.clear()
    await message.answer("Скасовано.", reply_markup=MAIN_KB)


async def new_post_handler(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await state.clear()
    await state.set_state(PostFlow.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=GENRE_KB)


async def choose_genre(message: Message, state: FSMContext) -> None:
    if message.text not in GENRES:
        await message.answer("Оберіть жанр кнопкою.")
        return
    await state.update_data(genre=message.text)
    await state.set_state(PostFlow.choosing_language)
    await message.answer("Оберіть мову:", reply_markup=LANG_KB)


async def choose_language(message: Message, state: FSMContext) -> None:
    if message.text not in LANGUAGES:
        await message.answer("Оберіть мову кнопкою.")
        return

    data = await state.get_data()
    genre = data["genre"]
    language = message.text

    tracks = await spotify_service.get_two_tracks(genre, language)
    if len(tracks) < 2:
        await message.bot.send_message(chat_id=ADMIN_ID, text="Не вдалося знайти достатньо треків.")
        await state.clear()
        await message.answer("Не вдалося підготувати пост.", reply_markup=MAIN_KB)
        return

    quote = await content_service.make_quote()
    photo_url = await content_service.get_photo(genre)
    photo_path = await content_service.download_binary(photo_url, ".jpg")

    audio_paths: list[str] = []
    for track in tracks:
        file_path = await userbot.fetch_mp3(track["title"], track["artist"])
        if not file_path:
            continue
        audio_paths.append(file_path)

    if len(audio_paths) < 2:
        await message.bot.send_message(chat_id=ADMIN_ID, text="Не вдалося знайти достатньо треків.")
        await state.clear()
        await message.answer("Не вдалося підготувати пост.", reply_markup=MAIN_KB)
        return

    preview_photo = await message.answer_photo(
        photo=FSInputFile(photo_path),
        caption=quote,
        reply_markup=ReplyKeyboardRemove(),
    )
    preview_audios: list[str] = []
    for i, path in enumerate(audio_paths[:2]):
        audio_msg = await message.answer_audio(
            audio=FSInputFile(path),
            title=tracks[i]["title"],
            performer=tracks[i]["artist"],
        )
        preview_audios.append(audio_msg.audio.file_id)

    await state.update_data(
        post_preview={
            "photo_id": preview_photo.photo[-1].file_id,
            "caption": quote,
            "audio_ids": preview_audios,
            "tracks": tracks[:2],
        },
        temp_files=[photo_path, *audio_paths],
    )
    await state.set_state(PostFlow.preview_ready)
    await message.answer("Прев'ю готове.", reply_markup=PREVIEW_KB)


async def publish_post(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    preview = data.get("post_preview")
    if not preview:
        await message.answer("Немає підготовленого посту.", reply_markup=MAIN_KB)
        await state.clear()
        return

    await message.bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=preview["photo_id"],
        caption=preview["caption"],
    )

    tracks = preview["tracks"]
    for i, audio_id in enumerate(preview["audio_ids"]):
        track = tracks[i]
        await message.bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=audio_id,
            title=track["title"],
            performer=track["artist"],
        )

    await clear_temp_files(state)
    await state.clear()
    await message.answer("Опубліковано.", reply_markup=MAIN_KB)


async def polls_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return
    await state.clear()
    await state.set_state(PollFlow.choosing_poll)
    await message.answer("Оберіть опитування:", reply_markup=POLL_SELECT_KB)


async def poll_choice(message: Message, state: FSMContext) -> None:
    mapping = {
        "Опитування 1": 0,
        "Опитування 2": 1,
        "Опитування 3": 2,
    }
    if message.text not in mapping:
        await message.answer("Оберіть опитування кнопкою.")
        return

    poll_data = POLL_TEMPLATES[mapping[message.text]]
    await message.answer_poll(
        question=poll_data["question"],
        options=poll_data["options"],
        is_anonymous=True,
    )
    await state.update_data(poll_preview=poll_data)
    await state.set_state(PollFlow.preview_ready)
    await message.answer("Прев'ю опитування готове.", reply_markup=PREVIEW_KB)


async def publish_poll(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    poll_data = data.get("poll_preview")
    if not poll_data:
        await message.answer("Немає підготовленого опитування.", reply_markup=MAIN_KB)
        await state.clear()
        return

    await message.bot.send_poll(
        chat_id=CHANNEL_ID,
        question=poll_data["question"],
        options=poll_data["options"],
        is_anonymous=True,
    )
    await state.clear()
    await message.answer("Опитування опубліковано.", reply_markup=MAIN_KB)


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cancel_handler, F.text == "Скасувати")

    dp.message.register(new_post_handler, F.text == "Новий пост")
    dp.message.register(choose_genre, PostFlow.choosing_genre)
    dp.message.register(choose_language, PostFlow.choosing_language)
    dp.message.register(publish_post, PostFlow.preview_ready, F.text == "Опублікувати")

    dp.message.register(polls_menu, F.text == "Опитування")
    dp.message.register(poll_choice, PollFlow.choosing_poll)
    dp.message.register(publish_poll, PollFlow.preview_ready, F.text == "Опублікувати")

    await userbot.start()
    try:
        await dp.start_polling(bot)
    finally:
        await userbot.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
