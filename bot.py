import asyncio
import logging
import os
import random
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()

if not BOT_TOKEN or not UNSPLASH_ACCESS_KEY or not ADMIN_ID or not CHANNEL_ID:
    raise RuntimeError(
        "Missing required env vars: BOT_TOKEN, UNSPLASH_ACCESS_KEY, ADMIN_ID, CHANNEL_ID"
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

GENRES = ["Pop", "Rock", "Rap", "Electronic"]
QUOTE_RSS_URL = "https://feeds.feedburner.com/brainyquote/QUOTEBR"

POLL_TEMPLATES = [
    {
        "title": "Опитування 1",
        "question": "Який жанр музики сьогодні ваш фаворит?",
        "options": ["Pop", "Rock", "Rap", "Electronic"],
    },
    {
        "title": "Опитування 2",
        "question": "Коли ви найчастіше слухаєте музику?",
        "options": ["Вранці", "Вдень", "Увечері", "Вночі"],
    },
    {
        "title": "Опитування 3",
        "question": "Що для вас важливіше у треку?",
        "options": ["Біт", "Текст", "Вокал", "Атмосфера"],
    },
]


@dataclass
class Track:
    title: str
    artist: str
    mp3_url: str
    local_path: str


@dataclass
class PostDraft:
    genre: str
    quote: str
    photo_path: str
    tracks: list[Track]


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Новий пост")],
            [KeyboardButton("Опитування")],
            [KeyboardButton("Скасувати")],
        ],
        resize_keyboard=True,
    )


