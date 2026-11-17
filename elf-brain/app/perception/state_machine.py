"""用户状态机（docs/03 §3.1）。

P0 简化：只依赖空闲秒数 + 前台应用类别做转移；
窗口标题原文在此层被丢弃（隐私红线：原文不出 perception 包）。
"""

import time
from enum import Enum
from typing import Awaitable, Callable


class UserState(str, Enum):
    DEEP_WORK = "deep_work"
    BUSY = "busy"
    TRANSITION = "transition"  # 黄金窗口：间隙（静默 10s-3min）
    IDLE = "idle"
    AWAY = "away"
    DND = "dnd"


WORK_APPS = {"code", "vscode", "idea", "word", "excel", "wpp", "notion", "feishu", "dingtalk"}
MEETING_APPS = {"zoom", "teams", "tencent meeting", "voov"}


class StateMachine:
    def __init__(self, quiet_start: int = 23, quiet_end: int = 8) -> None:
        self.state = UserState.IDLE
        self.quiet_start, self.quiet_end = quiet_start, quiet_end
        self._last_input_ts = time.time()
        self._foreground = ""
        # 状态变化回调（main.py 注入，触发时机决策）
        self.on_change: Callable[[UserState], Awaitable[None]] | None = None

    def in_quiet_hours(self) -> bool:
        h = time.localtime().tm_hour
        return h >= self.quiet_start or h < self.quiet_end

    def handle_event(self, ev_type: str, payload: dict) -> None:
        if ev_type == "perception.idle_change":
            idle_sec = float(payload.get("idle_sec", 0))
            self._last_input_ts = time.time() - idle_sec
            self._reevaluate()
        elif ev_type == "perception.foreground_change":
            # 只取泛化后的应用类别；title 原文不入库、不使用
            self._foreground = str(payload.get("app_class", "")).lower()
            self._reevaluate()
        elif ev_type == "perception.screen_lock":
            if payload.get("locked", True):
                self._set(UserState.AWAY)
            elif self.state == UserState.AWAY:
                # 解锁：退出 AWAY 并重估（原实现只进不出，锁屏后永远 AWAY）
                self.state = UserState.IDLE
                self._last_input_ts = time.time()  # 解锁视为一次输入
                self._reevaluate()
        elif ev_type == "perception.dnd_set":
            self._set(UserState.DND if payload.get("on") else UserState.IDLE)

    def _reevaluate(self) -> None:
        if self.state in (UserState.AWAY, UserState.DND) or self.in_quiet_hours():
            return
        idle = time.time() - self._last_input_ts
        if self._foreground in MEETING_APPS:
            self._set(UserState.BUSY)
        elif idle < 10:
            self._set(UserState.DEEP_WORK if self._foreground in WORK_APPS else UserState.IDLE)
        elif idle < 180:
            self._set(UserState.TRANSITION)  # 黄金窗口
        else:
            self._set(UserState.IDLE)

    def _set(self, new_state: UserState) -> None:
        if new_state == self.state:
            return
        self.state = new_state
        if self.on_change:
            import asyncio

            try:
                asyncio.get_running_loop().create_task(self.on_change(new_state))
            except RuntimeError:
                pass  # 无事件循环（测试场景）
