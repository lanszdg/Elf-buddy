"""Review 管线主流程（docs/01 §M8；接口 docs/03 §3.5；实现规格 docs/05 §7.4）。

nightly（quiet_hours 开始后，APScheduler cron）：
  1. 样本集 D = decision.made ⋈ speak.feedback_resolved（按 prediction_error 降序消费）
  2. 校准维护（docs/04 §1.3）：cold_start 且 |D|≥100 → 切 steady 首次建箱；steady → 当日新样本 EMA
  3. LLM 复盘（便宜模型——M8 成本红线）改写 coefficients.yaml 调参；
     token 预算硬上限，超限/无 key 自动降级为仅统计不调模型
  4. 记忆巩固：温度全量重算
  5. 命中率环比记账（验收：可查）
weekly（周日晚）：
  校准边界全量重建、技能 Curator 归档、成长汇报数据（记忆 diff 人格化素材）、token 成本周报
  ——汇报由精灵亲口说是 §7.5 的事，这里产出数据。
"""

import json
import math
import time
import uuid
from pathlib import Path

from app.core import llm
from app.core.config import Settings
from app.core.llm import LLMUnavailable
from app.memory.event_store import EventStore
from app.memory.interest_graph import InterestGraph
from app.memory.profile import UserProfileService
from app.review import calibration, coeff_writer
from app.review.skills import SkillStore

TOP_ERRORS_FOR_LLM = 8  # 每晚喂给复盘的高误差样本上限
MAX_ADJUSTMENTS_PER_NIGHT = 3  # 每晚最多落几条调参（防震荡）
MAX_SKILLS_PER_WEEK = 2  # 每周最多沉淀几条技能（防技能库膨胀）
MIN_SAMPLES_FOR_SKILLS = 10  # 技能沉淀所需的最少本周反馈样本
REPLAY_MAX_CASES = 8  # 每条候选技能回放的最大样本数（M8 成本红线）

# LLM 可调皮点路径白名单与钳制（防复盘漂移；阈值另行保序 L1<L2<L3）
_ADJUST_RULES = {
    "state_factor": {"max_delta": 0.1, "min": 0.0, "max": 1.0},
    "timing_factor": {"max_delta": 0.1, "min": 0.0, "max": 1.0},
    "channel_threshold": {"max_delta": 0.05, "min": 0.05, "max": 0.95},
    "daily_l3_quota": {"max_delta": 1.0, "min": 1.0, "max": 6.0},
    "disturbance_step": {"max_delta": 0.05, "min": 0.0, "max": 0.5},
}


