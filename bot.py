import asyncio
import html
import json
import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import aiohttp
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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music-channel-bot")


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is required")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required")


MAIN_MENU_TEXT_NEW = "🆕 Новий пост"
MAIN_MENU_TEXT_CANCEL = "❌ Скасувати"
MAIN_MENU_TEXT_POLL = "📊 Опитування"
POLL_BUTTON_TEXT = "📊 Опитування"
PUBLISH_BUTTON_TEXT = "✅ Опублікувати"


GENRES = {
    "lofi": "Lo-Fi",
    "ambient": "Ambient",
    "indie": "Indie",
    "chill": "Chill",
}

LANGUAGES = {
    "uk": "Українська",
    "ru": "Русский",
    "pl": "Polski",
}

LANGUAGE_HINTS = {
    "uk": ["україн", "ukrain", "ukr", "київ", "львів", "ї", "є", "і"],
    "ru": ["рус", "russian", "moscow", "питер", "ы", "э", "ё"],
    "pl": ["pol", "polish", "warsz", "krak", "ł", "ą", "ę", "ó", "ż", "ź", "ć", "ń", "ś"],
}

AUDIO_EXTENSIONS = (".mp3", ".ogg", ".wav", ".flac", ".m4a")

POLL_TEMPLATES = {
    "1": {"question": "Наскільки зайшов вайб цього посту?", "options": ["1", "2", "3", "4", "5"]},
    "2": {"question": "Оцініть підбір треків", "options": ["1", "2", "3", "4", "5"]},
    "3": {"question": "Чи хочете ще пост у цьому жанрі?", "options": ["1", "2", "3", "4", "5"]},
    "4": {"question": "Наскільки сподобалась атмосфера?", "options": ["1", "2", "3", "4", "5"]},
    "5": {"question": "Загальна оцінка посту", "options": ["1", "2", "3", "4", "5"]},
}

QUOTE_SOURCES = [
    [
        "Тиша не порожня — вона наповнена відповідями.",
        "Там, де спокій, народжується сила.",
        "Зупинись на мить і відчуй, як дихає світ.",
    ],
    [
        "Музика лікує те, що не вміють слова.",
        "Кожен новий день має свій ритм.",
        "Навіть маленький крок уперед — це вже рух до себе.",
    ],
]


class PostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    content_ready = State()
    choosing_poll = State()
    ready_to_publish = State()


@dataclass
class Track:
    title: str
    artist: str
    source_url: str
    local_path: str | None = None


router = Router()


