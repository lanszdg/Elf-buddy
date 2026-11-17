"""关系账户（数据模型 docs/02 §4；实现规格 docs/05 §7.1）。

信任记账：每次开口的反馈结算后记录命中/未命中 → 滑动窗口（近 50 次）→
hit_rate 驱动 trust_factor（进评分公式 01 §M3）与 daily_l3_quota（[1,6] 浮动）；
连续 2 次无视 → 当日安静期（禁 L3）；level/xp 只增不减（成长不回退铁律，docs/02 §8）。

持久化：data_dir/relationship.json（账户是"用户数据"，与调教系数分文件）。
"""

import json
import time
from pathlib import Path

WINDOW_SIZE = 50
QUIET_TRIGGER_CONSECUTIVE_MISSES = 2  # 连续 2 次无视 → 当日禁 L3（05 §7.1 验收）

# XP 来源（05 §4.1：opened/👍/对话 累计经验值；只增不减，负反馈不扣）
XP_GAIN = {
    "speak.opened": 1,
    "speak.consumed": 2,
    "speak.dwell": 3,
    "speak.followup": 4,
    "speak.shared_to_elf": 6,
}
XP_THUMBS_UP = 3

# 升级门槛（level 1 起；LEVEL_XP[n-1]=升到 n+1 所需累计 xp）
LEVEL_XP = [0, 10, 30, 60, 100, 150, 210, 280, 360, 450]
# 开口权限：设计稿（docs/02 §4）为冷启动锁 L3、升级解锁，但实装发现死锁——
# 不开口就没有 opened/dwell/followup，XP 永远到不了 2 级。故 L1 起即放开 L3，
# 打扰强度改由「每日配额 + 状态窗口 + 安静期」约束（docs/05 §5.5 偏差回写）。
L3_UNLOCK_LEVEL = 1


class RelationshipAccount:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.level = 1
        self.xp = 0
        self.window: list[dict] = []  # {decision_id, hit, ts}，近 50 次开口
        self.quiet_period_until: float | None = None
        self.l3_unlocked = True
        self.consecutive_misses = 0
        self._load()

    # ---- 派生量 ----
    @property
    def hit_rate(self) -> float:
        """窗口命中率；空窗口给中性先验 0.5（冷启动配额=3、trust=1.0 的支点）。"""
        if not self.window:
            return 0.5
        return sum(1 for w in self.window if w["hit"]) / len(self.window)

    @property
    def trust_factor(self) -> float:
        """命中率越高，打扰的边际成本越低（01 §M3：disturbance_cost × trust_factor）。

        全中 0.6（敢说话）~ 全失 1.4（话多成本高）；空窗口=1.0。
        """
        return 0.6 + 0.8 * (1 - self.hit_rate)

    @property
    def daily_l3_quota(self) -> int:
        if self.in_quiet_period:
            return 0  # 安静期：当日禁 L3（05 §7.1 验收）
        if not self.window:
            return 3  # 冷启动=3（docs/02 §4）
        return max(1, min(6, 1 + round(self.hit_rate * 5)))  # [1,6] 浮动

    @property
    def in_quiet_period(self) -> bool:
        return self.quiet_period_until is not None and time.time() < self.quiet_period_until

    # ---- 记账（speak/renderer.py 反馈结算时调用） ----
    def record_feedback(self, decision_id: str, actual_value: float, ev_type: str) -> None:
        hit = actual_value >= 0.5  # consumed 及以上=命中（05 §7.1 粗判 outcome）
        self.window.append({"decision_id": decision_id, "hit": hit, "ts": time.time()})
        self.window = self.window[-WINDOW_SIZE:]
        self.consecutive_misses = 0 if hit else self.consecutive_misses + 1
        if self.consecutive_misses >= QUIET_TRIGGER_CONSECUTIVE_MISSES and not self.in_quiet_period:
            # 当日安静期：到次日 0 点（"连点 2 次待会 → 当日不再 L3"）
            lt = time.localtime()
            self.quiet_period_until = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)
            )
        xp = XP_GAIN.get(ev_type, 0)
        if ev_type == "speak.feedback_explicit" and hit:
            xp = XP_THUMBS_UP  # 👍；👎 不加也不扣
        if xp:
            self.xp += xp
            self._maybe_level_up()
        self._save()

    def record_shown(self) -> None:
        """卡片真正曝光即给 1 XP（不进命中率窗口）。

        只靠 opened/dwell/followup 攒经验会形成"不开口→没经验→不升级"的死锁，
        曝光是最轻的正信号：看过就算参与过。
        """
        self.xp += 1
        self._maybe_level_up()
        self._save()

    def _maybe_level_up(self) -> None:
        while self.level < len(LEVEL_XP) and self.xp >= LEVEL_XP[self.level]:
            self.level += 1  # 只增不减
        if not self.l3_unlocked and self.level >= L3_UNLOCK_LEVEL:
            self.l3_unlocked = True

    def snapshot(self) -> dict:
        """GET /relationship 只读视图（docs/03 接口族）。"""
        return {
            "level": self.level,
            "xp": self.xp,
            "xp_next_level": LEVEL_XP[self.level] if self.level < len(LEVEL_XP) else None,
            "hit_rate": round(self.hit_rate, 3),
            "window_size": len(self.window),
            "consecutive_misses": self.consecutive_misses,
            "trust_factor": round(self.trust_factor, 3),
            "daily_l3_quota": self.daily_l3_quota,
            "in_quiet_period": self.in_quiet_period,
            "quiet_period_until": self.quiet_period_until,
            "l3_unlocked": self.l3_unlocked,
        }

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[relationship] 存档损坏，从零开始: {e}")
            return
        self.level = d.get("level", 1)
        self.xp = d.get("xp", 0)
        self.window = d.get("window", [])[-WINDOW_SIZE:]
        self.quiet_period_until = d.get("quiet_period_until")
        self.consecutive_misses = d.get("consecutive_misses", 0)
        # 只可从 False→True（防存档字段缺失导致权限回退）；当前冷启动即 True
        self.l3_unlocked = d.get("l3_unlocked", True) or self.level >= L3_UNLOCK_LEVEL

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "level": self.level,
                    "xp": self.xp,
                    "window": self.window,
                    "quiet_period_until": self.quiet_period_until,
                    "l3_unlocked": self.l3_unlocked,
                    "consecutive_misses": self.consecutive_misses,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
