import time
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class GroupConfig:
    group_id: str
    presets: List[str] = field(default_factory=lambda: ["default"])
    custom_prompt: str = ""
    action_mode: str = "suggest"
    group_rules: str = ""
    trigger_keywords: List[str] = field(default_factory=list)
    target_session: str = ""
    admin_ids: List[str] = field(default_factory=list)


class ChatRecord:
    def __init__(
        self,
        session_id: str,
        sender: str,
        content: str,
        timestamp: float,
        group_id: str = "",
        sender_id: str = "",
        message_id: str = "",
    ):
        self.session_id = session_id
        self.sender = sender
        self.sender_id = sender_id
        self.content = content
        self.timestamp = timestamp
        self.group_id = group_id
        self.message_id = message_id

    def format(self, index: int = 0) -> str:
        time_str = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        location = f"[群:{self.group_id}]" if self.group_id else ""
        prefix = f"[#{index}] " if index > 0 else ""
        return f"{prefix}[{time_str}]{location} {self.sender}: {self.content}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "sender": self.sender,
            "sender_id": self.sender_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "group_id": self.group_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChatRecord":
        return cls(
            session_id=d.get("session_id", ""),
            sender=d.get("sender", ""),
            sender_id=d.get("sender_id", ""),
            content=d.get("content", ""),
            timestamp=d.get("timestamp", 0.0),
            group_id=d.get("group_id", ""),
            message_id=d.get("message_id", ""),
        )
