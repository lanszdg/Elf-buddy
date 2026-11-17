"""时机引擎（docs/03 §3.2，评分与校准见 docs/04 §1）。

P0 硬编码规则版：零学习（关系账户固定、Review 不跑），
但评分公式、四通道、配额、校准映射的结构已按完整版落位。
"""

import math
import time
import uuid
from pathlib import Path

import yaml

from app.memory.event_store import EventStore
from app.perception.state_machine import UserState


class TimingEngine:
    def __init__(self, coeff_path: str, store: EventStore) -> None:
        self.coeff_path = coeff_path
        self.coeff = yaml.safe_load(Path(coeff_path).read_text(encoding="utf-8"))
        self.store = store
        self.account = None  # main.py 注入 RelationshipAccount（信任记账进评分）
        self._spoken_today: list[float] = []  # L3 开口时间戳（配额计数）

    def reload_coeff(self) -> None:
        """Review 管线改写 coefficients.yaml 后热重载（改文件不改代码，docs/03 §3.2）。"""
        self.coeff = yaml.safe_load(Path(self.coeff_path).read_text(encoding="utf-8"))

    # ---- 评分（docs/03 §3.2 公式） ----
    def _score_parts(self, content_value: float, user_state: UserState) -> dict:
        c = self.coeff
        state_factor = c["state_factor"].get(user_state.value, 0.0)
        timing_factor = self._timing_factor()
        disturbance_cost = self._disturbance_cost()
        # trust_factor 来自关系账户命中率（docs/02 §4）；账户未注入时退回系数文件
        trust_factor = self.account.trust_factor if self.account else c.get("trust_factor", 1.0)
        final = content_value * state_factor * timing_factor - disturbance_cost * trust_factor
        return {
            "content_value": round(content_value, 4),
            "state_factor": state_factor,
            "timing_factor": timing_factor,
            "disturbance_cost": round(disturbance_cost, 4),
            "trust_factor": round(trust_factor, 4),
            "final_score": round(final, 4),
            "thresholds": self.coeff["channel_threshold"],
        }

    def speak_score(self, content_value: float, user_state: UserState) -> float:
        return self._score_parts(content_value, user_state)["final_score"]

    def decide(self, user_state: UserState, candidates: list[dict], quiet_hours: bool) -> dict | None:
        """从想说清单选一条，定通道，构造 SpeakCommand（docs/03 §2.4）；或憋着。"""
        if quiet_hours or not candidates:
            return None
        if user_state in (UserState.BUSY, UserState.AWAY, UserState.DND):
            return None

        best = max(candidates, key=lambda c: self.speak_score(c["content_value"], user_state))
        snapshot = self._score_parts(best["content_value"], user_state)
        score = snapshot["final_score"]
        channel = self._pick_channel(score, user_state)
        if channel is None:
            return None  # L0 憋着

        expected = self._expected_value(score)  # 校准映射（docs/04 §1.1 锚点插值）
        decision_id = f"spk_{uuid.uuid4().hex[:12]}"
        self.store.append(  # 决策先于执行落库（接口红线）
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            ts=int(time.time() * 1000),
            type="decision.made",
            source="brain",
            payload={
                "decision_id": decision_id,
                "channel": channel,
                "content_id": best["content_id"],
                "score": score,
                "expected_value": expected,
                "mapping_version": self.coeff["normalization"]["version"],
                "user_state": user_state.value,
                # 白盒可复盘（docs/02 §5.3）：评分快照 + 硬规则校验记录
                "score_snapshot": snapshot,
                "suppression_check": {
                    "busy": user_state == UserState.BUSY,
                    "quiet_hours": quiet_hours,
                    "muted": False,  # 免打扰开关未实现（§6.2 会议/锁屏后接入）
                    "consecutive_ignores": self.account.consecutive_misses if self.account else 0,
                    "quota_left": self._quota_left(),
                },
            },
        )
        if channel == "L3":
            self._spoken_today.append(time.time())
        return {
            "type": "speak",
            "id": decision_id,
            "channel": channel,
            "payload": {
                "content_id": best["content_id"],
                "title": best["title"],
                "reason": best.get("matched_reason", ""),  # 白盒理由（红线5：必须来自精排落库）
                "summary": best.get("summary", ""),
                "url": best.get("url", ""),
                "cover": best.get("cover", ""),  # 封面图（§3.4：RSS enclosure/og:image）
            },
            "ttl_ms": 30000 if channel == "L3" else None,
        }

    # ---- 内部 ----
    def _pick_channel(self, score: float, user_state: UserState) -> str | None:
        t = self.coeff["channel_threshold"]
        # 开口权限=成长解锁项（docs/02 §4）：未解锁时 L3 降级 L2（动作吸引，不开口）
        l3_ok = (
            user_state == UserState.TRANSITION
            and self._quota_left() > 0
            and (self.account is None or self.account.l3_unlocked)
        )
        if score >= t["L3"] and l3_ok:
            return "L3"
        if score >= t["L2"]:
            return "L2"
        if score >= t["L1"]:
            return "L1"
        return None

    def _timing_factor(self) -> float:
        hour = time.localtime().tm_hour
        return max(self.coeff["timing_factor"].get(str(hour), 0.5), 0.1)

    def _disturbance_cost(self) -> float:
        """当日已开口次数的递增惩罚。"""
        day_ago = time.time() - 86400
        self._spoken_today = [t for t in self._spoken_today if t > day_ago]
        return self.coeff.get("disturbance_step", 0.15) * len(self._spoken_today)

    def _quota_left(self) -> int:
        day_ago = time.time() - 86400
        used = sum(1 for t in self._spoken_today if t > day_ago)
        # 配额随命中率 [1,6] 浮动、安静期=0（docs/02 §4）；账户未注入时退回系数文件
        quota = self.account.daily_l3_quota if self.account else self.coeff.get("daily_l3_quota", 3)
        return quota - used

    def _expected_value(self, score: float) -> float:
        """校准映射：steady 查箱（docs/04 §1.2 分段常量）；cold_start 锚点插值（§1.1）。"""
        n = self.coeff["normalization"]
        if n.get("phase") == "steady" and n.get("bins"):
            from app.review.calibration import locate_bin

            edges = [float(e) for e in n["bins"]["edges"]]
            return float(n["bins"]["accept_rate"][locate_bin(edges, score)])
        anchors = n["anchors"]
        if score <= anchors[0]["score"]:
            return anchors[0]["expected"]
        if score >= anchors[-1]["score"]:
            return anchors[-1]["expected"]
        for a, b in zip(anchors, anchors[1:]):
            if a["score"] <= score < b["score"]:
                t = (score - a["score"]) / (b["score"] - a["score"])
                return a["expected"] + t * (b["expected"] - a["expected"])
        return anchors[-1]["expected"]
