"""技能库（docs/02 §6.1：技能=Markdown 文件；docs/03 §3.5 weekly Curator 打分/归档）。

技能文件是 M5 回路的第 3 类资产（选题/讲述风格的策略参数），frontmatter 记账使用与成效。
闭环三段：生成（weekly 复盘从反馈样本沉淀）→ 消费（精排/讲述注入 prompt）→ 归因
（renderer 按 feedback_resolved 结算 touch 成败）；Curator 归档低成效/长期未用 → archive/
（只做移动不删除，与 valid_to 同一条数据红线）。
"""

import re
import time
from pathlib import Path

import yaml

_FM = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.\-]{1,63}$")

ARCHIVE_AFTER_UNUSED_DAYS = 30
MIN_USES_FOR_JUDGEMENT = 5
MIN_SUCCESS_RATE = 0.3
SKILL_KINDS = {"topic_selection", "narration_style"}  # timing 类由数值系数管（nightly 调参），不产文字技能
MAX_CONTENT_LEN = 200


class SkillStore:
    def __init__(self, skills_dir: str) -> None:
        self.dir = Path(skills_dir)
        self.archive_dir = self.dir / "archive"

    def scan(self) -> list[dict]:
        """全部在职技能（archive/ 不算）。"""
        if not self.dir.exists():
            return []
        out = []
        for f in sorted(self.dir.glob("*.md")):
            m = _FM.match(f.read_text(encoding="utf-8"))
            meta = yaml.safe_load(m.group(1)) if m else {}
            out.append(
                {
                    "name": meta.get("name", f.stem),
                    "path": f,
                    "created_ts": float(meta.get("created_ts", 0)),
                    "last_used_ts": float(meta.get("last_used_ts", 0)),
                    "uses": int(meta.get("uses", 0)),
                    "successes": int(meta.get("successes", 0)),
                }
            )
        return out

    # ============ 生成侧（weekly 复盘沉淀，docs/01 技能复盘→新技能入库） ============
    def add_skill(
        self,
        kind: str,
        name: str,
        content: str,
        score: float,
        evidence_events: int,
        now: float | None = None,
    ) -> bool:
        """落盘新技能；同 name 已存在 → 升级（version+1，保留使用记账）。返回是否写入。"""
        if kind not in SKILL_KINDS or not _NAME_RE.match(name):
            return False
        content = str(content).strip()[:MAX_CONTENT_LEN]
        if not content:
            return False
        now = now or time.time()
        score = min(0.9, max(0.2, float(score)))
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{name}.md"
        meta: dict = {}
        if path.exists():
            m = _FM.match(path.read_text(encoding="utf-8"))
            meta = yaml.safe_load(m.group(1)) if m else {}
        meta.update(
            {
                "name": name,
                "kind": kind,
                "version": int(meta.get("version", 0)) + 1,
                "score": round(score, 2),
                "created_by": "review-pipeline",
                "evidence_events": int(evidence_events),
            }
        )
        meta.setdefault("created_ts", now)
        # 升级不清使用记账；新建给 30 天证明窗口（last_used_ts=created_ts 起算）
        meta.setdefault("uses", 0)
        meta.setdefault("successes", 0)
        meta.setdefault("last_used_ts", now)
        path.write_text(
            f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)}---\n{content}\n",
            encoding="utf-8",
        )
        return True

    # ============ 消费侧（精排/讲述注入 prompt） ============
    def best(self, kind: str, n: int = 2) -> list[dict]:
        """某 kind 下 score 最高的在职技能（注入 prompt 用），[{name, content, score}]。"""
        if not self.dir.exists():
            return []
        out = []
        for f in sorted(self.dir.glob("*.md")):
            text = f.read_text(encoding="utf-8")
            m = _FM.match(text)
            meta = yaml.safe_load(m.group(1)) if m else {}
            if meta.get("kind") != kind:
                continue
            body = (text[m.end():] if m else "").strip()
            if not body:
                continue
            out.append(
                {
                    "name": str(meta.get("name", f.stem)),
                    "content": body[:MAX_CONTENT_LEN],
                    "score": float(meta.get("score", 0.5)),
                }
            )
        out.sort(key=lambda s: s["score"], reverse=True)
        return out[:n]

    # ============ 归因（renderer 按 feedback_resolved 结算记账） ============
    def touch(self, name: str, success: bool | None = None, now: float | None = None) -> None:
        """记账一次使用；success=True 记成功，False 只拉低成功率，None 只记使用（无归因通道的技能）。"""
        path = self.dir / f"{name}.md"
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
        m = _FM.match(text)
        meta = yaml.safe_load(m.group(1)) if m else {}
        body = text[m.end():] if m else ""
        meta["uses"] = int(meta.get("uses", 0)) + 1
        if success is True:
            meta["successes"] = int(meta.get("successes", 0)) + 1
        meta["last_used_ts"] = now or time.time()
        path.write_text(
            f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)}---\n{body}",
            encoding="utf-8",
        )

    def curate(self, now: float | None = None) -> dict:
        """weekly Curator：归档低成效/长期未用技能，返回处理记录。

        成效判断要求 successes>0（有成败归因记录）：topic_selection 类无逐次归因通道，
        若按 uses≥5 且 rate=0 误杀，只能靠「长期未用」兜底归档。
        """
        now = now or time.time()
        result = {"scanned": 0, "archived": []}
        for s in self.scan():
            result["scanned"] += 1
            reason = None
            rate = s["successes"] / s["uses"] if s["uses"] else None
            if (
                s["uses"] >= MIN_USES_FOR_JUDGEMENT
                and s["successes"] > 0
                and rate is not None
                and rate < MIN_SUCCESS_RATE
            ):
                reason = f"成效过低（{s['successes']}/{s['uses']}={rate:.2f}<{MIN_SUCCESS_RATE}）"
            elif s["uses"] > 0 and now - s["last_used_ts"] > ARCHIVE_AFTER_UNUSED_DAYS * 86400:
                reason = f"{ARCHIVE_AFTER_UNUSED_DAYS} 天未用"
            elif s["uses"] == 0 and now - s["created_ts"] > ARCHIVE_AFTER_UNUSED_DAYS * 86400:
                reason = f"创建 {ARCHIVE_AFTER_UNUSED_DAYS} 天未启用"
            if reason:
                self._archive(s, reason, now)
                result["archived"].append({"name": s["name"], "reason": reason})
        return result

    def _archive(self, skill: dict, reason: str, now: float) -> None:
        src: Path = skill["path"]
        text = src.read_text(encoding="utf-8")
        m = _FM.match(text)
        meta = yaml.safe_load(m.group(1)) if m else {}
        body = text[m.end():] if m else text
        meta.update({"archived_ts": now, "archive_reason": reason})
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dst = self.archive_dir / src.name
        dst.write_text(
            f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)}---\n{body}",
            encoding="utf-8",
        )
        src.unlink()
