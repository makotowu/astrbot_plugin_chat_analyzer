import time
from typing import List, Set

from .models import ChatRecord


def format_time_range(records: List[ChatRecord]) -> str:
    if not records:
        return "N/A"
    start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(records[0].timestamp))
    end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(records[-1].timestamp))
    return f"{start} ~ {end}"


def extract_session_info(records: List[ChatRecord]) -> str:
    groups: Set[str] = set()
    for r in records:
        if r.group_id:
            groups.add(r.group_id)
    if groups:
        return f"群聊: {', '.join(sorted(groups))}"
    return "未知"
