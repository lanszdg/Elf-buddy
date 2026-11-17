"""内容管线（docs/03 §3.3；算法 docs/04 §2）。

单文件实现抓取→打标→匹配三步（P0 骨架合并，P1 再拆包）。
"""

import asyncio
import calendar
import re
import time

import feedparser
import httpx
import numpy as np
import yaml
from pathlib import Path

from app.core import llm
from app.core.config import Settings
from app.core.llm import LLMUnavailable
from app.memory.event_store import EventStore

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ElfBuddy/0.1"}
_OG_RE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)

# §11.36 榜单来源短名表：feed 标题常是「InfoQ - 促进软件开发领域知识与创新的传播」
# 这种频道口号，直接显示会把榜单行挤爆（用户反馈标题被挤成残字）。
_SOURCE_MAP = [
    ("36kr.com", "36氪"), ("sspai.com", "少数派"), ("ifanr.com", "爱范儿"),
    ("geekpark.net", "极客公园"), ("infoq.cn", "InfoQ"), ("solidot.org", "Solidot"),
    ("cnblogs.com", "博客园"), ("oschina.net", "开源中国"), ("openai.com", "OpenAI"),
    ("deepmind.google", "DeepMind"), ("huggingface.co", "HuggingFace"),
    ("lilianweng.github.io", "LilianWeng"), ("sebastianraschka.com", "Ahead of AI"),
    ("jalammar.github.io", "Jay Alammar"), ("latent.space", "Latent Space"),
    ("arxiv.org", "arXiv"), ("technologyreview.com", "MIT科技评论"),
    ("techcrunch.com", "TechCrunch"), ("theverge.com", "The Verge"),
    ("arstechnica.com", "Ars Technica"), ("wired.com", "Wired"),
    ("importai.substack.com", "Import AI"), ("therundown.ai", "The Rundown"),
    ("tldr.tech", "TLDR"),
]


def _short_source(url: str, feed_title: str) -> str:
    """榜单来源显示名：已知站点用短名；其余砍 feed 标题的" - 口号"尾巴；再退域名。"""
    host = ""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc.lower()
    except Exception:
        pass
    for key, name in _SOURCE_MAP:
        if key in host:
            return name
    title = (feed_title or "").strip()
    for sep in (" - ", " — ", " | ", "–"):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if head:
                return head
    return (title[:20] if title else "") or host or "未知来源"


