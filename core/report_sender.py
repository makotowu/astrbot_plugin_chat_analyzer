from typing import List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import Context
from astrbot.api.message_components import Plain
from astrbot.api.event import MessageChain

from .constant import PRESET_LABELS, REPORT_PREFIX
from .models import ChatRecord
from .utils import extract_session_info, format_time_range


class ReportSender:
    def __init__(self, context: Context, bot_id: str):
        self._context = context
        self._bot_id = bot_id

    def build_header(
        self,
        records: List[ChatRecord],
        prompts: List[Tuple[str, str]],
    ) -> str:
        labels = [PRESET_LABELS.get(k, k) for k, _ in prompts]
        return (
            f"{REPORT_PREFIX}\n"
            f"\u23f0 时间范围: {format_time_range(records)}\n"
            f"\U0001f4dd 消息数量: {len(records)}\n"
            f"\U0001f465 会话: {extract_session_info(records)}\n"
            f"\U0001f4cb 分析策略: {'  '.join(labels)}\n"
            f"{'\u2500' * 30}\n"
        )

    async def send_result(
        self,
        target: str,
        report: str,
        analysis_text: str = "",
        flagged_pairs: Optional[List[Tuple[str, str, ChatRecord]]] = None,
        extra_text: str = "",
    ) -> None:
        if not target:
            logger.info("未配置报告发送目标，分析结果无法发送。")
            return
        try:
            full_report = report + extra_text
            await self._context.send_message(
                target,
                MessageChain(chain=[Plain(full_report)]),
            )
            logger.info(f"分析报告已发送至 {target}")
            if flagged_pairs:
                await self._send_violation_forward(target, analysis_text, flagged_pairs)
        except Exception as e:
            logger.error(f"发送分析报告失败: {e}")

    async def _send_violation_forward(
        self,
        target: str,
        analysis_text: str,
        pairs: List[Tuple[str, str, ChatRecord]],
    ) -> None:
        try:
            group_id = target.split(":")[-1] if ":" in target else target
            platforms = self._context.platform_manager.get_insts()
            for platform in platforms:
                client = platform.get_client()
                if not hasattr(client, "call_action"):
                    continue

                nodes = []
                if analysis_text:
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": self._bot_id or "",
                            "nickname": "AI 分析审核",
                            "id": "",
                            "content": [
                                {"type": "text", "data": {"text": analysis_text}}
                            ],
                        },
                    })

                pair_idx = 0
                for level, reason, record in pairs:
                    pair_idx += 1
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": self._bot_id or "",
                            "nickname": f"{level}原因 #{pair_idx}",
                            "id": "",
                            "content": [
                                {"type": "text", "data": {"text": reason}}
                            ],
                        },
                    })
                    uid = record.sender_id or record.sender
                    content_parts: list = []
                    if record.content:
                        content_parts.append(
                            {"type": "text", "data": {"text": record.content}}
                        )
                    for img_url in record.image_urls:
                        content_parts.append(
                            {"type": "image", "data": {"url": img_url}}
                        )
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": uid,
                            "nickname": record.sender,
                            "id": "",
                            "content": content_parts,
                        },
                    })

                if not nodes:
                    return

                forward_msg = {"group_id": group_id, "messages": nodes}
                await client.api.call_action("send_forward_msg", **forward_msg)
                logger.info(
                    f"定位消息转发已发送至 {target}，共 {len(pairs)} 组"
                )
                return
        except Exception as e:
            logger.error(f"发送定位消息转发失败: {e}")
