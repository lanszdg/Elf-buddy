"""coefficients.yaml 文本手术改写（红线：Review 管线只改此文件不改代码，docs/03 §3.2）。

pyyaml 整文件 dump 会丢失人工调教注释，故分两类处理：
- normalization 块：机器维护区，整体重新序列化替换（块首注明，人工改动需自行 version+1）
- 其余标量（state_factor.idle / timing_factor."15" / daily_l3_quota 等）：
  按点路径正则定位行，只替换数值，行尾注释与全文件其它内容原样保留
"""

import re
from pathlib import Path

import yaml

_TOP_KEY = re.compile(r"^([A-Za-z_][\w]*):")


def rewrite_normalization(path: str, norm: dict) -> None:
    """整体替换 normalization 块（其余顶级键与注释不动）。"""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.startswith("normalization:"))
    end = next(
        (i for i in range(start + 1, len(lines)) if _TOP_KEY.match(lines[i])),
        len(lines),
    )
    block = "# ---- 以下 normalization 块由 Review 管线维护（docs/04 §1）；人工改动请自行 version+1 ----\n"
    block += yaml.safe_dump({"normalization": norm}, allow_unicode=True, sort_keys=False)
    if not block.endswith("\n"):
        block += "\n"
    lines[start:end] = [block]
    p.write_text("".join(lines), encoding="utf-8")


def apply_scalar(path: str, dotted: str, value: float) -> bool:
    """按点路径替换标量数值，保留行尾注释。成功返回 True。

    支持："state_factor.idle"（段内子键）/"timing_factor.15"（数字子键，兼容带引号）/
    "daily_l3_quota"（顶级标量）。
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    fmt = f"{value:g}"

    if "." not in dotted:  # 顶级标量
        pat = re.compile(rf"^({re.escape(dotted)}\s*:\s*)(-?[\d.]+)(.*)$")
        for i, ln in enumerate(lines):
            m = pat.match(ln.rstrip("\n"))
            if m:
                lines[i] = f"{m.group(1)}{fmt}{m.group(3)}\n"
                p.write_text("".join(lines), encoding="utf-8")
                return True
        return False

    section, key = dotted.split(".", 1)
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{section}:"))
    except StopIteration:
        return False
    end = next(
        (i for i in range(start + 1, len(lines)) if _TOP_KEY.match(lines[i])),
        len(lines),
    )
    pat = re.compile(rf"^(\s*\"?{re.escape(key)}\"?\s*:\s*)(-?[\d.]+)(.*)$")
    for i in range(start + 1, end):
        m = pat.match(lines[i].rstrip("\n"))
        if m:
            lines[i] = f"{m.group(1)}{fmt}{m.group(3)}\n"
            p.write_text("".join(lines), encoding="utf-8")
            return True
    return False
