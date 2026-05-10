import json
import random
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import Context

from .action_executor import ActionExecutor
from .admin_manager import AdminManager
from .ai_client import AIClient
from .analysis_parser import (
    extract_action_suggestions,
    extract_admin_reminders,
    extract_overall_conclusion,
    extract_position_items,
    sanitize_analysis_output,
)
from .constant import ACTION_LABELS
from .models import ChatRecord, GroupConfig
from .prompt_builder import build_system_prompt, resolve_group_prompts
from .report_sender import ReportSender
from .storage import ChatStorage


class AnalysisEngine:
    def __init__(
        self,
        context: Context,
        storage: ChatStorage,
        ai: AIClient,
        report: ReportSender,
        executor: ActionExecutor,
        admin: AdminManager,
        skip_silent: bool,
        debug: bool = False,
    ):
        self._context = context
        self._storage = storage
        self._ai = ai
        self._report = report
        self._executor = executor
        self._admin = admin
        self._skip_silent = skip_silent
        self._debug = debug
        self._pending_actions: Dict[str, dict] = {}
        if debug:
            self._ai.set_debug(True)

    async def run(
        self, group_id: str, gc: GroupConfig, *, force_report: bool = False
    ):
        records = await self._storage.get_unanalyzed_records(group_id)
        if not records:
            return

        admin_ids = self._admin.all_ids(group_id)
        for r in records:
            if r.sender_id in admin_ids:
                r.is_admin = True

        prompts = resolve_group_prompts(gc)
        system_prompt = build_system_prompt(
            prompts,
            self._skip_silent and not force_report,
            gc.action_mode,
        )

        records.sort(key=lambda r: r.timestamp)
        chat_text = (
            f"群号: {group_id}\n"
            f"处置模式: {gc.action_mode}\n"
        )
        if gc.group_rules:
            chat_text += f"群规: {gc.group_rules[:500]}\n"
        chat_text += "\n" + "\n".join(r.format(i + 1) for i, r in enumerate(records))

        if self._debug:
            logger.debug(f"g:{group_id} system_prompt:\n{system_prompt[:2000]}")
            logger.debug(f"g:{group_id} chat_text:\n{chat_text[:2000]}")

        logger.info(
            f"开始分析群 {group_id}，共 {len(records)} 条记录，策略数 {len(prompts)}"
        )

        ai_output = await self._ai.analyze(chat_text, system_prompt)
        if not ai_output:
            logger.error(f"群 {group_id} AI 分析返回空结果")
            record_ids_to_mark = [r.db_id for r in records if r.db_id]
            if record_ids_to_mark:
                await self._storage.mark_analyzed(record_ids_to_mark)
            return

        sanitized_text = sanitize_analysis_output(ai_output)
        overall_conclusion = extract_overall_conclusion(sanitized_text)
        position_items = extract_position_items(sanitized_text, len(records))
        action_suggestions = extract_action_suggestions(sanitized_text, records, len(records))
        admin_reminders = extract_admin_reminders(sanitized_text, records, len(records))

        record_ids_to_mark = [r.db_id for r in records if r.db_id]

        if self._debug:
            logger.debug(
                f"g:{group_id} conclusion={overall_conclusion} "
                f"position_items={len(position_items)} "
                f"actions={len(action_suggestions)} "
                f"reminders={len(admin_reminders)}"
            )

        if (
            self._skip_silent
            and not force_report
            and overall_conclusion == "正常"
            and not position_items
            and not action_suggestions
        ):
            logger.info(
                f"群 {group_id} 本轮分析结论正常，且无定位消息和处置建议，跳过报告推送。"
            )
            if record_ids_to_mark:
                await self._storage.mark_analyzed(record_ids_to_mark)
            return

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
                    f"群 {group_id} 提取到 {len(position_items)} 条定位消息"
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
        compact_result = sanitized_text
        full_report = header + compact_result
        target = gc.target_session or ""

        for idx, target_id, sender_name, reminder in admin_reminders:
            if target_id in admin_ids:
                await self._admin._send_warning(target, group_id, [(f"{sender_name}({target_id}): {reminder}")])

        action_results_text = ""
        if gc.action_mode in ("confirm", "auto"):
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
                        action_results_text += self._handle_auto_sync(
                            group_id, clean_actions, target,
                        )
                    elif gc.action_mode == "confirm":
                        action_results_text += self._handle_confirm_sync(
                            group_id, clean_actions, target,
                        )

        await self._report.send_result(
            target, full_report, compact_result, flagged_pairs, action_results_text
        )

        if record_ids_to_mark:
            await self._storage.mark_analyzed(record_ids_to_mark)

        await self._storage.log_analysis(
            group_id=group_id,
            record_count=len(records),
            prompts=[p[0] for p in prompts],
            system_prompt=system_prompt[:500],
            ai_response=ai_output[:2000],
            conclusion=overall_conclusion,
            action_count=len(action_suggestions),
        )

        await self._storage.cleanup_old_records(group_id, keep_days=7)

    def _handle_auto_sync(self, group_id: str, actions: list, target: str) -> str:
        import asyncio
        asyncio.create_task(self._executor.execute_actions(group_id, actions))
        action_labels = [
            ACTION_LABELS.get(a[0], a[0]) for a in actions
        ]
        action_msg = (
            "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\U0001f6e1\ufe0f \u81ea\u52a8\u5904\u7f6e\u5df2\u89e6\u53d1: "
            + ", ".join(action_labels)
        )
        logger.info(f"群 {group_id} auto 模式已触发 {len(actions)} 项处置。")
        return action_msg

    def _handle_confirm_sync(self, group_id: str, actions: list, target: str) -> str:
        groups: Dict[str, list] = {}
        for act in actions:
            tid = act[3]
            groups.setdefault(tid, []).append(act)

        parts: List[str] = []
        parts.append("\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n")

        for tid, person_actions in groups.items():
            confirm_id = str(random.randint(10000000, 99999999))
            self._pending_actions[confirm_id] = {
                "group_id": group_id,
                "actions": person_actions,
                "target": target,
                "created_at": time.time(),
            }

            name = person_actions[0][4]

            action_groups: Dict[str, List[str]] = {}
            for act, idx, reason, _tid, _name, mute_dur, _msg_id, _notify in person_actions:
                act_label = ACTION_LABELS.get(act, act)
                if act == "禁言":
                    detail = f"#{idx} {reason}（{mute_dur}s）"
                else:
                    detail = f"#{idx} {reason}"
                action_groups.setdefault(act_label, []).append(detail)

            lines_desc: List[str] = []
            for act_label, details in action_groups.items():
                lines_desc.append(f"  \u00b7 {act_label}: {', '.join(details)}")

            confirm_msg = (
                f"\U0001f6a8 AI \u5efa\u8bae\u5bf9\u7fa4 {group_id} \u4ee5\u4e0b\u5904\u7f6e:\n"
                f"\U0001f464 {name}({tid})\n"
                + "\n".join(lines_desc)
                + f"\n\n\u8bf7\u7ba1\u7406\u5458\u786e\u8ba4:\n"
                f"  /执行确认 {confirm_id}\n"
                f"  /执行拒绝 {confirm_id}\n"
                f"(\u7f16\u53f7\u6709\u6548\u671f 60 \u5206\u949f)"
            )

            logger.info(
                f"群 {group_id} 待确认 {confirm_id}，"
                f"{name}({tid}) {len(person_actions)} 项处置。"
            )
            parts.append(confirm_msg)

        return "\n".join(parts)
