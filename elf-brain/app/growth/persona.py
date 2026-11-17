"""人格/立场底座（docs/02 §6.2 PersonaFile；docs/01 §M1）。

- soul.md：性格/口癖/精灵自身兴趣倾向（与用户重合→共鸣，不重合→碰撞），
  用户可手工编辑，首次运行落默认副本（与 coefficients.yaml 同一约定）
- StanceEntry 立场库：精灵发表过的看法入库（SQLite persona.db），
  讲述生成前按内容实体检索历史立场注入 prompt——前后一致
  （上周吐槽过的导演这周不会夸；转变态度须明确解释）
"""

import re
import sqlite3
import time
from pathlib import Path

_DEFAULT_SOUL = """# 小精灵的魂

## 性格
好奇心旺盛，有点话痨但懂得看脸色；对喜欢的东西会眼睛发亮，对无聊的标题党会撇嘴。
说话直率、有立场，不当复读机——转述内容时一定带上"我觉得"。

## 口癖
- 开场爱用"诶"、"那个那个"、"跟你讲"
- 兴奋时句子会变短，会蹦出"这个真的绝了"
- 不确定的事会说"我瞅着像是……你再瞅瞅？"

## 自己的兴趣倾向（精灵不是中立的）
- 偏爱：动手做的东西（独立游戏、开源项目、手冲咖啡）胜过纯概念炒作
- 偏爱：有工程师审美的事物——简洁、可复现、诚实标注局限
- 反感：标题党、AI 噱头营销、"震惊体"
- 与用户兴趣重合时共鸣（"我们都爱这个"）；不重合时好奇碰撞（"你居然喜欢这个？讲讲呗"）
"""


