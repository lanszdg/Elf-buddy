"""兴趣图谱（数据模型 docs/02 §2；实现规格 docs/05 §7.2）。

存储引擎说明（2026-10-10 验证）：Graphiti 官方嵌入式后端 falkordblite 不支持 win32
（PyPI 全版本无 Windows wheel，安装实测报 "not supported on the 'win32' platform"），
Neo4j 为 JVM 服务器无法塞进 PyInstaller 便携包——故以 SQLite 实现 Graphiti 语义：
时态边（valid_from/valid_to 失效归档而非覆盖）、episode 溯源、温度衰减、置信度。
引擎替换，字段与 docs/02 §2 一一对应，功能无删减。

温度（temperature）：近 14 天交互的指数衰减函数（半衰期 7 天）：
  raw = Σ pos: +w·0.5^(Δt/7)  +  Σ neutral: +0.15·0.5^(Δt/7)  −  Σ neg: 1.5w·0.5^(Δt/7)
  temperature = clamp(raw, floor, 1.0)；floor：explicit=0.3（明说的兴趣不冻死），inferred=0
负信号权重 ×1.5：一次 👎 的降温要盖过一次 👍 的升温（保护性，防纠缠推荐）。
"""

import json
import math
import sqlite3
import threading
import time
from pathlib import Path

HALF_LIFE_DAYS = 7.0
EXPLICIT_FLOOR = 0.3
PAUSED_BELOW = 0.2   # 温度跌破 → 自动挂 paused 时态边（冷却）
RESUME_ABOVE = 0.4   # 回升 → 关闭 paused 边（valid_to 归档）


