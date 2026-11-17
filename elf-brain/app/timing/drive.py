"""三层驱动唤醒调度（docs/01 §M3；docs/05 §3.2）。

1. 状态驱动：状态机进 TRANSITION/IDLE → 立即重评想说清单（main.py on_state_change 钩子）
2. 周期巡逻：5min 低频兜底，状态+清单指纹去重（OpenClaw 心跳：活跃约束/状态去重/轮巡降本）
3. 价值自触发：管线产出新高分内容 → 即时评估上浮（高分 10s 内到达，不等状态变化或巡逻）

去重静默（Meta ISM）：已决策说过的 content_id 短期静默；重启后从事件库重建。
但静默必须有租期——永久拉黑会让内容池越用越干（新用户几天后就无话可说），
故 SPOKEN_TTL 过期后可重新进候选（内容本身还有 age 衰减兜底）。
"""

import time

from app.memory.event_store import EventStore
from app.perception.state_machine import UserState
from app.timing.engine import TimingEngine

PATROL_INTERVAL = 300  # 周期巡逻间隔（秒，低频兜底）
SPOKEN_TTL = 48 * 3600  # 同一内容静默 48h 后可再说（24-72h 区间的中值）


class ThreeLayerDrive:
    def __init__(self, store: EventStore, timing: TimingEngine) -> None:
        self.store = store
        self.timing = timing
        self._spoken: dict[str, float] = {}  # content_id → 最近决策时间（秒）
        self._last_fingerprint = ""

    def rebuild(self) -> None:
        """重启重建去重集（main.py lifespan 在 store.init() 后调用）。"""
        for ev in self.store.query("decision.made", limit=1000):
            cid = ev["payload"].get("content_id")
            if cid:
                self._spoken[cid] = max(self._spoken.get(cid, 0.0), ev["ts"] / 1000)

    def fresh(self, candidates: list[dict]) -> list[dict]:
        """去重静默：滤掉租期内已决策说过的内容（过期自动解禁）。"""
        cutoff = time.time() - SPOKEN_TTL
        return [c for c in candidates if self._spoken.get(c.get("content_id", ""), 0) < cutoff]

    def mark_spoken(self, content_id: str) -> None:
        self._spoken[content_id] = time.time()

    @staticmethod
    def _fingerprint(state: UserState, candidates: list[dict]) -> str:
        ids = ",".join(sorted(c.get("content_id", "") for c in candidates))
        return f"{state.value}|{ids}"

    # ---- 层2：周期巡逻 ----
    async def patrol(self, try_speak, state: UserState, candidates: list[dict]) -> bool:
        """低频兜底：状态与清单都无变化则跳过（轮巡降本）；不合格状态不消费指纹。"""
        if state in (UserState.BUSY, UserState.AWAY, UserState.DND):
            return False
        fp = self._fingerprint(state, candidates)
        if fp == self._last_fingerprint:
            return False
        self._last_fingerprint = fp
        return await try_speak("patrol")

    # ---- 层3：价值自触发 ----
    async def on_wishlist(self, try_speak, state: UserState, candidates: list[dict]) -> bool:
        """管线新产出：最佳新候选在当前状态下够 L2 门槛 → 即时评估上浮。"""
        fresh = self.fresh(candidates)
        if not fresh:
            return False
        best_value = max(c["content_value"] for c in fresh)
        bar = self.timing.coeff["channel_threshold"]["L2"]
        if self.timing.speak_score(best_value, state) < bar:
            return False
        return await try_speak("value")
