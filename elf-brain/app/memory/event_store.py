"""L0 情景流事件存储（docs/02 §1）。

红线：append-only，只有 insert/select，没有 update/delete；
窗口标题等敏感原文不出 perception 包（事件入库前已泛化）。
"""

import json
import sqlite3
import threading
from pathlib import Path


class EventStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                ts INTEGER NOT NULL,
                type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                session_id TEXT
            )
            """
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
        self._conn.commit()

    def append(self, event_id: str, ts: int, type: str, source: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (event_id, ts, type, source, payload_json) VALUES (?,?,?,?,?)",
                (event_id, ts, type, source, json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()

    def query(self, type_prefix: str | None = None, since_ts: int = 0, limit: int = 500) -> list[dict]:
        sql = "SELECT event_id, ts, type, source, payload_json FROM events WHERE ts >= ?"
        args: list = [since_ts]
        if type_prefix:
            sql += " AND type LIKE ?"
            args.append(f"{type_prefix}%")
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [
            {"event_id": r[0], "ts": r[1], "type": r[2], "source": r[3], "payload": json.loads(r[4])}
            for r in rows
        ]

    def first_ts(self) -> float | None:
        """最早一条事件的秒级时间戳（里程碑「认识第 N 天」的原点）；未初始化/空库 None。"""
        if self._conn is None:
            return None
        row = self._conn.execute("SELECT MIN(ts) FROM events").fetchone()
        return (row[0] / 1000.0) if row and row[0] else None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
