import asyncio
import logging
import os
import random
import re
import tempfile
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F, Router
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
)


@dataclass
class Track:
    title: str
    artist: str
    audio_url: str


class BotStates(StatesGroup):
    choosing_genre = State()
    choosing_poll = State()


GENRES = ["Pop", "Rock", "Hip-Hop", "Electronic"]

MUSIC_FEEDS_BY_GENRE: dict[str, list[str]] = {
    "Pop": [
        "https://archive.org/advancedsearch.php?" + urlencode(
            {
                "q": "collection:(opensource_audio) AND format:(VBR MP3) AND (subject:(pop) OR title:(pop))",
                "fl[]": ["identifier", "title", "creator", "mediatype", "format"],
                "rows": 100,
                "page": 1,
                "output": "rss",
            },
            doseq=True,
        ),
    ],
    "Rock": [
        "https://archive.org/advancedsearch.php?" + urlencode(
            {
                "q": "collection:(opensource_audio) AND format:(VBR MP3) AND (subject:(rock) OR title:(rock))",
                "fl[]": ["identifier", "title", "creator", "mediatype", "format"],
                "rows": 100,
                "page": 1,
                "output": "rss",
            },
            doseq=True,
        ),
    ],
    "Hip-Hop": [
        "https://archive.org/advancedsearch.php?" + urlencode(
            {
                "q": "collection:(opensource_audio) AND format:(VBR MP3) AND (subject:(hip-hop) OR subject:(hiphop) OR title:(hip hop))",
                "fl[]": ["identifier", "title", "creator", "mediatype", "format"],
                "rows": 100,
                "page": 1,
                "output": "rss",
            },
            doseq=True,
        ),
    ],
    "Electronic": [
        "https://archive.org/advancedsearch.php?" + urlencode(
            {
                "q": "collection:(opensource_audio) AND format:(VBR MP3) AND (subject:(electronic) OR subject:(edm) OR title:(electronic))",
                "fl[]": ["identifier", "title", "creator", "mediatype", "format"],
                "rows": 100,
                "page": 1,
                "output": "rss",
            },
            doseq=True,
        ),
    ],
}

GENERAL_MUSIC_FEEDS = [
    "https://archive.org/advancedsearch.php?" + urlencode(
        {
            "q": "collection:(opensource_audio) AND format:(VBR MP3)",
            "fl[]": ["identifier", "title", "creator", "mediatype", "format"],
            "rows": 100,
            "page": 1,
            "output": "rss",
        },
        doseq=True,
    ),
]

POPULAR_MUSIC_FEEDS = [
    "https://archive.org/advancedsearch.php?" + urlencode(
        {
            "q": "collection:(opensource_audio) AND format:(VBR MP3) AND downloads:[100 TO 100000000]",
            "fl[]": ["identifier", "title", "creator", "mediatype", "format", "downloads"],
            "rows": 120,
            "page": 1,
            "sort[]": ["downloads desc"],
            "output": "rss",
        },
        doseq=True,
    ),
]

EMERGENCY_TRACKS = [
    Track(
        title="SoundHelix Song 1",
        artist="SoundHelix",
        audio_url="https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3",
    ),
    Track(
        title="SoundHelix Song 2",
        artist="SoundHelix",
        audio_url="https://www.soundhelix.com/examples/mp3/SoundHelix-Song-2.mp3",
    ),
]

QUOTE_FEEDS = [
    "https://www.brainyquote.com/link/quotebr.rss",
    "https://www.goodreads.com/quotes/tag/music?format=rss",
]

POLL_TEMPLATES = {
    "Опитування 1": {
        "question": "Який музичний жанр сьогоднішнього вайбу?",
        "options": ["Pop", "Rock", "Hip-Hop", "Electronic"],
    },
    "Опитування 2": {
        "question": "Що слухаємо наступним постом?",
        "options": ["Легкий чіл", "Енергійний драйв", "Ретро хвиля", "Нове відкриття"],
    },
    "Опитування 3": {
        "question": "Як публікувати музичні підбірки?",
        "options": ["Щодня", "Через день", "Лише у вихідні", "Тільки вечірній формат"],
    },
}


