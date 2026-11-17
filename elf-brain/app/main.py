"""Elf Buddy Brain — 精灵大脑服务。

P0 最小闭环（docs/01-architecture.md §6）：
感知事件上行 → 状态机 → 时机引擎（硬编码规则）→ SpeakCommand 下行（WS）。

运行：uvicorn app.main:app --port 8787
协议定义见 docs/03-interfaces.md §2。
"""

import asyncio
import json
import os
import traceback
import random
import re
import shutil
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.content.pipeline import ContentPipeline
from app.growth.account import RelationshipAccount
from app.growth.persona import PersonaService
from app.memory.event_store import EventStore
from app.memory.fact_extractor import learn_from_utterance
from app.memory.interest_graph import InterestGraph
from app.memory.profile import UserProfileService, parse_correction
from app.memory.reminder import ReminderService
from app.perception.state_machine import StateMachine, UserState
from app.review.digest import DigestService
from app.review.pipeline import ReviewPipeline
from app.speak.narration import NarrationService
from app.speak.renderer import SpeakRenderer
from app.story.service import StoryService
from app.timing.drive import PATROL_INTERVAL, ThreeLayerDrive
from app.timing.engine import TimingEngine
from app.voice.service import VoiceSession, VoiceUnavailable, ensure_models, read_elf_name

store = EventStore(settings.event_db_path)
state_machine = StateMachine()
timing = TimingEngine(coeff_path=settings.coeff_path, store=store)
pipeline = ContentPipeline(settings=settings, store=store)
speak = SpeakRenderer(store=store)
account = RelationshipAccount(str(Path(settings.data_dir) / "relationship.json"))
graph = InterestGraph(str(Path(settings.data_dir) / "graph.db"))
profile_service = UserProfileService(str(Path(settings.data_dir) / "profile.db"))
timing.account = account  # 信任记账进评分（trust_factor/配额浮动/安静期/L3解锁）
speak.account = account  # 反馈结算 → 命中率窗口与 XP 记账
pipeline.graph = graph  # 兴趣温度进 content_value（docs/01 §M3）
pipeline.profile = profile_service  # 消费偏好进精排 prompt（docs/05 §7.3）
speak.graph = graph  # 反馈 → 带符号 episode（docs/03 §3.4）
speak.pipeline = pipeline  # decision→content→兴趣节点 溯源
drive = ThreeLayerDrive(store=store, timing=timing)  # 三层驱动（docs/05 §3.2）
persona = PersonaService(settings.data_dir, settings.soul_path)  # 人格/立场底座（§8.2）
narration = NarrationService(persona, settings)  # 人格化讲述生成（§8.1）
reminder = ReminderService(settings.data_dir)  # 批4：提醒我 X → 到点开口


class WsManager:
    """管理 shell 的 WS 连接（P0 单连接）+ 未 ack 指令重发（docs/03 §5 红线3）。"""

    PENDING_TTL = 600  # 10 分钟未曝光视为过期，不再重发（陈旧卡片不骚扰）

    def __init__(self) -> None:
        self.conn: WebSocket | None = None
        # decision_id → (下发时间, SpeakCommand)；shell 上报 speak.shown 即 ack
        self.pending: dict[str, tuple[float, dict]] = {}

    async def push(self, cmd: dict) -> bool:
        if cmd.get("type") == "speak" and cmd.get("id"):
            self.pending[cmd["id"]] = (time.time(), cmd)
        if self.conn is None:
            return False
        await self.conn.send_text(json.dumps(cmd, ensure_ascii=False))
        return True

    def ack(self, decision_id: str) -> None:
        self.pending.pop(decision_id, None)

    async def resend_pending(self) -> None:
        """壳（重）连后重发未 ack 的 SpeakCommand；shell 按 decision_id 去重。"""
        now = time.time()
        for decision_id, (ts, cmd) in list(self.pending.items()):
            if now - ts > self.PENDING_TTL:
                self.pending.pop(decision_id, None)
                continue
            await self.push(cmd)


ws_manager = WsManager()
speak.ws = ws_manager  # 注入下行通道
review = ReviewPipeline(
    store=store, graph=graph, profile=profile_service, timing=timing, settings=settings, account=account
)
review.ws = ws_manager  # review.done 下行（docs/03 §2.3）
# 技能闭环装配（docs/02 §6.1）：消费侧注入 + 反馈归因共用同一个 SkillStore
narration.skills = review.skills
speak.skills = review.skills
pipeline.skills = review.skills
digest = DigestService(
    str(Path(settings.data_dir) / "digest.db"),
    store=store, pipeline=pipeline, graph=graph, profile=profile_service, settings=settings,
)
digest.persona = persona  # 杀手C：weekly 提炼的成长笔记写回人格
review.digest = digest  # §7.5：nightly→日记、weekly→成长汇报
story = StoryService(str(Path(settings.data_dir) / "story.db"))
story.graph = graph  # 完结写回兴趣 episode + 原子事实
story.store = store  # story.finished 事件
story.persona = persona  # soul 注入主持人人格


async def maybe_push_digest() -> bool:
    """晨报/成长汇报主动说（docs/05 §7.5：不做页面，用户在场时才说）。

    在 TRANSITION/IDLE 黄金窗口由状态机钩子调用；推送成功即 mark_offered。
    """
    if state_machine.in_quiet_hours():
        return False
    d = digest.pending()
    if d is None:
        return False
    ok = await ws_manager.push(
        {"type": "digest", "id": d["id"], "payload": {"kind": d["kind"], "title": d["title"], "text": d["text"]}}
    )
    if not ok:
        return False
    digest.mark_offered(d["id"])
    store.append(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        ts=int(time.time() * 1000),
        type="speak.digest",  # 新增事件类型（§5.4 文档回写补录 docs/02 §1.1）
        source="brain",
        payload={"digest_id": d["id"], "kind": d["kind"], "date": d.get("date"), "week": d.get("week")},
    )
    print(f"[digest] 主动说出 {d['kind']}: {d['title']}")
    return True


class IncomingEvent(BaseModel):
    """壳→脑上行事件（docs/03 §2.1）。"""

    type: str
    ts: int
    source: str = "shell"
    payload: dict = {}
    session_id: str | None = None


# ---- §8.3 情绪姿态下行（docs/03 §2.2 mood.set）：反馈记账派生情绪，驱动壳侧动作/表情 ----
_current_mood = "idle"
_excited_until = 0.0  # excited 为 180s 瞬态（👍/dwell/追问后），过后回到派生情绪
_STRONG_POSITIVE = {"speak.dwell", "speak.followup", "speak.shared_to_elf"}


def _derive_mood() -> str:
    """基础情绪：quiet_hours→sleepy；安静期/连拒≥2→sorry（被连拒肉眼可见变蔫）；否则 idle。"""
    if state_machine.in_quiet_hours():
        return "sleepy"
    if account.in_quiet_period or account.consecutive_misses >= 2:
        return "sorry"
    return "idle"


async def set_mood(mood: str) -> bool:
    global _current_mood
    if mood == _current_mood:
        return False
    _current_mood = mood
    print(f"[mood] → {mood}")
    return await ws_manager.push({"type": "mood.set", "payload": {"mood": mood}})


async def refresh_mood() -> None:
    if time.time() < _excited_until:
        await set_mood("excited")
    else:
        await set_mood(_derive_mood())


# ---- P1-7 收口仪式：消费完成（consumed）→ 稍后主动索评 1-5，回分记账 ----
_RATING_PENDING: dict = {"decision_id": "", "ts": 0.0}
_RATING_WINDOW = 900.0  # 15 分钟内的「1-5」回复才算给分


async def _rating_ask() -> None:
    """consumed 后 25 秒出声索评（错过金色窗口就不问，不追讨）。"""
    await asyncio.sleep(25)
    if not _RATING_PENDING.get("decision_id"):
        return
    if time.time() - _RATING_PENDING["ts"] > _RATING_WINDOW:
        _RATING_PENDING.clear()
        return
    if state_machine.in_quiet_hours():
        return
    ok = await ws_manager.push(
        {"type": "os.say", "payload": {"text": "对了，刚那条你打几分？1 到 5 回我就行～"}}
    )
    if ok:
        print(f"[rating] 索评：{_RATING_PENDING['decision_id']}")


async def on_speak_feedback(ev_type: str, payload: dict) -> None:
    """speak.* 反馈统一入口（/events 与 /chat 共用）：结算记账后派生情绪。"""
    global _excited_until
    await speak.on_feedback(ev_type, payload)
    if (ev_type == "speak.feedback_explicit" and payload.get("positive")) or ev_type in _STRONG_POSITIVE:
        _excited_until = time.time() + 180  # 被夸兴奋 3 分钟
    # P1-7 收口仪式：认真读完 → 待会儿轻声索评（记录即收口，Readwise 式闭环）
    if ev_type == "speak.consumed" and payload.get("decision_id"):
        _RATING_PENDING.update({"decision_id": payload["decision_id"], "ts": time.time()})
        asyncio.create_task(_rating_ask())
    # §11.35 表情下行：显式反馈/追问/久读 → 脸上立刻有反应（与 mood 姿态正交）
    if ev_type == "speak.feedback_explicit":
        await _push_expr("lovely" if payload.get("positive") else "pout", 1800)
    elif ev_type == "speak.followup":
        await _push_expr("happy", 2000)
    elif ev_type == "speak.dwell":
        await _push_expr("happy", 1500)
    await refresh_mood()


