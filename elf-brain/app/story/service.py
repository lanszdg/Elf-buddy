"""互动剧（story-1）：分支微故事生成与推进。

用户聊天触发「讲个故事」→ 开局一幕 + 2~3 个分支选择；用户点选 → 下一幕；
共 TOTAL_BEATS 幕，末幕必收尾。完结时把主题写回记忆画像（兴趣图谱 episode +
原子事实），之后的内容推荐/闲聊能感知「用户爱玩这类故事」。
LLM 走 cheap 模型 + JSON mode（全库统一范式）；无 key 抛 LLMUnavailable 由调用方降级。
"""

import json
import sqlite3
import threading
import time
import uuid

from app.core import llm
from app.core.config import settings

TOTAL_BEATS = 4  # 第 1 幕开局，第 4 幕收尾；3 次选择节奏刚好（太长用户会累）
_EXPIRE_SEC = 1800  # 30 分钟不推进自动作废（中途去工作的故事不隔夜续讲）


class StoryService:
    """故事状态存 story.db（重启可续）；依赖 graph/store/persona 由 main 装配注入。"""

    def __init__(self, db_path: str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS stories ("
            " story_id TEXT PRIMARY KEY,"
            " created_ts REAL NOT NULL,"
            " updated_ts REAL NOT NULL,"
            " theme TEXT NOT NULL,"
            " beats_json TEXT NOT NULL,"  # [{scene, choices, picked}] 已播出的幕
            " beat_count INTEGER NOT NULL,"
            " status TEXT NOT NULL)"  # active | done
        )
        self._conn.commit()
        self.graph = None  # InterestGraph（完结写回）
        self.store = None  # EventStore（story.finished 事件）
        self.persona = None  # PersonaService（soul 注入 prompt）

    def close(self) -> None:
        self._conn.close()

    # ---- LLM 生成 ----

    def _system(self) -> str:
        soul = getattr(self.persona, "soul", "") or ""
        return (
            soul
            + "\n你是互动剧主持人：写一个供用户做选择的微冒险。规则："
            "第二人称「你」带入；每幕≤150字、有画面感、停在抉择点；"
            "选择项≤12字、方向互不相同（不能只是「是/否」的换说法）；"
            "故事要符合你的精灵人格，活泼有点小脾气。只输出严格 JSON。"
        )

    async def _gen_opening(self, theme_hint: str) -> dict:
        hint = f"主题倾向：{theme_hint}（融入背景即可，别点名用户兴趣）。" if theme_hint else "主题你自由发挥。"
        r = await llm.chat(
            f"{hint}\n输出开局幕：{{\"title\":\"剧名≤10字\",\"scene\":\"第1幕正文\",\"choices\":[\"选项\",\"选项\",\"选项\"]}}",
            system=self._system(),
            model=settings.llm_model_cheap,
            timeout=60,
        )
        return self._validate_beat(r, is_opening=True)

    async def _gen_next(self, story: dict, picked: str) -> dict:
        so_far = "\n".join(
            f"第{i + 1}幕：{b['scene']}（用户选择：{b.get('picked', '—')}）"
            for i, b in enumerate(story["beats"])
        )
        final = story["beat_count"] + 1 >= TOTAL_BEATS
        tail = (
            '{"scene":"收尾幕正文（给故事一个落地结局，别留悬念）","choices":[],"is_end":true}'
            if final
            else '{"scene":"下一幕正文","choices":["选项","选项","选项"],"is_end":false}'
        )
        r = await llm.chat(
            f"剧名《{story['theme']}》。已发生：\n{so_far}\n用户刚才选：{picked}\n\n接着写{tail}",
            system=self._system(),
            model=settings.llm_model_cheap,
            timeout=60,
        )
        beat = self._validate_beat(r, is_opening=False)
        if final:  # 末幕强制收尾，不信 LLM 的 is_end
            beat["is_end"] = True
            beat["choices"] = []
        return beat

    @staticmethod
    def _validate_beat(beat: dict, is_opening: bool) -> dict:
        scene = str(beat.get("scene", "")).strip()
        if not scene:
            raise llm.LLMUnavailable("故事幕为空")
        choices = [str(c).strip() for c in (beat.get("choices") or []) if str(c).strip()][:3]
        if is_opening and len(choices) < 2:
            raise llm.LLMUnavailable("开局选择不足")
        return {
            "title": str(beat.get("title", "")).strip()[:20] if is_opening else "",
            "scene": scene[:400],
            "choices": choices,
            "is_end": bool(beat.get("is_end", False)),
        }

    # ---- 对外：开局 / 推进 ----

    async def start(self, theme_hint: str = "") -> dict:
        beat = await self._gen_opening(theme_hint)
        story_id = f"st_{uuid.uuid4().hex[:12]}"
        theme = beat["title"] or "无名小剧场"
        now = time.time()
        with self._lock:
            # 旧 active 一律作废：同时只讲一个故事
            self._conn.execute("UPDATE stories SET status='done', updated_ts=? WHERE status='active'", (now,))
            self._conn.execute(
                "INSERT INTO stories (story_id, created_ts, updated_ts, theme, beats_json, beat_count, status)"
                " VALUES (?,?,?,?,?,?, 'active')",
                (story_id, now, now, theme, json.dumps([beat], ensure_ascii=False), 1),
            )
            self._conn.commit()
        return self._payload(story_id, theme, 1, beat)

    async def choose(self, story_id: str, choice: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT theme, beats_json, beat_count FROM stories"
                " WHERE story_id=? AND status='active'",
                (story_id,),
            ).fetchone()
        if row is None:
            raise KeyError(story_id)
        theme, beats_json, beat_count = row
        if time.time() - self._updated_ts(story_id) > _EXPIRE_SEC:
            self._finish(story_id)
            raise KeyError(story_id)  # 过期作废，壳侧提示重开
        story = {"theme": theme, "beats": json.loads(beats_json), "beat_count": beat_count}
        beat = await self._gen_next(story, choice)
        beats = story["beats"]
        beats[-1]["picked"] = choice[:60]  # 上一幕记录用户选择
        beats.append({k: beat[k] for k in ("scene", "choices", "is_end")})
        now = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE stories SET beats_json=?, beat_count=?, updated_ts=? WHERE story_id=?",
                (json.dumps(beats, ensure_ascii=False), beat_count + 1, now, story_id),
            )
            self._conn.commit()
        if beat["is_end"]:
            self._finish(story_id)
            self._writeback(story_id, theme, beats)
        return self._payload(story_id, theme, beat_count + 1, beat)

    # ---- 内部 ----

    def _updated_ts(self, story_id: str) -> float:
        with self._lock:
            row = self._conn.execute("SELECT updated_ts FROM stories WHERE story_id=?", (story_id,)).fetchone()
        return row[0] if row else 0.0

    def _finish(self, story_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE stories SET status='done', updated_ts=? WHERE story_id=?",
                (time.time(), story_id),
            )
            self._conn.commit()

    def _writeback(self, story_id: str, theme: str, beats: list) -> None:
        """选择反哺记忆画像：主题 warming 兴趣图谱 + 原子事实（完结才写，中途关掉不算数）。"""
        try:
            if self.graph is not None:
                self.graph.ingest_episode(f"互动剧·{theme}", "pos", 0.5, story_id)
                self.graph.upsert_fact("user", "enjoyed_story_theme", theme, "story", story_id)
        except Exception as e:
            print(f"[story] 记忆写回失败：{e}")
        try:
            if self.store is not None:
                self.store.append(
                    event_id=f"evt_{uuid.uuid4().hex[:12]}",
                    ts=int(time.time() * 1000),
                    type="story.finished",
                    source="brain",
                    payload={"story_id": story_id, "theme": theme, "beats": len(beats)},
                )
        except Exception as e:
            print(f"[story] 事件落盘失败：{e}")

    @staticmethod
    def _payload(story_id: str, theme: str, beat_count: int, beat: dict) -> dict:
        return {
            "story_id": story_id,
            "beat": beat_count,
            "total_beats": TOTAL_BEATS,
            "title": theme,
            "scene": beat["scene"],
            "choices": beat["choices"],
            "is_end": beat["is_end"],
        }
