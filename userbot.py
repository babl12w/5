import asyncio
import os
import tempfile
from pathlib import Path

from pyrogram import Client, filters
from pyrogram.enums import MessageMediaType
from pyrogram.types import Message


class TgSoundUserbot:
    def __init__(self) -> None:
        api_id_raw = os.getenv("API_ID")
        api_hash = os.getenv("API_HASH")
        session_string = os.getenv("SESSION_STRING")

        if not api_id_raw or not api_hash or not session_string:
            raise RuntimeError("API_ID, API_HASH, SESSION_STRING must be set")

        self._api_id = int(api_id_raw)
        self._api_hash = api_hash
        self._session_string = session_string
        self._client = Client(
            name="music_userbot",
            api_id=self._api_id,
            api_hash=self._api_hash,
            session_string=self._session_string,
            in_memory=True,
            no_updates=False,
        )

    async def start(self) -> None:
        if not self._client.is_connected:
            await self._client.start()

    async def stop(self) -> None:
        if self._client.is_connected:
            await self._client.stop()

    async def _wait_for_audio(self, timeout: int = 40) -> Message:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for audio from @TgSoundBot")

            message = await self._client.listen(
                chat_id="TgSoundBot",
                filters=filters.chat("TgSoundBot"),
                timeout=int(max(1, remaining)),
            )

            if message.media in (MessageMediaType.AUDIO, MessageMediaType.DOCUMENT):
                if message.audio:
                    return message
                if message.document and (message.document.mime_type or "").startswith("audio/"):
                    return message

            if message.reply_markup and message.reply_markup.inline_keyboard:
                for row in message.reply_markup.inline_keyboard:
                    for button in row:
                        if button.callback_data or button.url:
                            try:
                                await message.click(button.text)
                                break
                            except Exception:
                                continue
                    else:
                        continue
                    break

    async def find_track_mp3(self, artist: str, title: str) -> str:
        query = f"{artist} - {title}"
        await self._client.send_message("TgSoundBot", query)
        audio_message = await self._wait_for_audio()

        suffix = ".mp3"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = Path(tmp.name)

        await self._client.download_media(audio_message, file_name=str(temp_path))
        return str(temp_path)
