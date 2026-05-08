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
from .buffer_manager import BufferManager
from .constant import ACTION_LABELS
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

        admin_ids = self._admin.all_ids(group_id)
        for r in records:
            if r.sender_id in admin_ids:
                r.is_admin = True

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

        overall_conclusion = extract_overall_conclusion(result)
        position_items = extract_position_items(result, len(records))
        action_suggestions = extract_action_suggestions(result, records, len(records))

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
        result = self._compact_report(result)
        full_report = header + result
        target = gc.target_session or ""

        admin_reminders = extract_admin_reminders(result, records, len(records))

        reminder_text = ""
        for idx, target_id, sender_name, reminder in admin_reminders:
            await self._admin.send_admin_reminder(
                group_id, target_id, sender_name, reminder, target,
            )
            if reminder_text:
                reminder_text += "\n"
            reminder_text += (
                f"\U0001f4e2 {sender_name}({target_id}): {reminder}"
            )

        if reminder_text:
            reminder_text = (
                "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                "\U0001f4e2 \u7ba1\u7406\u5458\u63d0\u9192:\n"
                + reminder_text
            )

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
                        action_results_text += await self._handle_auto(group_id, clean_actions, target)
                    elif gc.action_mode == "confirm":
                        action_results_text += await self._handle_confirm(group_id, clean_actions, target)

        await self._report.send_result(target, full_report, result, flagged_pairs, reminder_text + action_results_text)
        self._buf.save()

    async def _handle_auto(self, group_id: str, actions: list, target: str) -> str:
        results = await self._executor.execute_actions(group_id, actions)
        action_report = (
            "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\U0001f6e1\ufe0f \u81ea\u52a8\u5904\u7f6e\u7ed3\u679c:\n"
            + "\n".join(results)
        )
        logger.info(f"群 {group_id} auto 模式已执行 {len(actions)} 项处置。")
        return action_report

    async def _handle_confirm(self, group_id: str, actions: list, target: str) -> str:
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

    @staticmethod
    def _compact_report(result: str) -> str:
        sections: Dict[str, List[str]] = {}
        current_section: str | None = None

        for line in result.splitlines():
            stripped = line.strip()
            if stripped.startswith("【") and "】" in stripped:
                current_section = stripped
                sections.setdefault(current_section, [])
                continue
            if current_section is not None:
                sections[current_section].append(line)

        out: List[str] = []

        SKIP_SECTIONS = {"【处置建议】", "【管理员提醒】"}

        for sec_title, sec_lines in sections.items():
            if sec_title in SKIP_SECTIONS:
                continue
            out.append(sec_title)
            content = [l.strip() for l in sec_lines if l.strip()]
            out.extend(content)

        return "\n".join(out) + "\n"
