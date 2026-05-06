from typing import Optional

from astrbot.api import logger
from astrbot.api.all import Context


class AIClient:
    def __init__(self, context: Context, target_session: str):
        self._context = context
        self._target_session = target_session

    async def analyze(self, chat_text: str, system_prompt: str) -> Optional[str]:
        try:
            umo = self._target_session if self._target_session else None
            provider = self._context.get_using_provider(umo=umo)
            if provider is None:
                logger.error("无法获取 LLM 提供商，请检查配置。")
                return None
            resp = await provider.text_chat(
                prompt=f"聊天记录:\n{chat_text}",
                system_prompt=system_prompt,
            )
            if resp and resp.completion_text:
                return resp.completion_text.strip()
            return None
        except Exception as e:
            logger.error(f"AI 分析调用失败: {e}")
            return None
