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
        image_urls: List[str] | None = None,
        image_captions: List[str] | None = None,
    ):
        self.session_id = session_id
        self.sender = sender
        self.sender_id = sender_id
        self.content = content
        self.timestamp = timestamp
        self.group_id = group_id
        self.message_id = message_id
        self.image_urls: List[str] = image_urls or []
        self.image_captions: List[str] = image_captions or []
        self.is_admin: bool = False
        self._db_id: int = 0

    @property
    def db_id(self) -> int:
        return self._db_id

    @db_id.setter
    def db_id(self, value: int) -> None:
        self._db_id = value

    def format(self, index: int = 0) -> str:
        time_str = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        location = f"[群:{self.group_id}]" if self.group_id else ""
        prefix = f"[#{index}] " if index > 0 else ""
        admin_tag = "[管理员] " if self.is_admin else ""
        base = f"{prefix}[{time_str}]{location} {admin_tag}{self.sender}: {self.content}"
        for caption in self.image_captions:
            base += f"\n{prefix} > [图片: {caption}]"
        return base

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "sender": self.sender,
            "sender_id": self.sender_id,
            "content": self.content,
            "timestamp": self.timestamp,
            "group_id": self.group_id,
            "message_id": self.message_id,
            "image_urls": self.image_urls,
            "image_captions": self.image_captions,
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
            image_urls=d.get("image_urls") or [],
            image_captions=d.get("image_captions") or [],
        )
