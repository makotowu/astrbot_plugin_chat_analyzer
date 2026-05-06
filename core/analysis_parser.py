from typing import List, Set, Tuple

from .constant import ACTION_LINE_RE, POSITION_LINE_RE
from .models import ChatRecord


def extract_overall_conclusion(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip(" -*\t") for line in text.splitlines()]
    for idx, line in enumerate(lines):
        if line == "【总体结论】":
            for next_line in lines[idx + 1:]:
                if not next_line:
                    continue
                if next_line.startswith("【"):
                    break
                if "建议复核" in next_line or "需复核" in next_line:
                    return "建议复核"
                if "存在违规" in next_line:
                    return "建议复核"
                if "需关注" in next_line:
                    return "需关注"
                if "正常" in next_line:
                    return "正常"
    for line in lines:
        if "建议复核" in line or "需复核" in line:
            return "建议复核"
        if "存在违规" in line:
            return "建议复核"
        if "需关注" in line:
            return "需关注"
        if "正常" in line:
            return "正常"
    return ""


def extract_position_items(text: str, max_idx: int) -> List[Tuple[str, int, str]]:
    if not text:
        return []
    items: List[Tuple[str, int, str]] = []
    seen: Set[Tuple[str, int]] = set()
    for line in text.splitlines():
        m = POSITION_LINE_RE.search(line)
        if m:
            level = m.group(1).strip()
            idx = int(m.group(2))
            key = (level, idx)
            if 1 <= idx <= max_idx and key not in seen:
                items.append((level, idx, m.group(3).strip()))
                seen.add(key)
    return items


def extract_action_suggestions(
    text: str, records: List[ChatRecord], max_idx: int, default_mute: int = 600,
) -> List[Tuple[str, int, str, str, str, int, str, str]]:
    if not text:
        return []
    suggestions: List[Tuple[str, int, str, str, str, int, str, str]] = []
    seen: Set[Tuple[str, int]] = set()
    for line in text.splitlines():
        m = ACTION_LINE_RE.search(line)
        if m:
            action = m.group(1).strip()
            idx = int(m.group(2))
            key = (action, idx)
            if 1 <= idx <= max_idx and key not in seen:
                record = records[idx - 1]
                target_id = record.sender_id or record.sender
                mute_dur_str = m.group(3)
                if action == "禁言" and mute_dur_str:
                    mute_duration = max(1, int(mute_dur_str))
                elif action == "禁言":
                    mute_duration = default_mute
                else:
                    mute_duration = 0
                notify = (m.group(5) or "").strip()
                suggestions.append((
                    action, idx, (m.group(4) or "").strip(),
                    target_id, record.sender,
                    mute_duration, record.message_id,
                    notify,
                ))
                seen.add(key)
    return suggestions


def sanitize_analysis_output(text: str) -> str:
    if not text:
        return text

    sanitized = text
    replacements = [
        ("存在违规", "建议复核"),
        ("发现违规", "建议复核"),
        ("疑似违规", "建议复核"),
        ("违规 #", "复核 #"),
        ("违规项", "复核项"),
        ("违规内容", "风险内容"),
        ("违规言论", "风险言论"),
        ("违规行为", "风险行为"),
    ]
    for src, dst in replacements:
        sanitized = sanitized.replace(src, dst)

    return sanitized
