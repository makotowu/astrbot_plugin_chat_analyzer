from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.all import Context
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image


class ImageProcessor:
    def __init__(self, context: Context, storage=None):
        self._context = context
        self._storage = storage

    async def extract(self, event: AstrMessageEvent) -> Dict[str, List[str]]:
        urls: List[str] = []
        captions: List[str] = []
        for seg in event.get_messages():
            if isinstance(seg, Image):
                url = getattr(seg, "url", None) or getattr(seg, "file", None)
                if not url:
                    continue
                urls.append(url)
                caption = await self._get_caption(url)
                if caption:
                    captions.append(caption)
        return {"urls": urls, "captions": captions}

    async def _get_caption(self, image_url: str) -> str:
        if self._storage:
            cached = await self._storage.get_cached_caption(image_url)
            if cached:
                return cached

        try:
            provider = self._context.get_using_provider()
            if provider is None:
                return ""
            resp = await provider.text_chat(
                prompt="请用一句简短的中文描述这张图片的内容。",
                image_urls=[image_url],
                session_id="",
            )
            if resp and resp.completion_text:
                caption = resp.completion_text.strip()
                if self._storage and caption:
                    await self._storage.set_cached_caption(image_url, caption)
                return caption
            return ""
        except Exception:
            return ""

    def set_storage(self, storage) -> None:
        self._storage = storage