class ReviewPipeline:
    def __init__(
        self,
        *,
        store: EventStore,
        graph: InterestGraph,
        profile: UserProfileService,
        timing,  # TimingEngine（避免循环 import，结构化类型）
        settings: Settings,
        account=None,  # RelationshipAccount（成长汇报素材，可选）
    ) -> None:
        self.store = store
        self.graph = graph
        self.profile = profile
        self.timing = timing
        self.settings = settings
        self.account = account
        self.skills = SkillStore(str(Path(settings.data_dir) / "skills"))
        self.reports_dir = Path(settings.data_dir) / "reports"
        self.ws = None  # main.py 注入（review.done 下行，docs/03 §2.3）
        self.digest = None  # main.py 注入 DigestService（§7.5 日记/周报生成）

    # ============ nightly ============
    async def nightly(self, trigger: str = "cron") -> dict:
        started = time.time()
        report: dict = {"kind": "nightly", "trigger": trigger, "started_ts": started}
        samples = self._samples()  # 已按 prediction_error 降序
        report["samples_total"] = len(samples)
        report["top_errors"] = [
            {k: s[k] for k in ("decision_id", "score", "expected", "actual", "prediction_error", "user_state")}
            for s in samples[:5]
        ]

        norm = json.loads(json.dumps(self.timing.coeff.get("normalization", {})))
        norm.setdefault("phase", "cold_start")
        norm.setdefault("min_samples", 100)
        norm.setdefault("version", 1)
        coeff_dirty = False

        # 1) 校准维护（docs/04 §1.3）
        pairs = [(s["score"], s["actual"]) for s in samples]
        if norm["phase"] == "cold_start":
            if len(pairs) >= norm["min_samples"]:
                norm["phase"] = "steady"
                norm["bins"] = calibration.build_bins(pairs, prev=None)
                norm["version"] += 1
                report["phase_switch"] = "cold_start→steady"
                coeff_dirty = True
        else:
            since = self._watermark().get("nightly_ts") or (started - 86400)
            new_pairs = [(s["score"], s["actual"]) for s in samples if s["resolved_ts"] > since]
            report["ema_new_samples"] = len(new_pairs)
            if new_pairs and norm.get("bins"):
                norm["bins"] = calibration.update_rates(new_pairs, norm["bins"])
                norm["version"] += 1
                coeff_dirty = True

        # 2) LLM 复盘（预算硬护栏；失败/超限 → 仅统计不调模型）
        reflect = await self._reflect_guarded(samples[:TOP_ERRORS_FOR_LLM], samples, report)
        applied, skipped = self._apply_adjustments(reflect.get("adjustments", [])) if reflect else ([], [])
        if reflect:
            report["llm_notes"] = str(reflect.get("notes", ""))[:300]
        report["adjustments_applied"] = applied
        report["adjustments_skipped"] = skipped

        # 3) 记忆巩固：温度全量重算（paused 时态边随之自动开闭）
        self.graph.recompute_temperatures()

        # 4) 落盘 + 引擎热重载
        if coeff_dirty or applied:
            coeff_writer.rewrite_normalization(self.settings.coeff_path, norm)
            self.timing.reload_coeff()

        # 5) 命中率环比（验收：可查）
        report.update(self._hit_rate_wow(samples, days=7))
        report["finished_ts"] = time.time()
        await self._finish(report)
        self._save_watermark({"nightly_ts": started})
        # 6) 精灵日记（docs/05 §7.5：nightly 后生成，随次日晨报主动给出）
        if self.digest is not None:
            try:
                await self.digest.generate_journal(now=started)
            except Exception as e:
                print(f"[digest] 日记生成失败: {e}")
        return report

    # ============ weekly ============
    async def weekly(self, trigger: str = "cron") -> dict:
        started = time.time()
        report: dict = {"kind": "weekly", "trigger": trigger, "started_ts": started}
        samples = self._samples()
        report["samples_total"] = len(samples)

        # 1) 校准边界全量重建（docs/04 §1.3 weekly：score 分布随系数学习漂移，边界低频跟随）
        norm = json.loads(json.dumps(self.timing.coeff.get("normalization", {})))
        if norm.get("phase") == "steady" and samples:
            norm["bins"] = calibration.build_bins(
                [(s["score"], s["actual"]) for s in samples], prev=norm.get("bins")
            )
            norm["version"] = int(norm.get("version", 1)) + 1
            coeff_writer.rewrite_normalization(self.settings.coeff_path, norm)
            self.timing.reload_coeff()
            report["bins_rebuilt"] = True

        # 2) 温度全量重算 + 技能 Curator 归档 + 技能沉淀（生成侧：旧技能先归档再学新的）
        self.graph.recompute_temperatures()
        report["skills"] = self.skills.curate(now=started)
        report["skills_learned"] = await self._learn_skills(samples, started)

        # 3) 成长汇报数据（记忆 diff；§7.5 由精灵亲口说）
        report["growth"] = self._weekly_growth(started)

        # 4) 命中率环比 + token 成本周报
        report.update(self._hit_rate_wow(samples, days=7))
        usage = llm.usage_totals()
        prev = self._watermark().get("token_week") or {}
        report["token_week"] = {
            "total_tokens": usage["total_tokens"] - int(prev.get("total_tokens", 0)),
            "calls": usage["calls"] - int(prev.get("calls", 0)),
        }
        report["finished_ts"] = time.time()
        await self._finish(report)
        self._save_watermark({"nightly_ts": started, "token_week": usage})
        # 5) 成长汇报人格化（docs/05 §7.5：weekly 后生成，用户在场时主动说）
        if self.digest is not None:
            try:
                await self.digest.generate_weekly(report, now=started)
            except Exception as e:
                print(f"[digest] 周报生成失败: {e}")
            # 5b) 兴趣进展播报（消费 chasing 边：本周各兴趣 episode 动态盘点）
            try:
                await self.digest.generate_progress(now=started)
            except Exception as e:
                print(f"[digest] 兴趣进展播报生成失败: {e}")
            # 5c) 我的成长（杀手C：反馈结算→提炼成长笔记→写回人格→digest 宣告）
            try:
                await self.digest.generate_self_growth(report, now=started)
            except Exception as e:
                print(f"[digest] 我的成长生成失败: {e}")
            # 5d) 共同里程碑（批5：认识第 N 天 / 第 N 次聊 X，阈值跨越当周宣告）
            try:
                await self.digest.generate_milestone(now=started)
            except Exception as e:
                print(f"[digest] 里程碑生成失败: {e}")
        return report

    # ============ 样本与统计 ============
    def _samples(self) -> list[dict]:
        """D = decision.made ⋈ feedback_resolved，按 prediction_error 降序（消费顺序，docs/03 §3.5）。"""
        decisions = {}
        for e in self.store.query("decision.made", limit=100000):
            p = e["payload"]
            if "decision_id" in p:
                decisions[p["decision_id"]] = p
        out = []
        for e in self.store.query("speak.feedback_resolved", limit=100000):
            p = e["payload"]
            d = decisions.get(p.get("decision_id", ""))
            if not d or p.get("actual_value") is None:
                continue
            expected = p.get("expected_value")
            actual = float(p["actual_value"])
            pe = p.get("prediction_error")
            out.append(
                {
                    "decision_id": p["decision_id"],
                    "score": float(d.get("score", 0)),
                    "expected": expected,
                    "actual": actual,
                    "prediction_error": (
                        float(pe) if pe is not None else abs((expected or 0) - actual)
                    ),
                    "user_state": d.get("user_state", ""),
                    "content_id": d.get("content_id", ""),
                    "resolved_ts": e["ts"] / 1000,
                }
            )
        out.sort(key=lambda x: x["prediction_error"], reverse=True)
        return out

    @staticmethod
    def _hit_rate_wow(samples: list[dict], days: int) -> dict:
        """命中率环比：近 N 天 vs 前 N 天（hit = actual≥0.5，与关系账户同口径）。"""
        now = time.time()
        cur = [s for s in samples if s["resolved_ts"] > now - days * 86400]
        prev = [s for s in samples if now - 2 * days * 86400 < s["resolved_ts"] <= now - days * 86400]

        def rate(xs: list[dict]) -> float | None:
            return round(sum(1 for s in xs if s["actual"] >= 0.5) / len(xs), 4) if xs else None

        return {
            f"hit_rate_{days}d": rate(cur),
            f"hit_rate_prev_{days}d": rate(prev),
            f"resolved_{days}d": len(cur),
        }

    def _weekly_growth(self, now: float) -> dict:
        """成长汇报素材：本周记忆 diff（新事实/纠偏/新兴趣/episode 分布）+ 画像与关系快照。"""
        week_ago = now - 7 * 86400
        facts = self.graph.facts_since(week_ago)
        growth = {
            "new_facts": len(facts),
            "corrections": sum(1 for f in facts if f["created_by"] == "user_correction"),
            "new_interests": self.graph.nodes_since(week_ago),
            "episodes": self.graph.episode_stats_since(week_ago),
            "profile_slots": {s["key"]: s["value"] for s in self.profile.get_slots()},
        }
        if self.account is not None:
            snap = self.account.snapshot()
            growth["relationship"] = {
                "level": snap["level"],
                "xp": snap["xp"],
                "hit_rate": snap["hit_rate"],
            }
        return growth

    # ============ 技能沉淀（生成侧：weekly 从反馈样本提炼策略技能，docs/01 技能复盘） ============
    async def _learn_skills(self, samples: list[dict], started: float) -> dict:
        """从本周反馈样本提炼选题/讲述策略技能落盘（M8 成本红线：cheap 模型 + 预算护栏）。

        与 nightly 数值调参分工：coefficients.yaml 调数字，技能库沉淀文字策略；
        红线⑤——只从样本明说的事实归纳模式，禁止脑补用户喜好。
        失败/无 key/超预算 → 静默降级，weekly 主流程不受影响。
        提炼出的候选技能先经 _replay_validate 回放验证（用本周真实样本回放「当时应用它
        会更好还是更差」，EnvRigger 式验证环，docs/05 §11.33），验证不过/验证不了都不上岗。
        """
        week = [s for s in samples if s["resolved_ts"] >= started - 7 * 86400]
        if len(week) < MIN_SAMPLES_FOR_SKILLS:
            return {"status": "skipped", "reason": f"本周样本 {len(week)}<{MIN_SAMPLES_FOR_SKILLS}"}
        if not self.settings.llm_api_key:
            return {"status": "degraded", "reason": "无 API key"}
        hits = [s for s in week if s["actual"] >= 0.75][:5]
        misses = sorted(week, key=lambda s: s["prediction_error"], reverse=True)[:5]

        def _fmt(xs: list[dict]) -> str:
            return "\n".join(
                f"- 状态={s['user_state']} 预期={s['expected']} 实际={s['actual']} 误差={s['prediction_error']:.2f}"
                for s in xs
            ) or "（无）"

        hit_rate = sum(1 for s in week if s["actual"] >= 0.5) / len(week)
        prompt = (
            f"过去一周 {len(week)} 条开口反馈，命中率 {hit_rate:.0%}。\n\n"
            f"被喜欢（实际≥0.75）的样本：\n{_fmt(hits)}\n\n"
            f"误差最大的样本：\n{_fmt(misses)}\n\n"
            "从这些样本里归纳可复用的策略经验，写成技能：\n"
            "- kind 只能是 topic_selection（什么话题/时机更容易被接受）或 "
            "narration_style（怎么讲述更讨喜）；timing 数值调参不归你管\n"
            f"- 最多 {MAX_SKILLS_PER_WEEK} 条；name 用 snake_case 短名（如 weekend-evening-tech）；"
            "content ≤80 字说清做法与适用场景；score 0~1 表证据强度\n"
            "- 只归纳样本里看得见的模式，禁止脑补；看不出就返回空数组\n\n"
            '输出：{"skills":[{"kind":"topic_selection","name":"...","content":"...","score":0.7}]}'
        )
        est = len(prompt) // 2 + 900  # 输入粗估 + 输出预留
        if est > self.settings.review_token_budget:
            return {"status": "degraded", "reason": f"预算护栏：估算 {est}>{self.settings.review_token_budget}"}
        try:
            data = await llm.chat(
                prompt,
                system="你是技能提炼器，只输出严格 JSON。",
                model=self.settings.llm_model_cheap,  # M8 成本红线
                timeout=60,
            )
        except LLMUnavailable as e:
            return {"status": "degraded", "reason": f"LLM 不可用：{e}"}
        learned = []
        rejected = []
        # 回放样本池 = 提炼所用的同一批证据（命中+大误差，去重），EnvRigger 式验证（docs/05 §11.33）
        pool: list[dict] = []
        seen: set[int] = set()
        for c in hits + misses:
            if id(c) not in seen:
                seen.add(id(c))
                pool.append(c)
        pool = pool[:REPLAY_MAX_CASES]
        for sk in (data.get("skills") or [])[:MAX_SKILLS_PER_WEEK]:
            if not isinstance(sk, dict):
                continue
            kind = str(sk.get("kind", "")).strip()
            name = str(sk.get("name", "")).strip().lower()
            content = str(sk.get("content", ""))
            ok, why = await self._replay_validate(
                {"kind": kind, "name": name, "content": content}, pool
            )
            if not ok:
                rejected.append({"name": name, "reason": why})
                continue
            if self.skills.add_skill(
                kind,
                name,
                content,
                float(sk.get("score", 0.5) or 0.5),
                evidence_events=len(week),
                now=started,
            ):
                learned.append(name)
        return {"status": "ok", "learned": learned, "rejected": rejected}

    async def _replay_validate(self, skill: dict, cases: list[dict]) -> tuple[bool, str]:
        """回放验证（docs/05 §11.33，借鉴 EnvHarness 的 EnvRigger 验证环）：

        用本周真实反馈样本逐条回放「当时应用这条技能，结果会比实际更好还是更差」，
        worse 多于 better → 拒绝上岗；验证不了（LLM 挂了/超预算/结果无法解析）
        同样不上岗——保险丝失败闭合，宁可这周不学也不能让没验证的策略直接生效。
        """
        listing = "\n".join(
            f"{i + 1}. 状态={c['user_state']} 预期={c['expected']:.2f} 实际={c['actual']:.2f}"
            f"（{'被喜欢' if c['actual'] >= 0.75 else '被拒绝' if c['actual'] < 0.5 else '反应一般'}）"
            for i, c in enumerate(cases)
        )
        prompt = (
            f"刚提炼出一条精灵技能（{skill['kind']}·{skill['name']}）：{skill['content']}\n\n"
            "用下面本周真实反馈样本逐条回放：第 i 条样本的情境下，如果当时应用这条技能，"
            "结果会比实际更好还是更差？\n"
            f"{listing}\n\n"
            '按 JSON 输出：{"verdicts":[{"case":1,"result":"better|worse|same"}]}'
        )
        est = len(prompt) // 2 + 300  # 输入粗估 + 输出预留
        if est > self.settings.review_token_budget:
            return False, f"回放超预算（{est}>{self.settings.review_token_budget}）"
        try:
            data = await llm.chat(
                prompt,
                system="你是技能审核员，只依据样本明说的事实判断，只输出严格 JSON。",
                model=self.settings.llm_model_cheap,  # M8 成本红线
                timeout=60,
            )
        except LLMUnavailable as e:
            return False, f"回放验证不可用：{e}"
        verdicts = data.get("verdicts")
        if not isinstance(verdicts, list):
            return False, "回放结果无法解析"
        better = sum(1 for v in verdicts if isinstance(v, dict) and v.get("result") == "better")
        worse = sum(1 for v in verdicts if isinstance(v, dict) and v.get("result") == "worse")
        if worse > better:
            return False, f"回放否决（worse {worse} > better {better}）"
        return True, f"回放通过（better {better} / worse {worse}）"

    # ============ LLM 复盘（预算护栏） ============
    async def _reflect_guarded(self, top: list[dict], all_samples: list[dict], report: dict) -> dict | None:
        if not top:
            report["llm"] = {"status": "skipped", "reason": "无反馈样本"}
            return None
        if not self.settings.llm_api_key:
            report["llm"] = {"status": "degraded", "reason": "无 API key，仅统计"}
            return None
        prompt = self._reflect_prompt(top, all_samples)
        est = len(prompt) // 2 + 800  # 输入粗估 + 输出预留
        budget = self.settings.review_token_budget
        if est > budget:
            report["llm"] = {
                "status": "degraded",
                "reason": f"预算护栏：估算 {est} > 预算 {budget}，仅统计不调模型",
            }
            return None
        u0 = llm.usage_totals()["total_tokens"]
        try:
            data = await llm.chat(
                prompt,
                system="你是推荐系统的夜间调参器，只输出严格 JSON。",
                model=self.settings.llm_model_cheap,  # M8 成本红线：复盘必须便宜模型
                timeout=60,
            )
        except LLMUnavailable as e:
            report["llm"] = {"status": "degraded", "reason": f"LLM 不可用：{e}，仅统计"}
            return None
        used = llm.usage_totals()["total_tokens"] - u0
        report["llm"] = {"status": "ok", "token_used": used, "budget": budget}
        return data

    def _reflect_prompt(self, top: list[dict], all_samples: list[dict]) -> str:
        c = self.timing.coeff
        tunables = {
            "state_factor": c.get("state_factor"),
            "timing_factor": c.get("timing_factor"),
            "channel_threshold": c.get("channel_threshold"),
            "daily_l3_quota": c.get("daily_l3_quota"),
            "disturbance_step": c.get("disturbance_step"),
        }
        # 状态×命中率矩阵（docs/02 §3 TimingPreference 的证据来源）
        by_state: dict[str, list[int]] = {}
        for s in all_samples:
            st = by_state.setdefault(s["user_state"] or "unknown", [0, 0])
            st[0] += 1
            st[1] += 1 if s["actual"] >= 0.5 else 0
        matrix = {k: f"{v[1]}/{v[0]}" for k, v in by_state.items()}
        errors = "\n".join(
            f"- score={s['score']:.3f} 预期={s['expected']} 实际={s['actual']} "
            f"误差={s['prediction_error']:.2f} 状态={s['user_state']}"
            for s in top
        )
        return (
            f"当前系数：{json.dumps(tunables, ensure_ascii=False)}\n"
            f"状态×命中率（命中/尝试）：{json.dumps(matrix, ensure_ascii=False)}\n\n"
            f"预测误差最大的 {len(top)} 条样本：\n{errors}\n\n"
            "分析误差模式，提出少量系数微调（delta 可正可负）。规则：\n"
            "- 只能调这些路径：state_factor.<状态> / timing_factor.<小时> / "
            "channel_threshold.L1|L2|L3 / daily_l3_quota / disturbance_step\n"
            f"- 最多 {MAX_ADJUSTMENTS_PER_NIGHT} 条，每条给一句 reason\n"
            "- 误差大且实际<预期 → 该状态/时段过度开口（降系数或升阈值）；反之太保守\n"
            "- 看不出明确模式就返回空 adjustments，别硬调\n\n"
            '输出：{"adjustments":[{"path":"state_factor.idle","delta":-0.05,"reason":"..."}],"notes":"一句话总结今晚发现"}'
        )

    # ============ 调参落地（白名单+钳制+保序） ============
    def _apply_adjustments(self, adjustments: list) -> tuple[list, list]:
        applied, skipped = [], []
        coeff = self.timing.coeff
        for adj in adjustments[:MAX_ADJUSTMENTS_PER_NIGHT]:
            try:
                path = str(adj["path"])
                delta = float(adj["delta"])
                reason = str(adj.get("reason", ""))[:120]
            except (KeyError, TypeError, ValueError):
                skipped.append({"path": str(adj), "reason": "格式非法"})
                continue
            section = path.split(".", 1)[0]
            rule = _ADJUST_RULES.get(section)
            if rule is None:
                skipped.append({"path": path, "reason": "非白名单路径"})
                continue
            if abs(delta) > rule["max_delta"]:
                delta = math.copysign(rule["max_delta"], delta)
            old = self._read_path(coeff, path)
            if old is None:
                skipped.append({"path": path, "reason": "路径不存在"})
                continue
            new = min(max(old + delta, rule["min"]), rule["max"])
            if section == "daily_l3_quota":
                new = round(new)
            if new == old:
                skipped.append({"path": path, "reason": "已达钳制边界"})
                continue
            if section == "channel_threshold":
                trial = dict(coeff["channel_threshold"])
                trial[path.split(".", 1)[1]] = new
                if not (trial["L1"] < trial["L2"] < trial["L3"]):
                    skipped.append({"path": path, "reason": "会破坏 L1<L2<L3 保序"})
                    continue
            if coeff_writer.apply_scalar(self.settings.coeff_path, path, new):
                self._write_local(coeff, path, new)  # 后续保序判断基于新值
                applied.append({"path": path, "old": old, "new": new, "reason": reason})
            else:
                skipped.append({"path": path, "reason": "yaml 定位失败"})
        return applied, skipped

    @staticmethod
    def _read_path(coeff: dict, path: str) -> float | None:
        parts = path.split(".", 1)
        try:
            node = coeff[parts[0]] if len(parts) == 1 else coeff[parts[0]][parts[1]]
            return float(node)
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _write_local(coeff: dict, path: str, value: float) -> None:
        parts = path.split(".", 1)
        if len(parts) == 1:
            coeff[parts[0]] = value
        else:
            coeff[parts[0]][parts[1]] = value

    # ============ 记账 ============
    async def _finish(self, report: dict) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        if report["kind"] == "weekly":
            iso = time.localtime(report["started_ts"])
            name = f"weekly-{iso.tm_year}-W{time.strftime('%V', iso)}.json"
        else:
            name = f"nightly-{time.strftime('%Y%m%d', time.localtime(report['started_ts']))}.json"
        (self.reports_dir / name).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.store.append(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            ts=int(time.time() * 1000),
            type="review.ran",
            source="brain",
            payload=report,
        )
        if self.ws is not None:
            await self.ws.push({"type": "review.done", "payload": {"summary": self._summary_line(report)}})

    @staticmethod
    def _summary_line(report: dict) -> str:
        hr = report.get("hit_rate_7d")
        hr_txt = f"{hr:.0%}" if isinstance(hr, float) else "暂无"
        if report["kind"] == "weekly":
            g = report.get("growth", {})
            return (
                f"周报就绪：本周新记 {g.get('new_facts', 0)} 条事实、"
                f"新兴趣 {len(g.get('new_interests', []))} 个、"
                f"技能归档 {len(report.get('skills', {}).get('archived', []))} 个，"
                f"7日命中率 {hr_txt}"
            )
        llm_info = report.get("llm", {})
        return (
            f"夜间复盘完成：{report.get('samples_total', 0)} 样本，"
            f"调参 {len(report.get('adjustments_applied', []))} 项（{llm_info.get('status', '?')}），"
            f"7日命中率 {hr_txt}"
        )

    def _watermark(self) -> dict:
        f = self.reports_dir / ".watermark.json"
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_watermark(self, updates: dict) -> None:
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        wm = self._watermark()
        wm.update(updates)
        (self.reports_dir / ".watermark.json").write_text(
            json.dumps(wm, ensure_ascii=False), encoding="utf-8"
        )
