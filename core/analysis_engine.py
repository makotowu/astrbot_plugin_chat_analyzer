import time
import uuid
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import Context
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .action_executor import ActionExecutor
from .admin_manager import AdminManager
from .ai_client import AIClient
from .analysis_parser import (
    extract_action_suggestions,
    extract_overall_conclusion,
    extract_position_items,
    sanitize_analysis_output,
)
from .buffer_manager import BufferManager
from .constant import ACTION_LABELS, SKIP_SILENT_MARKER
from .models import ChatRecord, GroupConfig
from .prompt_builder import build_combined_prompt, resolve_group_prompts
from .report_sender import ReportSender


class AnalysisEngine:
    def __init__(
        self,
        context: Context,
        buf: BufferManager,
        ai: AIClient,
        report: ReportSender,
        executor: ActionExecutor,
        admin: AdminManager,
        skip_silent: bool,
    ):
        self._context = context
        self._buf = buf
        self._ai = ai
        self._report = report
        self._executor = executor
        self._admin = admin
        self._skip_silent = skip_silent
        self._pending_actions: Dict[str, dict] = {}

    async def run(
        self, group_id: str, gc: GroupConfig, *, force_report: bool = False
    ):
        records = await self._buf.pop_records(group_id)
        if not records:
            return

        prompts = resolve_group_prompts(gc)
        system_prompt = build_combined_prompt(
            prompts,
            self._skip_silent and not force_report,
            gc.group_rules,
            gc.action_mode,
        )
        chat_text = "\n".join(r.format(i + 1) for i, r in enumerate(records))

        logger.info(
            f"开始分析群 {group_id} 的 {len(records)} 条记录，"
            f"策略数 {len(prompts)}（合并为单次 AI 调用）"
        )

        raw = await self._ai.analyze(chat_text, system_prompt)
        if not raw:
            await self._buf.pushback_records(group_id, records)
            logger.error(f"群 {group_id} AI 分析返回空结果。")
            return

        result = sanitize_analysis_output(raw)
        if self._skip_silent and not force_report and result.strip() == SKIP_SILENT_MARKER:
            logger.info(f"群 {group_id} 本轮分析无异常，跳过报告推送。")
            return

        overall_conclusion = extract_overall_conclusion(result)
        position_items = extract_position_items(result, len(records))
        flagged_pairs: Optional[List[Tuple[str, str, ChatRecord]]] = None

        if overall_conclusion != "正常":
            if position_items:
                pairs: List[Tuple[str, str, ChatRecord]] = []
                for level, idx, reason in position_items:
                    if level == "违规":
                        level = "复核"
                    pairs.append((level, reason, records[idx - 1]))
                flagged_pairs = pairs
                logger.info(
                    f"群 {group_id} 提取到 {len(position_items)} 条定位消息:"
                    f" {[f'{level}#{idx}({reason})' for level, idx, reason in position_items]}"
                )
            else:
                fallback_reason = (
                    f"AI 总体结论为\"{overall_conclusion or '需人工复核'}\"，"
                    "但未返回明确编号，请逐条人工复核"
                )
                flagged_pairs = [("关注", fallback_reason, record) for record in records]
                logger.warning(
                    f"群 {group_id} 总体结论为 {overall_conclusion or '未知'}，"
                    "但未提取到定位清单，已回退为整批消息逐条转发"
                )

        header = self._report.build_header(records, prompts)
        full_report = header + result
        target = gc.target_session or ""

        action_results_text = ""
        if gc.action_mode in ("confirm", "auto"):
            action_suggestions = extract_action_suggestions(result, records, len(records))
            if action_suggestions:
                clean_actions, admin_warnings = self._admin.filter_actions(
                    group_id, action_suggestions, target
                )
                if admin_warnings:
                    action_results_text = (
                        "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                        "\u26a0\ufe0f \u7ba1\u7406\u5458\u5f02\u5e38\u63d0\u9192:\n"
                        + "\n".join(admin_warnings)
                    )
                if clean_actions:
                    if gc.action_mode == "auto":
                        action_results_text += await self._handle_auto(group_id, clean_actions, target)
                    elif gc.action_mode == "confirm":
                        action_results_text += await self._handle_confirm(group_id, clean_actions, target)

        await self._report.send_result(target, full_report, result, flagged_pairs, action_results_text)
        self._buf.save()

    async def _handle_auto(self, group_id: str, actions: list, target: str) -> str:
        results = await self._executor.execute_actions(group_id, actions)
        action_report = (
            "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\U0001f6e1\ufe0f \u81ea\u52a8\u5904\u7f6e\u7ed3\u679c:\n"
            + "\n".join(results)
        )
        logger.info(f"群 {group_id} auto 模式已执行 {len(actions)} 项处置。")
        if target:
            try:
                await self._context.send_message(
                    target,
                    MessageChain(chain=[Plain(action_report)]),
                )
            except Exception as e:
                logger.error(f"发送自动处置报告失败: {e}")
        return action_report

    async def _handle_confirm(self, group_id: str, actions: list, target: str) -> str:
        confirm_id = uuid.uuid4().hex[:8]
        self._pending_actions[confirm_id] = {
            "group_id": group_id,
            "actions": actions,
            "target": target,
            "created_at": time.time(),
        }
        lines_desc = []
        for act, idx, reason, tid, name, mute_dur, _msg_id, _notify in actions:
            act_label = ACTION_LABELS.get(act, act)
            if act == "禁言":
                desc = f"  {act_label} #{idx} {name}({tid}) {mute_dur}s: {reason}"
            else:
                desc = f"  {act_label} #{idx} {name}({tid}): {reason}"
            lines_desc.append(desc)
        confirm_msg = (
            f"\U0001f6a8 AI \u5efa\u8bae\u5bf9\u7fa4 {group_id} \u6267\u884c\u4ee5\u4e0b\u5904\u7f6e:\n"
            + "\n".join(lines_desc)
            + f"\n\n\u8bf7\u7ba1\u7406\u5458\u786e\u8ba4:\n"
            f"  /执行确认 {confirm_id}\n"
            f"  /执行拒绝 {confirm_id}\n"
            f"(\u7f16\u53f7\u6709\u6548\u671f 10 \u5206\u949f)"
        )
        if target:
            try:
                await self._context.send_message(
                    target,
                    MessageChain(chain=[Plain(confirm_msg)]),
                )
            except Exception as e:
                logger.error(f"发送确认请求失败: {e}")
        logger.info(
            f"群 {group_id} confirm 模式待确认 {confirm_id}，"
            f"{len(actions)} 项处置。"
        )
        return "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n" + confirm_msg
