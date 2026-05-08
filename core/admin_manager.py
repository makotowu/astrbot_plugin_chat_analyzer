import asyncio
from typing import Dict, List, Set, Tuple

from astrbot.api import logger
from astrbot.api.all import Context
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from .models import GroupConfig


class AdminManager:
    def __init__(self, context: Context, group_configs: Dict[str, GroupConfig], global_admin_ids: Set[str]):
        self._context = context
        self._group_configs = group_configs
        self._global_admin_ids = global_admin_ids

        self._builtin_admin_ids: Set[str] = set()
        try:
            main_cfg = self._context.get_config()
            raw = main_cfg.get("admins_id", [])
            if isinstance(raw, list):
                self._builtin_admin_ids = {str(uid).strip() for uid in raw if uid}
        except Exception:
            pass

    def check(self, event: AstrMessageEvent, group_id: str = "") -> bool:
        if event.role == "admin":
            return True
        sender_id = event.get_sender_id() or ""
        if sender_id and sender_id in self._global_admin_ids:
            return True
        if group_id:
            gc = self._group_configs.get(group_id)
            if gc and sender_id in gc.admin_ids:
                return True
        return False

    def all_ids(self, group_id: str) -> Set[str]:
        result: Set[str] = set(self._global_admin_ids)
        result.update(self._builtin_admin_ids)
        gc = self._group_configs.get(group_id)
        if gc:
            result.update(gc.admin_ids)
        return result

    def filter_actions(
        self, group_id: str, action_suggestions: list, target: str
    ) -> Tuple[list, List[str]]:
        admin_ids = self.all_ids(group_id)
        if not admin_ids:
            return list(action_suggestions), []
        clean: list = []
        warnings: List[str] = []
        for act_tuple in action_suggestions:
            tid = act_tuple[3]
            if tid in admin_ids:
                name = act_tuple[4]
                reason = act_tuple[2]
                warnings.append(
                    f"\u26a0\ufe0f \u7fa4\u7ba1\u7406\u5458 {name}({tid}) \u53d1\u8a00\u5f02\u5e38: {reason}\n"
                    f"  \u5df2\u8df3\u8fc7\u81ea\u52a8\u5904\u7f6e\uff0c\u8bf7\u7ba1\u7406\u5458\u6ce8\u610f\u89c4\u8303\u884c\u4e3a\u3002"
                )
                logger.info(f"群 {group_id} 管理员 {name}({tid}) 命中处置建议，已跳过")
            else:
                clean.append(act_tuple)
        if warnings and target:
            asyncio.create_task(self._send_warning(target, group_id, warnings))
        return clean, warnings

    async def _send_warning(self, target: str, group_id: str, warnings: List[str]):
        text = (
            f"\u26a0\ufe0f \u7fa4 {group_id} AI \u5206\u6790\u53d1\u73b0\u7ba1\u7406\u5458\u5f02\u5e38\u53d1\u8a00:\n"
            + "\n".join(warnings)
        )
        try:
            await self._context.send_message(
                target,
                MessageChain(chain=[Plain(text)]),
            )
        except Exception as e:
            logger.error(f"发送管理员警告失败: {e}")

    async def send_admin_reminder(
        self, group_id: str, target_id: str, sender_name: str,
        reminder_text: str, target_session: str,
    ) -> None:
        at_cq = f"[CQ:at,qq={target_id}]"
        msg = (
            f"\U0001f4e2 \u7ba1\u7406\u5458\u63d0\u9192\n"
            f"{at_cq} {sender_name}\uff0c{reminder_text}"
        )
        try:
            if target_session:
                platforms = self._context.platform_manager.get_insts()
                for platform in platforms:
                    client = platform.get_client()
                    if not hasattr(client, "api") or not hasattr(client.api, "call_action"):
                        continue
                    session_parts = target_session.split(":")
                    if len(session_parts) >= 3:
                        await client.api.call_action(
                            "send_group_msg",
                            group_id=int(session_parts[-1]),
                            message=msg,
                        )
                    break
            logger.info(f"群 {group_id} 管理员提醒已发送: {sender_name}({target_id})")
        except Exception as e:
            logger.error(f"发送管理员提醒失败: {e}")
