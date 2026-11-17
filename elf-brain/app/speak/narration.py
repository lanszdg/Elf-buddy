"""人格化讲述生成（docs/05 §8.1；docs/01 §M1 讲述生成；docs/02 §5.2 Narration）。

L3 决策后、推送前生成 hook+script_short，缓存进 CandidateEntry（item["narration"]）：
- 生成前按内容实体检索立场库注入（一致性：上周吐槽过的导演这周不会夸）
- 产出立场入库（StanceEntry，只做加法），供后续生成校验
- 失败/无 key → None，气泡回退 summary 展示，闭环不破

模型用 cheap（偏差记录 docs/05 §8.1：强模型 v4-pro 实测 65s 不可用于 L3 实时路径，
chat ~5s；L3 日配额 ≤6 次，成本可忽略；强模型保留给 /chat 长文本）。
"""

from app.core import llm
from app.core.config import Settings
from app.growth.persona import PersonaService


class NarrationService:
    def __init__(self, persona: PersonaService, settings: Settings) -> None:
        self.persona = persona
        self.settings = settings
        self.skills = None  # main.py 注入 SkillStore（docs/02 §6.1 技能消费：讲述风格经验）

    async def generate(self, item: dict, decision_id: str) -> dict | None:
        """为 L3 候选生成讲述；成功返回 Narration 并把新立场入库，失败 None。"""
        if not self.settings.llm_api_key:
            return None
        # 生成前检索立场记忆库（docs/02 §5.2 stance_check_passed 的输入）
        entities = [str(e) for e in item.get("entities", [])][:5]
        history = self.persona.stances_for(entities)
        stance_line = ""
        if history:
            lines = "；".join(f"对「{h['topic_entity']}」你说过：{h['stance']}" for h in history)
            stance_line = (
                f"你以前发表过的看法：{lines}。\n"
                "保持前后一致；若确实要改变态度，必须在稿子里点明转变的原因。\n\n"
            )
        # 技能消费（docs/02 §6.1 narration_style）：沉淀的讲述经验注入 prompt；
        # skills_used 随返回值走 cmd payload，由 renderer 按反馈结算归因 touch
        skill_line = ""
        skills_used: list[str] = []
        if self.skills is not None:
            top = self.skills.best("narration_style", 2)
            if top:
                lines = "；".join(f"{s['content'][:100]}" for s in top)
                skill_line = f"你沉淀的讲述经验（遵循但不要在稿子里复述经验原文）：{lines}。\n\n"
                skills_used = [s["name"] for s in top]
        prompt = (
            f"{stance_line}{skill_line}"
            f"内容标题：{item.get('title', '')}\n"
            f"内容摘要：{item.get('summary', '')[:300]}\n"
            f"推荐它的理由：{item.get('matched_reason', '')}\n\n"
            "以你的性格（见 system）写这段讲述：\n"
            "- hook：一句开场白（≤20字），像朋友拍肩膀一样自然，不用敬语\n"
            "- script_short：≤90秒口语播报稿（150~220字），带你的立场和反应，"
            "必须忠于标题摘要的事实、禁止编造未提及的信息\n"
            "- stance_check_passed：本稿是否与给你的历史立场一致（无历史立场则为 true）\n"
            "- stances：本稿对关键实体表达的看法（0~3条，供入库；无明确看法给空数组）\n\n"
            '输出 JSON：{"hook":"...","script_short":"...","stance_check_passed":true,'
            '"stances":[{"topic_entity":"...","stance":"..."}]}'
        )
        try:
            data = await llm.chat(
                prompt,
                system=f"{self.persona.soul}\n\n你是上面这只小精灵，只输出严格 JSON。",
                model=self.settings.llm_model_cheap,  # 偏差：强模型→cheap（实时路径延迟敏感）
                timeout=20,
                max_tokens=1200,
            )
        except llm.LLMUnavailable:
            return None
        hook = str(data.get("hook", "")).strip()
        script = str(data.get("script_short", "")).strip()
        if not hook or not script:
            return None
        # 立场入库（docs/02 §6.2：发表过的看法入库，供后续一致性校验）
        stances = data.get("stances")
        if isinstance(stances, list) and stances:
            self.persona.add_stances(stances[:3], source_decision_id=decision_id)
        return {
            "hook": hook[:40],
            "script_short": script[:400],
            "stance_check_passed": bool(data.get("stance_check_passed", True)),
            "skills_used": skills_used,  # renderer 归因记账（docs/02 §6.1）
        }