class ContentService:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.temp_dir = Path(tempfile.gettempdir()) / "tg_music_bot"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self, genre: str, language: str) -> tuple[list[Track], str, str]:
        tracks = await self._find_tracks(genre=genre, language=language)
        if len(tracks) < 2:
            raise RuntimeError("Не вдалося знайти щонайменше 2 треки.")

        quote = self._random_quote_ua()
        photo_path = await self._download_photo(genre)

        downloaded = []
        for idx, track in enumerate(tracks[:2], start=1):
            file_path = await self._download_track(track, idx)
            track.local_path = file_path
            downloaded.append(track)
        return downloaded, photo_path, quote

    async def _find_tracks(self, genre: str, language: str) -> list[Track]:
        collected: list[Track] = []
        queries = [
            {"genre": genre, "language": language, "random": False},
            {"genre": None, "language": language, "random": False},
            {"genre": None, "language": language, "random": True},
            {"genre": None, "language": None, "random": True},
        ]

        for q in queries:
            if len(collected) >= 2:
                break
            missing = 2 - len(collected)
            found = await self._fetch_from_rss_sources(limit=missing, **q)
            collected.extend(self._unique_tracks(collected, found))

            if len(collected) < 2:
                jamendo_found = await self._fetch_from_jamendo(limit=missing, **q)
                collected.extend(self._unique_tracks(collected, jamendo_found))

        return collected[:2]

    def _unique_tracks(self, existing: list[Track], incoming: list[Track]) -> list[Track]:
        seen = {(t.title.lower().strip(), t.artist.lower().strip()) for t in existing}
        result = []
        for item in incoming:
            key = (item.title.lower().strip(), item.artist.lower().strip())
            if key not in seen and item.source_url:
                result.append(item)
                seen.add(key)
        return result

    async def _fetch_text(self, url: str) -> str:
        async with self.session.get(url, timeout=20) as resp:
            resp.raise_for_status()
            return await resp.text()

    async def _fetch_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self.session.get(url, params=params, timeout=20) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _fetch_from_rss_sources(
        self,
        limit: int,
        genre: str | None,
        language: str | None,
        random: bool,
    ) -> list[Track]:
        rss_urls = [
            "https://archive.org/services/collection-rss.php?collection=opensource_audio",
            "https://freemusicarchive.org/playlist/rss",
        ]
        tracks: list[Track] = []
        for rss_url in rss_urls:
            try:
                xml_text = await self._fetch_text(rss_url)
                parsed = self._parse_rss_tracks(xml_text)
                filtered = self._filter_tracks(parsed, genre=genre, language=language)
                if random:
                    random.shuffle(filtered)
                tracks.extend(filtered[:limit])
                if len(tracks) >= limit:
                    return tracks[:limit]
            except Exception as exc:
                logger.warning("RSS source failed %s: %s", rss_url, exc)
        return tracks[:limit]

    def _parse_rss_tracks(self, xml_text: str) -> list[Track]:
        root = ElementTree.fromstring(xml_text)
        items = root.findall(".//item")
        parsed_tracks: list[Track] = []
        for item in items:
            title_raw = (item.findtext("title") or "").strip()
            if not title_raw:
                continue

            enclosure_url = ""
            enclosure = item.find("enclosure")
            if enclosure is not None:
                enclosure_url = (enclosure.attrib.get("url") or "").strip()

            description = item.findtext("description") or ""
            links_from_desc = re.findall(r"https?://[^\s\"'<>]+", description)
            desc_audio = next((u for u in links_from_desc if self._is_audio_url(u)), "")

            link = (item.findtext("link") or "").strip()
            source_url = enclosure_url or desc_audio or (link if self._is_audio_url(link) else "")
            if not source_url:
                continue

            artist, title = self._split_artist_title(title_raw)
            parsed_tracks.append(Track(title=title, artist=artist, source_url=source_url))

        return parsed_tracks

    def _split_artist_title(self, title_raw: str) -> tuple[str, str]:
        cleaned = html.unescape(title_raw)
        for sep in [" - ", " — ", " – ", " —", "-"]:
            if sep in cleaned:
                left, right = cleaned.split(sep, 1)
                if left.strip() and right.strip():
                    return left.strip(), right.strip()
        return "Unknown Artist", cleaned.strip()

    def _contains_language_hint(self, text: str, language: str) -> bool:
        low = text.lower()
        hints = LANGUAGE_HINTS.get(language, [])
        return any(h in low for h in hints)

    def _is_audio_url(self, url: str) -> bool:
        low = url.lower()
        return any(ext in low for ext in AUDIO_EXTENSIONS)

    def _filter_tracks(
        self,
        tracks: list[Track],
        genre: str | None,
        language: str | None,
    ) -> list[Track]:
        result = tracks
        if genre:
            g = genre.lower()
            result = [
                t
                for t in result
                if g in t.title.lower() or g in t.artist.lower() or g in t.source_url.lower()
            ] or result

        if language:
            by_lang = [
                t
                for t in result
                if self._contains_language_hint(f"{t.artist} {t.title}", language)
            ]
            if by_lang:
                result = by_lang

        random.shuffle(result)
        return result

    async def _fetch_from_jamendo(
        self,
        limit: int,
        genre: str | None,
        language: str | None,
        random: bool,
    ) -> list[Track]:
        if not JAMENDO_CLIENT_ID:
            return []

        tags = []
        if genre:
            tags.append(genre)
        if language:
            tags.append(language)

        params = {
            "client_id": JAMENDO_CLIENT_ID,
            "format": "json",
            "limit": max(10, limit * 4),
            "include": "musicinfo",
            "order": "popularity_total" if random else "relevance",
            "search": " ".join(tags) if tags else "popular",
            "audioformat": "mp32",
        }

        try:
            data = await self._fetch_json("https://api.jamendo.com/v3.0/tracks/", params)
        except Exception as exc:
            logger.warning("Jamendo failed: %s", exc)
            return []

        tracks: list[Track] = []
        for item in data.get("results", []):
            audio = (item.get("audio") or "").strip()
            if not audio:
                continue
            title = (item.get("name") or "Unknown Title").strip()
            artist = (item.get("artist_name") or "Unknown Artist").strip()
            track = Track(title=title, artist=artist, source_url=audio)
            tracks.append(track)

        filtered = self._filter_tracks(tracks, genre=genre, language=language)
        if random:
            random.shuffle(filtered)
        return filtered[:limit]

    async def _download_track(self, track: Track, idx: int) -> str:
        safe_artist = re.sub(r"[^\w\-]+", "_", track.artist, flags=re.UNICODE)[:30]
        safe_title = re.sub(r"[^\w\-]+", "_", track.title, flags=re.UNICODE)[:30]
        filename = f"track_{idx}_{safe_artist}_{safe_title}.mp3"
        filepath = self.temp_dir / filename
        async with self.session.get(track.source_url, timeout=60) as resp:
            resp.raise_for_status()
            data = await resp.read()
        filepath.write_bytes(data)
        return str(filepath)

    async def _download_photo(self, genre: str) -> str:
        if UNSPLASH_ACCESS_KEY:
            params = {
                "query": f"{genre} music mood",
                "orientation": "landscape",
                "content_filter": "high",
                "client_id": UNSPLASH_ACCESS_KEY,
            }
            try:
                url = f"https://api.unsplash.com/photos/random?{urlencode(params)}"
                data = await self._fetch_json(url, params={})
                image_url = data.get("urls", {}).get("small")
                if image_url:
                    return await self._download_image(image_url)
            except Exception as exc:
                logger.warning("Unsplash failed: %s", exc)
        return await self._download_image("https://images.unsplash.com/photo-1470225620780-dba8ba36b745?w=900")

    async def _download_image(self, url: str) -> str:
        filename = f"cover_{random.randint(1000, 999999)}.jpg"
        filepath = self.temp_dir / filename
        async with self.session.get(url, timeout=30) as resp:
            resp.raise_for_status()
            data = await resp.read()
        filepath.write_bytes(data)
        return str(filepath)

    def _random_quote_ua(self) -> str:
        source = random.choice(QUOTE_SOURCES)
        quote = random.choice(source)
        lines = quote.split(" — ")
        if len(lines) < 2:
            parts = quote.split(" ")
            if len(parts) > 6:
                midpoint = len(parts) // 2
                lines = [" ".join(parts[:midpoint]), " ".join(parts[midpoint:])]
            else:
                lines = [quote]
        if len(lines) == 1:
            lines.append("Нехай цей треклист зробить день м'якшим.")
        return "\n".join(lines[:4])


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MAIN_MENU_TEXT_NEW)],
            [KeyboardButton(text=MAIN_MENU_TEXT_POLL), KeyboardButton(text=MAIN_MENU_TEXT_CANCEL)],
        ],
        resize_keyboard=True,
    )