class MusicChannelBot:
    def __init__(self) -> None:
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        self.unsplash_key = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
        self.admin_id = self._parse_int_env("ADMIN_ID")
        self.channel_id = os.getenv("CHANNEL_ID", "").strip()

        self._validate_env()

        self.bot = Bot(
            token=self.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher(storage=MemoryStorage())
        self.router = Router()
        self.dp.include_router(self.router)

        self.session: aiohttp.ClientSession | None = None
        self.temp_root = Path(tempfile.gettempdir()) / "music_channel_bot"
        self.temp_root.mkdir(parents=True, exist_ok=True)

        self.pending_posts: dict[int, dict[str, Any]] = {}
        self.pending_polls: dict[int, dict[str, Any]] = {}

        self._register_handlers()

    @staticmethod
    def _parse_int_env(name: str) -> int:
        raw = os.getenv(name, "").strip()
        if not raw:
            return 0
        try:
            return int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be integer") from exc

    def _validate_env(self) -> None:
        missing = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.unsplash_key:
            missing.append("UNSPLASH_ACCESS_KEY")
        if not self.admin_id:
            missing.append("ADMIN_ID")
        if not self.channel_id:
            missing.append("CHANNEL_ID")
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    @property
    def main_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🆕 Новий пост")],
                [KeyboardButton(text="📊 Опитування")],
                [KeyboardButton(text="❌ Скасувати")],
            ],
            resize_keyboard=True,
            is_persistent=True,
            one_time_keyboard=False,
        )

    @property
    def genre_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Pop"), KeyboardButton(text="Rock")],
                [KeyboardButton(text="Hip-Hop"), KeyboardButton(text="Electronic")],
                [KeyboardButton(text="❌ Скасувати")],
            ],
            resize_keyboard=True,
            is_persistent=True,
            one_time_keyboard=False,
        )

    @property
    def poll_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Опитування 1")],
                [KeyboardButton(text="Опитування 2")],
                [KeyboardButton(text="Опитування 3")],
                [KeyboardButton(text="❌ Скасувати")],
            ],
            resize_keyboard=True,
            is_persistent=True,
            one_time_keyboard=False,
        )

    @staticmethod
    def publish_cancel_inline(prefix: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"{prefix}:publish"),
                    InlineKeyboardButton(text="❌ Скасувати", callback_data=f"{prefix}:cancel"),
                ]
            ]
        )

    def _register_handlers(self) -> None:
        self.router.message.register(self.start_command, CommandStart())
        self.router.message.register(self.cancel_action, F.text == "❌ Скасувати")
        self.router.message.register(self.new_post_action, F.text == "🆕 Новий пост")
        self.router.message.register(self.poll_action, F.text == "📊 Опитування")
        self.router.message.register(self.genre_selected, BotStates.choosing_genre, F.text.in_(GENRES))
        self.router.message.register(self.poll_selected, BotStates.choosing_poll, F.text.in_(list(POLL_TEMPLATES.keys())))

        self.router.callback_query.register(self.handle_post_publish, F.data == "post:publish")
        self.router.callback_query.register(self.handle_post_cancel, F.data == "post:cancel")
        self.router.callback_query.register(self.handle_poll_publish, F.data == "poll:publish")
        self.router.callback_query.register(self.handle_poll_cancel, F.data == "poll:cancel")

        self.router.errors.register(self.error_handler)

    async def is_admin(self, message: Message) -> bool:
        return bool(message.from_user and message.from_user.id == self.admin_id)

    async def start_command(self, message: Message, state: FSMContext) -> None:
        if not await self.is_admin(message):
            await message.answer("Доступ заборонено.")
            return
        await state.clear()
        await message.answer(
            "Головне меню керування музичним каналом.",
            reply_markup=self.main_keyboard,
        )

    async def cancel_action(self, message: Message, state: FSMContext) -> None:
        if not await self.is_admin(message):
            return
        await state.clear()
        self.pending_posts.pop(message.chat.id, None)
        self.pending_polls.pop(message.chat.id, None)
        await message.answer("Дію скасовано. Повертаю у головне меню.", reply_markup=self.main_keyboard)

    async def new_post_action(self, message: Message, state: FSMContext) -> None:
        if not await self.is_admin(message):
            return
        await state.set_state(BotStates.choosing_genre)
        await message.answer("Оберіть жанр для нового посту.", reply_markup=self.genre_keyboard)

    async def poll_action(self, message: Message, state: FSMContext) -> None:
        if not await self.is_admin(message):
            return
        await state.set_state(BotStates.choosing_poll)
        await message.answer("Оберіть шаблон опитування.", reply_markup=self.poll_keyboard)

    async def genre_selected(self, message: Message, state: FSMContext) -> None:
        if not await self.is_admin(message):
            return
        genre = message.text.strip()
        await message.answer(f"Готую пост у жанрі <b>{genre}</b>...", reply_markup=self.main_keyboard)
        await state.clear()

        try:
            quote = await self.fetch_quote()
            image_path = await self.fetch_unsplash_image(genre)
            tracks, stage_used = await self.fetch_tracks_with_fallback(genre)

            if stage_used > 1:
                await message.answer(
                    "Не вдалося отримати достатньо треків з першої спроби, "
                    "використано резервний сценарій підбору."
                )

            track_files = await self.download_tracks(tracks)
            payload = {
                "genre": genre,
                "quote": quote,
                "image_path": image_path,
                "tracks": tracks,
                "track_files": track_files,
            }
            self.pending_posts[message.chat.id] = payload

            await self.send_post_preview(message.chat.id, payload)
            await message.answer(
                "Пост готовий. Перевірте превʼю та оберіть дію.",
                reply_markup=self.publish_cancel_inline("post"),
            )
        except Exception as exc:
            logging.exception("Failed to prepare post: %s", exc)
            await message.answer(
                "Виникла помилка під час генерації посту, але бот працює стабільно. Спробуйте ще раз.",
                reply_markup=self.main_keyboard,
            )

    async def poll_selected(self, message: Message, state: FSMContext) -> None:
        if not await self.is_admin(message):
            return
        key = message.text.strip()
        template = POLL_TEMPLATES[key]
        await state.clear()

        try:
            poll_msg = await self.bot.send_poll(
                chat_id=message.chat.id,
                question=template["question"],
                options=template["options"],
                is_anonymous=False,
                allows_multiple_answers=False,
            )
            self.pending_polls[message.chat.id] = {
                "question": template["question"],
                "options": template["options"],
                "preview_poll_message_id": poll_msg.message_id,
            }
            await message.answer(
                "Опитування готове до публікації.",
                reply_markup=self.publish_cancel_inline("poll"),
            )
        except Exception as exc:
            logging.exception("Failed to create poll preview: %s", exc)
            await message.answer("Не вдалося створити опитування. Спробуйте ще раз.", reply_markup=self.main_keyboard)

    async def handle_post_publish(self, callback) -> None:
        chat_id = callback.message.chat.id
        payload = self.pending_posts.get(chat_id)
        if not payload:
            await callback.answer("Немає підготовленого посту.", show_alert=True)
            return

        try:
            await self.publish_post(payload)
            await callback.answer("Опубліковано")
            await callback.message.answer("Пост опубліковано в канал.", reply_markup=self.main_keyboard)
        except Exception as exc:
            logging.exception("Failed to publish post: %s", exc)
            await callback.answer("Помилка публікації", show_alert=True)
            await callback.message.answer("Не вдалося опублікувати пост.", reply_markup=self.main_keyboard)
        finally:
            await self.cleanup_post_payload(payload)
            self.pending_posts.pop(chat_id, None)

    async def handle_post_cancel(self, callback) -> None:
        chat_id = callback.message.chat.id
        payload = self.pending_posts.pop(chat_id, None)
        if payload:
            await self.cleanup_post_payload(payload)
        await callback.answer("Скасовано")
        await callback.message.answer("Публікацію скасовано.", reply_markup=self.main_keyboard)

    async def handle_poll_publish(self, callback) -> None:
        chat_id = callback.message.chat.id
        payload = self.pending_polls.pop(chat_id, None)
        if not payload:
            await callback.answer("Немає підготовленого опитування.", show_alert=True)
            return

        try:
            await self.bot.send_poll(
                chat_id=self.channel_id,
                question=payload["question"],
                options=payload["options"],
                is_anonymous=False,
                allows_multiple_answers=False,
            )
            await callback.answer("Опубліковано")
            await callback.message.answer("Опитування опубліковано в канал.", reply_markup=self.main_keyboard)
        except Exception as exc:
            logging.exception("Failed to publish poll: %s", exc)
            await callback.answer("Помилка публікації", show_alert=True)

    async def handle_poll_cancel(self, callback) -> None:
        chat_id = callback.message.chat.id
        payload = self.pending_polls.pop(chat_id, None)
        if payload and payload.get("preview_poll_message_id"):
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=payload["preview_poll_message_id"])
            except TelegramBadRequest:
                pass
        await callback.answer("Скасовано")
        await callback.message.answer("Опитування скасовано.", reply_markup=self.main_keyboard)

    async def fetch_unsplash_image(self, genre: str) -> Path:
        if not self.session:
            raise RuntimeError("HTTP session not initialized")

        query = f"{genre} music vibe aesthetic mood"
        url = "https://api.unsplash.com/photos/random"
        headers = {"Authorization": f"Client-ID {self.unsplash_key}"}
        params = {"query": query, "orientation": "portrait", "content_filter": "high"}

        async with self.session.get(url, headers=headers, params=params, timeout=30) as response:
            response.raise_for_status()
            data = await response.json()

        image_url = data.get("urls", {}).get("regular")
        if not image_url:
            raise RuntimeError("Unsplash did not return image URL")

        image_path = self.temp_root / f"image_{uuid.uuid4().hex}.jpg"
        async with self.session.get(image_url, timeout=60) as response:
            response.raise_for_status()
            image_path.write_bytes(await response.read())
        return image_path

    async def fetch_quote(self) -> str:
        if not self.session:
            raise RuntimeError("HTTP session not initialized")

        entries: list[dict[str, Any]] = []
        for url in QUOTE_FEEDS:
            try:
                async with self.session.get(url, timeout=30) as response:
                    response.raise_for_status()
                    content = await response.text()
                parsed = feedparser.parse(content)
                entries.extend(parsed.entries)
            except Exception:
                logging.exception("Quote feed failed: %s", url)

        if not entries:
            return "Music gives a soul to the universe, wings to the mind, flight to the imagination, and life to everything."

        sample = random.choice(entries)
        raw = sample.get("summary") or sample.get("title") or "Music is life."
        text = self._clean_text(raw)
        text = self._format_quote_lines(text)
        return text

    @staticmethod
    def _clean_text(value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _format_quote_lines(text: str) -> str:
        if not text:
            return "Music is life."
        wrapped = textwrap.wrap(text, width=52)
        if len(wrapped) < 2:
            wrapped = textwrap.wrap(text + " Feel every beat.", width=52)
        if len(wrapped) > 4:
            wrapped = wrapped[:4]
        return "\n".join(wrapped)

    async def fetch_tracks_with_fallback(self, genre: str) -> tuple[list[Track], int]:
        stages = [
            (1, MUSIC_FEEDS_BY_GENRE.get(genre, [])),
            (2, GENERAL_MUSIC_FEEDS),
            (3, POPULAR_MUSIC_FEEDS),
        ]

        for stage_id, feeds in stages:
            tracks = await self.fetch_tracks_from_feeds(feeds, min_required=2)
            if len(tracks) >= 2:
                return tracks[:2], stage_id

        stage4_tracks = await self.fetch_tracks_from_feeds(
            MUSIC_FEEDS_BY_GENRE.get(genre, []) + GENERAL_MUSIC_FEEDS + POPULAR_MUSIC_FEEDS,
            min_required=1,
        )
        if stage4_tracks:
            chosen = stage4_tracks[:1]
            if len(chosen) < 2:
                chosen.append(random.choice(EMERGENCY_TRACKS))
            return chosen, 4

        return EMERGENCY_TRACKS[:2], 4

    async def fetch_tracks_from_feeds(self, feeds: list[str], min_required: int = 2) -> list[Track]:
        if not self.session:
            raise RuntimeError("HTTP session not initialized")

        pool: list[Track] = []
        seen: set[str] = set()

        for feed_url in feeds:
            try:
                async with self.session.get(feed_url, timeout=40) as response:
                    response.raise_for_status()
                    body = await response.text()
                parsed = feedparser.parse(body)

                for entry in parsed.entries:
                    track = self._extract_track(entry)
                    if not track:
                        continue
                    if track.audio_url in seen:
                        continue
                    seen.add(track.audio_url)
                    pool.append(track)

                if len(pool) >= max(12, min_required):
                    break
            except Exception:
                logging.exception("Music feed failed: %s", feed_url)

        random.shuffle(pool)
        return pool

    def _extract_track(self, entry: Any) -> Track | None:
        links = entry.get("links", [])
        possible_urls = []

        for item in links:
            href = item.get("href", "")
            item_type = item.get("type", "")
            rel = item.get("rel", "")
            if not href:
                continue
            if href.lower().endswith(".mp3"):
                possible_urls.append(href)
            elif "audio" in item_type.lower() or rel == "enclosure":
                possible_urls.append(href)

        if not possible_urls:
            direct = entry.get("enclosures", [])
            for encl in direct:
                href = encl.get("href", "")
                if href and (href.lower().endswith(".mp3") or "audio" in encl.get("type", "").lower()):
                    possible_urls.append(href)

        if not possible_urls:
            return None

        title = self._clean_text(entry.get("title", "Unknown track"))
        artist = self._clean_text(
            entry.get("author")
            or entry.get("creator")
            or entry.get("artist")
            or "Unknown artist"
        )

        return Track(title=title[:128], artist=artist[:64], audio_url=possible_urls[0])

    async def download_tracks(self, tracks: list[Track]) -> list[Path]:
        if not self.session:
            raise RuntimeError("HTTP session not initialized")

        paths: list[Path] = []
        for idx, track in enumerate(tracks, start=1):
            file_path = self.temp_root / f"track_{idx}_{uuid.uuid4().hex}.mp3"
            try:
                async with self.session.get(track.audio_url, timeout=120) as response:
                    response.raise_for_status()
                    file_path.write_bytes(await response.read())
                if file_path.stat().st_size == 0:
                    raise RuntimeError("Empty audio file")
                paths.append(file_path)
            except Exception:
                logging.exception("Failed to download track: %s", track.audio_url)
                if file_path.exists():
                    file_path.unlink(missing_ok=True)

        if len(paths) < 2:
            needed = 2 - len(paths)
            for fallback_track in EMERGENCY_TRACKS[:needed]:
                file_path = self.temp_root / f"fallback_{uuid.uuid4().hex}.mp3"
                async with self.session.get(fallback_track.audio_url, timeout=120) as response:
                    response.raise_for_status()
                    file_path.write_bytes(await response.read())
                paths.append(file_path)

        return paths[:2]

    async def send_post_preview(self, chat_id: int, payload: dict[str, Any]) -> None:
        image = FSInputFile(str(payload["image_path"]))
        await self.bot.send_photo(chat_id=chat_id, photo=image, caption=payload["quote"])

        for idx, audio_path in enumerate(payload["track_files"]):
            track = payload["tracks"][idx] if idx < len(payload["tracks"]) else EMERGENCY_TRACKS[idx]
            audio = FSInputFile(str(audio_path))
            await self.bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title=track.title,
                performer=track.artist,
            )

    async def publish_post(self, payload: dict[str, Any]) -> None:
        image = FSInputFile(str(payload["image_path"]))
        await self.bot.send_photo(chat_id=self.channel_id, photo=image, caption=payload["quote"])

        for idx, audio_path in enumerate(payload["track_files"]):
            track = payload["tracks"][idx] if idx < len(payload["tracks"]) else EMERGENCY_TRACKS[idx]
            audio = FSInputFile(str(audio_path))
            await self.bot.send_audio(
                chat_id=self.channel_id,
                audio=audio,
                title=track.title,
                performer=track.artist,
            )

    async def cleanup_post_payload(self, payload: dict[str, Any]) -> None:
        for path in [payload.get("image_path"), *(payload.get("track_files", []))]:
            if isinstance(path, Path) and path.exists():
                path.unlink(missing_ok=True)

    async def error_handler(self, event, exception) -> bool:
        logging.exception("Unhandled update error: %s", exception)
        return True

    async def run(self) -> None:
        logging.info("Starting bot")
        async with aiohttp.ClientSession() as session:
            self.session = session
            await self.dp.start_polling(self.bot)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def main() -> None:
    configure_logging()
    app = MusicChannelBot()
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