class ContentPipeline:
    def __init__(self, settings: Settings, store: EventStore) -> None:
        self.settings = settings
        self.store = store
        self.graph = None  # main.py 注入 InterestGraph（温度进 content_value）
        self.profile = None  # main.py 注入 UserProfileService（消费偏好进精排 prompt，docs/05 §7.3）
        self.skills = None  # main.py 注入 SkillStore（选题经验进精排，docs/02 §6.1）
        self._items: list[dict] = []      # 已打标内容池（48h）
        self._wishlist: list[dict] = []   # 想说清单（≤3 条，供时机引擎）
        self._interests: list[dict] = []
        self._feeds: list[str] = []
        self.reload_interests()
        self.reload_sources()

    def reload_sources(self) -> None:
        """内容源优先级：用户引导页自选的 data/sources.json > config 默认 8 源（docs/05 §3.4）。

        用户源是"用户数据"，与调教系数分文件存放，onboarding 不改 config。
        """
        import json

        user_file = Path(self.settings.data_dir) / "sources.json"
        if user_file.exists():
            self._feeds = json.loads(user_file.read_text(encoding="utf-8"))
        else:
            self._feeds = list(self.settings.rss_feeds)

    def interest_names(self) -> list[str]:
        """当前兴趣名列表（main.py 同步图谱节点用）。"""
        return [i["name"] for i in self._interests]

    def reload_interests(self) -> None:
        """兴趣优先级：用户 onboarding 的 interests.json > coefficients.yaml 种子兴趣。

        用户兴趣是"用户数据"，与调教系数分文件存放，onboarding 不改 coefficients.yaml。
        """
        user_file = Path(self.settings.data_dir) / "interests.json"
        if user_file.exists():
            import json

            self._interests = json.loads(user_file.read_text(encoding="utf-8"))
        else:
            coeff = yaml.safe_load(Path(self.settings.coeff_path).read_text(encoding="utf-8"))
            self._interests = coeff.get("seed_interests", [])

    def wishlist(self) -> list[dict]:
        cutoff = time.time() - 48 * 3600
        return [w for w in self._wishlist if w.get("queued_ts", 0) > cutoff]

    def find(self, content_id: str) -> dict | None:
        """按 content_id 查内容（/chat 追问锚定用；docs/05 §2.4）。"""
        for it in self._wishlist + self._items:
            if it["content_id"] == content_id:
                return it
        return None

    async def run_once(self) -> None:
        raw = await self._ingest()
        tagged = await self._tag_batch(raw)
        self._items = [t for t in tagged if t["quality_score"] >= 0.4]
        self._wishlist = await self._match(self._items)

    # ---- 1. 抓取（8 源并发；§3.3 提速、§3.4 封面） ----
    async def _ingest(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as client:
            feeds = await asyncio.gather(
                *(self._fetch_feed(client, url) for url in self._feeds), return_exceptions=True
            )
        items: list[dict] = []
        seen: set[str] = set()
        for f in feeds:
            if isinstance(f, Exception) or f is None:
                continue
            for e in f["entries"][:12]:  # 每源 12 条（§3.3 控量提速）
                cid = f"cnt_{abs(hash(e.get('link', e.title))) & 0xFFFFFFFF:x}"
                if cid in seen:
                    continue
                seen.add(cid)
                items.append(
                    {
                        "content_id": cid,
                        "title": e.get("title", ""),
                        "summary": e.get("summary", "")[:500],
                        "url": e.get("link", ""),
                        "source": _short_source(f.get("url", ""), f["title"]),
                        "cover": self._entry_cover(e),  # RSS enclosure/media 直出封面
                        "fetched_ts": time.time(),
                        # RSS/Atom 自带发布时间：追问「什么时候的新闻」的锚定依据（feedparser 归一化到 published/updated）
                        "published": e.get("published", "") or e.get("updated", ""),
                        # 归一化发布时间戳（UTC struct_time→秒）：新鲜度排序/估值用，
                        # 旧文重抓不会装新鲜（用户反馈「消息都不是最新的」，§11.25）
                        "published_ts": self._entry_ts(e),
                    }
                )
        return items

    @staticmethod
    def _entry_ts(e) -> float:
        """feedparser 的 published_parsed/updated_parsed 是 UTC struct_time → 秒时间戳（缺省 0）。
        未来时间戳判废：国内站点 RSS 常把北京时间标成 GMT（超前 8h），不可信，回退 fetched_ts。"""
        st = e.get("published_parsed") or e.get("updated_parsed")
        try:
            ts = float(calendar.timegm(st)) if st else 0.0
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return ts if 0 < ts <= time.time() + 600 else 0.0

    async def _fetch_feed(self, client: httpx.AsyncClient, url: str) -> dict | None:
        try:
            r = await client.get(url)
            feed = feedparser.parse(r.text)
            return {"title": feed.feed.get("title", url), "url": url, "entries": feed.entries}
        except Exception as ex:
            print(f"[ingest] {url}: {type(ex).__name__}: {ex}")
            return None

    @staticmethod
    def _entry_cover(e) -> str:
        """RSS 自带图：enclosure / media:content / media:thumbnail（无则待 og:image 补抓）。"""
        for enc in e.get("enclosures", []):
            if str(enc.get("type", "")).startswith("image") and enc.get("href"):
                return enc["href"]
        for mc in e.get("media_content", []):
            if mc.get("url"):
                return mc["url"]
        for mt in e.get("media_thumbnail", []):
            if mt.get("url"):
                return mt["url"]
        return ""

    async def _fill_covers(self, items: list[dict]) -> None:
        """候选内容的 og:image 补抓（§3.4）：只抓精排候选（≤10 条），不抓全量——控耗时控请求。"""
        need = [it for it in items if not it.get("cover") and it.get("url")]
        if not need:
            return
        async with httpx.AsyncClient(timeout=6, headers=_UA, follow_redirects=True) as client:
            covers = await asyncio.gather(
                *(self._fetch_og(client, it["url"]) for it in need), return_exceptions=True
            )
        for it, cover in zip(need, covers):
            if isinstance(cover, str) and cover:
                it["cover"] = cover

    @staticmethod
    async def _fetch_og(client: httpx.AsyncClient, url: str) -> str:
        try:
            r = await client.get(url)
            m = _OG_RE.search(r.text[:200000])
            return m.group(1) if m else ""
        except Exception:
            return ""

    # ---- 2. 打标（便宜模型；docs/03 §3.3） ----
    async def _tag_batch(self, items: list[dict]) -> list[dict]:
        """批量打标：40 条/批 + 2 路并发（§3.3 实测：5 路并发触发限流 67.8s，
        40条/批双发仅 7.8s）；30s 超时重试一次，无 key/仍败降级关键词启发式。"""
        if not items:
            return []
        names = [i["name"] for i in self._interests]
        batches = [items[i : i + 40] for i in range(0, len(items), 40)]
        sem = asyncio.Semaphore(2)  # 实测 2 路并发 7.8s/双批；3 路即触限流

        async def _one(b: list[dict]) -> dict[int, dict] | None:
            async with sem:
                return await self._tag_llm(b, names)

        results = await asyncio.gather(*(_one(b) for b in batches))
        out: list[dict] = []
        for batch, llm_result in zip(batches, results):
            for j, it in enumerate(batch):
                scores = llm_result.get(j) if llm_result is not None else None
                it.update(scores if scores is not None else self._tag_heuristic(it))
                out.append(it)
        return out

    async def _tag_llm(self, batch: list[dict], names: list[str]) -> dict[int, dict] | None:
        """打一批（key=批内序号）；失败只做一轮二分缩批且两半并发（§3.3：DeepSeek 长生成
        延迟随负载波动，缩批保底质量）——单批预算封顶 40+30=70s，仍败降级关键词，
        不在重试里烧等待（降级路径实测可产出合理想说清单）。"""
        try:
            return await self._tag_llm_once(batch, names)
        except LLMUnavailable as ex:
            if len(batch) <= 10:
                print(f"[tag] ≤10条仍败，本批降级关键词：{ex}")
                return None
            mid = len(batch) // 2
            print(f"[tag] {len(batch)}条失败（{ex}），缩批重试两半并发")
            left, right = await asyncio.gather(
                self._tag_llm_once(batch[:mid], names),
                self._tag_llm_once(batch[mid:], names),
                return_exceptions=True,
            )
            merged: dict[int, dict] = {}
            if isinstance(left, dict):
                merged.update(left)
            if isinstance(right, dict):
                merged.update({k + mid: v for k, v in right.items()})
            return merged or None

    async def _tag_llm_once(self, batch: list[dict], names: list[str]) -> dict[int, dict]:
        """单次调用打一批；超时随批量缩放（40条=40s，20条=30s，10条=20s）。"""
        listing = "\n".join(
            f"[{j}] 标题：{it['title']}\n摘要：{it['summary'][:120]}" for j, it in enumerate(batch)
        )
        prompt = (
            f"用户当前兴趣：{'、'.join(names)}\n\n"
            "对下列每条内容输出：\n"
            "- i: 内容编号（原样返回）\n"
            "- quality: 0~1，信息量与可信度（标题党/空洞给低分）\n"
            "- domains: 领域标签（≤3 个）\n"
            "- entities: 关键实体（≤5 个）\n"
            "- sentiment: -1~1\n"
            "- interest_scores: 对每个用户兴趣的相关度 0~1（key 严格用兴趣原名，不相关给 0）\n\n"
            f"内容列表：\n{listing}\n\n"
            '输出：{"results":[{"i":0,"quality":0.0,"domains":[],"entities":[],"sentiment":0.0,"interest_scores":{}}]}'
        )
        data = await llm.chat(
            prompt,
            system="你是内容打标器，只输出严格 JSON。",
            model=self.settings.llm_model_cheap,
            timeout=max(20, len(batch)),  # 随批量缩放但有下限：40条=40s，尾批2条也有20s
            max_tokens=8000,  # 40 条/批的结构化输出超 DeepSeek 默认 4096 上限
        )
        result: dict[int, dict] = {}
        for r in data.get("results", []):
            try:
                idx = int(r["i"])
                result[idx] = {
                    "quality_score": max(0.0, min(1.0, float(r.get("quality", 0.5)))),
                    "domains": list(r.get("domains", []))[:3],
                    "entities": list(r.get("entities", []))[:5],
                    "sentiment": float(r.get("sentiment", 0)),
                    "interest_scores": {
                        n: max(0.0, min(1.0, float(r.get("interest_scores", {}).get(n, 0)))) for n in names
                    },
                }
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def _tag_heuristic(self, it: dict) -> dict:
        """无 LLM 降级：质量=标题长度；相关度=兴趣名 2-gram 与文本重合。"""
        text = it["title"] + " " + it["summary"][:200]
        scores = {}
        for interest in self._interests:
            name = interest["name"]
            kws = {name[k : k + 2] for k in range(len(name) - 1)}
            hit = sum(1 for w in kws if w and w in text)
            scores[name] = min(1.0, hit / 3)
        return {
            "quality_score": min(1.0, len(it["title"]) / 30),
            "domains": [],
            "entities": [],
            "sentiment": 0.0,
            "interest_scores": scores,
        }

    def latest_items(self, n: int = 5, per_source: int = 2) -> list[dict]:
        """按发布时间倒序取最新入库条目（chat 新闻类意图用；全部来自真实抓取，红线⑤）。

        §11.36 榜单多样性：每源最多 per_source 条——InfoQ 日更多篇，不限源会把
        "最新 N 条"全部刷成同一家的榜（用户反馈热榜全是 InfoQ）。
        """
        def ts(it: dict) -> float:
            return it.get("published_ts") or it["fetched_ts"]

        picked: list[dict] = []
        counts: dict[str, int] = {}
        for it in sorted(self._items, key=ts, reverse=True):
            src = it.get("source", "") or "未知来源"
            if counts.get(src, 0) >= per_source:
                continue
            picked.append(it)
            counts[src] = counts.get(src, 0) + 1
            if len(picked) >= n:
                break
        return picked

    def _freshness(self, it: dict) -> float:
        """发布新鲜度 0~1：τ=72h 指数衰减，优先按 RSS 发布时间（published_ts）算，
        缺发布时间的退 fetched_ts。旧常青文重抓不会装新鲜（docs/05 §11.25）。"""
        ts = it.get("published_ts") or it["fetched_ts"]
        age_h = max(0.0, (time.time() - ts) / 3600)
        return float(np.exp(-age_h / 72))

    # ---- 3. 匹配（docs/04 §2 粗排+精排） ----
    async def _match(self, items: list[dict]) -> list[dict]:
        if not items or not self._interests:
            return []
        # 粗排：rel = 各兴趣相关度的最大值（docs/04 §2.1；P0 由打标直接产出 interest_scores）
        for it in items:
            best_name, best_rel = max(it["interest_scores"].items(), key=lambda kv: kv[1])
            it["rel"] = best_rel
            it["matched_node"] = best_name
        # 新鲜度进粗排（§11.25）：旧常青内容不再压过新内容；0.5+0.5f 混合，相关度仍主导
        top = sorted(items, key=lambda x: x["rel"] * (0.5 + 0.5 * self._freshness(x)), reverse=True)[:10]
        await self._fill_covers(top)  # §3.4：候选补抓 og:image 封面（与精排并行不值当，量小）
        # 精排（docs/04 §2.2）：单次批量 LLM；失败降级 rel 直通
        reranked = await self._rerank_llm(top)
        out = []
        for it in top:
            rr = reranked.get(it["content_id"]) if reranked is not None else None
            it["relevance"] = rr["relevance"] if rr else it["rel"]
            it["matched_reason"] = rr["reason"] if rr else f"和你关注的「{it['matched_node']}」相关"
            it["content_value"] = self._content_value(it)
            it["queued_ts"] = time.time()
            if it["relevance"] >= 0.3:
                out.append(it)
        return out[:6]  # 想说清单留 6 条：3 条太薄，说两轮就弹尽粮绝

    async def _rerank_llm(self, top: list[dict]) -> dict[str, dict] | None:
        """docs/04 §2.2 精排：单次批量调用，reason 强制锚定输入内容（接口红线⑤）。"""
        if not top:
            return {}
        interest_desc = "；".join(f"{i['name']}（{i.get('desc', '')}）" for i in self._interests)
        listing = "\n".join(
            f"[{it['content_id']}] {it['title']} | {it['summary'][:150]}" for it in top
        )
        # 纠偏生效点：消费偏好注入精排——排序与理由随偏好可查地改变（docs/05 §7.3 验收）
        pref = self.profile.preference_text() if self.profile else ""
        pref_line = f"用户消费偏好：{pref}（评估相关度与写理由时遵循这些偏好）\n\n" if pref else ""
        # 技能消费（docs/02 §6.1 topic_selection）：沉淀的选题经验注入精排 prompt
        skill_line = ""
        if self.skills is not None:
            top = self.skills.best("topic_selection", 2)
            if top:
                lines = "；".join(s["content"][:100] for s in top)
                skill_line = f"选题经验（评估 relevance 时参考）：{lines}\n\n"
                for s in top:
                    self.skills.touch(s["name"])  # 无逐次归因通道，只记使用（防 curator 误杀）
        prompt = (
            f"用户兴趣：{interest_desc}\n\n"
            f"{pref_line}{skill_line}"
            "为下列每条候选评估与用户兴趣的相关度 relevance(0~1)，并写一句 matched_reason（≤30 字）。\n"
            "matched_reason 必须说出与用户哪个兴趣相关、相关点是什么；"
            "只能依据给出的标题和摘要，禁止编造未提及的信息。\n\n"
            f"候选：\n{listing}\n\n"
            '输出：{"results":[{"id":"内容id","relevance":0.0,"reason":"..."}]}'
        )
        try:
            data = await llm.chat(
                prompt,
                system="你是推荐精排器，只输出严格 JSON，理由必须锚定输入内容。",
                # §3.3 偏差：精排由强模型改便宜模型（v4-pro 推理级 65s/批 → chat 4.6s/批，
                # ≤30字锚定理由质量实测无差）；强模型保留给 /chat 追问长文本
                model=self.settings.llm_model_cheap,
                timeout=25,  # 健康实测 ~5s；超时降级 rel 直通（理由用模板，质量损失可控）
            )
        except LLMUnavailable as ex:
            print(f"[match] LLM 不可用，精排降级 rel 直通：{ex}")
            return None
        result: dict[str, dict] = {}
        for r in data.get("results", []):
            try:
                result[str(r["id"])] = {
                    "relevance": max(0.0, min(1.0, float(r.get("relevance", 0)))),
                    "reason": str(r.get("reason", ""))[:60],
                }
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def _content_value(self, it: dict) -> float:
        """content_value = 兴趣温度 × 匹配度 × 时效衰减。τ=12h；时效按发布时间算
        （published_ts 优先，缺则 fetched_ts）——旧文重抓不再满血（docs/05 §11.25）。"""
        temp = self.graph.temperature_of(it["matched_node"]) if self.graph else 1.0
        base = (0.7 * it["relevance"] + 0.3 * it["quality_score"]) * temp
        age_h = max(0.0, (time.time() - (it.get("published_ts") or it["fetched_ts"])) / 3600)
        return base * float(np.exp(-age_h / 12))
