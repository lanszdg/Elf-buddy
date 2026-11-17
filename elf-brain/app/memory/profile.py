"""消费画像服务（数据模型 docs/02 §3；实现规格 docs/05 §7.3）。

槽位=当前消费偏好的快照（length/tone/medium/hot_ratio），带置信度+来源+证据；
纠偏历史走 InterestGraph.memory_facts 时态归档（只做加法红线）——
槽位管"现在"，事实表管"来龙去脉"，两者由 POST /correction 同事务语义写入。

生效点：精排 prompt 注入偏好文本（"纠偏后推荐倾向可查地改变"的诚实闭环——
排序与理由随偏好变化，且 prompt 可审计）。
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

# 受控词汇表（docs/02 §3 ProfileSlot.key）；解析只输出枚举内值，不编造
SLOT_VOCAB: dict[str, list[str]] = {
    "length_pref": ["short", "long", "any"],
    "tone_pref": ["snarky", "neutral", "deep"],
    "medium_pref": ["text", "video", "audio", "any"],
    "hot_ratio_ok": ["yes", "no"],
}

# 展示与 prompt 注入用的中文映射
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


class UserProfileService:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profile_slots (
                key TEXT PRIMARY KEY,       -- length_pref/tone_pref/medium_pref/hot_ratio_ok
                value TEXT NOT NULL,
                confidence REAL NOT NULL,
                source TEXT NOT NULL,        -- explicit/inferred/corrected
                evidence_event_ids TEXT NOT NULL DEFAULT '[]',
                updated_ts REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    # ---- 写（correction / 7.4 Review 推断） ----
    def set_slot(self, key: str, value: str, source: str, evidence: list[str], confidence: float = 1.0) -> bool:
        """upsert 当前值；非法槽位/值拒绝（受控词汇表，防 LLM 解析漂移）。"""
        if key not in SLOT_VOCAB or value not in SLOT_VOCAB[key]:
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO profile_slots (key, value, confidence, source, evidence_event_ids, updated_ts)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, confidence=excluded.confidence,"
                " source=excluded.source, evidence_event_ids=excluded.evidence_event_ids, updated_ts=excluded.updated_ts",
                (key, value, confidence, source, json.dumps(evidence), time.time()),
            )
            self._conn.commit()
        return True

    # ---- 读 ----
    def get_slots(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value, confidence, source, evidence_event_ids, updated_ts FROM profile_slots"
            ).fetchall()
        return [
            {
                "key": k,
                "label": _SLOT_LABEL.get(k, k),
                "value": v,
                "value_label": _VALUE_LABEL.get(v, v),
                "confidence": c,
                "source": s,
                "evidence_event_ids": json.loads(e or "[]"),
                "updated_ts": t,
            }
            for k, v, c, s, e, t in rows
        ]

    def preference_text(self) -> str:
        """精排 prompt 注入用：'篇幅偏好短、口吻偏犀利吐槽'；无槽位返回空串。"""
        parts = [f"{s['label']}{s['value_label']}" for s in self.get_slots()]
        return "、".join(parts)

    def close(self) -> None:
        self._conn.close()


# ---- 纠偏文本解析（POST /correction） ----

# 无 LLM 降级：中文关键词规则（顺序即优先级，长词先匹配）
_KEYWORD_RULES: list[tuple[str, str, list[str]]] = [
    ("length_pref", "long", ["长文", "长的", "深度长", "长一点", "详细"]),
    ("length_pref", "short", ["短文", "短的", "简短", "短一点", "碎片", "速览"]),
    ("tone_pref", "snarky", ["吐槽", "毒舌", "犀利", "锐评"]),
    ("tone_pref", "deep", ["深度", "严肃", "硬核", "学院"]),
    ("tone_pref", "neutral", ["中立", "客观", "平和"]),
    ("medium_pref", "video", ["视频"]),
    ("medium_pref", "audio", ["播客", "音频", "听"]),
    ("medium_pref", "text", ["文字", "文章", "图文"]),
    ("hot_ratio_ok", "no", ["少热点", "不要热点", "不想看热点", "别推热点"]),
    ("hot_ratio_ok", "yes", ["可以看热点", "来点热点", "热点可以"]),
]


def parse_correction_keywords(text: str) -> list[dict]:
    """关键词降级解析：命中即出槽位；一个槽位只取第一个命中。"""
    hits: list[dict] = []
    seen: set[str] = set()
    for slot, value, words in _KEYWORD_RULES:
        if slot in seen:
            continue
        if any(w in text for w in words):
            hits.append({"slot": slot, "value": value})
            seen.add(slot)
    return hits


async def parse_correction(text: str) -> list[dict]:
    """LLM（cheap）优先解析纠偏 → 受控槽位列表；不可用/解析失败降级关键词。

    输出元素：{"slot": key, "value": 受控枚举值}——只输出词汇表内值，不编造。
    """
    from app.core import llm
    from app.core.config import settings
    from app.core.llm import LLMUnavailable

    vocab = "；".join(f"{k}∈{'/'.join(v)}" for k, v in SLOT_VOCAB.items())
    prompt = (
        f"用户对内容偏好说了句话：「{text}」\n\n"
        f"受控槽位：{vocab}\n\n"
        "把这句话解析成槽位修正（只输出上面定义的槽位与取值，拿不准的不输出）。\n"
        '输出：{"items":[{"slot":"length_pref","value":"long"}]}；没有可解析的偏好就 {"items":[]}'
    )
    try:
        data = await llm.chat(
            prompt,
            system="你是偏好解析器，只输出严格 JSON，取值必须来自受控词汇表。",
            model=settings.llm_model_cheap,  # 纠偏是轻量任务，不烧强模型
            timeout=30,
        )
        items = [
            {"slot": i["slot"], "value": i["value"]}
            for i in data.get("items", [])
            if i.get("slot") in SLOT_VOCAB and i.get("value") in SLOT_VOCAB.get(i.get("slot", ""), [])
        ]
        if items:
            return items
    except (LLMUnavailable, Exception) as e:
        print(f"[correction] LLM 解析不可用，降级关键词：{e}")
    return parse_correction_keywords(text)
