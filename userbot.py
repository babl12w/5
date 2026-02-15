import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

from pyrogram import Client


@dataclass
class DownloadedTrack:
    title: str
    performer: str
    file_path: str


class TgSoundUserbot:
    def __init__(self) -> None:
        api_id_raw = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        if not api_id_raw or not api_hash:
            raise RuntimeError("API_ID and API_HASH are required")

        session_string = os.getenv("PYROGRAM_SESSION_STRING")
        self._client = Client(
            name="music_userbot",
            api_id=int(api_id_raw),
            api_hash=api_hash,
            session_string=session_string,
            workdir=".",
            in_memory=bool(session_string),
        )
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if not self._started:
            await self._client.start()
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._client.stop()
            self._started = False

    async def search_and_download(self, query: str, destination_dir: str, limit: int = 2, timeout_sec: int = 45) -> List[DownloadedTrack]:
        await self.start()

        async with self._lock:
            bot_chat = await self._client.get_users("TgSoundBot")
            request_message = await self._client.send_message(bot_chat.id, query)
            start_ts = request_message.date.timestamp()

            found: List[DownloadedTrack] = []
            seen_file_ids: set[str] = set()
            deadline = time.monotonic() + timeout_sec

            while time.monotonic() < deadline and len(found) < limit:
                async for msg in self._client.get_chat_history(bot_chat.id, limit=40):
                    if msg.date.timestamp() < start_ts:
                        break

                    audio = msg.audio or (msg.document if msg.document and (msg.document.mime_type or "").startswith("audio/") else None)
                    if not audio:
                        continue

                    file_unique_id = getattr(audio, "file_unique_id", None)
                    if not file_unique_id or file_unique_id in seen_file_ids:
                        continue

                    seen_file_ids.add(file_unique_id)
                    title = getattr(audio, "title", None) or query
                    performer = getattr(audio, "performer", None) or "Unknown"
                    file_name = f"{file_unique_id}.mp3"
                    download_path = str(Path(destination_dir) / file_name)
                    saved_path = await self._client.download_media(msg, file_name=download_path)
                    if saved_path:
                        found.append(DownloadedTrack(title=title, performer=performer, file_path=saved_path))
                        if len(found) >= limit:
                            break

                if len(found) < limit:
                    await asyncio.sleep(2)

            return found
