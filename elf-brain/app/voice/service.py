"""语音会话服务（docs/05 §8.4）：KWS 唤醒 → ASR 指令窗 → 事件产出。

多轮状态机：
  wake     —— 只吃 KWS 流，听到唤醒词 → 切 command，推 voice.wake
  command  —— 只吃 ASR 流，端点检出或 10s 上限 → 有字切 converse，无字回 wake
  converse —— 续聊窗：不用再叫名字，直接接着说；静音 12s 或说话人明显讲完 → 回 wake

回声自激防护：壳侧 TTS 播放期间发 voice.gate 关闸，ASR 暂停（否则它会听见自己说话再自问自答），
KWS 保持在线，所以"它在说话时喊名字"依然能打断（barge-in）。

模型不在安装包内（portable 红线），首次启用语音时从 ModelScope 按需下载到 data/voice/。
"""
from __future__ import annotations

import json
import tarfile
import urllib.request
from array import array
from pathlib import Path

from app.voice.wakeword import WakeWordError, build_keywords

SAMPLE_RATE = 16000
COMMAND_MAX_SAMPLES = SAMPLE_RATE * 10  # 首次指令窗上限 10s
CONVERSE_WINDOW = SAMPLE_RATE * 12  # 续聊窗：12s 没说话自动退回唤醒
SPEECH_PEAK = 0.012  # 简易能量门（峰值≈-38dBFS）：只用于续聊窗计时，不做 VAD

_KWS_BASE = "https://modelscope.cn/models/pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/resolve/master"
_KWS_FILES = {
    "encoder.int8.onnx": "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder.int8.onnx": "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "joiner.int8.onnx": "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "tokens.txt": "tokens.txt",
}
# ASR 用 streaming paraformer 中英双语（int8，216MB）：中文精度显著高于原 14M zipformer
# （用户反馈「语音识别能力很差」，§11.26）；隐私红线不变——仍是 sherpa-onnx 全本地推理。
_ASR_TAR = "https://modelscope.cn/models/ZhaoChaoqun/sherpa-onnx-asr-models/resolve/master/sherpa-onnx-streaming-paraformer-bilingual-zh-en.tar.bz2"
_ASR_ID = "streaming-paraformer-bilingual-zh-en-int8"
# tar 内成员按模式匹配（不同发布批次命名不一），只取 int8；
# mac 打的 tar 会夹带 ._ 开头的 AppleDouble 残桩，必须排除，否则抢到假文件
_ASR_MEMBERS = {  # 目标名 → 判定（basename 小写）
    "encoder.int8.onnx": lambda n: "encoder" in n and "int8" in n and not n.startswith("._"),
    "decoder.int8.onnx": lambda n: "decoder" in n and "int8" in n and not n.startswith("._"),
    "tokens.txt": lambda n: n == "tokens.txt",
}


class VoiceUnavailable(Exception):
    pass


def ensure_models(voice_dir: Path) -> None:
    """缺什么下什么（幂等）；网络失败抛 VoiceUnavailable，语音功能整体降级。"""
    kws_dir = voice_dir / "kws"
    asr_dir = voice_dir / "asr"
    kws_dir.mkdir(parents=True, exist_ok=True)
    asr_dir.mkdir(parents=True, exist_ok=True)
    try:
        for dst, src in _KWS_FILES.items():
            if not (kws_dir / dst).exists():
                urllib.request.urlretrieve(f"{_KWS_BASE}/{src}", kws_dir / dst)
        # 版本标记：模型换代（§11.26 换 paraformer）时强制老用户清空重下
        marker = asr_dir / "model.version"
        if (
            marker.exists()
            and marker.read_text(encoding="utf-8").strip() == _ASR_ID
            and all((asr_dir / f).exists() for f in _ASR_MEMBERS)
        ):
            return
        for old in asr_dir.iterdir():
            old.unlink()
        tmp = voice_dir / "_asr.tar.bz2"
        urllib.request.urlretrieve(_ASR_TAR, tmp)
        with tarfile.open(tmp, "r:bz2") as tar:
            for m in tar:
                name = Path(m.name).name.lower()
                if not m.isfile():
                    continue
                for dst, match in _ASR_MEMBERS.items():
                    if match(name) and not (asr_dir / dst).exists():
                        (asr_dir / dst).write_bytes(tar.extractfile(m).read())  # type: ignore[union-attr]
        tmp.unlink()
        missing = [f for f in _ASR_MEMBERS if not (asr_dir / f).exists()]
        # onnx 必大于 1MB，小了就是抢到 AppleDouble/LFS 残桩
        bad = [
            f
            for f in _ASR_MEMBERS
            if f.endswith(".onnx") and (asr_dir / f).exists() and (asr_dir / f).stat().st_size < 1_000_000
        ]
        if missing or bad:
            raise VoiceUnavailable(f"ASR 模型解包异常：缺 {missing} 损坏 {bad}")
        marker.write_text(_ASR_ID, encoding="utf-8")
    except Exception as e:
        raise VoiceUnavailable(f"语音模型下载失败：{e}") from e


