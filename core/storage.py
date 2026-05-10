import asyncio
import json
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from astrbot.api import logger

from .models import ChatRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    sender_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    timestamp REAL NOT NULL DEFAULT 0,
    group_id TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0,
    image_urls TEXT NOT NULL DEFAULT '[]',
    image_captions TEXT NOT NULL DEFAULT '[]',
    analyzed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chat_records_group ON chat_records(group_id);
CREATE INDEX IF NOT EXISTS idx_chat_records_timestamp ON chat_records(timestamp);
CREATE INDEX IF NOT EXISTS idx_chat_records_analyzed ON chat_records(group_id, analyzed);

CREATE TABLE IF NOT EXISTS image_captions (
    image_url TEXT PRIMARY KEY,
    caption TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analysis_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL DEFAULT '',
    record_count INTEGER NOT NULL DEFAULT 0,
    prompts TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL DEFAULT '',
    ai_response TEXT NOT NULL DEFAULT '',
    conclusion TEXT NOT NULL DEFAULT '',
    action_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_analysis_logs_group ON analysis_logs(group_id);
CREATE INDEX IF NOT EXISTS idx_analysis_logs_time ON analysis_logs(created_at);
"""


class ChatStorage:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    async def append_record(self, record: ChatRecord) -> None:
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO chat_records
                       (session_id, sender, sender_id, content, timestamp,
                        group_id, message_id, is_admin, image_urls, image_captions)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.session_id,
                        record.sender,
                        record.sender_id,
                        record.content,
                        record.timestamp,
                        record.group_id,
                        record.message_id,
                        1 if record.is_admin else 0,
                        json.dumps(record.image_urls, ensure_ascii=False),
                        json.dumps(record.image_captions, ensure_ascii=False),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    async def get_unanalyzed_records(
        self, group_id: str, limit: int = 0
    ) -> List[ChatRecord]:
        async with self._lock:
            conn = self._get_conn()
            try:
                query = (
                    "SELECT * FROM chat_records "
                    "WHERE group_id = ? AND analyzed = 0 "
                    "ORDER BY timestamp ASC"
                )
                if limit > 0:
                    query += f" LIMIT {limit}"
                rows = conn.execute(query, (group_id,)).fetchall()
                return [self._row_to_record(r) for r in rows]
            finally:
                conn.close()

    async def mark_analyzed(self, record_ids: List[int]) -> None:
        if not record_ids:
            return
        async with self._lock:
            conn = self._get_conn()
            try:
                placeholders = ",".join("?" for _ in record_ids)
                conn.execute(
                    f"UPDATE chat_records SET analyzed = 1 WHERE id IN ({placeholders})",
                    record_ids,
                )
                conn.commit()
            finally:
                conn.close()

    async def query_records(
        self,
        group_id: str,
        keyword: str = "",
        sender: str = "",
        sender_id: str = "",
        limit: int = 50,
        offset: int = 0,
        since: float = 0,
        until: float = 0,
    ) -> List[ChatRecord]:
        async with self._lock:
            conn = self._get_conn()
            try:
                conditions = ["group_id = ?"]
                params: list = [group_id]

                if keyword:
                    conditions.append("content LIKE ?")
                    params.append(f"%{keyword}%")
                if sender:
                    conditions.append("sender = ?")
                    params.append(sender)
                if sender_id:
                    conditions.append("sender_id = ?")
                    params.append(sender_id)
                if since > 0:
                    conditions.append("timestamp >= ?")
                    params.append(since)
                if until > 0:
                    conditions.append("timestamp <= ?")
                    params.append(until)

                where = " AND ".join(conditions)
                query = (
                    f"SELECT * FROM chat_records WHERE {where} "
                    "ORDER BY timestamp ASC LIMIT ? OFFSET ?"
                )
                params.extend([limit, offset])
                rows = conn.execute(query, params).fetchall()
                return [self._row_to_record(r) for r in rows]
            finally:
                conn.close()

    async def count_unanalyzed(self, group_id: str) -> int:
        async with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM chat_records "
                    "WHERE group_id = ? AND analyzed = 0",
                    (group_id,),
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()

    async def get_cached_caption(self, image_url: str) -> Optional[str]:
        async with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT caption FROM image_captions WHERE image_url = ?",
                    (image_url,),
                ).fetchone()
                return row["caption"] if row else None
            finally:
                conn.close()

    async def set_cached_caption(self, image_url: str, caption: str) -> None:
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO image_captions (image_url, caption, created_at) "
                    "VALUES (?, ?, ?)",
                    (image_url, caption, time.time()),
                )
                conn.commit()
            finally:
                conn.close()

    async def log_analysis(
        self,
        group_id: str,
        record_count: int,
        prompts: List[str],
        system_prompt: str,
        ai_response: str,
        conclusion: str,
        action_count: int,
    ) -> None:
        async with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO analysis_logs
                       (group_id, record_count, prompts, system_prompt,
                        ai_response, conclusion, action_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        group_id,
                        record_count,
                        json.dumps(prompts, ensure_ascii=False),
                        system_prompt,
                        ai_response,
                        conclusion,
                        action_count,
                        time.time(),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    async def cleanup_old_records(
        self, group_id: str, keep_days: int = 7
    ) -> int:
        cutoff = time.time() - keep_days * 86400
        async with self._lock:
            conn = self._get_conn()
            try:
                cursor = conn.execute(
                    "DELETE FROM chat_records "
                    "WHERE group_id = ? AND timestamp < ? AND analyzed = 1",
                    (group_id, cutoff),
                )
                deleted = cursor.rowcount
                conn.commit()
                if deleted:
                    logger.info(f"群 {group_id} 清理了 {deleted} 条已分析的旧记录")
                return deleted
            finally:
                conn.close()

    async def get_analysis_logs(
        self, group_id: str, limit: int = 10
    ) -> List[Dict]:
        async with self._lock:
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT conclusion, record_count, action_count, created_at "
                    "FROM analysis_logs WHERE group_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (group_id, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    async def query_flagged_members(
        self, group_id: str, sender: str, limit: int = 20
    ) -> int:
        async with self._lock:
            conn = self._get_conn()
            try:
                logs = conn.execute(
                    "SELECT ai_response FROM analysis_logs "
                    "WHERE group_id = ? ORDER BY created_at DESC LIMIT ?",
                    (group_id, limit),
                ).fetchall()
            finally:
                conn.close()

        count = 0
        for log in logs:
            text = log["ai_response"] or ""
            if sender in text and ("关注" in text or "复核" in text):
                count += 1
        return count

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ChatRecord:
        record = ChatRecord(
            session_id=row["session_id"] or "",
            sender=row["sender"] or "",
            sender_id=row["sender_id"] or "",
            content=row["content"] or "",
            timestamp=row["timestamp"] or 0.0,
            group_id=row["group_id"] or "",
            message_id=row["message_id"] or "",
        )
        record.db_id = row["id"]
        record.is_admin = bool(row["is_admin"])
        try:
            record.image_urls = json.loads(row["image_urls"] or "[]")
        except (json.JSONDecodeError, TypeError):
            record.image_urls = []
        try:
            record.image_captions = json.loads(row["image_captions"] or "[]")
        except (json.JSONDecodeError, TypeError):
            record.image_captions = []
        return record

    async def record_count(self, group_id: str) -> int:
        async with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM chat_records WHERE group_id = ?",
                    (group_id,),
                ).fetchone()
                return row["cnt"] if row else 0
            finally:
                conn.close()
