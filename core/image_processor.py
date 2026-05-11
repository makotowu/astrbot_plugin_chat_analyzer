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
                prompt=(
                    "你是内容审核助手。请审核这张图片是否存在以下违规内容："
                    "色情低俗、暴力血腥、政治敏感、广告引流、违法信息。"
                    "如果图片正常，回复\"正常\"并简述内容。"
                    "如果有问题，回复\"异常: 类别 - 简要说明\"。"
                ),
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
