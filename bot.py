import asyncio
import logging
import os
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from PIL import Image, ImageDraw, ImageFont

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "")

if not all([BOT_TOKEN, ADMIN_ID, CHANNEL_ID, UNSPLASH_ACCESS_KEY, JAMENDO_CLIENT_ID]):
    missing = [
        key
        for key, value in {
            "BOT_TOKEN": BOT_TOKEN,
            "ADMIN_ID": str(ADMIN_ID) if ADMIN_ID else "",
            "CHANNEL_ID": CHANNEL_ID,
            "UNSPLASH_ACCESS_KEY": UNSPLASH_ACCESS_KEY,
            "JAMENDO_CLIENT_ID": JAMENDO_CLIENT_ID,
        }.items()
        if not value
    ]
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("music-post-bot")

GENRES = ["Pop", "Rock", "Electronic", "Hip-Hop"]
LANGUAGE_LABELS = ["🇺🇦 Українська", "🇷🇺 Російська", "🇵🇱 Польська"]
LANGUAGE_CONFIG = {
    "🇺🇦 Українська": {"iso": "uk", "jamendo_tag": "ukrainian"},
    "🇷🇺 Російська": {"iso": "ru", "jamendo_tag": "russian"},
    "🇵🇱 Польська": {"iso": "pl", "jamendo_tag": "polish"},
}
POLL_TEMPLATES: dict[str, tuple[str, list[str]]] = {
    "Опитування 1": ("Текст опитування 1", ["Варіант 1", "Варіант 2", "Варіант 3"]),
    "Опитування 2": ("Текст опитування 2", ["Варіант 1", "Варіант 2", "Варіант 3"]),
    "Опитування 3": ("Текст опитування 3", ["Варіант 1", "Варіант 2", "Варіант 3"]),
    "Опитування 4": ("Текст опитування 4", ["Варіант 1", "Варіант 2", "Варіант 3"]),
    "Опитування 5": ("Текст опитування 5", ["Варіант 1", "Варіант 2", "Варіант 3"]),
}


@dataclass
class TrackItem:
    track_id: str
    name: str
    artist: str
    jamendo_url: str
    audio_path: str


class PostStates(StatesGroup):
    choosing_genre = State()
    choosing_language = State()
    preview_ready = State()
    choosing_poll = State()
    confirm_publish = State()


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1️⃣ Новий пост")],
            [KeyboardButton(text="2️⃣ Скасувати")],
        ],
        resize_keyboard=True,
    )


def genre_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=g)] for g in GENRES] + [[KeyboardButton(text="2️⃣ Скасувати")]],
        resize_keyboard=True,
    )


def language_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=l)] for l in LANGUAGE_LABELS] + [[KeyboardButton(text="2️⃣ Скасувати")]],
        resize_keyboard=True,
    )


def poll_start_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Опитування")], [KeyboardButton(text="2️⃣ Скасувати")]],
        resize_keyboard=True,
    )


def poll_select_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=key)] for key in POLL_TEMPLATES.keys()] + [[KeyboardButton(text="2️⃣ Скасувати")]],
        resize_keyboard=True,
    )


def publish_confirm_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✅ Опублікувати")], [KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )


async def cleanup_files(state: FSMContext) -> None:
    data = await state.get_data()
    paths = data.get("temp_files", [])
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to delete temp file: %s", path)


def track_caption(index: int, item: TrackItem) -> str:
    return (
        f"Пісня №{index}\n"
        f"Назва: {item.name}\n"
        f"Виконавець: {item.artist}\n"
        f"Jamendo: {item.jamendo_url}"
    )


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as response:
        response.raise_for_status()
        return await response.json()


async def jamendo_request(
    session: aiohttp.ClientSession,
    language_tag: str,
    genre: str | None,
    order: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "client_id": JAMENDO_CLIENT_ID,
        "format": "json",
        "limit": limit,
        "audioformat": "mp32",
        "include": "musicinfo",
        "tags": language_tag,
        "audiodownload_allowed": "true",
        "order": order,
    }
    if genre:
        params["fuzzytags"] = genre.lower()
    payload = await fetch_json(session, "https://api.jamendo.com/v3.0/tracks/", params)
    return payload.get("results", [])