async def _push_expr(expr: str, duration_ms: int) -> None:
    """§11.35 expression.set 下行：推给壳改脸上部件，duration_ms 后壳侧自动回落 normal。"""
    await ws_manager.push(
        {"type": "expression.set", "payload": {"expr": expr, "duration_ms": duration_ms}}
    )


async def content_loop() -> None:
    """内容管线周期任务：每 30 min 抓取→打标→匹配→想说清单（docs/04 §2.0）。"""
    while True:
        try:
            await pipeline.run_once()
            # 层3 价值自触发：新清单有高分内容 → 即时评估上浮（docs/05 §3.2，10s 内到达）
            await drive.on_wishlist(try_speak, state_machine.state, pipeline.wishlist())
        except Exception as e:  # P0：不让管线异常打死主循环
            print(f"[content_loop] {e}")
        await asyncio.sleep(1800)


async def patrol_loop() -> None:
    """层2 周期巡逻：5min 低频兜底（docs/01 §M3）；顺路重估情绪（quiet_hours 边界是时间驱动）。"""
    while True:
        await asyncio.sleep(PATROL_INTERVAL)
        try:
            await refresh_mood()  # §8.3：quiet_hours 进出→sleepy/派生情绪
            await drive.patrol(try_speak, state_machine.state, pipeline.wishlist())
        except Exception as e:
            print(f"[patrol] {e}")


# ---- 事件驱动回访（杀手B·时机艺术：对的时刻提对的事） ----
_REVISIT: dict | None = None  # {"node","ts","text","shown"}
_REVISIT_LAST_SHOWN = {"ts": 0.0}
_REVISIT_COOLDOWN = 7200.0  # 全局 2h 冷却，回访是点缀不是骚扰