class VoiceSession:
    """一次 WS 连接 = 一个会话；PCM int16 16kHz 单声道流入，事件流出。"""

    def __init__(self, voice_dir: Path, elf_name: str):
        import sherpa_onnx

        try:
            keywords = build_keywords(elf_name, voice_dir / "kws")
        except WakeWordError as e:
            raise VoiceUnavailable(str(e)) from e
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(voice_dir / "kws" / "tokens.txt"),
            encoder=str(voice_dir / "kws" / "encoder.int8.onnx"),
            decoder=str(voice_dir / "kws" / "decoder.int8.onnx"),
            joiner=str(voice_dir / "kws" / "joiner.int8.onnx"),
            keywords_file=str(keywords),
            num_threads=1,
        )
        self.asr = sherpa_onnx.OnlineRecognizer.from_paraformer(
            tokens=str(voice_dir / "asr" / "tokens.txt"),
            encoder=str(voice_dir / "asr" / "encoder.int8.onnx"),
            decoder=str(voice_dir / "asr" / "decoder.int8.onnx"),
            num_threads=2,
            enable_endpoint_detection=True,
        )
        self.state = "wake"
        self.kws_stream = self.kws.create_stream()
        self.asr_stream = None
        self.cmd_samples = 0
        self.gated = False  # TTS 播放中：ASR 暂停，KWS 仍在线（可插话打断）
        self.idle_samples = 0  # 续聊窗：连续静音采样数
        self.heard_speech = False  # 续聊窗内是否已开口（开了口才认端点）

    def set_gate(self, on: bool) -> None:
        """壳侧 TTS 起止通知。关闸期间丢掉 ASR 残音，开闸后重新计时续聊窗。"""
        self.gated = on
        if self.asr_stream is not None:
            self.asr_stream = self.asr.create_stream()
        self.cmd_samples = 0
        self.idle_samples = 0
        self.heard_speech = False

    def feed_pcm(self, pcm: bytes) -> list[dict]:
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        floats = [s / 32768.0 for s in samples]
        if self.state == "wake":
            return self._feed_wake(floats)
        if self.gated:
            return []  # 它在说话：别把自己的声音喂给 ASR，否则自问自答
        if self.state == "command":
            return self._feed_command(floats)
        return self._feed_converse(floats)

    def _feed_wake(self, floats: list[float]) -> list[dict]:
        self.kws_stream.accept_waveform(SAMPLE_RATE, floats)
        while self.kws.is_ready(self.kws_stream):
            self.kws.decode_stream(self.kws_stream)
        if not self.kws.get_result(self.kws_stream):  # 空串=未命中
            return []
        self.kws_stream = self.kws.create_stream()  # 绑定无 reset()，重开流即清空
        self.state = "command"
        self.asr_stream = self.asr.create_stream()
        self.cmd_samples = 0
        self.idle_samples = 0
        self.heard_speech = False
        # 唤醒后残留的这段音频也喂给 ASR，指令说得快也不丢头
        self.asr_stream.accept_waveform(SAMPLE_RATE, floats)
        return [{"type": "voice.wake"}]

    def _feed_command(self, floats: list[float]) -> list[dict]:
        stream = self.asr_stream
        stream.accept_waveform(SAMPLE_RATE, floats)
        self.cmd_samples += len(floats)
        while self.asr.is_ready(stream):
            self.asr.decode_stream(stream)
        done = self.asr.is_endpoint(stream) or self.cmd_samples >= COMMAND_MAX_SAMPLES
        if not done:
            return []
        text = self.asr.get_result(stream).strip()
        if not text:
            self._back_to_wake()
            return [{"type": "voice.asr_final", "text": ""}]
        # 有内容 → 开续聊窗：下一句不用再喊名字
        self.state = "converse"
        self.asr_stream = self.asr.create_stream()
        self.idle_samples = 0
        self.heard_speech = False
        return [{"type": "voice.asr_final", "text": text}, {"type": "voice.converse"}]

    def _feed_converse(self, floats: list[float]) -> list[dict]:
        if floats and max(abs(f) for f in floats) >= SPEECH_PEAK:
            self.heard_speech = True
            self.idle_samples = 0
        else:
            self.idle_samples += len(floats)
        stream = self.asr_stream
        stream.accept_waveform(SAMPLE_RATE, floats)
        while self.asr.is_ready(stream):
            self.asr.decode_stream(stream)
        if self.heard_speech and self.asr.is_endpoint(stream):
            text = self.asr.get_result(stream).strip()
            self.asr_stream = self.asr.create_stream()
            self.idle_samples = 0
            self.heard_speech = False
            return [{"type": "voice.asr_final", "text": text}] if text else []
        if self.idle_samples >= CONVERSE_WINDOW:
            self._back_to_wake()
            return [{"type": "voice.idle"}]
        return []

    def _back_to_wake(self) -> None:
        self.state = "wake"
        self.asr_stream = None
        self.kws_stream = self.kws.create_stream()
        self.cmd_samples = 0
        self.idle_samples = 0
        self.heard_speech = False


def read_elf_name(data_dir: Path) -> str:
    try:
        return json.loads((data_dir / "profile.json").read_text(encoding="utf-8-sig")).get("elf_name") or "小芽"
    except Exception:
        return "小芽"
