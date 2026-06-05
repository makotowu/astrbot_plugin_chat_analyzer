import asyncio
import os
import time
from typing import Dict, List, Optional, Set, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import *
from astrbot.api.star import StarTools
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.event.filter import (
    EventMessageType,
    after_message_sent,
    command,
    event_message_type,
)
from astrbot.api.message_components import At, Plain

from .core.action_executor import ActionExecutor
from .core.admin_manager import AdminManager
from .core.ai_client import AIClient
from .core.analysis_engine import AnalysisEngine
from .core.config import parse_group_configs, persist_strategy_admins
from .core.constant import (
    CLEAN_MESSAGE_RE,
    REPORT_PREFIX,
    TRIGGER_COOLDOWN_SECONDS,
)
from .core.image_processor import ImageProcessor
from .core.models import ChatRecord, GroupConfig
from .core.report_sender import ReportSender
from .core.storage import ChatStorage


@register("astrbot_plugin_chat_analyzer", "makotowu", "", "", "")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.cfg = config
        self.context = context

        self.interval_minutes = max(
            1, int(self.cfg.get("chat_analysis_interval_minutes", 30))
        )
        self.max_messages = max(
            0, int(self.cfg.get("chat_analysis_max_messages", 0))
        )
        self.skip_silent = bool(self.cfg.get("chat_analysis_skip_silent", True))
        self.debug = bool(self.cfg.get("chat_analysis_debug", False))
        self.target_session = str(
            self.cfg.get("chat_analysis_target_session", "") or ""
        ).strip()

        admin_raw = str(self.cfg.get("chat_analysis_admin_ids", "") or "").strip()
        self._admin_ids: Set[str] = {
            uid.strip() for uid in admin_raw.replace("\uff0c", ",").split(",") if uid.strip()
        }

        self._group_configs = parse_group_configs(config)
        self._active = (
            bool(self.cfg.get("chat_analysis_enabled", False))
            and bool(self._group_configs)
        )

        if not self._active:
            if not self._group_configs:
                logger.warning(
                    "未配置任何策略组，插件不会运行。"
                    "请在 WebUI 的「多群独立分析策略组」中添加策略组并关联群号。"
                )
        else:
            logger.info(
                f"聊天记录分析已启动，间隔 {self.interval_minutes} 分钟，"
                f"最大消息数 {str(self.max_messages) if self.max_messages > 0 else '不限制'}，"
                f"监控群数 {len(self._group_configs)}"
            )
            for gid, gc in self._group_configs.items():
                if gc.group_rules and gc.action_mode == "suggest":
                    logger.warning(
                        f"群 {gid} 已配置群规但处置模式为 suggest（仅建议），"
                        "建议改为 confirm 或 auto 以启用自动处置能力。"
                    )

        data_dir = StarTools.get_data_dir(plugin_name="astrbot_plugin_chat_analyzer")
        db_path = os.path.join(data_dir, "chat_analyzer.db")
        self._storage = ChatStorage(db_path)

        self._bot_id: str = ""
        self._report = ReportSender(self.context, self._bot_id)
        self._image = ImageProcessor(self.context, self._storage)
        self._executor = ActionExecutor(self.context)
        self._ai_client = AIClient(self.context, self.target_session)
        self._admin = AdminManager(self.context, self._group_configs, self._admin_ids)
        self._engine = AnalysisEngine(
            self.context, self._storage, self._ai_client,
            self._report, self._executor, self._admin,
            self.skip_silent, self.debug,
        )

        self._trigger_cooldowns: Dict[str, float] = {}
        self._trigger_tasks: Set[asyncio.Task] = set()

        self._task: Optional[asyncio.Task] = None
        if self._active:
            self._task = asyncio.create_task(self._analysis_loop())

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        return event.get_sender_id() or ""

    def _get_self_id(self, event: AstrMessageEvent) -> str:
        sid = event.get_self_id() or ""
        if sid and not self._bot_id:
            self._bot_id = sid
            self._report.bot_id = sid
        return sid

    async def _sync_group_admins(
        self, event: AstrMessageEvent, group_id: str, gc: GroupConfig
    ) -> None:
        try:
            group_info = await event.get_group()
            if group_info is None:
                yield event.plain_result(
                    "\u274c \u65e0\u6cd5\u83b7\u53d6\u7fa4\u4fe1\u606f\uff0c\u8bf7\u786e\u8ba4\u5f53\u524d\u5e73\u53f0\u652f\u6301\u6b64\u64cd\u4f5c\u3002"
                )
                return
        except Exception as e:
            yield event.plain_result(f"\u274c \u83b7\u53d6\u7fa4\u4fe1\u606f\u5931\u8d25: {e}")
            logger.error(f"获取群 {group_id} 信息失败: {e}")
            return

        synced: List[str] = []
        if group_info.group_owner and str(group_info.group_owner) not in gc.admin_ids:
            synced.append(str(group_info.group_owner))
        if group_info.group_admins:
            for aid in group_info.group_admins:
                aid_str = str(aid)
                if aid_str not in gc.admin_ids and aid_str not in synced:
                    synced.append(aid_str)

        if synced:
            gc.admin_ids.extend(synced)
            persist_strategy_admins(self.cfg, group_id, gc.admin_ids)
            yield event.plain_result(
                f"\u2705 \u5df2\u540c\u6b65\u7fa4 {group_id} \u7684\u7fa4\u4e3b\u53ca\u7ba1\u7406\u5458\u4e3a\u7b56\u7565\u7ec4\u7ba1\u7406\u5458\n"
                f"\u65b0\u589e: {', '.join(synced)}\n"
                f"\u5f53\u524d\u7b56\u7565\u7ec4\u7ba1\u7406\u5458: {', '.join(gc.admin_ids)}"
            )
            logger.info(f"群 {group_id} 已同步群主+管理员到策略组: {synced}")
        else:
            yield event.plain_result(
                "\u2705 \u7fa4\u4e3b\u53ca\u7ba1\u7406\u5458\u5df2\u5168\u90e8\u5728\u7b56\u7565\u7ec4\u7ba1\u7406\u5458\u5217\u8868\u4e2d\uff0c\u65e0\u9700\u540c\u6b65\u3002"
            )

    @event_message_type(EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self._active:
            return
        group_id = event.get_group_id() or ""
        if group_id not in self._group_configs:
            return
        sender = event.get_sender_name() or "unknown"

        text_parts: List[str] = []
        image_info = await self._image.extract(event)

        for seg in event.get_messages():
            if isinstance(seg, Plain):
                t = CLEAN_MESSAGE_RE.sub("", (str(seg.text) or "").strip()).strip()
                if t:
                    text_parts.append(t)

        text = " ".join(text_parts).strip()
        if not text and not image_info["captions"]:
            return
        if text.startswith(REPORT_PREFIX):
            return

        record = ChatRecord(
            session_id=event.unified_msg_origin,
            sender=sender,
            sender_id=self._get_sender_id(event),
            content=text,
            timestamp=time.time(),
            group_id=group_id,
            message_id=getattr(event.message_obj, "message_id", ""),
            image_urls=image_info["urls"],
            image_captions=image_info["captions"],
        )
        await self._storage.append_record(record)
        trigger_text = text or " ".join(image_info["captions"])
        await self._check_trigger(group_id, trigger_text)

    @after_message_sent()
    async def on_bot_reply(self, event: AstrMessageEvent):
        if not self._active:
            return
        group_id = event.get_group_id() or ""
        if group_id not in self._group_configs:
            return
        result = event.get_result()
        if not result:
            return
        chain = getattr(result, "chain", None) or []
        text_parts = []
        for seg in chain:
            seg_text = getattr(seg, "text", None)
            if seg_text:
                text_parts.append(str(seg_text))
        text = "".join(text_parts).strip()
        if not text:
            return
        if text.startswith(REPORT_PREFIX):
            return
        record = ChatRecord(
            session_id=event.unified_msg_origin,
            sender="AstrBot",
            sender_id=self._get_self_id(event),
            content=text,
            timestamp=time.time(),
            group_id=group_id,
        )
        await self._storage.append_record(record)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------

    def _resolve_group(self, event: AstrMessageEvent, gid: str) -> Tuple[str, Optional[GroupConfig]]:
        group_id = (gid or "").strip()
        if not group_id:
            group_id = event.get_group_id() or ""
        if not group_id:
            return "", None
        return group_id, self._group_configs.get(group_id)

    def _cmd_check_active(self) -> Optional[str]:
        if not self._active:
            return "插件未启用或未配置策略组，请先在 WebUI 中配置。"
        return None

    @command("analyze", alias={"审核", "analyze_now", "分析"})
    async def cmd_analyze(self, event: AstrMessageEvent, gid: str = ""):
        if err := self._cmd_check_active():
            yield event.plain_result(err)
            return
        group_id, gc = self._resolve_group(event, gid)
        if not group_id:
            yield event.plain_result("请指定群号，例如：/审核 123456")
            return
        if gc is None:
            yield event.plain_result(
                f"群 {group_id} 未配置分析策略，请在 WebUI 的「多群独立分析策略组」中添加。"
            )
            return
        if not self._admin.check(event, group_id):
            yield event.plain_result("权限不足，仅管理员可用。")
            return

        pending = await self._storage.count_unanalyzed(group_id)
        if not pending:
            yield event.plain_result(f"群 {group_id} 当前没有待分析的聊天记录。")
            return

        target = gc.target_session or self.target_session
        if target:
            admin_name = event.get_sender_name() or "管理员"
            admin_id = event.get_sender_id() or ""
            notify = (
                f"\U0001f4e2 管理员 {admin_name}({admin_id}) "
                f"发起了对群 {group_id} 的手动审核，正在分析中..."
            )
            try:
                await self.context.send_message(target, MessageChain(chain=[Plain(notify)]))
            except Exception as e:
                logger.error(f"发送审核通知失败: {e}")

        result_target_text = "通知群" if target else "日志"
        yield event.plain_result(f"正在分析群 {group_id}，结果将发送到{result_target_text}...")
        await self._engine.run(group_id, gc, force_report=True)

    @command("admin", alias={"策略组管理员", "setadmin"})
    async def cmd_admin(self, event: AstrMessageEvent, sync: str = ""):
        group_id = event.get_group_id() or ""
        if not group_id:
            yield event.plain_result("此命令仅可在群聊中使用。")
            return
        if not self._admin.check(event, group_id):
            yield event.plain_result("权限不足，仅管理员可用。")
            return
        gc = self._group_configs.get(group_id)
        if gc is None:
            yield event.plain_result("本群未配置分析策略，请在 WebUI 中先添加策略组。")
            return

        if sync == "--sync":
            yield event.plain_result("\u23f3 \u6b63\u5728\u540c\u6b65\u7fa4\u7ba1\u7406\u5458\u5217\u8868...")
            async for msg in self._sync_group_admins(event, group_id, gc):
                yield msg
            return

        at_qqs: List[str] = []
        for seg in event.get_messages():
            if isinstance(seg, At):
                qq = str(seg.qq)
                if qq != "all" and qq != event.get_self_id():
                    at_qqs.append(qq)
        if not at_qqs:
            yield event.plain_result("请 @ 要添加为策略组管理员的成员。")
            return

        added: List[str] = []
        for qq in at_qqs:
            if qq not in gc.admin_ids:
                gc.admin_ids.append(qq)
                added.append(qq)

        if added:
            persist_strategy_admins(self.cfg, group_id, gc.admin_ids)
            yield event.plain_result(
                f"\u2705 \u5df2\u5c06 {', '.join(added)} \u6dfb\u52a0\u4e3a\u7fa4 {group_id} \u7684\u7b56\u7565\u7ec4\u7ba1\u7406\u5458\u3002"
                f"\n\u5f53\u524d\u7b56\u7565\u7ec4\u7ba1\u7406\u5458: {', '.join(gc.admin_ids) if gc.admin_ids else '\u65e0'}"
            )
            logger.info(f"群 {group_id} 策略组管理员已更新: {gc.admin_ids}")
        else:
            yield event.plain_result(
                f"\u6307\u5b9a\u6210\u5458\u5df2\u5728\u7b56\u7565\u7ec4\u7ba1\u7406\u5458\u5217\u8868\u4e2d\u3002"
                f"\n\u5f53\u524d\u7b56\u7565\u7ec4\u7ba1\u7406\u5458: {', '.join(gc.admin_ids) if gc.admin_ids else '\u65e0'}"
            )

    @command("execute_confirm", alias={"执行确认", "执行"})
    async def cmd_confirm(self, event: AstrMessageEvent, confirm_id: str = ""):
        cid = (confirm_id or "").strip()
        if not cid:
            yield event.plain_result("请提供确认编号，例如：/执行确认 12345678")
            return
        pending = self._engine._pending_actions.pop(cid, None)
        if pending is None:
            yield event.plain_result(f"确认编号 {cid} 不存在或已过期。")
            return
        if not self._admin.check(event, pending["group_id"]):
            yield event.plain_result("权限不足，仅管理员可用。")
            return
        results = await self._executor.execute_actions(pending["group_id"], pending["actions"])
        summary = f"\U0001f4cb \u5904\u7f6e\u7ed3\u679c (编号 {cid}):\n" + "\n".join(results)
        yield event.plain_result(summary)

    @command("execute_reject", alias={"执行拒绝"})
    async def cmd_reject(self, event: AstrMessageEvent, confirm_id: str = ""):
        cid = (confirm_id or "").strip()
        if not cid:
            yield event.plain_result("请提供确认编号，例如：/执行拒绝 12345678")
            return
        pending = self._engine._pending_actions.pop(cid, None)
        if pending is None:
            yield event.plain_result(f"确认编号 {cid} 不存在或已过期。")
            return
        if not self._admin.check(event, pending["group_id"]):
            yield event.plain_result("权限不足，仅管理员可用。")
            return
        yield event.plain_result(f"\u274c \u5df2\u62d2\u7edd\u7f16\u53f7 {cid} \u7684\u5904\u7f6e\u5efa\u8bae\u3002")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for t in list(self._trigger_tasks):
            if not t.done():
                t.cancel()

    # ------------------------------------------------------------------
    # 定时循环 & 关键词触发
    # ------------------------------------------------------------------

    async def _analysis_loop(self):
        while True:
            try:
                await asyncio.sleep(self.interval_minutes * 60)
                for gid, gc in self._group_configs.items():
                    await self._engine.run(gid, gc)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"聊天记录分析循环异常: {e}")

    async def _check_trigger(self, group_id: str, text: str):
        if not group_id:
            return
        gc = self._group_configs.get(group_id)
        if not gc or not gc.trigger_keywords:
            return
        if not any(kw in text for kw in gc.trigger_keywords):
            return
        now = time.time()
        last = self._trigger_cooldowns.get(group_id, 0)
        if now - last < TRIGGER_COOLDOWN_SECONDS:
            return
        self._trigger_cooldowns[group_id] = now
        task = asyncio.create_task(self._trigger_analysis(group_id, gc))
        self._trigger_tasks.add(task)
        task.add_done_callback(self._trigger_tasks.discard)
        logger.info(f"群 {group_id} 触发关键词即时分析")

    async def _trigger_analysis(self, group_id: str, gc: GroupConfig):
        try:
            await self._engine.run(group_id, gc)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"群 {group_id} 即时分析异常: {e}")