async def collect_tracks(session: aiohttp.ClientSession, genre: str, language_tag: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()

    async def take(items: list[dict[str, Any]]) -> None:
        for item in items:
            tid = str(item.get("id", ""))
            if not tid or tid in seen:
                continue
            if not item.get("audiodownload"):
                continue
            seen.add(tid)
            collected.append(item)
            if len(collected) >= 2:
                return

    await take(await jamendo_request(session, language_tag, genre, "popularity_total"))
    if len(collected) < 2:
        await take(await jamendo_request(session, language_tag, None, "popularity_total"))
    if len(collected) < 2:
        random_items = await jamendo_request(session, language_tag, None, "popularity_week", limit=30)
        random_items = random_items[::-1]
        await take(random_items)
    if len(collected) < 2 and collected:
        collected.append(collected[0])
    return collected[:2]


async def download_binary(session: aiohttp.ClientSession, url: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="tg_bot_", suffix=suffix)
    os.close(fd)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        response.raise_for_status()
        content = await response.read()
    with open(path, "wb") as f:
        f.write(content)
    return path


async def get_unsplash_image(session: aiohttp.ClientSession, genre: str) -> str:
    params = {
        "query": f"moody {genre} music aesthetic",
        "orientation": "portrait",
        "content_filter": "high",
    }
    headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    async with session.get(
        "https://api.unsplash.com/photos/random",
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        payload = await response.json()
    image_url = payload.get("urls", {}).get("small") or payload.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash did not return an image URL")
    return await download_binary(session, image_url, ".jpg")


async def get_quote_sources(session: aiohttp.ClientSession) -> list[str]:
    quotes: list[str] = []
    try:
        q1 = await fetch_json(session, "https://api.quotable.io/random", {"maxLength": 180})
        content = q1.get("content", "").strip()
        if content:
            quotes.append(content)
    except Exception:
        logger.exception("Failed to fetch quote from quotable")

    try:
        q2_payload = await fetch_json(session, "https://zenquotes.io/api/random")
        if q2_payload and isinstance(q2_payload, list):
            content = q2_payload[0].get("q", "").strip()
            if content:
                quotes.append(content)
    except Exception:
        logger.exception("Failed to fetch quote from zenquotes")

    if len(quotes) < 2:
        quotes.extend(
            [
                "Життя змінюється там, де починається твоя сміливість.",
                "Кожен ранок — новий шанс зробити день особливим.",
            ]
        )
    return quotes[:2]


async def translate_to_ukrainian(session: aiohttp.ClientSession, text: str) -> str:
    params = {"q": text, "langpair": "en|uk"}
    async with session.get(
        "https://api.mymemory.translated.net/get",
        params=params,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        response.raise_for_status()
        payload = await response.json()
    translated = payload.get("responseData", {}).get("translatedText", "").strip()
    return translated or text


def normalize_quote(text: str) -> str:
    cleaned = " ".join(text.replace("\n", " ").split())
    lines = textwrap.wrap(cleaned, width=26)
    if len(lines) < 2:
        midpoint = max(1, len(cleaned) // 2)
        lines = [cleaned[:midpoint].strip(), cleaned[midpoint:].strip()]
    lines = [line for line in lines if line][:4]
    return "\n".join(lines[:4])


def overlay_quote(image_path: str, quote: str) -> str:
    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)

    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    font = None
    for candidate in font_candidates:
        if Path(candidate).exists():
            font = ImageFont.truetype(candidate, size=max(26, width // 20))
            break
    if not font:
        font = ImageFont.load_default()

    text = normalize_quote(quote)
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=12, align="center")
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2
    y = int(height * 0.65)

    padding = 24
    draw.rounded_rectangle(
        [(x - padding, y - padding), (x + tw + padding, y + th + padding)],
        radius=18,
        fill=(0, 0, 0, 150),
    )
    draw.multiline_text((x, y), text, font=font, fill=(255, 255, 255), spacing=12, align="center")

    out_path = image_path.replace(".jpg", "_quote.jpg")
    image.save(out_path, quality=88, optimize=True)
    return out_path


async def build_post_content(genre: str, language_label: str) -> tuple[str, list[TrackItem], str, list[str]]:
    cfg = LANGUAGE_CONFIG[language_label]
    temp_files: list[str] = []

    async with aiohttp.ClientSession() as session:
        tracks_raw = await collect_tracks(session, genre, cfg["jamendo_tag"])
        if not tracks_raw:
            raise RuntimeError("Не вдалося знайти треки в Jamendo")

        track_items: list[TrackItem] = []
        for item in tracks_raw:
            audio_url = item.get("audiodownload") or item.get("audio")
            if not audio_url:
                continue
            audio_path = await download_binary(session, audio_url, ".mp3")
            temp_files.append(audio_path)
            track_items.append(
                TrackItem(
                    track_id=str(item.get("id", "")),
                    name=item.get("name", "Невідома назва"),
                    artist=item.get("artist_name", "Невідомий виконавець"),
                    jamendo_url=item.get("shareurl") or item.get("shorturl") or "https://www.jamendo.com/",
                    audio_path=audio_path,
                )
            )

        if len(track_items) == 1:
            track_items.append(track_items[0])
        if len(track_items) < 2:
            raise RuntimeError("Недостатньо треків для поста")

        base_image = await get_unsplash_image(session, genre)
        temp_files.append(base_image)

        quotes = await get_quote_sources(session)
        selected_quote = await translate_to_ukrainian(session, quotes[0])
        if cfg["iso"] != "uk":
            selected_quote = await translate_to_ukrainian(session, selected_quote)

        image_with_quote = overlay_quote(base_image, selected_quote)
        temp_files.append(image_with_quote)

    return image_with_quote, track_items[:2], selected_quote, temp_files


async def send_main_menu(message: Message) -> None:
    await message.answer("Головне меню", reply_markup=main_menu_kb())


async def cancel_flow(message: Message, state: FSMContext) -> None:
    await cleanup_files(state)
    await state.clear()
    await message.answer("Скасовано. Повертаю в головне меню.", reply_markup=main_menu_kb())


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


async def admin_only(message: Message) -> bool:
    if not is_admin(message):
        await message.answer("Доступ дозволено лише адміну.", reply_markup=ReplyKeyboardRemove())
        return False
    return True


bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    if not await admin_only(message):
        return
    await state.clear()
    await send_main_menu(message)


@dp.message(F.text == "2️⃣ Скасувати")
async def cancel_button(message: Message, state: FSMContext) -> None:
    if not await admin_only(message):
        return
    await cancel_flow(message, state)


@dp.message(F.text == "❌ Скасувати")
async def cancel_publish(message: Message, state: FSMContext) -> None:
    if not await admin_only(message):
        return
    await cancel_flow(message, state)


@dp.message(F.text == "1️⃣ Новий пост")
async def new_post(message: Message, state: FSMContext) -> None:
    if not await admin_only(message):
        return
    await cleanup_files(state)
    await state.clear()
    await state.set_state(PostStates.choosing_genre)
    await message.answer("Оберіть жанр:", reply_markup=genre_kb())


@dp.message(PostStates.choosing_genre, F.text.in_(GENRES))
async def genre_selected(message: Message, state: FSMContext) -> None:
    await state.update_data(genre=message.text)
    await state.set_state(PostStates.choosing_language)
    await message.answer("Оберіть мову треків:", reply_markup=language_kb())


@dp.message(PostStates.choosing_language, F.text.in_(LANGUAGE_LABELS))
async def language_selected(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    genre = data.get("genre")
    language = message.text
    if not genre:
        await state.set_state(PostStates.choosing_genre)
        await message.answer("Оберіть жанр:", reply_markup=genre_kb())
        return

    status_message = await message.answer("Генерую пост, зачекайте...")

    try:
        image_path, tracks, quote, temp_files = await build_post_content(genre, language)
        await state.update_data(
            genre=genre,
            language=language,
            quote=quote,
            image_path=image_path,
            tracks=[t.__dict__ for t in tracks],
            temp_files=temp_files,
        )

        await message.answer_photo(photo=FSInputFile(image_path), caption=f"1️⃣ Фото з цитатою\n\n{quote}")
        await message.answer_audio(audio=FSInputFile(tracks[0].audio_path), caption=track_caption(1, tracks[0]))
        await message.answer_audio(audio=FSInputFile(tracks[1].audio_path), caption=track_caption(2, tracks[1]))
        await state.set_state(PostStates.preview_ready)
        await message.answer("Пост сформовано.", reply_markup=poll_start_kb())
    except Exception:
        logger.exception("Failed to generate post")
        await message.answer("Сталася помилка під час формування поста.", reply_markup=main_menu_kb())
        await cleanup_files(state)
        await state.clear()
    finally:
        try:
            await status_message.delete()
        except TelegramBadRequest:
            pass


@dp.message(PostStates.preview_ready, F.text == "Опитування")
async def choose_poll(message: Message, state: FSMContext) -> None:
    await state.set_state(PostStates.choosing_poll)
    await message.answer("Оберіть опитування:", reply_markup=poll_select_kb())


@dp.message(PostStates.choosing_poll, F.text.in_(list(POLL_TEMPLATES.keys())))
async def poll_selected(message: Message, state: FSMContext) -> None:
    key = message.text
    await state.update_data(selected_poll=key)
    await state.set_state(PostStates.confirm_publish)
    question, options = POLL_TEMPLATES[key]
    await message.answer(
        f"Обрано: {key}\n\nПитання: {question}\nВаріанти: {', '.join(options)}",
        reply_markup=publish_confirm_kb(),
    )


@dp.message(PostStates.confirm_publish, F.text == "✅ Опублікувати")
async def publish_post(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    image_path = data.get("image_path")
    tracks_data = data.get("tracks", [])
    poll_key = data.get("selected_poll")

    if not image_path or len(tracks_data) < 2 or poll_key not in POLL_TEMPLATES:
        await message.answer("Дані поста неповні. Сформуйте пост заново.", reply_markup=main_menu_kb())
        await cleanup_files(state)
        await state.clear()
        return

    question, options = POLL_TEMPLATES[poll_key]

    try:
        await bot.send_photo(CHANNEL_ID, FSInputFile(image_path), caption="1️⃣ Фото з цитатою")
        for idx, t in enumerate(tracks_data[:2], start=1):
            track = TrackItem(**t)
            await bot.send_audio(CHANNEL_ID, FSInputFile(track.audio_path), caption=track_caption(idx, track))
        await bot.send_poll(CHANNEL_ID, question=question, options=options, is_anonymous=False)

        await message.answer("Публікацію успішно виконано. Дякую.", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("Publishing failed")
        await message.answer("Не вдалося опублікувати пост.", reply_markup=main_menu_kb())
    finally:
        await cleanup_files(state)
        await state.clear()


@dp.message()
async def fallback(message: Message) -> None:
    if not await admin_only(message):
        return
    await message.answer("Оберіть дію з меню.", reply_markup=main_menu_kb())


async def main() -> None:
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
