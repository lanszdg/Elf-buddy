"""对话事实提炼器（docs/02 §2.3 memory_facts 的 created_by='extractor' 写入侧）。

闲聊回复成功后由 chat_reply fire-and-forget 调用（main.py）：把用户本轮话语送
cheap LLM，只提炼用户【明说】的、关于用户自己的持久事实写入图谱。
红线⑤：不推断不编造——说不准就返回空；LLM 异常/解析失败静默跳过，不打扰对话主链路。
成本（M8 红线）：cheap 模型单轮 <300 tokens，且仅 ≥_MIN_LEN 的闲聊才触发。
"""

import re

from app.core import llm
from app.core.config import settings

_MIN_LEN = 6        # 太短的随口接话不花提炼成本
_MAX_FACTS = 5      # 单轮提炼上限
_MAX_OBJ_LEN = 40
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

PROMPT = (
    "用户对你说：{text}\n\n"
    "只提炼这段话里用户【明确说出的】关于用户自己的持久事实"
    "（偏好/习惯/拥有物/身份/计划/忌口等）。\n"
    "规则：必须是用户明说的，禁止推断禁止脑补；没有就返回空数组，宁缺毋滥。\n"
    "predicate：snake_case 英文小写（如 likes_coffee / owns_camera / works_as）；\n"
    "object：简短小写词或原文短词组（≤4 词）。\n"
    '只输出严格 JSON：{{"facts":[{{"subject":"user","predicate":"...","object":"..."}}]}}'
)

SYSTEM = "你是事实抽取器，只输出严格 JSON，不输出任何解释。"


def _clean_predicate(p: object) -> str:
    p = str(p or "").strip().lower()
    return p if _PREDICATE_RE.match(p) else ""


def _clean_object(o: object) -> str:
    o = str(o or "").strip()
    return o if 0 < len(o) <= _MAX_OBJ_LEN else ""


async def learn_from_utterance(text: str, graph, event_ref: str = "") -> int:
    """提炼并写入原子事实，返回写入条数；任何失败静默返回 0（主链路无感）。"""
    text = (text or "").strip()
    if len(text) < _MIN_LEN:
        return 0
    try:
        r = await llm.chat(
            PROMPT.format(text=text),
            system=SYSTEM,
            model=settings.llm_model_cheap,
            timeout=30,
        )
        facts = r.get("facts")
        if not isinstance(facts, list):
            return 0
        n = 0
        for f in facts[:_MAX_FACTS]:
            if not isinstance(f, dict) or str(f.get("subject", "")).strip().lower() != "user":
                continue
            pred, obj = _clean_predicate(f.get("predicate")), _clean_object(f.get("object"))
            if not pred or not obj:
                continue
            if graph.has_active_fact("user", pred, obj):  # 重复陈述不重复开新行
                continue
            graph.upsert_fact(
                "user", pred, obj,
                created_by="extractor", event_ref=event_ref,
                preserve_user_correction=True,  # 自动提炼不得归档用户明说的事实
            )
            n += 1
        return n
    except Exception as e:
        print(f"[extractor] {e}")
        return 0
