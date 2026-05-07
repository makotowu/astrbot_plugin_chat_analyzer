from typing import List

from astrbot.api import logger
from astrbot.api.all import Context

from .constant import ACTION_LABELS


class ActionExecutor:
    def __init__(self, context: Context):
        self._context = context

    def _get_qq_client(self):
        platforms = self._context.platform_manager.get_insts()
        for platform in platforms:
            client = platform.get_client()
            if hasattr(client, "api") and hasattr(client.api, "call_action"):
                return client
        return None

    async def execute_actions(self, group_id: str, actions: list) -> List[str]:
        client = self._get_qq_client()
        if client is None:
            logger.error(f"群 {group_id} 无法获取 QQ 客户端，操作未执行。")
            return ["无法获取 QQ 客户端，操作未执行。"]

        results: List[str] = []
        for action, idx, reason, target_id, sender_name, mute_duration, message_id, ai_notify in actions:
            label = ACTION_LABELS.get(action, action)
            try:
                if action != "清昵" and message_id:
                    try:
                        await client.delete_msg(message_id=int(message_id))
                        logger.info(f"群 {group_id} 已撤回消息 {message_id}")
                    except Exception as de:
                        logger.warning(f"群 {group_id} 撤回消息 {message_id} 失败: {de}")

                if action == "禁言":
                    dur = max(1, mute_duration)
                    await client.api.call_action(
                        "set_group_ban",
                        group_id=int(group_id),
                        user_id=int(target_id),
                        duration=dur,
                    )
                    msg = (
                        f"\u2705 {label} #{idx} {sender_name}({target_id}) "
                        f"\u7981\u8a00 {dur} \u79d2: {reason}"
                    )
                    notify = ai_notify or (
                        f"\U0001f6ab {sender_name} \u88ab\u7981\u8a00 {dur} \u79d2\n"
                        f"\u539f\u56e0: {reason}"
                    )
                    results.append(msg)
                    logger.info(f"群 {group_id} {label} {sender_name}({target_id}) 禁言 {dur}s")
                elif action == "移除":
                    await client.api.call_action(
                        "set_group_kick",
                        group_id=int(group_id),
                        user_id=int(target_id),
                        reject_add_request=False,
                    )
                    msg = (
                        f"\u2705 {label} #{idx} {sender_name}({target_id}): {reason}"
                    )
                    notify = ai_notify or (
                        f"\U0001f6ab {sender_name} \u88ab\u79fb\u51fa\u7fa4\u804a\n"
                        f"\u539f\u56e0: {reason}"
                    )
                    results.append(msg)
                    logger.info(f"群 {group_id} {label} {sender_name}({target_id})")
                elif action == "拉黑":
                    await client.api.call_action(
                        "set_group_kick",
                        group_id=int(group_id),
                        user_id=int(target_id),
                        reject_add_request=True,
                    )
                    msg = (
                        f"\u2705 {label} #{idx} {sender_name}({target_id}): {reason}"
                    )
                    notify = ai_notify or (
                        f"\U0001f6ab {sender_name} \u88ab\u79fb\u51fa\u7fa4\u804a\u5e76\u62c9\u9ed1\n"
                        f"\u539f\u56e0: {reason}"
                    )
                    results.append(msg)
                    logger.info(f"群 {group_id} {label} {sender_name}({target_id})")
                elif action == "清昵":
                    await client.api.call_action(
                        "set_group_card",
                        group_id=int(group_id),
                        user_id=int(target_id),
                        card="",
                    )
                    msg = (
                        f"\u2705 {label} #{idx} {sender_name}({target_id}): {reason}"
                    )
                    notify = ai_notify or (
                        f"\U0001f6ab {sender_name} \u7684\u7fa4\u6635\u79f0\u5df2\u88ab\u6e05\u7a7a\n"
                        f"\u539f\u56e0: {reason}"
                    )
                    results.append(msg)
                    logger.info(f"群 {group_id} {label} {sender_name}({target_id})")
                else:
                    msg = f"\u274c #{idx} \u672a\u77e5\u64cd\u4f5c\u7c7b\u578b: {action}"
                    results.append(msg)
                    continue

                if "notify" in locals():
                    try:
                        at_text = f"[CQ:at,qq={target_id}]"
                        await client.api.call_action(
                            "send_group_msg",
                            group_id=int(group_id),
                            message=f"\U0001f6ab {at_text} {notify}",
                        )
                    except Exception as ne:
                        logger.warning(f"群 {group_id} 发送处置通知失败: {ne}")
            except Exception as e:
                err_msg = (
                    f"\u274c {label} #{idx} {sender_name}({target_id}) "
                    f"\u5931\u8d25: {e}"
                )
                results.append(err_msg)
                logger.error(f"群 {group_id} 执行 {label} 失败: {e}")

        return results
