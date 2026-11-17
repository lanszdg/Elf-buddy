"""在线神经 TTS（docs/05 §11.27）：Edge 神经网络中文语音合成。

用户反馈「发音死板不自然」（§11.27）：壳侧 speechSynthesis 只能用系统自带 SAPI 语音
（多数机器是机械音）。改 Edge 在线神经语音（小艺，活泼少女音，贴小精灵人设），
MP3 下行给壳侧播放。隐私红线只约束麦克风音频不出机；朗读文本本就出机给 LLM API。
离线/失败时壳侧回落系统 TTS，不哑巴。
"""
import asyncio
import base64

import edge_tts

VOICE = "zh-CN-XiaoyiNeural"  # 小艺：活泼少女音
RATE = "+8%"  # 略快，精灵活泼
_TIMEOUT = 30  # 秒


async def synth_b64(text: str) -> str:
    """在线合成 MP3 → base64；任何失败抛异常，由调用方回落系统 TTS。"""
    communicate = edge_tts.Communicate(text.replace("\n", "，"), VOICE, rate=RATE)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])
    mp3 = b"".join(chunks)
    if not mp3:
        raise RuntimeError("empty audio")
    return base64.b64encode(mp3).decode()