class PersonaService:
    def __init__(self, data_dir: str, soul_path: str) -> None:
        sp = Path(soul_path)
        if not sp.exists():
            sp.write_text(_DEFAULT_SOUL, encoding="utf-8")  # 用户可编辑副本，同 coefficients 约定
        self._soul_path = sp
        self._soul = ""
        self._db = sqlite3.connect(str(Path(data_dir) / "persona.db"), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS stances(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   topic_entity TEXT NOT NULL,
                   stance TEXT NOT NULL,
                   said_ts INTEGER NOT NULL,
                   source_decision_id TEXT)"""
        )
        # 成长笔记（杀手C）：weekly 从反馈结算提炼的自我演化记录，注入 soul 影响后续所有生成
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS growth_notes(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   note TEXT NOT NULL,
                   week TEXT NOT NULL,
                   created_ts INTEGER NOT NULL)"""
        )
        # 决策台账（P0-2）：用户说「打算/计划/决定…」落库，7/30 天后回访结算
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS decisions(
                   id TEXT PRIMARY KEY,
                   what TEXT NOT NULL,
                   planned_ts REAL NOT NULL,      -- 用户说这话的时刻
                   status TEXT NOT NULL DEFAULT 'open',  -- open/followed_up/resolved
                   echoed_7d INTEGER NOT NULL DEFAULT 0,
                   echoed_30d INTEGER NOT NULL DEFAULT 0,
                   resolved_ts REAL,
                   created_ts REAL NOT NULL)"""
        )
        self._db.commit()

    # ---- 人设 ----
    @property
    def soul(self) -> str:
        """每次读取回盘——用户手工改 soul.md 无需重启（与 reload_coeff 同一哲学）。

        尾部追加近期成长笔记（杀手C）：精灵自我演化对后续所有生成生效。
        """
        try:
            self._soul = self._soul_path.read_text(encoding="utf-8")
        except OSError:
            pass
        notes = self.recent_growth_notes(3)
        if notes:
            return self._soul + "\n\n## 最近的成长（你自己总结出来的，自然体现在言行里）\n" + "\n".join(f"- {n}" for n in notes)
        return self._soul

    # ---- 成长笔记（杀手C：决策结算 → 自我演化） ----
    def add_growth_note(self, note: str, week: str) -> None:
        self._db.execute(
            "INSERT INTO growth_notes(note, week, created_ts) VALUES(?,?,?)",
            (note[:200], week, int(time.time())),
        )
        self._db.commit()

    def recent_growth_notes(self, limit: int = 3) -> list[str]:
        rows = self._db.execute(
            "SELECT note FROM growth_notes ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r[0] for r in rows]

    def has_growth_note(self, week: str) -> bool:
        return bool(self._db.execute(
            "SELECT 1 FROM growth_notes WHERE week = ? LIMIT 1", (week,)
        ).fetchone())

    # ---- 决策台账（P0-2：计划落库 → 7/30 天回访结算） ----
    def add_decision(self, decision_id: str, what: str) -> bool:
        """登记一条用户计划；同文本周内已登记过 → 跳过（不重复）。返回是否新登记。"""
        what = (what or "").strip()
        if not what:
            return False
        week_ago = time.time() - 7 * 86400
        row = self._db.execute(
            "SELECT 1 FROM decisions WHERE what=? AND created_ts>? LIMIT 1", (what, week_ago)
        ).fetchone()
        if row:
            return False
        self._db.execute(
            "INSERT INTO decisions(id, what, planned_ts, status, created_ts) VALUES(?,?,?,'open',?)",
            (decision_id, what[:200], time.time(), time.time()),
        )
        self._db.commit()
        return True

    def due_decisions(self, min_age_days: int, max_age_days: int, echoed_field: str) -> list[dict]:
        """取回访窗口内、该档还没问过的 open 决策。echoed_field ∈ {echoed_7d, echoed_30d}。"""
        now = time.time()
        rows = self._db.execute(
            f"SELECT id, what, planned_ts FROM decisions"
            f" WHERE status='open' AND {echoed_field}=0"
            f" AND planned_ts>=? AND planned_ts<=? ORDER BY planned_ts LIMIT 3",
            (now - max_age_days * 86400, now - min_age_days * 86400),
        ).fetchall()
        return [{"id": r[0], "what": r[1], "planned_ts": r[2]} for r in rows]

    def mark_echoed(self, decision_id: str, echoed_field: str) -> None:
        """该档问过了：置位 + 30 天档问完即 resolved（台账有终态，不无限回访）。"""
        self._db.execute(
            f"UPDATE decisions SET {echoed_field}=1 WHERE id=?", (decision_id,)
        )
        if echoed_field == "echoed_30d":
            self._db.execute(
                "UPDATE decisions SET status='resolved', resolved_ts=? WHERE id=?",
                (time.time(), decision_id),
            )
        self._db.commit()

    def open_decisions(self, limit: int = 5) -> list[dict]:
        """最近还没回访结算的用户计划（晨报/温故数据可用）。"""
        rows = self._db.execute(
            "SELECT id, what, planned_ts FROM decisions WHERE status='open'"
            " ORDER BY created_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"id": r[0], "what": r[1], "planned_ts": r[2]} for r in rows]

    # ---- 立场库 ----
    def add_stances(self, entries: list[dict], source_decision_id: str | None = None) -> int:
        """讲述生成产出立场入库（只做加法——立场史是记忆，不删不改）。"""
        n = 0
        for e in entries:
            topic, stance = str(e.get("topic_entity", "")).strip(), str(e.get("stance", "")).strip()
            if not topic or not stance:
                continue
            self._db.execute(
                "INSERT INTO stances(topic_entity, stance, said_ts, source_decision_id) VALUES(?,?,?,?)",
                (topic[:60], stance[:200], int(time.time()), source_decision_id),
            )
            n += 1
        self._db.commit()
        return n

    def stances_mentioning(self, text: str, limit: int = 3) -> list[dict]:
        """用户话语里直接提到了哪些立场的主题（杀手D：无 NER 也能命中「Claude Code 订阅」）。

        topic_entity 是 LLM 生成的自由短语（可能含 " / 、，" 分隔），按分隔符切段后
        任一 ≥2 字段是用户消息子串即命中；立场库量级小（几十条），全表扫描无压力。
        """
        text = (text or "").strip()
        if not text:
            return []
        out: list[dict] = []
        for rid, topic, stance, ts in self._db.execute(
            "SELECT id, topic_entity, stance, said_ts FROM stances ORDER BY id DESC"
        ).fetchall():
            parts = [p.strip() for p in re.split(r"[ /／、，,;；|]+", topic) if len(p.strip()) >= 2]
            if any(p in text for p in parts):
                out.append({"id": rid, "topic_entity": topic, "stance": stance, "said_ts": ts})
            if len(out) >= limit:
                break
        return out

    def stances_for(self, entities: list[str], limit: int = 5) -> list[dict]:
        """按内容实体检索历史立场（生成前一致性校验用）；最新优先。

        模糊双向匹配：库中实体是内容实体的子串或反之（"诺兰"命中"导演诺兰"）。
        """
        if not entities:
            return []
        out: list[dict] = []
        seen: set[int] = set()
        for ent in entities:
            ent = str(ent).strip()
            if not ent:
                continue
            rows = self._db.execute(
                "SELECT id, topic_entity, stance, said_ts FROM stances "
                "WHERE ? LIKE '%'||topic_entity||'%' OR topic_entity LIKE '%'||?||'%' "
                "ORDER BY id DESC LIMIT ?",  # id 自增=入库序（同秒 said_ts 无法区分先后）
                (ent, ent, limit),
            ).fetchall()
            for rid, topic, stance, ts in rows:
                if rid in seen:
                    continue
                seen.add(rid)
                out.append({"id": rid, "topic_entity": topic, "stance": stance, "said_ts": ts})
        return sorted(out, key=lambda x: x["id"], reverse=True)[:limit]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM stances").fetchone()[0])

    def close(self) -> None:
        self._db.close()
