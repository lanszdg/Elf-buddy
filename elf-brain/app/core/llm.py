"""LLM 接入（OpenAI 兼容协议；docs/01 §5 成本分层）。

- 打标用便宜模型、精排/讲述用强模型，模型名由环境变量配置（config.Settings）
- 无 API key 时抛 LLMUnavailable，调用方降级到启发式路径（保证无 key 也能跑通闭环）
- 所有输出要求 JSON mode，解析容错在调用方
"""

import json

import httpx

from app.core.config import settings


class LLMUnavailable(Exception):
    """未配置 key 或服务不可达——调用方应降级，不得让异常打死管线。"""


# token 用量累计（Review 预算硬护栏 + weekly 成本报表，docs/03 §3.5）
_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def usage_totals() -> dict:
    """进程启动至今的累计 token 用量（Review 跑批前后取差值记账）。"""
    return dict(_USAGE)


async def chat(
    prompt: str, *, system: str, model: str, timeout: float = 60.0, max_tokens: int | None = None
) -> dict:
    """单次对话调用，返回解析后的 JSON 对象。

    协议：POST {base}/chat/completions（OpenAI 兼容），response_format=json_object。
    max_tokens：大批量打标需显式提上限（DeepSeek 默认 4096，40 条/批输出会截断）。
    """
    if not settings.llm_api_key:
        raise LLMUnavailable("ELF_LLM_API_KEY 未配置")
    url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=body,
            )
        if r.status_code != 200:
            raise LLMUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        usage = body.get("usage") or {}
        _USAGE["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
        _USAGE["completion_tokens"] += int(usage.get("completion_tokens", 0))
        _USAGE["total_tokens"] += int(usage.get("total_tokens", 0))
        _USAGE["calls"] += 1
        content = body["choices"][0]["message"]["content"]
    except LLMUnavailable:
        raise
    except Exception as ex:
        # httpx 超时类异常 str 为空，补类型名便于排查（§3.3 曾因此裸奔一行空日志）
        raise LLMUnavailable(str(ex) or type(ex).__name__) from ex
    # 容错：模型可能在 JSON 外包裹说明文字，截取首尾花括号
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise LLMUnavailable(f"响应非 JSON: {content[:120]}")
    try:
        return json.loads(content[start : end + 1])
    except json.JSONDecodeError as ex:
        raise LLMUnavailable(f"JSON 解析失败: {ex}; {content[:120]}") from ex