class InterestGraph:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        # RLock：ingest/sync 持锁后内部还会调 recompute/temperature_of（同线程嵌套取锁）
        self._lock = threading.RLock()
        self._conn.executescript(
            """
            -- docs/02 §2.1 InterestNode
            CREATE TABLE IF NOT EXISTS interest_nodes (
                name TEXT NOT NULL,
                level TEXT NOT NULL,            -- domain/topic/entity
                temperature REAL NOT NULL DEFAULT 1.0,
                first_seen_ts REAL NOT NULL,
                last_active_ts REAL NOT NULL,
                source TEXT NOT NULL,           -- explicit/inferred
                confidence REAL NOT NULL DEFAULT 1.0,
                valid_to REAL,                  -- 时态归档（NULL=活跃）
                PRIMARY KEY (name, level)
            );
            -- docs/02 §2.2 InterestEdge（时态）
            CREATE TABLE IF NOT EXISTS interest_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                relation TEXT NOT NULL,         -- contains/related_to/chasing/paused/dropped
                valid_from REAL NOT NULL,
                valid_to REAL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_event_ids TEXT NOT NULL DEFAULT '[]',
                note TEXT
            );
            -- 带符号 episode（docs/03 §3.4 ingest_episode；温度计算的原料）
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                node_name TEXT NOT NULL,
                valence TEXT NOT NULL,          -- pos/neg/neutral
                weight REAL NOT NULL,
                event_ref TEXT                  -- 溯源 decision_id / event_id
            );
            CREATE INDEX IF NOT EXISTS idx_episodes_node ON episodes(node_name, ts);
            -- docs/02 §2.3 MemoryFact（7.3 user.correction 的落点）
            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from REAL NOT NULL,
                valid_to REAL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source_event_ids TEXT NOT NULL DEFAULT '[]',
                created_by TEXT NOT NULL        -- extractor/user_correction
            );
            """
        )
        self._conn.commit()

    # ---- 节点 ----
    def sync_explicit_interests(self, names: list[str]) -> None:
        """onboarding/改兴趣后同步：明说的兴趣=explicit domain 节点，置信度恒 1。

        首次出现播种 pos episode（w=1.0，温度从 1.0 开始自然衰减）；
        已存在的节点不重复播种（温度由真实交互演进）。
        """
        now = time.time()
        with self._lock:
            for name in names:
                cur = self._conn.execute(
                    "SELECT source FROM interest_nodes WHERE name=? AND level='domain' AND valid_to IS NULL",
                    (name,),
                ).fetchone()
                if cur is None:
                    self._conn.execute(
                        "INSERT INTO interest_nodes (name, level, temperature, first_seen_ts, last_active_ts, source, confidence)"
                        " VALUES (?, 'domain', 1.0, ?, ?, 'explicit', 1.0)",
                        (name, now, now),
                    )
                    self._conn.execute(
                        "INSERT INTO episodes (ts, node_name, valence, weight, event_ref) VALUES (?,?,?,?,?)",
                        (now, name, "pos", 1.0, "onboarding"),
                    )
                elif cur[0] == "inferred":
                    # 推断节点被用户明说 → 升级 explicit（置信度 1；只做加法铁律不删历史）
                    self._conn.execute(
                        "UPDATE interest_nodes SET source='explicit', confidence=1.0"
                        " WHERE name=? AND level='domain' AND valid_to IS NULL",
                        (name,),
                    )
            self._conn.commit()
        self.recompute_temperatures()

    def _ensure_node(self, name: str) -> None:
        """episode 落到未知节点 → 自动建 inferred 节点（新兴趣雏形；复核提炼在 7.4）。"""
        now = time.time()
        self._conn.execute(
            "INSERT OR IGNORE INTO interest_nodes (name, level, temperature, first_seen_ts, last_active_ts, source, confidence)"
            " VALUES (?, 'domain', 0.5, ?, ?, 'inferred', 0.5)",
            (name, now, now),
        )

    # ---- episode 写入（反馈闭环调用，docs/03 §3.4） ----
    def ingest_episode(self, node_name: str, valence: str, weight: float, event_ref: str = "") -> None:
        """带符号 episode 进图谱；写完即时重算温度（验收：温度随 👍👎 可见变化）。"""
        now = time.time()
        with self._lock:
            self._ensure_node(node_name)
            self._conn.execute(
                "INSERT INTO episodes (ts, node_name, valence, weight, event_ref) VALUES (?,?,?,?,?)",
                (now, node_name, valence, weight, event_ref),
            )
            self._conn.execute(
                "UPDATE interest_nodes SET last_active_ts=? WHERE name=? AND valid_to IS NULL",
                (now, node_name),
            )
            self._conn.commit()
        self.recompute_temperatures()

    # ---- 温度 ----
    def temperature_of(self, name: str) -> float:
        """实时温度（近 14 天 episode 指数衰减）；节点不存在按 1.0（不惩罚未知）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT source FROM interest_nodes WHERE name=? AND valid_to IS NULL LIMIT 1", (name,)
            ).fetchone()
            if row is None:
                return 1.0
            cutoff = time.time() - 14 * 86400
            rows = self._conn.execute(
                "SELECT ts, valence, weight FROM episodes WHERE node_name=? AND ts>=?",
                (name, cutoff),
            ).fetchall()
        raw = 0.0
        now = time.time()
        for ts, valence, weight in rows:
            decay = math.pow(0.5, (now - ts) / 86400 / HALF_LIFE_DAYS)
            if valence == "pos":
                raw += weight * decay
            elif valence == "neutral":
                raw += 0.15 * decay
            else:  # neg：保护性 ×1.5
                raw -= 1.5 * weight * decay
        floor = EXPLICIT_FLOOR if row[0] == "explicit" else 0.0
        return max(floor, min(1.0, raw))

    def recompute_temperatures(self) -> None:
        """温度落盘快照 + paused 时态边自动开闭（Review 每日重算也走这里，7.4）。"""
        with self._lock:
            names = [
                r[0]
                for r in self._conn.execute(
                    "SELECT DISTINCT name FROM interest_nodes WHERE valid_to IS NULL"
                ).fetchall()
            ]
        now = time.time()
        with self._lock:
            for name in names:
                t = self.temperature_of(name)
                self._conn.execute(
                    "UPDATE interest_nodes SET temperature=? WHERE name=? AND valid_to IS NULL",
                    (t, name),
                )
                open_paused = self._conn.execute(
                    "SELECT id FROM interest_edges WHERE src=? AND relation='paused' AND valid_to IS NULL",
                    (name,),
                ).fetchone()
                if t < PAUSED_BELOW and open_paused is None:
                    self._conn.execute(
                        "INSERT INTO interest_edges (src, dst, relation, valid_from, confidence, source_event_ids, note)"
                        " VALUES (?, ?, 'paused', ?, 1.0, '[]', ?)",
                        (name, name, now, f"温度冷却至 {t:.2f}，自动挂起"),
                    )
                elif t >= RESUME_ABOVE and open_paused is not None:
                    self._conn.execute(
                        "UPDATE interest_edges SET valid_to=? WHERE id=?", (now, open_paused[0])
                    )
            self._conn.commit()

    # ---- 时态边（7.3 纠偏/7.4 复核的写入口） ----
    def open_edge(self, src: str, dst: str, relation: str, note: str = "", event_ref: str = "") -> None:
        """开新边前把同 (src,dst,relation) 的旧开边归档（失效归档而非覆盖）。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE interest_edges SET valid_to=? WHERE src=? AND dst=? AND relation=? AND valid_to IS NULL",
                (now, src, dst, relation),
            )
            self._conn.execute(
                "INSERT INTO interest_edges (src, dst, relation, valid_from, confidence, source_event_ids, note)"
                " VALUES (?,?,?,?,1.0,?,?)",
                (src, dst, relation, now, json.dumps([event_ref] if event_ref else []), note),
            )
            self._conn.commit()

    # ---- 原子事实（docs/02 §2.3；7.3 user.correction 落点 / extractor 提炼落点） ----
    def upsert_fact(
        self,
        subject: str,
        predicate: str,
        obj: str,
        created_by: str,
        event_ref: str = "",
        preserve_user_correction: bool = False,
    ) -> int:
        """同 (subject,predicate) 的旧事实 valid_to 归档——纠错只做加法，历史可查（数据红线）。

        preserve_user_correction=True（extractor 用）：归档时跳过用户明说的事实，
        自动提炼不得使 user_correction 事实失效。
        返回新事实 id（POST /correction 的 applied_fact_ids，docs/03 §1.4）。
        """
        now = time.time()
        with self._lock:
            sql = "UPDATE memory_facts SET valid_to=? WHERE subject=? AND predicate=? AND valid_to IS NULL"
            args: list = [now, subject, predicate]
            if preserve_user_correction:
                sql += " AND created_by != 'user_correction'"
            self._conn.execute(sql, args)
            cur = self._conn.execute(
                "INSERT INTO memory_facts (subject, predicate, object, valid_from, confidence, source_event_ids, created_by)"
                " VALUES (?,?,?,?,1.0,?,?)",
                (subject, predicate, obj, now, json.dumps([event_ref] if event_ref else []), created_by),
            )
            self._conn.commit()
            return cur.lastrowid

    # ---- 快照（GET /interests；成长面板数据源，§4） ----
    def snapshot(self, include_dormant: bool = False) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, level, source, confidence, first_seen_ts, last_active_ts"
                " FROM interest_nodes WHERE valid_to IS NULL ORDER BY first_seen_ts"
            ).fetchall()
            paused = {
                r[0]
                for r in self._conn.execute(
                    "SELECT src FROM interest_edges WHERE relation='paused' AND valid_to IS NULL"
                ).fetchall()
            }
            counts = dict(
                self._conn.execute(
                    "SELECT node_name, COUNT(*) FROM episodes GROUP BY node_name"
                ).fetchall()
            )
        out = []
        for name, level, source, confidence, first_seen, last_active in rows:
            temp = self.temperature_of(name)
            status = "paused" if name in paused else ("dormant" if temp < PAUSED_BELOW else "active")
            if not include_dormant and status != "active":
                continue
            out.append(
                {
                    "name": name,
                    "level": level,
                    "temperature": round(temp, 3),
                    "status": status,
                    "source": source,
                    "confidence": confidence,
                    "first_seen_ts": first_seen,
                    "last_active_ts": last_active,
                    "episode_count": counts.get(name, 0),
                }
            )
        return out

    # ---- Review 成长汇报 diff 访问器（docs/03 §3.5 weekly；只读） ----
    def count_facts(self) -> int:
        """当前生效记忆条数（成长面板"记忆条数"；归档不计）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE valid_to IS NULL"
            ).fetchone()[0]

    def active_facts(self, limit: int = 30) -> list[dict]:
        """当前生效的事实，新的在前（对话 prompt 注入用：让聊天感知长期记忆）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject, predicate, object, valid_from, created_by"
                " FROM memory_facts WHERE valid_to IS NULL"
                " ORDER BY valid_from DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"subject": s, "predicate": p, "object": o, "valid_from": v, "created_by": c}
            for s, p, o, v, c in rows
        ]

    def archive_matching_facts(self, text: str) -> list[str]:
        """翻篇许可（P0-3）：用户明说「不再/脱坑/早就不…」→ 命中的生效事实时态归档。

        只做加法铁律不破：归档不是删除（valid_to 可查），但被翻篇的事不再注入对话。
        返回被翻篇的「predicate：object」短语列表（没有命中返回空）。
        """
        text = (text or "").strip()
        if not text:
            return []
        now = time.time()
        archived: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, predicate, object FROM memory_facts WHERE valid_to IS NULL"
            ).fetchall()
            for fid, pred, obj in rows:
                if (obj and len(obj) >= 2 and obj in text) or (
                    pred and len(pred) >= 2 and pred in text
                ):
                    self._conn.execute(
                        "UPDATE memory_facts SET valid_to=? WHERE id=?", (now, fid)
                    )
                    archived.append(f"{pred}：{obj}")
            self._conn.commit()
        return archived

    def has_active_fact(self, subject: str, predicate: str, obj: str) -> bool:
        """活跃事实中是否已有同值（extractor 去重：重复陈述不重复开新行）。"""
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM memory_facts WHERE subject=? AND predicate=? AND object=? AND valid_to IS NULL",
                (subject, predicate, obj),
            ).fetchone() is not None

    def facts_since(self, ts: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT subject, predicate, object, valid_from, created_by"
                " FROM memory_facts WHERE valid_from >= ? ORDER BY valid_from",
                (ts,),
            ).fetchall()
        return [
            {"subject": s, "predicate": p, "object": o, "valid_from": v, "created_by": c}
            for s, p, o, v, c in rows
        ]

    def nodes_since(self, ts: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM interest_nodes WHERE first_seen_ts >= ?", (ts,)
            ).fetchall()
        return [r[0] for r in rows]

    def episode_stats_since(self, ts: float) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT valence, COUNT(*) FROM episodes WHERE ts >= ? GROUP BY valence", (ts,)
            ).fetchall()
        stats = {"pos": 0, "neg": 0, "neutral": 0}
        for v, c in rows:
            stats[v] = stats.get(v, 0) + c
        return stats

    def node_episode_stats_since(self, name: str, ts: float) -> dict:
        """单兴趣节点的带符号 episode 统计（兴趣进展播报数据源）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT valence, COUNT(*) FROM episodes WHERE node_name=? AND ts >= ? GROUP BY valence",
                (name, ts),
            ).fetchall()
        stats = {"pos": 0, "neg": 0, "neutral": 0}
        for v, c in rows:
            stats[v] = stats.get(v, 0) + c
        return stats

    # ---- chasing 边（兴趣进展播报：weekly 对齐生产，digest 消费） ----
    def chasing_names(self) -> list[str]:
        """当前在追的兴趣（开着的 chasing 自环边 src=dst）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT src FROM interest_edges WHERE relation='chasing' AND valid_to IS NULL AND src=dst"
            ).fetchall()
        return [r[0] for r in rows]

    def revisit_candidate(self) -> dict | None:
        """7-14 天前的开心 episode 里挑一条回访候选（事件驱动回访的数据源）。

        只回访还在追或温度仍在线的节点——凉了的话题不硬提。
        """
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_name, ts, weight, event_ref FROM episodes"
                " WHERE valence='pos' AND ts>=? AND ts<=?"
                " ORDER BY weight DESC LIMIT 8",
                (now - 14 * 86400, now - 7 * 86400),
            ).fetchall()
        chasing = set(self.chasing_names())
        for node_name, ts, weight, event_ref in rows:
            if node_name in chasing or self.temperature_of(node_name) >= 0.4:
                return {"node": node_name, "ts": ts, "weight": weight, "event_ref": event_ref}
        return None

    def echo_candidates(self, min_days: int = 3, max_days: int = 30, limit: int = 20) -> list[dict]:
        """3-30 天前的正向 episode 按节点聚合（P0-1 温故追问池）：每节点取最近一条。

        比单条 revisit_candidate 宽——间隔重复需要一批候选来调度，而不是每天碰运气挑一条。
        """
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT node_name, MAX(ts) AS ts, COUNT(*) AS n FROM episodes"
                " WHERE valence='pos' AND ts>=? AND ts<=?"
                " GROUP BY node_name ORDER BY ts DESC LIMIT ?",
                (now - max_days * 86400, now - min_days * 86400, limit),
            ).fetchall()
        return [{"node": r[0], "ts": r[1], "count": r[2]} for r in rows]

    def sync_chasing(self, names: list[str], min_temp: float = 0.4) -> None:
        """weekly 对齐：温度达标的兴趣开 chasing 自环边，降温的关掉（时态边只做开闭）。"""
        now = time.time()
        with self._lock:
            for n in names:
                if self.temperature_of(n) >= min_temp:
                    exists = self._conn.execute(
                        "SELECT 1 FROM interest_edges WHERE src=? AND dst=? AND relation='chasing' AND valid_to IS NULL",
                        (n, n),
                    ).fetchone()
                    if not exists:
                        self._conn.execute(
                            "INSERT INTO interest_edges (src, dst, relation, valid_from, confidence, source_event_ids, note)"
                            " VALUES (?,?,?,?,1.0,'[]','高温兴趣，进展追踪中')",
                            (n, n, "chasing", now),
                        )
                else:
                    self._conn.execute(
                        "UPDATE interest_edges SET valid_to=? WHERE src=? AND dst=? AND relation='chasing' AND valid_to IS NULL",
                        (now, n, n),
                    )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