async def _revisit_refresh() -> None:
    """每日刷新回访候选：7-14 天前的开心事 + LLM 生成自然开场（cheap，失败用模板）。"""
    global _REVISIT
    from app.core import llm

    try:
        cand = graph.revisit_candidate()
    except Exception as e:
        print(f"[revisit] candidate: {e}")
        return
    if not cand:
        return
    if _REVISIT and _REVISIT["node"] == cand["node"] and _REVISIT.get("shown"):
        return  # 这条已经说过了，等下次 nightly 换一条
    days_ago = max(1, int((time.time() - cand["ts"]) // 86400))
    try:
        r = await llm.chat(
            f"大约 {days_ago} 天前，用户跟你聊「{cand['node']}」时挺开心的。\n"
            "写一句自然的回访开场：像突然想起一件你们共同的事，随口一问（比如'后来呢''最近还有在看吗'），"
            '≤50字。输出：{"text":"开场白"}',
            system=persona.soul + "\n只输出严格 JSON。",
            model=settings.llm_model_cheap,
            timeout=20,
        )
        text = str(r.get("text", "")).strip()
    except Exception as e:
        print(f"[revisit] llm: {e}")
        text = ""
    if not text:
        text = f"对了，{days_ago} 天前你跟我聊「{cand['node']}」时还挺来劲的，后来有下文吗？"
    _REVISIT = {"node": cand["node"], "ts": cand["ts"], "text": text, "shown": False}
    print(f"[revisit] 候选就绪：{cand['node']}（{days_ago} 天前）")


async def _maybe_revisit_speak() -> bool:
    """回访时机：try_speak 的金色窗口（TRANSITION/IDLE、非安静时段、用户在场）内才说。"""
    global _REVISIT
    if _REVISIT is None or _REVISIT["shown"]:
        return False
    if time.time() - _REVISIT_LAST_SHOWN["ts"] < _REVISIT_COOLDOWN:
        return False
    ok = await ws_manager.push(
        {
            "type": "speak",
            "id": f"spk_{uuid.uuid4().hex[:12]}",
            "channel": "L3",
            "payload": {
                "content_id": "",
                "title": f"回访·{_REVISIT['node']}",
                "reason": "revisit",
                "summary": "",
                "url": "",
                "narration": {
                    "hook": "突然想起件事——",
                    "script_short": _REVISIT["text"],
                    "stance_check_passed": True,
                },
            },
        }
    )
    if not ok:
        return False
    _REVISIT["shown"] = True
    _REVISIT_LAST_SHOWN["ts"] = time.time()
    print(f"[revisit] 说出回访：{_REVISIT['node']}")
    return True


# ---- P0 特异性三件套：温故追问 / 决策回访 / 偶遇电台（docs/05 §7.4 兴趣场景挖深） ----
async def _daily_specials(evening: bool = False) -> None:
    """晨间（quiet_end+35）生成当天三条特异性 digest；晚间（quiet_start+25）补一条温故。

    生成失败静默跳过——这三件是点缀不是任务，不能让异常刷屏日志。
    """
    gens = [digest.generate_echo] if evening else [
        digest.generate_echo, digest.generate_decision, digest.generate_serendipity,
    ]
    for gen in gens:
        try:
            d = await gen()
            if d:
                print(f"[specials] 就绪 {d['kind']}: {d['title']}")
        except Exception as e:
            print(f"[specials] {getattr(gen, '__name__', gen)}: {e}")


async def try_speak(trigger: str, user_present: bool = False) -> bool:
    """三层驱动共用评估路径（docs/05 §3.2）：digest 优先，一次只说一件事。

    价值锚点：用户在场时才说——壳离线（无 WS）/非活跃状态/quiet_hours 一律憋着。
    已决策内容去重（Meta ISM 去重静默），不再烦用户。
    user_present=True（§11.34-A 戳一戳）：用户主动在场，跳过 quiet_hours/状态门禁，
    按 TRANSITION 评分——戳=从非活跃转入交互的瞬时信号。
    """
    if user_present:
        st = UserState.TRANSITION
    else:
        if state_machine.in_quiet_hours():
            return False
        st = state_machine.state
        if st in (UserState.BUSY, UserState.AWAY, UserState.DND):
            return False
    if ws_manager.conn is None:
        return False
    if await maybe_push_digest():
        return True
    if await _maybe_revisit_speak():  # 回访优先于内容推荐（人际 > 资讯）
        return True
    cmd = timing.decide(
        user_state=st,
        candidates=drive.fresh(pipeline.wishlist()),
        quiet_hours=False,
    )
    if cmd is None:
        return False
    # §8.1：L3 开口前生成人格化讲述（生成前立场库校验一致性），缓存进 CandidateEntry；
    # L1/L2 不生成（03 §2.1：L3 必带 narration；L2 仅动作）——省 token；失败回退 summary
    if cmd["channel"] == "L3":
        item = pipeline.find(cmd["payload"]["content_id"])
        if item is not None:
            if "narration" not in item:
                item["narration"] = await narration.generate(item, cmd["id"])
            if item.get("narration"):
                cmd["payload"]["narration"] = item["narration"]
    drive.mark_spoken(cmd["payload"]["content_id"])
    await speak.push(cmd)
    _last_spoke.update(  # §8.4：语音追问锚定最近卡片上下文
        {"content_id": cmd["payload"]["content_id"], "decision_id": cmd["id"], "ts": time.time()}
    )
    print(f"[drive:{trigger}] 说出 {cmd['channel']}: {cmd['payload']['title'][:30]}")
    return True


async def on_state_change(new_state: UserState) -> None:
    """层1 状态驱动（docs/01 §M3）：进入 TRANSITION/IDLE 即重评想说清单。"""
    prev, prev_ts = _STATE_SINCE["state"], _STATE_SINCE["ts"]
    _STATE_SINCE.update({"state": new_state.value, "ts": time.time()})
    # 杀手A·共谋感：长时间深度工作刚结束 → 出声咕哝一句（不阻塞状态机）
    if prev == UserState.DEEP_WORK.value and new_state != UserState.DEEP_WORK:
        mins = int((time.time() - prev_ts) // 60)
        if mins >= 45:
            asyncio.create_task(
                _buddy_aside("deep_work_exit", f"用户刚结束一段 {mins} 分钟的深度工作。")
            )
    if new_state in (UserState.TRANSITION, UserState.IDLE):
        await try_speak("state")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init()
    drive.rebuild()  # 重启恢复"已说过"去重集（store.init 之后才能查）
    state_machine.on_change = on_state_change
    graph.sync_explicit_interests(pipeline.interest_names())  # onboarding 兴趣 → 图谱节点
    task = asyncio.create_task(content_loop())
    asyncio.create_task(patrol_loop())  # 层2 周期巡逻
    asyncio.create_task(os_loop())  # §11.35 内心 OS 独白循环
    # Review 调度（docs/03 §3.5）：nightly 于 quiet_hours 开始后运行；weekly 周日晚
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        review.nightly,
        CronTrigger(hour=state_machine.quiet_start, minute=15),
        id="review_nightly",
    )
    scheduler.add_job(
        review.weekly,
        CronTrigger(day_of_week="sun", hour=state_machine.quiet_start, minute=20),
        id="review_weekly",
    )
    scheduler.add_job(
        digest.generate_morning,
        CronTrigger(hour=state_machine.quiet_end, minute=5),
        id="digest_morning",
    )
    scheduler.add_job(
        _revisit_refresh,
        CronTrigger(hour=state_machine.quiet_start, minute=30),
        id="revisit_refresh",
    )
    scheduler.add_job(  # P0 三件套：晨报后 30 分钟生成当天特异性 digest
        _daily_specials,
        CronTrigger(hour=state_machine.quiet_end, minute=35),
        id="daily_specials",
    )
    scheduler.add_job(  # 晚间补一条温故（白天没问成、今天还没生成过时）
        lambda: asyncio.create_task(_daily_specials(evening=True)),
        CronTrigger(hour=state_machine.quiet_start, minute=25),
        id="evening_echo",
    )
    scheduler.start()
    asyncio.create_task(_revisit_refresh())  # 启动即刷新回访候选
    asyncio.create_task(_daily_specials())  # 启动补生成：中午才开机也有当天的特异性内容
    # 启动补生成：错过 8:05 cron（中午才开机）且今天无晨报 → 补一条，等在场时说出
    if not digest.has_today("morning") and not state_machine.in_quiet_hours():
        asyncio.create_task(digest.generate_morning())
    yield
    scheduler.shutdown(wait=False)
    task.cancel()
    store.close()
    graph.close()
    profile_service.close()
    digest.close()
    persona.close()
    reminder.close()
    story.close()


app = FastAPI(title="elf-brain", lifespan=lifespan)
# renderer（localhost/file://）直连 brain 均为跨源；本地单用户服务，全放开
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---- §9.1 接口对齐：全部 API 走 /v1 前缀 + 一次性 token（docs/03 §0） ----
# 生产壳拉起 brain 时生成随机 token 经 ELF_TOKEN 传入；开发直连未注入则不校验（便于 curl 调试）。
_TOKEN = os.getenv("ELF_TOKEN", "")


class V1Guard:
    """ASGI 守卫：拒绝非 /v1 路径；校验 token 后剥掉前缀交给既有路由。

    HTTP 用 X-Elf-Token 头；WS 用 ?token= 查询参数（浏览器 WS 无法自定义头）。
    """

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/v1"):
            await self._deny(scope, send, 404)
            return
        # CORS 预检不带自定义头，放行给内层 CORSMiddleware 应答
        is_preflight = scope["type"] == "http" and scope.get("method") == "OPTIONS"
        if _TOKEN and not is_preflight:
            if scope["type"] == "http":
                headers = {
                    k.decode().lower(): v.decode() for k, v in scope.get("headers", [])
                }
                given = headers.get("x-elf-token", "")
            else:
                query = parse_qs(scope.get("query_string", b"").decode())
                given = (query.get("token") or [""])[0]
            if given != _TOKEN:
                await self._deny(scope, send, 401)
                return
        scope["path"] = path[3:] or "/"
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, send, status: int) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401 if status == 401 else 4404})
            return
        code = "unauthorized" if status == 401 else "not_found"
        body = json.dumps({"error": {"code": code, "message": code}}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


app.add_middleware(V1Guard)


@app.post("/events")
async def post_event(ev: IncomingEvent) -> dict:
    store.append(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        ts=ev.ts or int(time.time() * 1000),
        type=ev.type,
        source=ev.source,
        payload=ev.payload,
    )
    if ev.type.startswith("perception."):
        state_machine.handle_event(ev.type, ev.payload)
    if ev.type == "speak.shown":
        # §9.4 幂等重发：曝光确认 = ack，从未 ack 队列移除（不进反馈结算）
        decision_id = ev.payload.get("decision_id")
        if decision_id:
            ws_manager.ack(decision_id)
        # 曝光即给 1 XP：只靠 opened/dwell/followup 攒经验会让"不开口"永远升不了级
        account.record_shown()
    elif ev.type.startswith("speak."):
        await on_speak_feedback(ev.type, ev.payload)
    return {"ok": True}


# ---- 追问对话（docs/05 §2.4；接口红线⑤：回答锚定已入库内容） ----
# 新闻类意图（docs/05 §11.25）：无卡片上下文时「问最新新闻」原走闲聊分支，
# 人格 prompt 禁止编造资讯 → 结构性无解；改从内容池按发布时间取真实条目列表回答
_NEWS_RE = re.compile(
    r"新闻|资讯|消息|动态|热点|大事|新鲜事|新东西|发生了什么|有什么新"
    r"|(最新|最近|这两天|这几天|今早|昨晚|今天|昨天).{0,8}(消息|新闻|资讯|动态|事|进展)"
)
# 杀手D·敢说不（docs/05 §11.35）：用户表达「打算/计划」且话题命中立场库 → 触发诚实挑战
_PLAN_RE = re.compile(r"打算|计划|准备|决定|想去|明天去|周末去|下周去|我要买|我想买|入手")
# 批4·联网深读：用户消息带链接 → 抓原文截注入 prompt（ Elf 真的读过再答）
_URL_RE = re.compile(r"https?://[^\s，。）)\"'】\]》]+")
# P0-3 翻篇许可：用户明说「不再/脱坑/早就不…」→ 命中的记忆事实归档，之后对话不再提起
_CLOSED_RE = re.compile(
    r"不再|早就不|已经不|脱坑|弃坑|退坑|出坑|不玩了|不看了|没看了|不听了|不追了|换口味|没兴趣了|厌倦了"
)
# story-1 互动剧：讲个故事 → 开局一幕 + 分支卡片；点选走 /story/choose 推进
_STORY_RE = re.compile(
    r"讲个故事|来个故事|给我讲|讲个|编个故事|玩个互动|互动剧|冒险故事|讲个笑话|演个|开个小剧场"
)


async def _fetch_url_text(url: str, max_chars: int = 3000) -> str:
    """抓 URL 原文，粗略去标签截断（M8 成本红线：单次 ≤3k 字入 prompt）。"""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as cli:
            r = await cli.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"})
            r.raise_for_status()
        body = r.text
        m = re.search(r"<body[^>]*>(.*?)</body>", body, re.S | re.I)
        body = m.group(1) if m else body
        body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        print(f"[webread] 抓取失败 {url}: {e}")
        return ""


# 批4·提醒：「提醒我 X」→ 支持「N分钟后」「（明天/今天/今晚）（早上/上午/中午/下午/晚上）H点（半|M分）」
_REMIND_RE = re.compile(r"^提醒我\s*(?P<body>.+)$")


def _parse_reminder(text: str) -> tuple[str, float] | None:
    """返回 (提醒内容, due_ts)；解析不出时间 → None（回落普通闲聊，不误吞）。"""
    m = _REMIND_RE.match(text.strip())
    if not m:
        return None
    body = m.group("body").strip().rstrip("。！!")
    if not body:
        return None
    now = time.time()
    tm = time.localtime(now)
    mm = re.search(r"(\d{1,3})\s*分钟[之后后]?", body)
    if mm:
        content = (body[: mm.start()] + body[mm.end():]).strip(" ，,")
        return (content or body, now + int(mm.group(1)) * 60)
    hm = re.search(
        r"(?:(明天|今天|今晚))?[^点时]*?(早上|上午|中午|下午|晚上)?(\d{1,2})点(?:(\d{1,2})分|半)?",
        body,
    )
    if hm:
        day_off = 1 if hm.group(1) == "明天" else 0
        ampm, hour, minute = hm.group(2), int(hm.group(3)), int(hm.group(4) or 0)
        if hm.group(0).endswith("半"):
            minute = 30
        if (ampm in ("下午", "晚上") or (hm.group(1) == "今晚" and not ampm)) and hour < 12:
            hour += 12
        due = time.mktime((tm.tm_year, tm.tm_mon, tm.tm_mday + day_off, hour, minute, 0, 0, 0, -1))
        if due <= now:  # 今天的时间已过 → 顺延到明天
            due += 86400
        content = (body[: hm.start()] + body[hm.end():]).strip(" ，,的")
        return (content or body, due)
    return None


def _age_text(ts: float) -> str:
    """时间戳 → 「N分钟/小时/天前」口语。"""
    h = max(0.0, (time.time() - ts) / 3600)
    if h < 1:
        return f"{max(1, int(h * 60))}分钟前"
    if h < 24:
        return f"{int(h)}小时前"
    return f"{int(h / 24)}天前"


# ---- 主动问新闻现抓（docs/05 §11.34-B） ----
_NEWS_STALE_SECONDS = 1200  # 池子最新条目超过 20 分钟视为旧
_news_refetch_lock = asyncio.Lock()  # run_once 无内部锁，防并发重入（连问两次只抓一次）


async def _ensure_news_fresh() -> None:
    """主动问新闻且池子旧（>20 分钟）→ 先现抓一轮再答（抓取免费）。

    加锁防并发重入；等锁期间不排队（问新闻别等太久，直接用现有池子诚实标注时间）；
    现抓失败静默用现有池子答（不编造，红线⑤）。
    """
    def newest_ts() -> float:
        latest = pipeline.latest_items(1)
        if not latest:
            return 0.0
        return latest[0].get("published_ts") or latest[0]["fetched_ts"]

    if _news_refetch_lock.locked():
        return
    if time.time() - newest_ts() <= _NEWS_STALE_SECONDS:
        return
    async with _news_refetch_lock:
        if time.time() - newest_ts() <= _NEWS_STALE_SECONDS:
            return  # 进锁后已新鲜（content_loop 刚抓完）
        try:
            print("[chat/news] 池子旧，现抓一轮再答")
            await pipeline.run_once()
        except Exception as e:
            print(f"[chat/news] 现抓失败，用现有池子答: {e}")


async def _news_listing_reply(text: str) -> str:
    """从内容池取最新条目回答新闻类问题（全部真实抓取，红线⑤不编造）。

    每条给「标题 + 一句话看点（来自真实摘要）+ 多久前」，末尾挂展开追问——
    只甩标题列表用户没法判断哪条值得看（docs/05 §11.32）。
    """
    from app.core import llm

    await _ensure_news_fresh()  # §11.34-B：池子旧先现抓，别拿两小时前的"最新"糊弄
    items = pipeline.latest_items(5)
    if not items:
        return ""

    def gist(it: dict) -> str:
        return str(it.get("summary") or "").strip().replace("\n", " ")

    listing = "\n".join(
        f"- {it['title']}｜{it.get('source', '')}｜{_age_text(it.get('published_ts') or it['fetched_ts'])}"
        + (f"｜摘要：{gist(it)[:120]}" if gist(it) else "")
        for it in items
    )
    prompt = (
        f"用户问：{text}\n\n"
        f"我刚抓到的最新内容（全部真实，禁止编造清单外内容）：\n{listing}\n\n"
        '按 JSON 输出：{"reply":"口语化列 3~4 条最值得说的；每条 = 标题 + 一句看点'
        '（只能来自给出的摘要）+ 多久前；末尾加一句「想听哪条我可以展开讲讲」。≤160字"}'
    )
    system = (
        "你是用户桌面上的小精灵。只能基于清单里给出的内容回答，禁止编造清单外的资讯；"
        "都偏旧就老实说。只输出严格 JSON。"
    )
    try:
        r = await llm.chat(prompt, system=system, model=settings.llm_model_cheap, timeout=30)
        reply = str(r.get("reply", "")).strip()
        if reply:
            return reply
    except Exception as e:
        print(f"[chat/news] {e}")
    # LLM 失败/空 → 模板兜底（零成本，保底能答；看点截取真实摘要前 40 字，红线⑤）
    tail = "想听哪条我可以展开讲讲。"
    return "我看到的有：" + "；".join(
        f"《{it['title']}》（{_age_text(it.get('published_ts') or it['fetched_ts'])}）"
        + (f"：{gist(it)[:40]}" if gist(it) else "")
        for it in items[:3]
    ) + "。" + tail


class ChatRequest(BaseModel):
    text: str
    content_id: str = ""  # §8.4：语音闲聊无卡片上下文时为空
    decision_id: str = ""


# ---- §11.35 多轮对话记忆：最近 20 轮注入 prompt（"聊不起来"的根因是单轮无上下文） ----
_CHAT_HISTORY: deque[dict] = deque(maxlen=20)
_CHAT_HISTORY_TTL = 7200.0  # 2 小时无对话即清空（久别重逢不该背旧账）


def _chat_history_text() -> str:
    now = time.time()
    while _CHAT_HISTORY and now - _CHAT_HISTORY[0].get("ts", 0) > _CHAT_HISTORY_TTL:
        _CHAT_HISTORY.popleft()
    return "\n".join(
        f"{'用户' if h['role'] == 'user' else '小精灵'}：{h['text']}" for h in _CHAT_HISTORY
    )


def _chat_remember(role: str, text: str) -> None:
    _CHAT_HISTORY.append({"role": role, "text": text[:200], "ts": time.time()})


# ---- 记忆相关性召回缓存：同一话题 5 分钟内复用排序结果，避免每轮调 LLM ----
_RECALL_CACHE: dict[str, tuple[float, list[str]]] = {}
_RECALL_CACHE_TTL = 300.0


async def _recall_facts(topic: str, facts: list[dict]) -> list[dict]:
    """按当前话题从生效事实里挑最相关的 5 条（LLM 排序，失败回退最新 5 条）。"""
    if not topic.strip() or len(facts) <= 5:
        return facts[:5]
    latest = max(f.get("valid_from", 0.0) for f in facts) if facts else 0.0
    key = f"{topic.strip()[:80]}|{latest:.0f}"
    hit = _RECALL_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _RECALL_CACHE_TTL:
        wanted = hit[1]
        return [f for f in facts if f"{f['predicate']}：{f['object']}" in wanted] or facts[:5]
    from app.core import llm
    numbered = [f"{i+1}. {f['predicate']}：{f['object']}" for i, f in enumerate(facts)]
    try:
        r = await llm.chat(
            "用户现在说：「" + topic.strip()[:200] + "」\n\n"
            "你记住的关于用户的事实：\n" + "\n".join(numbered)
            + "\n\n挑出和当前话题最相关、此刻带出来最自然的 5 条（编号升序）。"
            '输出：{"keep":[编号,...]}',
            system="你只输出严格 JSON，不解释。",
            model=settings.llm_model_cheap,
            timeout=20,
        )
        import json as _json
        keep = _json.loads(str(r.get("keep", "[]")))
        picked = [facts[int(i) - 1] for i in keep if 1 <= int(i) <= len(facts)][:5]
        if picked:
            _RECALL_CACHE[key] = (now, [f"{f['predicate']}：{f['object']}" for f in picked])
            if len(_RECALL_CACHE) > 50:
                for k in sorted(_RECALL_CACHE, key=lambda k: _RECALL_CACHE[k][0])[:-50]:
                    _RECALL_CACHE.pop(k, None)
            return picked
    except Exception as e:
        print(f"[recall] {e}")
    return facts[:5]


async def _memory_block(topic_hint: str = "") -> str:
    """长期记忆注入 prompt（docs/02 §2.3 读侧接线：对话真正感知"记住了什么"）。

    生效事实按当前话题相关性召回 top5（无话题/失败回退最新 5 条）+ 当前兴趣 + 消费画像。
    """
    lines: list[str] = []
    facts = [f for f in graph.active_facts(30) if f["subject"] == "user"][:15]
    if facts:
        lines.append("你记住的关于用户的事实（用户亲口说过的，可信，聊天可以自然地带出来）：")
        lines += [f"- {f['predicate']}：{f['object']}" for f in await _recall_facts(topic_hint, facts)]
    interests = pipeline.interest_names()
    if interests:
        lines.append(f"用户关注的兴趣：{'、'.join(interests[:8])}")
    try:
        pref = profile_service.preference_text()
    except Exception:
        pref = ""
    if pref:
        lines.append(f"用户的内容消费偏好：{pref}")
    return ("\n".join(lines) + "\n\n") if lines else ""


async def _chat_reply_inner(text: str, content_id: str, decision_id: str) -> tuple[str, str]:
    """/chat 与 /ws/voice 共用的回答生成。返回 (reply, mood)：mood 由 LLM 按语气挑（壳侧表情）。

    有 content_id = 卡片追问（锚定标题/摘要，红线⑤）；无 = 闲聊（soul 人格，不涉内容）。
    """
    from app.core import llm
    from app.core.llm import LLMUnavailable

    # P0-3 翻篇许可：先于记忆注入执行——被翻篇的事实本条回复里就不会再被提起
    archived: list[str] = []
    if _CLOSED_RE.search(text):
        try:
            archived = graph.archive_matching_facts(text)
            if archived:
                print(f"[close] 翻篇归档 {len(archived)} 条：{archived[:3]}")
        except Exception as e:
            print(f"[close] {e}")
    # §11.35 多轮记忆 + 长期记忆：一起拼进 prompt（过期自动清，见 _chat_history_text）
    history = _chat_history_text()
    history_block = f"你们最近几轮对话：\n{history}\n（接着这个话头聊）\n\n" if history else ""
    context_block = await _memory_block(text) + history_block
    if archived:
        context_block += (
            "用户刚明确表示这些事翻篇了（可顺势回一句「好，以后不提了」）：\n"
            + "\n".join(f"- {a}" for a in archived[:5])
            + "\n\n"
        )

    if content_id:
        deep = False
        item = pipeline.find(content_id)
        if item is None:
            return "那条内容我记不太清了，要不看看原文？", "normal"
        # 追问=很强正信号（docs/02 §5.3，actual 0.9），先记账再回答
        if decision_id:
            await on_speak_feedback("speak.followup", {"decision_id": decision_id})
        prompt = (
            context_block
            + f"内容标题：{item['title']}\n"
            f"发布时间：{item.get('published', '') or '未知'}\n"
            f"来源：{item.get('source', '')}\n"
            f"内容摘要：{item.get('summary', '')[:600]}\n\n"
            f"用户的追问：{text}\n\n"
            '按 JSON 输出：{"reply":"回答，≤120字，口语化，像朋友聊天",'
            '"mood":"happy|lovely|surprised|pout|angry|shy|normal 选一个，贴回答的语气"}'
        )
        system = (
            "你是用户桌面上的小精灵，刚给用户讲了上面这条内容。"
            "只能依据给出的标题、发布时间、来源和摘要回答，禁止编造未提及的信息；"
            "给出的信息里没说的就坦白说'这里面没提'。"
            "只输出严格 JSON。"
        )
        fallback = "我现在脑子不在线，要不先自己看看原文？"
    else:
        # story-1 互动剧：命中 → 开局一幕 + story.scene 分支卡片（选择走 /story/choose）；
        # LLM 不可用/异常静默回落正常闲聊
        if _STORY_RE.search(text):
            try:
                r = await story.start(text.strip()[:120])
                await ws_manager.push({"type": "story.scene", "id": r["story_id"], "payload": r})
                print(f"[story] 开局《{r['title']}》{r['story_id']}")
                return f"《{r['title']}》开演——{r['scene']}", "happy"
            except Exception as e:
                print(f"[story] 开局失败回落闲聊：{e}")
        # 批4·提醒：「提醒我 X」→ 落库 + 确认回复（不进闲聊 LLM；解析不出时间才回落闲聊）
        parsed = _parse_reminder(text)
        if parsed:
            rtext, due_ts = parsed
            reminder.add(rtext, due_ts)
            when = time.strftime("%m月%d日 %H:%M", time.localtime(due_ts))
            return f"记住啦，{when} 提醒你{rtext}～", "happy"
        # 新闻类意图（§11.25）：从内容池取真实最新条目列表回答；答不了才回落闲聊
        if _NEWS_RE.search(text):
            news = await _news_listing_reply(text)
            if news:
                return news, "happy"
        # 杀手D·敢说不：用户表达计划/决定 + 话题命中立场库 + 信任度达标 → 诚实挑战，不当应声虫
        challenge_note = ""
        plan_m = _PLAN_RE.search(text)
        if plan_m:
            # P0-2 决策台账：计划句落库，7/30 天后回访结算（digest.generate_decision 消费）
            try:
                if persona.add_decision(f"dec_{uuid.uuid4().hex[:12]}", text.strip()[:120]):
                    print(f"[decision] 台账登记：{text.strip()[:40]}")
            except Exception as e:
                print(f"[decision] 登记失败：{e}")
        if plan_m and account.trust_factor >= 1.0:
            ents = [n for n in pipeline.interest_names() if n and n in text]
            matched = persona.stances_for(ents) or persona.stances_mentioning(text)
            for s in matched:
                if "不" in s["stance"] or "反对" in s["stance"] or "担心" in s["stance"] or "质疑" in s["stance"]:
                    challenge_note = (
                        f"用户提到一个计划：「{text[:60]}」。"
                        f"你此前对「{s['topic_entity']}」的立场是：{s['stance']}。"
                        "如果立场里含质疑/反对，请诚实地把你的担忧说出来（先认可用户的考虑，再给出你的不同看法，≤60字内嵌进回复）；"
                        "如果立场其实是支持，就只轻轻确认，别硬杠。"
                    )
                    break
        # 批4·联网深读：消息带链接 → 抓原文注入（精灵真的读过再答）
        web_block = ""
        url_m = _URL_RE.search(text)
        if url_m:
            web_text = await _fetch_url_text(url_m.group(0))
            if web_text:
                web_block = (
                    f"用户分享的链接原文摘录：\n{web_text}\n"
                    "（认真读过了再答：摘录里没说的别编；链接打不开的损失不兜）\n\n"
                )
                context_block += web_block
        # 兴趣深聊模式：用户话语命中高温度兴趣（≥0.5）→ 放宽字数、允许观点、立场库一致性校验
        deep = False
        hit = next(
            (n for n in pipeline.interest_names() if n and graph.temperature_of(n) >= 0.5 and n in text),
            "",
        )
        if hit:
            deep = True
            stances = persona.stances_for([hit])
            stance_txt = "；".join(f"{s['topic_entity']}：{s['stance']}" for s in stances)
            prompt = (
                context_block
                + f"用户对你说：{text}\n\n"
                + (f"你之前对「{hit}」相关话题的立场记录：{stance_txt}（保持一致，别自相矛盾）\n" if stance_txt else "")
                + (challenge_note + "\n" if challenge_note else "")
                + '输出：{"reply":"≤250字，像懂行的老朋友深聊：可以有明确观点和立场，可以展开细节和例子；'
                "拿不准的直说拿不准，禁止编造具体资讯/数据\","
                '"mood":"happy|lovely|surprised|pout|angry|shy|normal 选一个，贴回复的语气"}'
            )
            system = (
                persona.soul
                + "\n用户聊到了你很熟的兴趣话题，拿出真本事深聊：有观点、有细节，你是朋友不是应声虫；"
                "观点可以不同，诚实表达。只输出严格 JSON。"
            )
            fallback = "我在呢，你说。"
        else:
            # 闲聊（§8.4 语音）：人格在、不聊具体内容；不进反馈学习（非内容场景）
            limit_txt = "≤150字，说清原文要点+你的看法" if web_block else "≤60字，口语化，像朋友随口接话"
            prompt = (
                context_block
                + f"用户对你说：{text}\n\n"
                + (challenge_note + "\n" if challenge_note else "")
                + f'输出：{{"reply":"{limit_txt}",'
                '"mood":"happy|lovely|surprised|pout|angry|shy|normal 选一个，贴回复的语气"}'
            )
            system = persona.soul + "\n只输出严格 JSON。闲聊不编造任何资讯/事实；不知道就卖萌带过。"
            fallback = "我在呢，你说。"
    try:
        r = await llm.chat(
            prompt,
            system=system,
            model=settings.llm_model_strong if (content_id or deep) else settings.llm_model_cheap,
            timeout=45,
        )
        reply = str(r.get("reply", "")).strip()
        mood = str(r.get("mood", "happy")).strip() or "happy"
        # 记忆结晶提炼（docs/02 §2.3 extractor 侧）：闲聊话语后台提炼，fire-and-forget 不阻塞回复
        if not content_id and len(text.strip()) >= 6:
            asyncio.create_task(learn_from_utterance(text, graph))
    except (LLMUnavailable, Exception) as e:
        print(f"[chat] {e}")
        reply, mood = fallback, "pout"
    if not reply:
        reply = "这里面好像没提到这个，看看原文？" if content_id else fallback
        mood = "normal" if content_id else mood
    return reply, mood


async def chat_reply(text: str, content_id: str, decision_id: str) -> tuple[str, str]:
    """§11.35 记忆包装：/chat 与 /ws/voice 共用——答完把本轮问答记入多轮历史。"""
    reply, mood = await _chat_reply_inner(text, content_id, decision_id)
    _chat_remember("user", text)
    _chat_remember("elf", reply)
    return reply, mood


@app.post("/chat")
async def post_chat(req: ChatRequest) -> dict:
    # P1-7 收口：索评窗口内的「1-5」短回复 → 记为评分，不闲聊
    if _RATING_PENDING.get("decision_id") and time.time() - _RATING_PENDING["ts"] < _RATING_WINDOW:
        m = re.fullmatch(r"\s*([1-5])\s*(分|(?:颗\s*)?星|\.0)?\s*[!！~～。.]?\s*", req.text)
        if m:
            score = int(m.group(1))
            store.append(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                ts=int(time.time() * 1000),
                type="speak.rated",
                source="shell",
                payload={"decision_id": _RATING_PENDING["decision_id"], "score": score},
            )
            _RATING_PENDING.clear()
            return {
                "reply_text": f"记下啦，{score} 分～这条我记在小本本上，以后按这个口味给你找。",
                "mood": "happy" if score >= 4 else "normal",
            }
    reply, mood = await chat_reply(req.text, req.content_id, req.decision_id)
    return {"reply_text": reply, "mood": mood}


# ---- story-1 互动剧：壳侧点选分支 → 推进下一幕并推送 story.scene ----
class StoryChooseRequest(BaseModel):
    story_id: str
    choice: str


@app.post("/story/choose")
async def post_story_choose(req: StoryChooseRequest) -> dict:
    try:
        r = await story.choose(req.story_id, req.choice.strip()[:60])
    except KeyError:
        return {"error": {"code": "story_not_found", "message": "故事不存在或已过期"}}
    except Exception as e:
        print(f"[story] 推进失败：{e}")
        return {"error": {"code": "story_failed", "message": "这幕没编出来，要不再开一局？"}}
    await ws_manager.push({"type": "story.scene", "id": r["story_id"], "payload": r})
    print(f"[story] 第{r['beat']}/{r['total_beats']}幕《{r['title']}》is_end={r['is_end']}")
    mood = "lovely" if r["is_end"] else "happy"
    return {"ok": True, "reply_text": r["scene"], "mood": mood}


# ---- 戳一戳（docs/05 §11.34-A：用户主动戳 → 优先推未曝光内容，无则节流闲聊） ----
_last_poke_chat = {"ts": 0.0}  # 闲聊节流（60s，M8 成本红线）
_POKE_CHAT_INTERVAL = 60.0
_recent_poke_replies: deque[str] = deque(maxlen=5)  # 近期说过的接话，prompt 里去重
_poke_times: deque[float] = deque(maxlen=50)  # 每次戳的时间戳，供 LLM 感知"第几次戳/隔多久"


async def _poke_chat() -> tuple[str, str]:
    """戳一戳闲聊：人格化随口接话（cheap 模型，不编造资讯/事实）。

    §11.36 个性化接线：注入长期记忆 + 用户最近一句话 + 近期接话去重——
    记住用户名字的精灵被戳，和陌生精灵被戳，反应不一样。
    返回 (reply, mood)：mood 是小精灵自己挑的表情（壳侧 expression.set）。
    """
    from app.core import llm

    last_user = next((h["text"] for h in reversed(_CHAT_HISTORY) if h["role"] == "user"), "")
    recent = "；".join(_recent_poke_replies)
    mem = await _memory_block(last_user)
    # 戳频次感知：10 分钟窗口内第几次戳、距上次几秒，让 LLM 说出"又戳！这都第 6 次了"
    now = time.time()
    pokes = [t for t in _poke_times if now - t <= 600]
    poke_n = len(pokes)
    poke_gap = int(now - pokes[-2]) if len(pokes) >= 2 else -1
    freq_note = ""
    if poke_n >= 2:
        freq_note = f"这是用户 10 分钟内第 {poke_n} 次戳你"
        if poke_gap >= 0:
            freq_note += f"，距上次只隔了 {poke_gap} 秒"
        freq_note += "。"
        if poke_n >= 5:
            freq_note += "被连着戳了好几下，可以佯装生气抗议/无奈求饶，配 pout 或 angry。"
        elif poke_n >= 3:
            freq_note += "被戳得有点频繁，可以带点撒娇式抱怨。"
    prompt = (
        mem
        + f"用户最近对你说：{last_user or '（好久没说话了）'}\n"
        + (f"你最近被戳时说过：{recent}（别重复这几句）\n" if recent else "")
        + (freq_note + "\n" if freq_note else "")
        + "用户戳了戳你（没有说具体内容，就是伸手戳了一下）。"
        '输出：{"reply":"≤30字，口语化，像被戳到的小精灵随口接话——'
        "可以好奇/撒娇/卖关子/佯装生气，结合你记住的用户特点说点只属于你们俩的话；"
        '别提具体资讯和事实",'
        '"mood":"happy|lovely|surprised|pout|angry|shy|normal 选一个，贴你这句接话的语气"}'
    )
    try:
        r = await llm.chat(
            prompt,
            system=persona.soul + "\n只输出严格 JSON。闲聊不编造任何资讯/事实。",
            model=settings.llm_model_cheap,
            timeout=30,
        )
        reply = str(r.get("reply", "")).strip()
        mood = str(r.get("mood", "pout")).strip() or "pout"
        if reply:
            _recent_poke_replies.append(reply)
        return reply, mood
    except Exception as e:
        print(f"[poke] 闲聊失败: {e}")
        return "", "pout"


@app.post("/poke")
async def post_poke() -> dict:
    """壳侧戳一戳接脑：优先 try_speak 推内容（有未曝光高分候选时气泡走 WS 下行）；
    无内容可推 → 60s 节流闲聊（kind=chat，壳展示 reply_text）；
    noop=节流中或 LLM 挂 → 壳侧兜底本地俏皮话。"""
    if await try_speak("poke", user_present=True):
        return {"kind": "speak"}
    now = time.time()
    _poke_times.append(now)  # 记录每次戳（含节流/闲聊），供频次感知
    if now - _last_poke_chat["ts"] < _POKE_CHAT_INTERVAL:
        return {"kind": "noop"}
    _last_poke_chat["ts"] = now
    reply, mood = await _poke_chat()
    if reply:
        return {"kind": "chat", "reply_text": reply, "mood": mood}
    return {"kind": "noop"}


# ---- 语音对话（docs/05 §8.4：KWS 唤醒→ASR→chat_reply→voice.say 下行） ----
_last_spoke: dict = {}  # 最近一次说出的卡片上下文（语音追问锚定；10 分钟有效）


# ---- §11.35 内心 OS：静默独白循环（cheap + 45min 节流 + 本地兜底） ----
_OS_INTERVAL = 2700.0  # 45min 一条（M8 成本红线）
_OS_LOCAL = [
    "（盯着你的屏幕发呆）",
    "（悄悄伸了个懒腰）",
    "（数你敲了多少下键盘）",
    "（打了个小哈欠）",
    "（在心里哼歌）",
    "（偷偷瞄了你一眼）",
]

# ---- 杀手A·共谋感：活动流旁白（看见你在做什么，低频、有灵魂地咕哝） ----
_STATE_SINCE = {"state": UserState.IDLE.value, "ts": 0.0}  # on_state_change 维护
_BUDDY_SAY = {"key": "", "ts": 0.0}
_BUDDY_COOLDOWN = 1800.0  # 同类旁白 30min 不重复


async def _buddy_aside(kind: str, context: str) -> bool:
    """cheap LLM 一句陪伴旁白，走 os.say 通道；失败静默（共谋感是点缀不是任务）。"""
    now = time.time()
    if state_machine.in_quiet_hours():
        return False
    if _BUDDY_SAY["key"] == kind and now - _BUDDY_SAY["ts"] < _BUDDY_COOLDOWN:
        return False
    from app.core import llm

    try:
        r = await llm.chat(
            f"{context}\n写一句你此刻的旁白：像一起待着的小搭档出声咕哝，有灵魂、口语化，≤30字。"
            '输出：{"say":"旁白"}',
            system=persona.soul + "\n只输出严格 JSON。",
            model=settings.llm_model_cheap,
            timeout=20,
        )
        text = str(r.get("say", "")).strip()
    except Exception as e:
        print(f"[buddy] {e}")
        return False
    if not text:
        return False
    if await ws_manager.push({"type": "os.say", "payload": {"text": text}}):
        _BUDDY_SAY.update({"key": kind, "ts": now})
        print(f"[buddy] {kind}: {text}")
        return True
    return False


async def _inner_os() -> str:
    """生成一句内心独白（cheap 模型；失败/超时 → 本地兜底，零成本保底）。"""
    from app.core import llm

    last_user = next((h["text"] for h in reversed(_CHAT_HISTORY) if h["role"] == "user"), "")
    prompt = (
        f"现在大约 {time.strftime('%H')} 点。"
        f"用户最近对你说：{last_user or '（好久没说话了）'}\n"
        '输出：{"os":"≤25字，你此刻的内心独白或小动作+心情，像漫画旁白；'
        '禁止编造资讯/事实"}'
    )
    try:
        r = await llm.chat(
            prompt,
            system=persona.soul + "\n只输出严格 JSON。这是写给自己的内心话，不用招呼用户。",
            model=settings.llm_model_cheap,
            timeout=30,
        )
        os_text = str(r.get("os", "")).strip()
        if os_text:
            return os_text
    except Exception as e:
        print(f"[os] 生成失败: {e}")
    return random.choice(_OS_LOCAL)


async def os_loop() -> None:
    """每 ~45min 自言自语一句；quiet_hours 精灵睡了不说梦话。"""
    await asyncio.sleep(1200)  # 开机先安静 20min，不打扰刚坐下
    while True:
        await asyncio.sleep(_OS_INTERVAL + random.uniform(-600, 600))  # ±10min 抖动，像活物
        if state_machine.in_quiet_hours():
            continue
        # 批4·提醒：到点开口（安静期内不补发，醒来再说）
        for r in reminder.due():
            if await ws_manager.push({"type": "os.say", "payload": {"text": f"诶，到点了——{r['text']}"}}):
                reminder.done(r["id"])
                print(f"[remind] 到点提醒: {r['text']}")
        # 杀手A·共谋感：连续深度工作 ≥30min → 旁白优先于内心独白
        now = time.time()
        if (
            state_machine.state == UserState.DEEP_WORK
            and _STATE_SINCE["state"] == UserState.DEEP_WORK.value
            and now - _STATE_SINCE["ts"] >= 1800
        ):
            mins = int((now - _STATE_SINCE["ts"]) // 60)
            if await _buddy_aside("deep_work", f"用户已经连续深度工作 {mins} 分钟了，一直在忙。"):
                continue
        text = await _inner_os()
        if await ws_manager.push({"type": "os.say", "payload": {"text": text}}):
            print(f"[os] {text}")



@app.websocket("/ws/voice")
async def ws_voice(ws: WebSocket) -> None:
    """壳→脑语音通道：上行 PCM int16 16kHz 单声道二进制帧；下行 voice.* 事件。

    隐私红线：音频只在环回，不出机；脑侧 sherpa-onnx 全本地推理。
    连接即开会话（模型懒加载+缺失即下载），断开即销毁。
    """
    await ws.accept()
    try:
        await asyncio.to_thread(ensure_models, Path(settings.data_dir) / "voice")
        session = await asyncio.to_thread(
            VoiceSession, Path(settings.data_dir) / "voice", read_elf_name(Path(settings.data_dir))
        )
    except (VoiceUnavailable, Exception) as e:
        print(f"[voice] 会话开启失败: {e}")
        await ws.send_text(json.dumps({"type": "voice.error", "payload": {"message": str(e)}}, ensure_ascii=False))
        await ws.close()
        return
    print("[voice] 会话开启（KWS 待唤醒）")

    # voice-1 barge-in：回复异步化——LLM/TTS 在途时主循环照常收音频与控制帧，
    # 喊名字（voice.wake）或新一句 ASR 到达即 cancel 在途回复，旧 voice.say 不再下发。
    pending: asyncio.Task | None = None

    async def _reply(heard: str) -> None:
        try:
            # 10 分钟内有说过的卡片 → 按追问锚定；否则闲聊
            ctx = _last_spoke if time.time() - _last_spoke.get("ts", 0) < 600 else {}
            reply, mood = await chat_reply(heard, ctx.get("content_id", ""), ctx.get("decision_id", ""))
            payload = {"text": reply, "heard": heard, "mood": mood}
            try:  # 在线神经 TTS（§11.27），失败回落壳侧系统 TTS
                from app.voice.tts import synth_b64

                payload["audio"] = await synth_b64(reply)
            except Exception as e:
                print(f"[voice] 在线TTS失败，回落系统TTS: {e}")
            await ws.send_text(
                json.dumps({"type": "voice.say", "payload": payload}, ensure_ascii=False)
            )
        except asyncio.CancelledError:
            print(f"[voice] 在途回复被打断（barge-in），作废：{heard[:20]!r}")
            raise
        except Exception:
            traceback.print_exc()

    def _cancel_pending(reason: str) -> None:
        nonlocal pending
        if pending is not None and not pending.done():
            print(f"[voice] {reason}，取消在途回复")
            pending.cancel()

    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            pcm = msg.get("bytes")
            if pcm is None:
                # 文本帧=壳侧控制：voice.gate（TTS 起止）→ 暂停/恢复 ASR，防回声自激
                raw = msg.get("text")
                if raw:
                    try:
                        ctl = json.loads(raw)
                    except Exception:
                        continue
                    if ctl.get("type") == "voice.gate":
                        await asyncio.to_thread(session.set_gate, bool(ctl.get("on")))
                continue
            events = await asyncio.to_thread(session.feed_pcm, pcm)  # 解码是 CPU 活，别堵事件循环
            for ev in events:
                if ev["type"] == "voice.wake":
                    print("[voice] 唤醒")
                    _cancel_pending("唤醒插话")
                elif ev["type"] == "voice.asr_final":
                    text = ev["text"]
                    print(f"[voice] ASR: {text!r}")
                    if text:
                        _cancel_pending("新一句到达")
                        pending = asyncio.create_task(_reply(text))
                await ws.send_text(json.dumps(ev, ensure_ascii=False))
    except WebSocketDisconnect:
        print("[voice] 会话关闭")
    except Exception as e:  # 解码异常不杀服务，只断本会话
        print(f"[voice] 会话异常: {e}")
    finally:
        _cancel_pending("会话断开")


# ---- 关系账户（docs/03 接口族：GET /relationship 只读；成长面板数据源） ----
@app.get("/relationship")
async def get_relationship() -> dict:
    return account.snapshot()


# ---- 兴趣图谱（docs/03 接口族：GET /interests；成长面板"你都知道我什么"数据源） ----
@app.get("/interests")
async def get_interests(include_dormant: bool = False) -> list[dict]:
    return graph.snapshot(include_dormant=include_dormant)


# ---- P1-5 品味档案卡：「我眼中的你」（facts+兴趣 → 一句话画像，成长面板展示） ----
_taste_cache: dict = {"ts": 0.0, "facts_sig": 0, "text": ""}
_TASTE_TTL = 6 * 3600  # 6 小时再生一次足够（品味演化以周计）


async def _taste_profile() -> str:
    """从生效事实 + 兴趣温度生成一句「我眼中的你」。LLM 优先，失败/无 key 用模板拼接。"""
    facts = graph.active_facts(30)
    sig = graph.count_facts()
    cached = _taste_cache
    if time.time() - cached["ts"] < _TASTE_TTL and cached["facts_sig"] == sig and cached["text"]:
        return cached["text"]
    interests = graph.snapshot()
    hot = [i["name"] for i in interests if i["status"] == "active"][:5]
    user_facts = [f"{f['predicate']}：{f['object']}" for f in facts if f["subject"] == "user"][:8]
    fallback = (
        f"你最近在「{'、'.join(hot[:3]) or '…'}」上最上头；"
        + (f"我记住的你：{'；'.join(user_facts[:3])}。" if user_facts else "我还在慢慢认识你。")
    )
    text = fallback
    if settings.llm_api_key:
        try:
            from app.core import llm

            r = await llm.chat(
                "根据我记住的关于用户的事实和最近的兴趣温度，写一段「我眼中的你」："
                "第二人称、≤80字、像很懂他的朋友的一句精准描述（具体到癖好和倾向，别泛）。\n"
                f"事实：{json.dumps(user_facts, ensure_ascii=False)}\n"
                f"最近的兴趣（按热度）：{json.dumps(hot, ensure_ascii=False)}\n"
                '输出：{"text":"画像"}',
                system=persona.soul + "\n只输出严格 JSON。",
                model=settings.llm_model_cheap,
                timeout=30,
            )
            got = str(r.get("text", "")).strip()
            if got:
                text = got
        except Exception as e:
            print(f"[taste] {e}")
    _taste_cache.update({"ts": time.time(), "facts_sig": sig, "text": text})
    return text


# ---- 成长面板（docs/05 §4.2/4.3：双击精灵面板数据，一次拿全） ----
@app.get("/growth/panel")
async def get_growth_panel() -> dict:
    return {
        "account": account.snapshot(),
        "interests": graph.snapshot(include_dormant=True),
        "facts_total": graph.count_facts(),
        "facts_recent_7d": len(graph.facts_since(time.time() - 7 * 86400)),
        "taste_profile": await _taste_profile(),
    }


# ---- 今天的新鲜事（docs/05 §11.34-C：成长面板热榜区数据源；全部真实入库，红线⑤） ----
@app.get("/trending")
async def get_trending() -> list[dict]:
    """最新入库 10 条（按发布时间倒序）：title/source/age/url，点卡片看原文。"""
    return [
        {
            "content_id": it["content_id"],
            "title": it["title"],
            "source": it.get("source", ""),
            "age": _age_text(it.get("published_ts") or it["fetched_ts"]),
            "url": it.get("url", ""),
        }
        for it in pipeline.latest_items(10)
    ]


# ---- 消费画像+纠偏（docs/03 §1.4；docs/05 §7.3） ----
class CorrectionRequest(BaseModel):
    text: str


@app.post("/correction")
async def post_correction(req: CorrectionRequest) -> dict:
    """用户纠偏："我现在还是喜欢长的" → 槽位 upsert + 事实时态归档（只做加法红线）。

    返回 applied_fact_ids（新事实 id）；解析为空时返回空列表，诚实不编造。
    """
    text = req.text.strip()
    if not text:
        return {"applied_fact_ids": []}
    event_id = f"evt_{uuid.uuid4().hex[:12]}"
    store.append(
        event_id=event_id,
        ts=int(time.time() * 1000),
        type="user.correction",
        source="shell",
        payload={"text": text},
    )
    items = await parse_correction(text)
    fact_ids: list[str] = []
    for it in items:
        fid = graph.upsert_fact("user", it["slot"], it["value"], "user_correction", event_id)
        profile_service.set_slot(it["slot"], it["value"], source="corrected", evidence=[event_id])
        fact_ids.append(str(fid))
    print(f"[correction] 「{text}」→ {items} → facts {fact_ids}")
    return {"applied_fact_ids": fact_ids}


@app.get("/profile/slots")
async def get_profile_slots() -> list[dict]:
    """当前消费偏好快照（成长面板/调试可查）。"""
    return profile_service.get_slots()


# ---- 模型配置（docs/05 §11.36：设置界面入口——运行时生效 + 写回 data/.env 持久化） ----
class LlmConfigRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""  # 空或含 ****（打码回显）= 不改动
    model_cheap: str = ""
    model_strong: str = ""


def _mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "****" + key[-4:] if len(key) > 10 else "****"


def _persist_env(updates: dict[str, str]) -> None:
    """把 LLM 配置写回 data/.env：已有键整行替换，其余行（注释/别的 key）原样保留。"""
    env_file = Path(settings.data_dir) / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out: list[str] = []
    done: set[str] = set()
    for line in lines:
        k = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else ""
        if k in updates:
            out.append(f"{k}={updates[k]}")
            done.add(k)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append(f"{k}={v}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")


@app.get("/config/llm")
async def get_llm_config() -> dict:
    """当前模型配置（key 打码回显，不回传明文）。"""
    return {
        "base_url": settings.llm_base_url,
        "api_key": _mask_key(settings.llm_api_key),
        "model_cheap": settings.llm_model_cheap,
        "model_strong": settings.llm_model_strong,
        "has_key": bool(settings.llm_api_key),
    }


@app.post("/config/llm")
async def post_llm_config(req: LlmConfigRequest) -> dict:
    """运行时更新模型配置（立即生效，不用重启）并持久化到 data/.env。"""
    updates: dict[str, str] = {}

    def _apply(attr: str, env: str, val: str) -> None:
        val = val.strip()
        if val:
            setattr(settings, attr, val)
            os.environ[env] = val  # 同进程后续 os.getenv 读取点也一致
            updates[env] = val

    _apply("llm_base_url", "ELF_LLM_BASE_URL", req.base_url)
    _apply("llm_model_cheap", "ELF_LLM_MODEL_CHEAP", req.model_cheap)
    _apply("llm_model_strong", "ELF_LLM_MODEL_STRONG", req.model_strong)
    if req.api_key.strip() and "****" not in req.api_key:
        _apply("llm_api_key", "ELF_LLM_API_KEY", req.api_key)

    if updates:
        _persist_env(updates)
        print(f"[config] LLM 配置已更新并持久化：{list(updates)}")
    return {"ok": True, "changed": list(updates)}


# ---- Review 管线（docs/03 §3.5；docs/05 §7.4） ----
@app.post("/review/nightly")
async def run_review_nightly() -> dict:
    """手动触发夜间复盘（验收/调试用；cron 调度在 lifespan 里）。"""
    return await review.nightly(trigger="manual")


@app.post("/review/weekly")
async def run_review_weekly() -> dict:
    """手动触发每周复盘（校准边界重建/技能归档/成长汇报数据）。"""
    return await review.weekly(trigger="manual")


@app.get("/review/latest")
async def get_review_latest() -> dict:
    """最近一次的 Review 报告（命中率环比/调参记录/token 成本可查）。"""
    out: dict = {}
    for kind in ("nightly", "weekly"):
        files = sorted(review.reports_dir.glob(f"{kind}-*.json")) if review.reports_dir.exists() else []
        if files:
            out[kind] = json.loads(files[-1].read_text(encoding="utf-8"))
    return out or {"exists": False}


# ---- 晨报/精灵日记/成长汇报（docs/03 §1.5；docs/05 §7.5） ----
@app.get("/journal")
async def get_journal(date: str | None = None) -> list[dict]:
    """精灵日记（第一人称，每日一条）；date 缺省=今天。"""
    return digest.journal_entries(date)


@app.get("/weekly-report")
async def get_weekly_report(week: str | None = None) -> dict:
    """成长汇报（记忆 diff 人格化）；week 缺省=最新。"""
    r = digest.weekly_report(week)
    return r or {"exists": False}


@app.post("/digest/morning")
async def gen_morning() -> dict:
    """手动生成今日晨报（调试验收用；正常由 8:05 cron / 启动补生成产出）。"""
    return await digest.generate_morning()


# ---- 精灵档案（onboarding；docs/03 §2.5） ----
class Profile(BaseModel):
    """shell 引导页提交：精灵名字、捏脸参数（docs/05 §1.4）、用户兴趣、自选内容源。"""

    elf_name: str
    sprite_config: dict | None = None  # {color, eyes, accessory, size}
    interests: list[dict]  # [{name, desc}]
    feed_urls: list[str] = []  # 自选内容源（§3.4）；空=用默认 8 源


def _profile_path() -> Path:
    return Path(settings.data_dir) / "profile.json"


@app.get("/profile")
async def get_profile() -> dict:
    """shell 启动时查询：无档案则进入首次引导；§5.2 设置面板取全量（含兴趣）。"""
    p = _profile_path()
    if not p.exists():
        return {"exists": False}
    data = json.loads(p.read_text(encoding="utf-8"))
    interests = Path(settings.data_dir) / "interests.json"
    if interests.exists():
        data["interests"] = json.loads(interests.read_text(encoding="utf-8"))
    sources = Path(settings.data_dir) / "sources.json"
    if sources.exists():
        data["feed_urls"] = json.loads(sources.read_text(encoding="utf-8"))
    return {"exists": True, **data}


# ---- 清空重来（docs/05 §5.2 设置面板：抹掉全部学习/档案数据，回首次引导） ----
# 保留：.env（LLM key）、SOUL.md（用户手改的人格）、voice/（30MB 模型，重下代价大）
_RESET_FILES = [
    "event.db", "graph.db", "profile.db", "persona.db", "digest.db", "reminders.db",
    "relationship.json", "interests.json", "sources.json", "profile.json", "coefficients.yaml",
]
_RESET_DIRS = ["reports"]  # Review 报告也是学习数据，一并清空


async def _wipe_and_exit() -> None:
    """先让响应回去，再关库抹文件退进程；壳检测到退出码 0 自动拉起（生产模式）。"""
    await asyncio.sleep(0.5)
    store.close()
    graph.close()
    profile_service.close()
    persona.close()
    digest.close()
    for name in _RESET_FILES:
        p = Path(settings.data_dir) / name
        p.unlink(missing_ok=True)
        for side in (p.with_suffix(p.suffix + "-wal"), p.with_suffix(p.suffix + "-shm")):
            side.unlink(missing_ok=True)
    for name in _RESET_DIRS:
        shutil.rmtree(Path(settings.data_dir) / name, ignore_errors=True)
    print("[reset] 数据已清空，进程退出待重启")
    os._exit(0)


@app.post("/reset")
async def post_reset() -> dict:
    asyncio.create_task(_wipe_and_exit())
    return {"ok": True}


@app.post("/profile")
async def post_profile(profile: Profile) -> dict:
    """落档案+兴趣，后台立刻跑一轮管线让兴趣生效（打标约 2 分钟，不阻塞响应）。"""
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    _profile_path().write_text(
        json.dumps(
            profile.model_dump(exclude={"interests", "feed_urls"}), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    (Path(settings.data_dir) / "interests.json").write_text(
        json.dumps(profile.interests, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    pipeline.reload_interests()
    if profile.feed_urls:  # 自选内容源（§3.4）：用户数据独立落 sources.json
        (Path(settings.data_dir) / "sources.json").write_text(
            json.dumps(profile.feed_urls, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        pipeline.reload_sources()
    else:  # 空数组=回默认源：删掉用户文件，reload 落回 config 默认表
        user_sources = Path(settings.data_dir) / "sources.json"
        if user_sources.exists():
            user_sources.unlink()
            pipeline.reload_sources()
    graph.sync_explicit_interests(pipeline.interest_names())  # 改兴趣 → 新节点播种/升级 explicit
    store.append(
        event_id=f"evt_{uuid.uuid4().hex[:12]}",
        ts=int(time.time() * 1000),
        type="profile.onboarded",
        source="shell",
        payload={"elf_name": profile.elf_name, "interests": [i["name"] for i in profile.interests]},
    )

    async def _refresh() -> None:
        try:
            await pipeline.run_once()
        except Exception as e:
            print(f"[profile] 首轮管线失败: {e}")

    asyncio.create_task(_refresh())
    return {"ok": True}


# ---- P1-6/8 久别回归：精灵明信片（≥12h）/ 攒着的话（≥48h 一次性交付） ----
_LAST_ONLINE: dict = {"ts": 0.0}


async def _postcard_deliver(absence_s: float) -> None:
    try:
        d = await digest.generate_postcard(absence_days=max(1, int(absence_s // 86400)))
        if not d:
            return
        ok = await ws_manager.push(
            {"type": "digest", "id": d["id"], "payload": {"kind": "postcard", "title": d["title"], "text": d["text"]}}
        )
        if ok:
            digest.mark_offered(d["id"])
            print(f"[postcard] 久别回归送出：{d['title']}")
    except Exception as e:
        print(f"[postcard] {e}")


async def _banked_deliver() -> None:
    """攒着的话：离线期间没递出去的一次打包（单独一条就单独递，不硬凑 bundle）。"""
    try:
        msgs = digest.banked_messages()
        if not msgs:
            return
        if len(msgs) == 1:
            d = msgs[0]
            ok = await ws_manager.push(
                {"type": "digest", "id": d["id"], "payload": {"kind": d["kind"], "title": d["title"], "text": d["text"]}}
            )
            if ok:
                digest.mark_offered(d["id"])
            return
        lines = "\n".join(f"{i+1}. {m['text']}" for i, m in enumerate(msgs))
        ok = await ws_manager.push(
            {
                "type": "digest",
                "id": f"bank_{uuid.uuid4().hex[:12]}",
                "payload": {"kind": "banked", "title": "📬 攒着的话", "text": f"你不在的时候我一直替你盯着，攒了几句话：\n{lines}"},
            }
        )
        if ok:
            for m in msgs:
                digest.mark_offered(m["id"])
            print(f"[banked] 久别回归一次交付 {len(msgs)} 条")
    except Exception as e:
        print(f"[banked] {e}")


@app.websocket("/ws/shell")
async def ws_shell(ws: WebSocket) -> None:
    """脑→壳下行通道（docs/03 §2.3）。"""
    await ws.accept()
    ws_manager.conn = ws
    # §8.3：壳（重）连即同步当前情绪——brain 重启/壳重连后姿态不丢
    await ws_manager.push({"type": "mood.set", "payload": {"mood": _current_mood}})
    # §9.4：重发壳掉线期间未 ack 的 SpeakCommand（shell 按 decision_id 去重）
    await ws_manager.resend_pending()
    # 久别回归：≥48h 攒着的话一次交付；≥12h 精灵明信片（两者互斥，攒话优先）
    now = time.time()
    absence = now - _LAST_ONLINE["ts"] if _LAST_ONLINE["ts"] else 0.0
    _LAST_ONLINE["ts"] = now
    if absence >= 48 * 3600:
        asyncio.create_task(_banked_deliver())
    elif absence >= 12 * 3600:
        asyncio.create_task(_postcard_deliver(absence))
    try:
        while True:
            await ws.receive_text()  # P0 壳不上行 WS 消息，仅保活
    except WebSocketDisconnect:
        ws_manager.conn = None
