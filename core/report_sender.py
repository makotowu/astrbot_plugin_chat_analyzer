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
        self.bot_id = bot_id

    async def _resolve_bot_id(self, target: str) -> str:
        if self.bot_id:
            return self.bot_id

        session_parts = target.split(":")
        session_type = session_parts[1] if len(session_parts) > 1 else ""

        try:
            platforms = self._context.platform_manager.get_insts()
            for platform in platforms:
                meta = platform.meta()
                if meta.name not in ("aiocqhttp", "qq", "onebot", "snowluma", "napcat"):
                    if session_type.lower() not in ("groupmessage", "friendmessage"):
                        continue
                if hasattr(platform, "bot_self_id"):
                    sid = str(platform.bot_self_id)
                    if sid:
                        self.bot_id = sid
                        logger.info(f"已从 platform 获取 bot_id: {sid}")
                        return sid
                client = platform.get_client()
                if hasattr(client, "self_id"):
                    sid = str(client.self_id)
                    if sid:
                        self.bot_id = sid
                        logger.info(f"已从 client 获取 bot_id: {sid}")
                        return sid
                if hasattr(client, "api") and hasattr(client.api, "call_action"):
                    info = await client.api.call_action("get_login_info")
                    uid = info.get("user_id") or info.get("userId")
                    if uid:
                        sid = str(uid)
                        self.bot_id = sid
                        logger.info(f"已从 get_login_info 获取 bot_id: {sid}")
                        return sid
        except Exception as e:
            logger.warning(f"获取 bot_id 时出错: {e}")

        logger.warning("无法自动获取 bot_id，转发消息将使用默认值 10000")
        return "10000"

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
            await self._context.send_message(
                target,
                MessageChain(chain=[Plain(report)]),
            )
            logger.info(f"分析报告已发送至 {target}")

            if flagged_pairs:
                await self._send_violation_forward(target, analysis_text, flagged_pairs)

            if extra_text:
                await self._context.send_message(
                    target,
                    MessageChain(chain=[Plain(extra_text)]),
                )
        except Exception as e:
            logger.error(f"发送分析报告失败: {e}")

    async def send_flagged_only(
        self,
        target: str,
        pairs: List[Tuple[str, str, ChatRecord]],
    ) -> None:
        if not target or not pairs:
            return
        try:
            header = (
                "\U0001f4cc AI \u5ba1\u6838\u5b9a\u4f4d\u6d88\u606f\uff0c\u8bf7\u7ba1\u7406\u5458\u590d\u6838\uff1a"
            )
            await self._context.send_message(
                target,
                MessageChain(chain=[Plain(header)]),
            )
            await self._send_violation_forward(target, "", pairs)
        except Exception as e:
            logger.error(f"发送定位消息失败: {e}")

    async def _send_violation_forward(
        self,
        target: str,
        analysis_text: str,
        pairs: List[Tuple[str, str, ChatRecord]],
    ) -> None:
        try:
            bot_id = await self._resolve_bot_id(target)
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
                            "user_id": bot_id,
                            "nickname": "AI 分析审核",
                            "content": [
                                {"type": "text", "data": {"text": analysis_text}}
                            ],
                        },
                    })

                pair_idx = 0
                for level, reason, record in pairs:
                    uid = record.sender_id or bot_id
                    pair_idx += 1
                    nodes.append({
                        "type": "node",
                        "data": {
                            "user_id": bot_id,
                            "nickname": f"{level}原因 #{pair_idx}",
                            "content": [
                                {"type": "text", "data": {"text": reason}}
                            ],
                        },
                    })
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
                            "nickname": record.sender or f"用户{uid}",
                            "content": content_parts,
                        },
                    })

                if not nodes:
                    return

                logger.info(
                    f"准备发送转发消息: group_id={group_id}, nodes={len(nodes)}, "
                    f"bot_id={bot_id}"
                )
                forward_msg = {"group_id": group_id, "messages": nodes}
                await client.api.call_action("send_forward_msg", **forward_msg)
                logger.info(
                    f"定位消息转发已发送至 {target}，共 {len(pairs)} 组"
                )
                return
        except Exception as e:
            logger.error(f"发送定位消息转发失败: {e}")
