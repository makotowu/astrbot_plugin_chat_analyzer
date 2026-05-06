import asyncio
import json
import os
from collections import deque
from typing import Deque, Dict, List

from astrbot.api import logger

from .models import ChatRecord


class BufferManager:
    def __init__(self, data_path: str, max_messages: int):
        self._data_path = data_path
        self._max_messages = max_messages
        self._buffers: Dict[str, Deque[ChatRecord]] = {}
        self._lock = asyncio.Lock()
        self._msg_count: Dict[str, int] = {}

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    @property
    def buffers(self) -> Dict[str, Deque[ChatRecord]]:
        return self._buffers

    @staticmethod
    def buffer_key(group_id: str) -> str:
        return group_id

    def ensure_buffer(self, key: str) -> None:
        if key not in self._buffers:
            cap = self._max_messages * 3 if self._max_messages > 0 else 10000
            self._buffers[key] = deque(maxlen=cap)

    async def append_record(self, group_id: str, record: ChatRecord) -> None:
        key = self.buffer_key(group_id)
        async with self._lock:
            self.ensure_buffer(key)
            self._buffers[key].append(record)
            self._msg_count[key] = self._msg_count.get(key, 0) + 1
            should_save = self._msg_count[key] % 10 == 0
        if should_save:
            self.save()

    async def pop_records(self, group_id: str) -> List[ChatRecord]:
        key = self.buffer_key(group_id)
        records: List[ChatRecord] = []
        async with self._lock:
            buf = self._buffers.get(key)
            if not buf:
                return records
            count = len(buf) if self._max_messages <= 0 else min(len(buf), self._max_messages)
            for _ in range(count):
                records.append(buf.popleft())
        return records

    async def pushback_records(self, group_id: str, records: List[ChatRecord]) -> None:
        key = self.buffer_key(group_id)
        async with self._lock:
            buf = self._buffers.get(key)
            if buf is not None:
                buf.extendleft(reversed(records))

    async def pending_count(self, group_id: str) -> int:
        key = self.buffer_key(group_id)
        async with self._lock:
            buf = self._buffers.get(key)
            return len(buf) if buf else 0

    def load(self) -> int:
        if not os.path.exists(self._data_path):
            return 0
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return 0
            loaded = 0
            for key, items in data.items():
                if not isinstance(items, list):
                    continue
                records = [ChatRecord.from_dict(r) for r in items if isinstance(r, dict)]
                if records:
                    self.ensure_buffer(key)
                    self._buffers[key].extend(records)
                    loaded += len(records)
            if loaded:
                logger.info(f"从磁盘恢复了 {loaded} 条待分析消息")
            return loaded
        except Exception as e:
            logger.error(f"加载持久化缓冲失败: {e}")
            return 0

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
            data: Dict[str, List[Dict]] = {}
            for key, buf in self._buffers.items():
                if buf:
                    data[key] = [r.to_dict() for r in buf]
            with open(self._data_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓冲到磁盘失败: {e}")
