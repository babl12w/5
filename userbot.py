import asyncio
import os
import tempfile
from pathlib import Path
from typing import Optional

from pyrogram import Client
from pyrogram.enums import ChatAction
from pyrogram.errors import RPCError


class TgSoundUserbot:
    def __init__(self) -> None:
        api_id_raw = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        session_string = os.getenv("SESSION_STRING")

        if not api_id_raw or not api_hash or not session_string:
            raise RuntimeError("API_ID, API_HASH, SESSION_STRING are required")

        self.api_id = int(api_id_raw)
        self.api_hash = api_hash
        self.session_string = session_string
        self.client = Client(
            name="music_channel_userbot",
            api_id=self.api_id,
            api_hash=self.api_hash,
            session_string=self.session_string,
            in_memory=True,
        )
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.client.start()

    async def stop(self) -> None:
        await self.client.stop()

    async def fetch_mp3(self, title: str, artist: str, timeout: int = 90) -> Optional[str]:
        query = f"{title} {artist}".strip()
        async with self._lock:
            try:
                await self.client.send_chat_action("TgSoundBot", ChatAction.TYPING)
                await self.client.send_message("TgSoundBot", query)
            except RPCError:
                return None

            end_time = asyncio.get_running_loop().time() + timeout
            seen_ids: set[int] = set()

            while asyncio.get_running_loop().time() < end_time:
                try:
                    async for message in self.client.get_chat_history("TgSoundBot", limit=15):
                        if message.id in seen_ids:
                            continue
                        seen_ids.add(message.id)
                        if message.audio and message.from_user and message.from_user.is_bot:
                            directory = Path(tempfile.mkdtemp(prefix="tgsound_"))
                            target = directory / f"{message.audio.file_unique_id}.mp3"
                            downloaded = await self.client.download_media(message, file_name=str(target))
                            return downloaded
                except RPCError:
                    return None

                await asyncio.sleep(2)

            return None