def genre_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"genre:{key}")] for key, label in GENRES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def language_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"lang:{key}")] for key, label in LANGUAGES.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def poll_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=POLL_BUTTON_TEXT, callback_data="poll:open")]]
    )


def poll_select_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"Опитування {k}", callback_data=f"poll:template:{k}")] for k in POLL_TEMPLATES]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=PUBLISH_BUTTON_TEXT, callback_data="publish:now")],
            [InlineKeyboardButton(text=MAIN_MENU_TEXT_CANCEL, callback_data="cancel:any")],
        ]
    )


async def is_admin(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == ADMIN_ID


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if not await is_admin(message):
        await message.answer("Доступ лише для адміністратора.")
        return
    await state.clear()
    await message.answer(
        "Вітаю! Оберіть дію з меню нижче.",
        reply_markup=main_keyboard(),
    )


@router.message(F.text == MAIN_MENU_TEXT_NEW)
async def start_new_post(message: Message, state: FSMContext):
    if not await is_admin(message):
        return
    await state.clear()
    await state.set_state(PostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_keyboard())


@router.message(F.text == MAIN_MENU_TEXT_CANCEL)
async def cancel_from_menu(message: Message, state: FSMContext):
    if not await is_admin(message):
        return
    await state.clear()
    await message.answer("Скасовано. Повертаю в головне меню.", reply_markup=main_keyboard())


@router.message(F.text == MAIN_MENU_TEXT_POLL)
async def menu_poll_hint(message: Message, state: FSMContext):
    if not await is_admin(message):
        return
    current_state = await state.get_state()
    if current_state not in {PostStates.content_ready.state, PostStates.choosing_poll.state, PostStates.ready_to_publish.state}:
        await message.answer("Спочатку створіть контент через «🆕 Новий пост».", reply_markup=main_keyboard())
        return
    await message.answer("Оберіть шаблон опитування:", reply_markup=poll_select_keyboard())


@router.callback_query(F.data.startswith("genre:"), PostStates.choosing_genre)
async def pick_genre(callback, state: FSMContext):
    genre_key = callback.data.split(":", 1)[1]
    if genre_key not in GENRES:
        await callback.answer("Невідомий жанр", show_alert=True)
        return
    await state.update_data(genre=genre_key)
    await state.set_state(PostStates.choosing_language)
    await callback.message.answer("Тепер оберіть мову треків:", reply_markup=language_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("lang:"), PostStates.choosing_language)
async def pick_language(callback, state: FSMContext, bot: Bot):
    language_key = callback.data.split(":", 1)[1]
    if language_key not in LANGUAGES:
        await callback.answer("Невідома мова", show_alert=True)
        return

    data = await state.get_data()
    genre = data.get("genre")
    if genre not in GENRES:
        await callback.answer("Оберіть жанр спочатку", show_alert=True)
        return

    await callback.answer("Генерую контент...")
    service: ContentService = bot["content_service"]
    try:
        tracks, photo_path, quote = await service.generate(genre=genre, language=language_key)
    except Exception as exc:
        logger.exception("Generate content failed")
        await callback.message.answer(
            f"На жаль, RSS/API зараз не дали достатньо треків: {exc}\nСпробуйте ще раз.",
            reply_markup=main_keyboard(),
        )
        await state.clear()
        return

    await state.update_data(
        genre=genre,
        language=language_key,
        quote=quote,
        photo_path=photo_path,
        tracks=[track.__dict__ for track in tracks],
    )
    await state.set_state(PostStates.content_ready)

    await callback.message.answer_photo(photo=FSInputFile(photo_path), caption=quote)
    for tr in tracks:
        caption = f"<b>{html.escape(tr.artist)}</b> — {html.escape(tr.title)}"
        await callback.message.answer_audio(audio=FSInputFile(tr.local_path), caption=caption)

    await callback.message.answer(
        "Контент готовий. Далі запустіть опитування.",
        reply_markup=poll_start_keyboard(),
    )


@router.callback_query(F.data == "poll:open")
async def open_poll_templates(callback, state: FSMContext):
    current_state = await state.get_state()
    if current_state != PostStates.content_ready.state:
        await callback.answer("Спершу згенеруйте контент.", show_alert=True)
        return
    await state.set_state(PostStates.choosing_poll)
    await callback.message.answer("Оберіть шаблон опитування:", reply_markup=poll_select_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("poll:template:"), PostStates.choosing_poll)
async def create_poll(callback, state: FSMContext):
    poll_id = callback.data.split(":")[-1]
    tpl = POLL_TEMPLATES.get(poll_id)
    if not tpl:
        await callback.answer("Невірний шаблон", show_alert=True)
        return

    poll_message = await callback.message.answer_poll(
        question=tpl["question"],
        options=tpl["options"],
        is_anonymous=False,
        allows_multiple_answers=False,
    )

    await state.update_data(
        poll_template_id=poll_id,
        poll_message_id=poll_message.message_id,
    )
    await state.set_state(PostStates.ready_to_publish)
    await callback.message.answer("Опитування створено. Можна публікувати пост.", reply_markup=publish_keyboard())
    await callback.answer("Готово")


@router.callback_query(F.data == "publish:now", PostStates.ready_to_publish)
async def publish_now(callback, state: FSMContext, bot: Bot):
    data = await state.get_data()
    photo_path = data.get("photo_path")
    quote = data.get("quote")
    tracks_data = data.get("tracks") or []

    if not photo_path or len(tracks_data) < 2:
        await callback.answer("Немає достатніх даних для публікації", show_alert=True)
        return

    try:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=FSInputFile(photo_path), caption=quote)
        for tr in tracks_data[:2]:
            caption = f"<b>{html.escape(tr['artist'])}</b> — {html.escape(tr['title'])}"
            await bot.send_audio(chat_id=CHANNEL_ID, audio=FSInputFile(tr["local_path"]), caption=caption)
    except TelegramBadRequest as exc:
        logger.exception("Publish failed")
        await callback.message.answer(f"Помилка публікації: {exc}", reply_markup=main_keyboard())
        await state.clear()
        return

    await callback.message.answer("Пост миттєво опубліковано в канал ✅", reply_markup=main_keyboard())
    await state.clear()
    await callback.answer("Опубліковано")


@router.callback_query(F.data == "cancel:any")
async def cancel_inline(callback, state: FSMContext):
    await state.clear()
    await callback.message.answer("Скасовано. Головне меню активне.", reply_markup=main_keyboard())
    await callback.answer("Скасовано")


@router.message()
async def fallback(message: Message):
    if await is_admin(message):
        await message.answer("Скористайтеся кнопками меню.", reply_markup=main_keyboard())


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    timeout = aiohttp.ClientTimeout(total=90)
    session = aiohttp.ClientSession(timeout=timeout)
    bot["content_service"] = ContentService(session)

    dp.include_router(router)

    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        await session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