def genre_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(genre)] for genre in GENRES]
    rows.append([KeyboardButton("Скасувати")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def poll_keyboard() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(template["title"])] for template in POLL_TEMPLATES]
    rows.append([KeyboardButton("Скасувати")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Опублікувати", callback_data="publish_post")]]
    )


def _extract_tracks_from_html(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[dict[str, str]] = []

    def add_track(title: str, artist: str, url: str) -> None:
        if not url:
            return
        full_url = urljoin(base_url, url)
        if not full_url.lower().endswith(".mp3"):
            return
        cleaned_title = (title or "Unknown track").strip()
        cleaned_artist = (artist or "Unknown artist").strip()
        if any(t["mp3_url"] == full_url for t in found):
            return
        found.append({"title": cleaned_title, "artist": cleaned_artist, "mp3_url": full_url})

    for tag in soup.find_all(src=True):
        add_track(tag.get("title", ""), tag.get("data-artist", ""), tag["src"])
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
        if href.lower().endswith(".mp3"):
            artist, title = _split_artist_title(text)
            add_track(title, artist, href)

    for script in soup.find_all("script"):
        if not script.string:
            continue
        for match in re.findall(r"https?://[^\"']+\.mp3", script.string):
            add_track("Unknown track", "Unknown artist", match)

    return found


def _split_artist_title(raw: str) -> tuple[str, str]:
    if not raw:
        return "Unknown artist", "Unknown track"
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
        return artist.strip() or "Unknown artist", title.strip() or "Unknown track"
    return "Unknown artist", raw.strip()


def _fetch_tracks_candidates(search_query: str | None) -> list[dict[str, str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    urls = []
    if search_query:
        encoded = quote_plus(search_query)
        urls.extend(
            [
                f"https://ua-zvuk.net/?s={encoded}",
                f"https://ua-zvuk.net/index.php?do=search&subaction=search&story={encoded}",
            ]
        )
    else:
        urls.append("https://ua-zvuk.net/")

    tracks: list[dict[str, str]] = []
    for url in urls:
        try:
            resp = session.get(url, timeout=20)
            resp.raise_for_status()
            parsed = _extract_tracks_from_html(resp.text, "https://ua-zvuk.net/")
            for tr in parsed:
                if all(existing["mp3_url"] != tr["mp3_url"] for existing in tracks):
                    tracks.append(tr)
        except Exception as exc:
            logger.warning("Track source request failed for %s: %s", url, exc)
    return tracks


def _download_file(url: str, suffix: str) -> str:
    temp_dir = Path(tempfile.gettempdir()) / "music_channel_bot"
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_path = temp_dir / f"{abs(hash(url))}{suffix}"
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    local_path.write_bytes(response.content)
    return str(local_path)


def _find_tracks_with_fallback(genre: str) -> list[Track]:
    queries: list[str | None] = [genre, None, "popular music", "music"]
    collected: list[dict[str, str]] = []

    for query in queries:
        candidates = _fetch_tracks_candidates(query)
        for item in candidates:
            if all(existing["mp3_url"] != item["mp3_url"] for existing in collected):
                collected.append(item)
        if len(collected) >= 2:
            break

    if not collected:
        return []

    selected = collected[:2] if len(collected) >= 2 else collected[:1]
    tracks: list[Track] = []
    for item in selected:
        try:
            mp3_path = _download_file(item["mp3_url"], ".mp3")
            tracks.append(
                Track(
                    title=item["title"],
                    artist=item["artist"],
                    mp3_url=item["mp3_url"],
                    local_path=mp3_path,
                )
            )
        except Exception as exc:
            logger.warning("Failed to download mp3 %s: %s", item["mp3_url"], exc)

    return tracks


def _get_unsplash_photo(genre: str) -> str:
    query = f"{genre} mood music"
    endpoint = "https://api.unsplash.com/search/photos"
    params = {
        "query": query,
        "per_page": 30,
        "orientation": "landscape",
        "client_id": UNSPLASH_ACCESS_KEY,
    }
    response = requests.get(endpoint, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()
    results = data.get("results", [])
    if not results:
        raise RuntimeError("Unsplash returned no images")
    image = random.choice(results)
    image_url = image.get("urls", {}).get("regular")
    if not image_url:
        raise RuntimeError("Unsplash image URL missing")
    return _download_file(image_url, ".jpg")


def _clean_quote(text: str) -> str:
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    selected = [part for part in parts if part][:4]
    if len(selected) < 2:
        selected = [text[i : i + 90].strip() for i in range(0, min(len(text), 360), 90)]
    selected = [line for line in selected if line][:4]
    return "\n".join(selected[:4])


def _get_quote() -> str:
    feed = feedparser.parse(QUOTE_RSS_URL)
    entries = getattr(feed, "entries", [])
    if not entries:
        return "Музика — це мова емоцій, яка не потребує перекладу."
    entry = random.choice(entries)
    raw = entry.get("summary") or entry.get("description") or entry.get("title") or ""
    quote = _clean_quote(raw)
    return quote or "Музика — це мова емоцій, яка не потребує перекладу."


def build_post_text(quote: str, tracks: list[Track]) -> str:
    lines = ["📝 <b>Цитата</b>", quote, "", "🎵 <b>Треки</b>"]
    for idx, track in enumerate(tracks, start=1):
        lines.append(f"🎵 {idx}. {track.artist} — {track.title}")
    return "\n".join(lines)


async def generate_post_data(genre: str) -> PostDraft:
    quote = await asyncio.to_thread(_get_quote)
    photo_path = await asyncio.to_thread(_get_unsplash_photo, genre)
    tracks = await asyncio.to_thread(_find_tracks_with_fallback, genre)
    return PostDraft(genre=genre, quote=quote, photo_path=photo_path, tracks=tracks)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "Головне меню:",
        reply_markup=main_menu_keyboard(),
    )


async def send_main_menu(update: Update) -> None:
    await update.message.reply_text("Повернулись у головне меню.", reply_markup=main_menu_keyboard())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text = (update.message.text or "").strip()

    if text == "Скасувати":
        context.user_data.clear()
        await send_main_menu(update)
        return

    if user_id != ADMIN_ID:
        await update.message.reply_text("Цей бот доступний лише адміну.")
        return

    if text == "Новий пост":
        context.user_data["mode"] = "genre"
        await update.message.reply_text("Обери жанр:", reply_markup=genre_keyboard())
        return

    if text == "Опитування":
        context.user_data["mode"] = "poll"
        await update.message.reply_text("Оберіть шаблон опитування:", reply_markup=poll_keyboard())
        return

    mode = context.user_data.get("mode")

    if mode == "genre" and text in GENRES:
        wait_msg = await update.message.reply_text("Формую пост, зачекай...", reply_markup=main_menu_keyboard())
        try:
            draft = await generate_post_data(text)
        except Exception as exc:
            logger.exception("Post generation failed")
            await wait_msg.edit_text(f"Помилка формування посту: {exc}")
            return

        if len(draft.tracks) < 2:
            await context.bot.send_message(
                ADMIN_ID,
                "Не вдалося знайти достатньо треків. Перевір джерело.",
            )
            await update.message.reply_text("Не вдалося підготувати пост.")
            return

        context.user_data["pending_post"] = draft
        caption = build_post_text(draft.quote, draft.tracks)
        with open(draft.photo_path, "rb") as photo_file:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo_file,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=publish_keyboard(),
            )

        for track in draft.tracks:
            with open(track.local_path, "rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=ADMIN_ID,
                    audio=audio_file,
                    title=track.title,
                    performer=track.artist,
                )

        await update.message.reply_text("Прев'ю надіслано. Натисни «Опублікувати».")
        return

    if mode == "poll":
        template = next((item for item in POLL_TEMPLATES if item["title"] == text), None)
        if not template:
            await update.message.reply_text("Оберіть один з доступних варіантів опитування.")
            return

        await context.bot.send_poll(
            chat_id=ADMIN_ID,
            question=template["question"],
            options=template["options"],
            is_anonymous=False,
        )
        await update.message.reply_text("Опитування створено.", reply_markup=main_menu_keyboard())
        context.user_data.clear()
        return

    await update.message.reply_text("Використай кнопки меню.", reply_markup=main_menu_keyboard())


async def handle_publish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.edit_message_reply_markup(reply_markup=None)
        return

    draft: PostDraft | None = context.user_data.get("pending_post")
    if not draft:
        await query.message.reply_text("Немає посту для публікації.")
        await query.edit_message_reply_markup(reply_markup=None)
        return

    caption = build_post_text(draft.quote, draft.tracks)

    with open(draft.photo_path, "rb") as photo_file:
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=photo_file,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )

    for track in draft.tracks:
        with open(track.local_path, "rb") as audio_file:
            await context.bot.send_audio(
                chat_id=CHANNEL_ID,
                audio=audio_file,
                title=track.title,
                performer=track.artist,
            )

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("Опубліковано в канал.")
    context.user_data.pop("pending_post", None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)
    if isinstance(update, Update) and update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Сталася помилка.")


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(handle_publish_callback, pattern=r"^publish_post$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application


def cleanup_temp_files() -> None:
    temp_dir = Path(tempfile.gettempdir()) / "music_channel_bot"
    if not temp_dir.exists():
        return
    for file_path in temp_dir.glob("*"):
        try:
            file_path.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    cleanup_temp_files()
    app = build_application()
    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)
