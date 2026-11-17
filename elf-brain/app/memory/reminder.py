"""提醒/日程（批4）：最小 reminder 表 + 到点开口。

「提醒我 X」解析在 main.py（_parse_reminder），这里只管存取：
SQLite 单表，append + due 查询 + mark_done，不引入调度器（os_loop 每轮顺手查）。
"""

import sqlite3
import threading
import time
from pathlib import Path


class ReminderService:
    def __init__(self, data_dir: str) -> None:
        self._db = sqlite3.connect(
            str(Path(data_dir) / "reminders.db"), check_same_thread=False
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS reminders(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   text TEXT NOT NULL,
                   due_ts REAL NOT NULL,
                   done_ts REAL,
                   created_ts REAL NOT NULL)"""
        )
        self._db.commit()
        self._lock = threading.Lock()

    def add(self, text: str, due_ts: float) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminders(text, due_ts, created_ts) VALUES(?,?,?)",
                (text[:120], due_ts, time.time()),
            )
            self._db.commit()
            return int(cur.lastrowid)

    def due(self, now: float | None = None) -> list[dict]:
        """到点且未完成的提醒（上限 5 条/轮，防刷屏）。"""
        now = now or time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text FROM reminders WHERE done_ts IS NULL AND due_ts <= ?"
                " ORDER BY due_ts LIMIT 5",
                (now,),
            ).fetchall()
        return [{"id": r[0], "text": r[1]} for r in rows]

    def done(self, reminder_id: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE reminders SET done_ts=? WHERE id=?", (time.time(), reminder_id)
            )
            self._db.commit()

    def close(self) -> None:
        self._db.close()
