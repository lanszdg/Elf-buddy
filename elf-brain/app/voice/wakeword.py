"""唤醒词 → KWS keywords.txt 生成（docs/05 §8.4）。

KWS 模型（zipformer-wenetspeech）的音素表是「拼音声母 + 带调韵母」（如 x iǎo y á），
用 pypinyin 把精灵名字逐字拆成音素，逐个对照 tokens.txt 校验存在性——
不在表内的音素会让 KWS 静默失效，必须生成期就报错而不是运行时没反应。
"""
from pathlib import Path

from pypinyin import Style, lazy_pinyin


class WakeWordError(Exception):
    pass


def _load_tokens(tokens_path: Path) -> set[str]:
    tokens: set[str] = set()
    for line in tokens_path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            tokens.add(parts[0])
    return tokens


def name_to_phonemes(name: str, tokens: set[str]) -> list[str]:
    """名字逐字 → [声母, 带调韵母] 音素序列；任一音素不在表内即抛错。"""
    initials = lazy_pinyin(name, style=Style.INITIALS, strict=False, neutral_tone_with_five=True)
    finals = lazy_pinyin(name, style=Style.FINALS_TONE, strict=False, neutral_tone_with_five=True)
    phonemes: list[str] = []
    for ch, ini, fin in zip(name, initials, finals):
        if not ("\u4e00" <= ch <= "\u9fff"):
            raise WakeWordError(f"唤醒词含非汉字字符：{ch!r}（KWS 音素表只支持中文名）")
        for p in (ini, fin):
            if not p:
                continue
            if p in tokens:
                phonemes.append(p)
            else:
                raise WakeWordError(f"字 {ch!r} 的音素 {p!r} 不在 KWS tokens.txt 内")
    if not phonemes:
        raise WakeWordError("唤醒词为空")
    return phonemes


def build_keywords(elf_name: str, kws_dir: Path) -> Path:
    """生成 keywords.txt（一条唤醒词；:1.0 提升分 #0.3 阈值，比默认略松防漏唤）。"""
    tokens = _load_tokens(kws_dir / "tokens.txt")
    phonemes = name_to_phonemes(elf_name, tokens)
    out = kws_dir / "keywords.txt"
    out.write_text(" ".join(phonemes) + " :1.0 #0.3\n", encoding="utf-8")
    return out
