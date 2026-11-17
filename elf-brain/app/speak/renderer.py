"""讲述渲染与反馈处理（docs/03 §3.4）。

P0：SpeakCommand 经 WS 直推（人格化文案 P1 接入 persona 包）；
反馈按 actual_value 映射表（docs/02 §5.3）回填。
"""

import time
import uuid

from app.memory.event_store import EventStore

# docs/02 §5.3 actual_value 映射
ACTUAL_VALUE = {
    "speak.dismissed": 0.0,
    "speak.opened": 0.25,
    "speak.consumed": 0.5,
    "speak.dwell": 0.75,
    "speak.followup": 0.9,
    "speak.shared_to_elf": 1.0,
}


class SpeakRenderer:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        self.ws = None  # main.py 注入 WsManager
        self.account = None  # main.py 注入 RelationshipAccount（信任记账）
        self.graph = None  # main.py 注入 InterestGraph（反馈→带符号 episode）
        self.pipeline = None  # main.py 注入（decision→content→兴趣节点 溯源）
        self.skills = None  # main.py 注入 SkillStore（技能成效归因，docs/02 §6.1）
        self._pending: dict[str, float] = {}  # decision_id → 最强反馈值
        self._skill_use: dict[str, list[str]] = {}  # decision_id → 本次讲述用到的技能名

    async def push(self, cmd: dict) -> None:
        if self.ws:
            await self.ws.push(cmd)
        self._pending[cmd["id"]] = 0.0
        # narration 生成先于 push（main.py try_speak），payload 里已带 skills_used
        self._skill_use[cmd["id"]] = list(
            (cmd["payload"].get("narration") or {}).get("skills_used") or []
        )

    def _find_decision(self, decision_id: str) -> dict | None:
        """取决策时落库的 expected_value/mapping_version（时序语义：决策时写入，永不回写）。"""
        for ev in self.store.query("decision.made", limit=500):
            if ev["payload"].get("decision_id") == decision_id:
                return ev["payload"]
        return None

    async def on_feedback(self, ev_type: str, payload: dict) -> None:
        decision_id = payload.get("decision_id")
        if decision_id not in self._pending:
            return
        # 显式反馈（👍👎，docs/05 §2.3）：👍=停留久同级 0.75，👎=秒关同级 0
        if ev_type == "speak.feedback_explicit":
            value = 0.75 if payload.get("positive") else 0.0
        else:
            value = ACTUAL_VALUE.get(ev_type, 0.0)
        self._pending[decision_id] = max(self._pending[decision_id], value)
        # P0：单条反馈即结算落库（完整版：反馈窗口关闭后统一结算）
        # 👎 要拉低结算值：显式负反馈直接结算为 0，不受此前较强信号影响
        if ev_type == "speak.feedback_explicit" and not payload.get("positive"):
            self._pending[decision_id] = 0.0
        actual = self._pending[decision_id]
        decision = self._find_decision(decision_id) or {}
        expected = decision.get("expected_value")
        # docs/02 §5.3 feedback 全字段：outcome/expected/actual/prediction_error/mapping_version
        # RPE 原则：学习只发生在"意外"时——prediction_error 是 Review 管线的消费排序键
        self.store.append(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            ts=int(time.time() * 1000),
            type="speak.feedback_resolved",
            source="brain",
            payload={
                "decision_id": decision_id,
                "outcome": "hit" if actual >= 0.5 else "miss",
                "expected_value": expected,
                "actual_value": actual,
                "prediction_error": (
                    abs(expected - actual) if expected is not None else None
                ),
                "mapping_version": decision.get("mapping_version"),
                "resolved_by": ev_type,
            },
        )
        # 信任记账：命中率窗口 / 连续无视安静期 / XP 升级（docs/05 §7.1）
        if self.account is not None:
            self.account.record_feedback(decision_id, actual, ev_type)
        # 技能成效归因（docs/02 §6.1）：本次讲述注入的技能随本条反馈结算成败
        if self.skills is not None:
            for name in self._skill_use.pop(decision_id, []):
                self.skills.touch(name, success=actual >= 0.5)
        # 带符号 episode 进兴趣图谱（docs/03 §3.4；docs/05 §7.2）
        # 温度随 👍👎 可见变化：valence/weight 按本条事件信号强度映射
        if self.graph is not None and self.pipeline is not None:
            item = self.pipeline.find(decision.get("content_id", "")) if decision else None
            node = (item or {}).get("matched_node")
            if node:
                if value >= 0.5:
                    valence, weight = "pos", value
                elif value >= 0.25:
                    valence, weight = "neutral", 0.15
                else:
                    valence, weight = "neg", 0.8
                self.graph.ingest_episode(node, valence, weight, event_ref=decision_id)
