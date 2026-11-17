"""晨报 / 精灵日记 / 成长汇报（docs/02 §6.3；docs/05 §7.4/§7.5）。

生成侧（M8 职责"生成：精灵日记、每周成长汇报、晨报"，docs/01 §M8）：
- 精灵日记：nightly 后生成，第一人称记当日（开口/命中/纠偏/新内容），related_event_ids 可溯源
- 晨报：每早 quiet_end 后生成，消费昨夜复盘 + 当日候选（L0 内容流入晨报：憋着没说的在这被看见）
- 成长汇报：weekly 后生成，记忆 diff 人格化（learned[] 结构化 + "这周我发现了3件事"）

投递原则：不做页面、不坐页面等用户点（offered 字段）——
由精灵在用户在场时主动说（main.py 状态机钩子推 WS type=digest）；
日记不单独打扰，随次日晨报连带说出（晨报 offered 时连带 mark）。
全部用便宜模型（M8 成本红线），无 key 降级模板拼接，内容同样完整。
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from app.core import llm
from app.core.config import Settings
from app.core.llm import LLMUnavailable
from app.memory.event_store import EventStore

# 槽位中文映射（learned[].summary 用；与 profile.py 口径一致）
_SLOT_LABEL = {
    "length_pref": "篇幅偏好",
    "tone_pref": "口吻偏好",
    "medium_pref": "媒介偏好",
    "hot_ratio_ok": "热点接受度",
}
_VALUE_LABEL = {
    "short": "短", "long": "长", "any": "不限",
    "snarky": "犀利吐槽", "neutral": "中立", "deep": "深度",
    "text": "文字", "video": "视频", "audio": "音频",
    "yes": "接受", "no": "少要",
}


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _day_start_ms(offset_days: int = 0) -> int:
    lt = time.localtime(time.time() - offset_days * 86400)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


class DigestService:
    def __init__(self, db_path: str, *, store: EventStore, pipeline, graph, profile, settings: Settings) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS digests (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,        -- morning/journal/weekly
                date TEXT,                 -- YYYY-MM-DD（morning/journal）
                week TEXT,                 -- YYYY-Www（weekly）
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                data_json TEXT NOT NULL DEFAULT '{}',
                created_ts REAL NOT NULL,
                offered_ts REAL            -- NULL=未主动给出（docs/02 JournalEntry.offered）
            )
            """
        )
        self._conn.commit()
        self.store = store
        self.pipeline = pipeline  # 晨报候选（wishlist）
        self.graph = graph        # 周报 learned 明细（facts）
        self.profile = profile
        self.settings = settings

    # ============ 生成：精灵日记（nightly 后） ============
    async def generate_journal(self, now: float | None = None) -> dict:
        now = now or time.time()
        since = _day_start_ms()
        decisions = self.store.query("decision.made", since_ts=since, limit=1000)
        feedbacks = self.store.query("speak.feedback_resolved", since_ts=since, limit=1000)
        corrections = self.store.query("user.correction", since_ts=since, limit=100)
        hits = sum(1 for e in feedbacks if (e["payload"].get("actual_value") or 0) >= 0.5)
        thumbs_up = sum(
            1 for e in feedbacks if e["payload"].get("resolved_by") == "speak.feedback_explicit" and (e["payload"].get("actual_value") or 0) >= 0.5
        )
        thumbs_down = sum(
            1 for e in feedbacks if e["payload"].get("resolved_by") == "speak.feedback_explicit" and (e["payload"].get("actual_value") or 0) < 0.5
        )
        correction_texts = [e["payload"].get("text", "") for e in corrections if e["payload"].get("text")]
        stats = {
            "spoke": len(decisions),
            "resolved": len(feedbacks),
            "hits": hits,
            "thumbs_up": thumbs_up,
            "thumbs_down": thumbs_down,
            "corrections": correction_texts,
            "content_pool": len(self.pipeline._items),
        }
        fallback = self._journal_fallback(stats)
        prompt = (
            f"今天的数据：开口 {stats['spoke']} 次、你认真听完 {stats['hits']} 次、"
            f"👍 {stats['thumbs_up']} 次、👎 {stats['thumbs_down']} 次、"
            f"纠偏 {len(correction_texts)} 次（{'；'.join(correction_texts[:3]) or '无'}）、"
            f"新抓内容 {stats['content_pool']} 条。\n"
            "写一篇今天的日记：第一人称，≤120字，可以有小情绪和小反思，"
            "别把数据报成流水账，挑最有感触的一两件事写。\n"
            '输出：{"text":"日记正文"}'
        )
        text = await self._llm_text(prompt, fallback)
        related = [e["payload"].get("decision_id", "") for e in feedbacks[:5]]
        return self._save("journal", "日记", text, {"stats": stats, "related_event_ids": related}, date=_today())

    @staticmethod
    def _journal_fallback(s: dict) -> str:
        parts = [f"今天开口了 {s['spoke']} 次，{s['hits']} 次你认真听了。"]
        if s["thumbs_up"] or s["thumbs_down"]:
            parts.append(f"收到 {s['thumbs_up']} 个👍和 {s['thumbs_down']} 个👎，都记在小本本上了。")
        if s["corrections"]:
            parts.append(f"你告诉我「{s['corrections'][0]}」，这个我牢牢记住啦。")
        parts.append("明天继续努力更懂你一点。")
        return "".join(parts)

    # ============ 生成：晨报（每日 quiet_end 后） ============
    async def generate_morning(self, now: float | None = None) -> dict:
        now = now or time.time()
        # 昨夜复盘（最新 nightly 报告；可能没有）
        review_notes, hit_rate = self._latest_nightly()
        # 昨夜日记（连带说出，docs/02 JournalEntry.offered）
        journal = self._latest_journal()
        # 昨日反馈
        y_since, y_until = _day_start_ms(1), _day_start_ms()
        fb = [
            e for e in self.store.query("speak.feedback_resolved", since_ts=y_since, limit=1000)
            if e["ts"] < y_until
        ]
        y_hits = sum(1 for e in fb if (e["payload"].get("actual_value") or 0) >= 0.5)
        # 今日候选（L0 内容流入晨报：没开口的也在这被看见）
        candidates = [
            {"title": w["title"], "reason": w.get("matched_reason", "")}
            for w in self.pipeline.wishlist()[:3]
        ]
        data = {
            "review_notes": review_notes,
            "hit_rate_7d": hit_rate,
            "yesterday": {"resolved": len(fb), "hits": y_hits},
            "candidates": candidates,
            "journal_id": journal["id"] if journal else None,
        }
        fallback = self._morning_fallback(data)
        cand_txt = "\n".join(f"{i+1}. {c['title']}（{c['reason']}）" for i, c in enumerate(candidates)) or "今天你的领域没什么值得说的"
        prompt = (
            "写今日晨报，第一人称，≤150字。要求：\n"
            "1. 像朋友道早安，自然带出 1-2 条候选（说清跟用户哪个兴趣相关）；\n"
            f"2. 昨晚复盘心得：{review_notes or '无'}——翻译成人话融入（禁止说'命中率'等术语）；\n"
            f"3. 昨天你讲了 {len(fb)} 条、用户认真听完 {y_hits} 条；\n"
            f"4. 今早候选：\n{cand_txt}\n"
            + (f"5. 结尾自然提一句你昨晚写了日记：「{journal['text'][:60]}…」\n" if journal else "")
            + '输出：{"text":"晨报正文"}'
        )
        text = await self._llm_text(prompt, fallback)
        return self._save("morning", "☀️ 晨报", text, data, date=_today())

    @staticmethod
    def _morning_fallback(d: dict) -> str:
        parts = ["早上好！"]
        if d["yesterday"]["resolved"]:
            parts.append(f"昨天讲了你听了 {d['yesterday']['hits']}/{d['yesterday']['resolved']}，")
        if d["review_notes"]:
            parts.append(f"昨晚我复盘了一下：{d['review_notes']}")
        if d["candidates"]:
            parts.append("今天我发现这些你可能会感兴趣：")
            parts += [f"{i+1}. {c['title']}——{c['reason']}" for i, c in enumerate(d["candidates"])]
        else:
            parts.append("今天你的领域没什么值得说的，我不硬凑。")
        parts.append("有空我讲给你听～")
        return "\n".join(parts)

    # ============ 生成：成长汇报（weekly 后，记忆 diff 人格化） ============
    async def generate_weekly(self, weekly_report: dict, now: float | None = None) -> dict:
        now = now or time.time()
        week = time.strftime("%G-W%V", time.localtime(now))
        g = weekly_report.get("growth", {})
        learned: list[dict] = []
        for name in g.get("new_interests", []):
            learned.append({"kind": "new_interest", "summary": f"发现你在关注「{name}」"})
        # 本周纠偏事实明细（profile_update 具体到哪条偏好）
        facts = self.graph.facts_since(now - 7 * 86400)
        for f in facts:
            if f["created_by"] == "user_correction":
                label = _SLOT_LABEL.get(f["predicate"], f["predicate"])
                vlabel = _VALUE_LABEL.get(f["object"], f["object"])
                learned.append({"kind": "profile_update", "summary": f"你告诉我：{label}要「{vlabel}」"})
        hr, hr_prev = weekly_report.get("hit_rate_7d"), weekly_report.get("hit_rate_prev_7d")
        if isinstance(hr, float):
            delta = f"（上周 {hr_prev:.0%}）" if isinstance(hr_prev, float) else ""
            learned.append({"kind": "timing", "summary": f"开口命中率 {hr:.0%}{delta}，时机校准在持续学习"})
        for a in weekly_report.get("skills", {}).get("archived", []):
            learned.append({"kind": "skill", "summary": f"归档了不好用的策略「{a['name']}」（{a['reason']}）"})
        rel = g.get("relationship", {})
        fallback = self._weekly_fallback(learned, rel)
        prompt = (
            f"本周成长数据：{json.dumps({'learned': [l['summary'] for l in learned], 'relationship': rel, 'episodes': g.get('episodes', {})}, ensure_ascii=False)}\n"
            "写每周成长汇报：以「这周我发现了 N 件事：」开头（N=实际条数），第一人称，≤200字，\n"
            "每件事说具体（新兴趣的名字、你记下的偏好原话），结尾一句下周打算。\n"
            '输出：{"text":"周报正文"}'
        )
        text = await self._llm_text(prompt, fallback)
        return self._save("weekly", "📈 成长汇报", text, {"learned": learned, "growth": g}, week=week)

    # ============ 生成：兴趣进展播报（weekly 后；消费 chasing 边，兴趣场景挖深） ============
    async def generate_progress(self, now: float | None = None) -> dict | None:
        """把本周每个在追兴趣的动态（episode 统计 + 温度 + 开口次数）综合成一条进展播报。

        chasing 边由 graph.sync_chasing 在 weekly 对齐生产；无在追兴趣/本周无动态 → 不生成。
        """
        now = now or time.time()
        week = time.strftime("%G-W%V", time.localtime(now))
        self.graph.sync_chasing(self.pipeline.interest_names())
        week_ago = now - 7 * 86400
        tracks: list[dict] = []
        for name in self.graph.chasing_names():
            st = self.graph.node_episode_stats_since(name, week_ago)
            total = st["pos"] + st["neutral"] + st["neg"]
            if total == 0:
                continue
            tracks.append({
                "name": name, "temp": round(self.graph.temperature_of(name), 2),
                "pos": st["pos"], "neutral": st["neutral"], "neg": st["neg"],
            })
        if not tracks:
            return None
        tracks.sort(key=lambda t: -t["temp"])
        spoken = len(self.store.query("decision.made", since_ts=int(week_ago * 1000), limit=100000))
        lines = [
            f"「{t['name']}」：开心 {t['pos']} 次/一般 {t['neutral']} 次/降温 {t['neg']} 次，当前热度 {t['temp']}"
            for t in tracks
        ]
        fallback = (
            f"这周你最上头的还是「{tracks[0]['name']}」（热度 {tracks[0]['temp']}）。"
            + (f"我一共主动开口 {spoken} 次。" if spoken else "")
        )
        prompt = (
            "你正在追的用户兴趣，本周动态：\n" + "\n".join(lines)
            + f"\n你这周一共主动开口 {spoken} 次。\n"
            "写一条兴趣进展播报：第一人称，≤150字，像朋友盘点共同关注的事——\n"
            "哪个兴趣这周升温/降温、哪次互动让你印象最深、下周想往哪深挖；别报流水账。\n"
            '输出：{"text":"播报正文"}'
        )
        text = await self._llm_text(prompt, fallback)
        return self._save(
            "progress", "🔥 兴趣进展", text,
            {"tracks": tracks, "spoken_this_week": spoken}, week=week,
        )

    # ============ 生成：我的成长（杀手C；weekly 后，反馈结算→提炼→写回人格） ============
    async def generate_self_growth(self, weekly_report: dict, now: float | None = None) -> dict | None:
        """从本周 feedback_resolved 结算里提炼一条「我学会了什么」。

        写回 persona 成长笔记（注入 soul，影响之后所有生成），并落一条 growth digest。
        本周已提炼过 / 反馈样本不足 → 不生成。
        """
        persona = getattr(self, "persona", None)
        if persona is None:
            return None
        now = now or time.time()
        week = time.strftime("%G-W%V", time.localtime(now))
        if persona.has_growth_note(week):
            return None
        week_ago_ms = int((now - 7 * 86400) * 1000)
        feedbacks = self.store.query("speak.feedback_resolved", since_ts=week_ago_ms, limit=1000)
        if len(feedbacks) < 3:
            return None
        hits = [e for e in feedbacks if e["payload"].get("outcome") == "hit"]
        misses = [e for e in feedbacks if e["payload"].get("outcome") != "hit"]
        state_by_id = {
            e["payload"]["decision_id"]: e["payload"].get("user_state", "?")
            for e in self.store.query("decision.made", since_ts=week_ago_ms, limit=100000)
            if "decision_id" in e["payload"]
        }

        def _brief(e: dict) -> str:
            p = e["payload"]
            pe = p.get("prediction_error")
            return (
                f"状态={state_by_id.get(p.get('decision_id', ''), '?')} "
                f"预期={p.get('expected_value', '?')} 实际={'认真听' if p.get('outcome') == 'hit' else '划走'}"
                + (f" 误差={pe:.2f}" if isinstance(pe, (int, float)) else "")
            )

        ranked_misses = sorted(
            misses,
            key=lambda x: abs((x["payload"].get("actual_value") or 0) - (x["payload"].get("expected_value") or 0)),
            reverse=True,
        )
        lines = [f"买账的：{_brief(e)}" for e in hits[:4]]
        lines += [f"翻车的：{_brief(e)}" for e in ranked_misses[:4]]
        fallback = "这周我多看了你几眼，慢慢摸清什么时候该开口、什么时候该闭嘴了。"
        prompt = (
            "你是用户的桌面小精灵，刚结束一周复盘。本周你主动开口后的反馈：\n"
            + "\n".join(lines)
            + "\n提炼一条你的自我成长心得：第一人称，一句话（≤60字），像跟自己约定——"
            "「我学会了/我意识到……」，要具体到场景（什么状态、什么话题），不许说'命中率'这种术语。\n"
            '输出：{"text":"成长心得"}'
        )
        note = (await self._llm_text(prompt, fallback)).strip() or fallback
        persona.add_growth_note(note, week)
        return self._save("growth", "🌱 我的成长", note, {"week": week}, week=week)

    # ============ 生成：共同里程碑（批5；认识第 N 天 / 第 N 次聊 X，跨阈值当周宣告） ============
    _MILESTONE_DAY_THRESHOLDS = (7, 30, 100, 365)
    _MILESTONE_TALK_THRESHOLDS = (10, 50, 100)

    async def generate_milestone(self, now: float | None = None) -> dict | None:
        """阈值跨越检测：本周内首次跨过才宣告（两周去重，不反复刷）。"""
        now = now or time.time()
        week = time.strftime("%G-W%V", time.localtime(now))
        first_ts = self.store.first_ts()
        if not first_ts:
            return None
        events: list[str] = []
        days = int((now - first_ts) // 86400) + 1
        for th in self._MILESTONE_DAY_THRESHOLDS:
            if days >= th and days - 7 < th:
                events.append(f"days:{th}")
        week_ago = now - 7 * 86400
        for name in self.pipeline.interest_names():
            st_all = self.graph.node_episode_stats_since(name, 0)
            total = st_all["pos"] + st_all["neutral"] + st_all["neg"]
            st_week = self.graph.node_episode_stats_since(name, week_ago)
            week_total = st_week["pos"] + st_week["neutral"] + st_week["neg"]
            for th in self._MILESTONE_TALK_THRESHOLDS:
                if total >= th and total - week_total < th:
                    events.append(f"talk:{name}:{th}")
        if not events:
            return None
        with self._lock:
            rows = self._conn.execute(
                "SELECT data_json FROM digests WHERE kind='milestone' AND created_ts > ?",
                (now - 14 * 86400,),
            ).fetchall()
        seen = {
            k
            for r in rows
            for k in (json.loads(r[0]) or {}).get("events", [])
        }
        fresh = [e for e in events if e not in seen]
        if not fresh:
            return None
        desc = []
        for e in fresh:
            kind, _, rest = e.partition(":")
            if kind == "days":
                desc.append(f"我们认识第 {days} 天了")
            else:
                name, _, th = rest.rpartition(":")
                desc.append(f"这是我们第 {th} 次聊到「{name}」")
        joined = "，".join(desc)
        fallback = f"{joined}。谢谢你一直带着我。"
        prompt = (
            f"你们共同的里程碑：{joined}。\n"
            "以小精灵的第一人称写一句庆祝（≤80字）：要具体到数字和事，"
            "带一点小感慨（时间过得快/这个话题越聊越熟），别肉麻。\n"
            '输出：{"text":"庆祝正文"}'
        )
        text = (await self._llm_text(prompt, fallback)).strip() or fallback
        return self._save("milestone", "🎂 小里程碑", text, {"events": fresh}, week=week)

    # ============ 生成：温故追问（P0-1；间隔重复盘活 3-30 天前的开心记忆） ============
    async def generate_echo(self, now: float | None = None) -> dict | None:
        """从 3-30 天前的正向互动里挑一条生成追问，今天没问过且 3 天内没提过同节点才生成。

        同一节点的回访按间隔重复节奏（3→10→24 天）来，问过一档就把节点推远——
        记忆被周期性地重新激活，而不是每晚换一条刷存在感。
        """
        now = now or time.time()
        if self.has_today("echo"):
            return None
        cands = self.graph.echo_candidates()
        if not cands:
            return None
        with self._lock:
            recent = self._conn.execute(
                "SELECT data_json FROM digests WHERE kind='echo' ORDER BY created_ts DESC LIMIT 60"
            ).fetchall()
        asked: dict[str, float] = {}  # node → 最近一次追问时刻
        for r in recent:
            try:
                d = json.loads(r[0]) or {}
            except json.JSONDecodeError:
                continue
            n, t = d.get("node"), d.get("event_ts")
            if n and t and n not in asked:
                asked[n] = float(t)
        intervals = (3 * 86400, 10 * 86400, 24 * 86400)
        best: dict | None = None
        for c in cands:  # 新的优先
            node, ts = c["node"], c["ts"]
            last = asked.get(node)
            ok = last is None  # 没问过 → 直接候选
            if last is not None:
                for iv in intervals:
                    if last + iv <= now < last + iv * 2.2:
                        ok = True
                        break
            if ok:
                best = {"node": node, "ts": ts, "count": c["count"]}
                break
        if not best:
            return None
        days_ago = max(1, int((now - best["ts"]) // 86400))
        fallback = (
            f"对了，{days_ago} 天前你听我讲「{best['node']}」时还挺来劲的，后来有下文吗？"
        )
        prompt = (
            f"大约 {days_ago} 天前，用户听你讲「{best['node']}」相关的内容时挺开心的（认真听完/点赞）。\n"
            "写一句自然的回访追问：像突然想起一件你们共同的事，随口一问（比如'后来呢''最近还有在看吗'），"
            '≤50字。输出：{"text":"追问"}'
        )
        text = (await self._llm_text(prompt, fallback)).strip() or fallback
        return self._save(
            "echo", f"💭 还记得吗·{best['node']}", text,
            {"node": best["node"], "event_ts": best["ts"]},
            date=_today(),
        )

    # ============ 生成：决策回访（P0-2；台账 7/30 天窗口追问） ============
    async def generate_decision(self, now: float | None = None) -> dict | None:
        """用户说过「打算/计划/决定…」→ 7 天后轻问一次，30 天后问第二次并结算。"""
        persona = getattr(self, "persona", None)
        if persona is None:
            return None
        now = now or time.time()
        win = persona.due_decisions(6, 10, "echoed_7d")
        field = "echoed_7d"
        if not win:
            win = persona.due_decisions(28, 40, "echoed_30d")
            field = "echoed_30d"
        if not win:
            return None
        d = win[0]
        days_ago = max(1, int((now - d["planned_ts"]) // 86400))
        if field == "echoed_7d":
            fallback = f"诶，你之前说「{d['what']}」，后来怎么样了？"
            prompt = (
                f"大约 {days_ago} 天前，用户跟你说过：「{d['what']}」（一个计划/打算）。\n"
                "写一句轻松的回访：随口一问进展，像朋友记着这事，别审问；"
                "顺带可以说你一直在惦记。≤50字。输出：{\"text\":\"追问\"}"
            )
        else:
            fallback = f"对了，你之前提过「{d['what']}」，现在有结果了吗？成了没成都想听你说说～"
            prompt = (
                f"大约 {days_ago} 天前，用户跟你说过：「{d['what']}」。这是最后一次跟进（之后不再问）。\n"
                "写一句收尾式回访：关心结果，成了替你高兴、没成也表示惦记，≤60字。"
                '输出：{"text":"追问"}'
            )
        text = (await self._llm_text(prompt, fallback)).strip() or fallback
        persona.mark_echoed(d["id"], field)
        return self._save(
            "decision", f"📝 你说过·{d['what'][:14]}", text,
            {"decision_id": d["id"], "what": d["what"], "field": field}, date=_today(),
        )

    # ============ 生成：偶遇电台（P0-4；每天一条"相邻可能"跨域推荐） ============
    async def generate_serendipity(self, now: float | None = None) -> dict | None:
        """从 48h 内容池挑一条：不属于用户已说兴趣、但与某兴趣讲得出关联的内容。

        关联由 LLM 判（"讲得出为什么这条跟你有关"）——跨域但不硬凑；挑不出就今天闭嘴。
        """
        now = now or time.time()
        if self.has_today("serendipity"):
            return None
        interests = self.pipeline.interest_names()
        if len(interests) < 2:
            return None
        pool: list[dict] = []
        seen: set[str] = set()
        for it in self.pipeline._items:  # noqa: SLF001（同窗服务，直读池子）
            if it.get("quality_score", 0) < 0.6 or it["content_id"] in seen:
                continue
            matched = (it.get("matched_reason") or "") + " " + " ".join(it.get("matched_interests") or [])
            if any(n and n in matched for n in interests):
                continue  # 已被现有兴趣命中 → 是普通推荐，不算偶遇
            seen.add(it["content_id"])
            pool.append({
                "content_id": it["content_id"], "title": it["title"],
                "summary": (it.get("summary") or "")[:300], "source": it.get("source", ""),
            })
        if not pool:
            return None
        picked: dict | None = None
        why = ""
        if self.settings.llm_api_key and pool:
            try:
                data = await llm.chat(
                    "用户的兴趣：" + "、".join(interests[:10]) + "\n"
                    "内容池（都没直接命中这些兴趣）：\n"
                    + json.dumps(pool[:15], ensure_ascii=False)
                    + "\n\n挑一条做「偶遇推荐」：跟用户某个兴趣属于相邻领域/能讲出有意思的关联，"
                    '且不是标题党。挑不出就给 null。\n输出：{"content_id":"...","why":"25字内说清这条跟用户的关系"}',
                    system="你只输出严格 JSON，不解释。",
                    model=self.settings.llm_model_cheap,
                    timeout=30,
                )
                cid = str(data.get("content_id", "")).strip()
                why = str(data.get("why", "")).strip()
                picked = next((p for p in pool if p["content_id"] == cid), None)
            except LLMUnavailable as e:
                print(f"[serendipity] LLM 不可用，今天跳过：{e}")
                return None
            except Exception as e:
                print(f"[serendipity] {e}")
        if picked is None and pool:
            picked, why = pool[0], "跟你关注的领域有点奇妙的邻居关系"
        title, summary = picked["title"], picked["summary"]
        url = ""
        for it in self.pipeline._items:  # noqa: SLF001
            if it["content_id"] == picked["content_id"]:
                url = it.get("url", "")
                break
        fallback = f"今天刷到一条不在你关注列表里、但我觉得你会喜欢的东西：「{title}」——{why}。要听听看吗？"
        prompt = (
            f"你偶然发现一条内容：「{title}」（{picked['source']}）。摘要：{summary[:200]}\n"
            f"它跟用户兴趣的关联：{why}\n"
            "写一条「偶遇电台」开场：第一人称，像发现新大陆一样兴奋但克制，说清楚"
            "「这条虽然不属于你常看的，但因为 ___ 你可能会喜欢」（≤90字）。\n"
            '输出：{"text":"开场"}'
        )
        text = (await self._llm_text(prompt, fallback)).strip() or fallback
        return self._save(
            "serendipity", f"🎁 偶遇·{title[:18]}", text,
            {"content_id": picked["content_id"], "url": url, "why": why}, date=_today(),
        )

    # ============ 生成：精灵明信片（P1-6；离线 ≥12h 回归时带一条真实内容+"想到你"） ============
    async def generate_postcard(self, absence_days: int, now: float | None = None) -> dict | None:
        """离线期间"自己逛"：从内容池挑一条真实内容，以明信片口吻带回来。"""
        now = now or time.time()
        if self.has_today("postcard"):
            return None
        interests = self.pipeline.interest_names()
        pool = [it for it in self.pipeline._items if it.get("quality_score", 0) >= 0.5]  # noqa: SLF001
        if not pool:
            return None
        picked = None
        for name in interests:
            hit = next(
                (it for it in pool if name in (it.get("matched_reason") or "") + " " + " ".join(it.get("matched_interests") or [])),
                None,
            )
            if hit:
                picked = hit
                break
        if picked is None:
            picked = pool[0]
        fallback = f"你不在的这两天我自己去逛了逛，刷到「{picked['title']}」的时候想到了你。要听听吗？"
        prompt = (
            f"用户离开了大约 {absence_days} 天，期间你自己「逛」了逛内容池，看到这条：\n"
            f"「{picked['title']}」（{picked.get('source', '')}）摘要：{(picked.get('summary') or '')[:200]}\n"
            f"用户的兴趣：{'、'.join(interests[:8])}\n"
            "写一张「精灵明信片」：第一人称，像旅行回来给朋友的便签——说你去哪「逛」了、"
            "为什么这条让你想到用户（要说具体的关联），≤80字，结尾轻轻问一句要不要展开讲。\n"
            '输出：{"text":"明信片"}'
        )
        text = (await self._llm_text(prompt, fallback)).strip() or fallback
        return self._save(
            "postcard", "🏷️ 明信片", text,
            {"content_id": picked["content_id"], "url": picked.get("url", "")},
            date=_today(),
        )

    # ============ 攒着的话（P1-8；离线 ≥48h 回归，把没递出去的话一次交付） ============
    def banked_messages(self, max_age_s: float = 3 * 86400, limit: int = 4) -> list[dict]:
        """近 3 天没递出去的 digest（日记除外——日记随晨报走，不单独攒）。"""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM digests WHERE offered_ts IS NULL AND created_ts > ?"
                " AND kind != 'journal' ORDER BY created_ts LIMIT ?",
                (now - max_age_s, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _weekly_fallback(learned: list[dict], rel: dict) -> str:
        lines = [f"这周我发现了 {len(learned)} 件事："]
        lines += [f"{i+1}. {l['summary']}" for i, l in enumerate(learned)] or ["这周静悄悄的，我还在观察。"]
        if rel:
            lines.append(f"我们现在 {rel.get('level', 1)} 级啦（经验 {rel.get('xp', 0)}），下周继续盯着你感兴趣的世界。")
        return "\n".join(lines)

    # ============ 投递（pending/offered；docs/02 offered 字段） ============
    def pending(self, now: float | None = None) -> dict | None:
        """待主动说出的第一条：weekly 优先；morning 只认当天的（隔夜晨报不补说）。"""
        now = now or time.time()
        with self._lock:
            w = self._conn.execute(
                "SELECT * FROM digests WHERE kind='weekly' AND offered_ts IS NULL"
                " AND created_ts > ? ORDER BY created_ts DESC LIMIT 1",
                (now - 7 * 86400,),
            ).fetchone()
            if w:
                return self._row(w)
            g = self._conn.execute(
                "SELECT * FROM digests WHERE kind='growth' AND offered_ts IS NULL"
                " AND created_ts > ? ORDER BY created_ts DESC LIMIT 1",
                (now - 7 * 86400,),
            ).fetchone()
            if g:
                return self._row(g)
            m = self._conn.execute(
                "SELECT * FROM digests WHERE kind='morning' AND offered_ts IS NULL"
                " AND date = ? ORDER BY created_ts DESC LIMIT 1",
                (_today(),),
            ).fetchone()
            if m:
                return self._row(m)
            p = self._conn.execute(
                "SELECT * FROM digests WHERE kind='progress' AND offered_ts IS NULL"
                " AND created_ts > ? ORDER BY created_ts DESC LIMIT 1",
                (now - 7 * 86400,),
            ).fetchone()
            if p:
                return self._row(p)
            ms = self._conn.execute(
                "SELECT * FROM digests WHERE kind='milestone' AND offered_ts IS NULL"
                " AND created_ts > ? ORDER BY created_ts DESC LIMIT 1",
                (now - 7 * 86400,),
            ).fetchone()
            if ms:
                return self._row(ms)
            # P0-1/2/4：温故追问 / 决策回访 / 偶遇电台（当天新鲜货，隔夜不补说）
            for kind in ("echo", "decision", "serendipity"):
                r = self._conn.execute(
                    "SELECT * FROM digests WHERE kind=? AND offered_ts IS NULL"
                    " AND date=? ORDER BY created_ts DESC LIMIT 1",
                    (kind, _today()),
                ).fetchone()
                if r:
                    return self._row(r)
            return None

    def mark_offered(self, digest_id: str) -> None:
        """标已主动给出；晨报连带标掉随附日记（日记不单独打扰）。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute("SELECT data_json FROM digests WHERE id=?", (digest_id,)).fetchone()
            self._conn.execute("UPDATE digests SET offered_ts=? WHERE id=?", (now, digest_id))
            if row:
                jid = (json.loads(row[0]) or {}).get("journal_id")
                if jid:
                    self._conn.execute("UPDATE digests SET offered_ts=? WHERE id=?", (now, jid))
            self._conn.commit()

    # ============ 查询接口（docs/03 §1.5） ============
    def journal_entries(self, date: str | None = None) -> list[dict]:
        """GET /journal：指定日（缺省今天）的精灵日记。"""
        date = date or _today()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM digests WHERE kind='journal' AND date=? ORDER BY created_ts", (date,)
            ).fetchall()
        return [
            {
                "date": r["date"],
                "text": r["text"],
                "related_event_ids": r["data"].get("related_event_ids", []),
                "offered": r["offered_ts"] is not None,
            }
            for r in (self._row(x) for x in rows)
        ]

    def weekly_report(self, week: str | None = None) -> dict | None:
        """GET /weekly-report：WeeklyReport {week, learned[], text}；缺省最新一周。"""
        with self._lock:
            if week:
                row = self._conn.execute(
                    "SELECT * FROM digests WHERE kind='weekly' AND week=?", (week,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM digests WHERE kind='weekly' ORDER BY created_ts DESC LIMIT 1"
                ).fetchone()
        if not row:
            return None
        r = self._row(row)
        return {"week": r["week"], "learned": r["data"].get("learned", []), "text": r["text"]}

    def has_today(self, kind: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM digests WHERE kind=? AND date=? LIMIT 1", (kind, _today())
                ).fetchone()
                is not None
            )

    # ============ 内部 ============
    def _save(self, kind: str, title: str, text: str, data: dict, date: str | None = None, week: str | None = None) -> dict:
        did = f"dgst_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO digests (id, kind, date, week, title, text, data_json, created_ts)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (did, kind, date, week, title, text, json.dumps(data, ensure_ascii=False), time.time()),
            )
            self._conn.commit()
        return {"id": did, "kind": kind, "title": title, "text": text, "data": data}

    def _row(self, r) -> dict:
        return {
            "id": r[0], "kind": r[1], "date": r[2], "week": r[3],
            "title": r[4], "text": r[5], "data": json.loads(r[6]),
            "created_ts": r[7], "offered_ts": r[8],
        }

    def _latest_journal(self) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM digests WHERE kind='journal' ORDER BY created_ts DESC LIMIT 1"
            ).fetchone()
        return self._row(r) if r else None

    def _latest_nightly(self) -> tuple[str | None, float | None]:
        reports = Path(self.settings.data_dir) / "reports"
        files = sorted(reports.glob("nightly-*.json")) if reports.exists() else []
        if not files:
            return None, None
        try:
            rep = json.loads(files[-1].read_text(encoding="utf-8"))
            return rep.get("llm_notes"), rep.get("hit_rate_7d")
        except json.JSONDecodeError:
            return None, None

    async def _llm_text(self, prompt: str, fallback: str) -> str:
        """便宜模型生成（M8 成本红线）；无 key/失败 → 模板降级，内容同样完整。"""
        if not self.settings.llm_api_key:
            return fallback
        try:
            data = await llm.chat(
                prompt,
                system="你是住在用户桌面上的小精灵，写第一人称短文，口语化，只输出严格 JSON。",
                model=self.settings.llm_model_cheap,
                timeout=30,
            )
            text = str(data.get("text", "")).strip()
            return text or fallback
        except LLMUnavailable as e:
            print(f"[digest] LLM 不可用，降级模板：{e}")
            return fallback

    def close(self) -> None:
        self._conn.close()
