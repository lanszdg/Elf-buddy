"""全局配置：路径、LLM 接入。

打包分发说明：PyInstaller onefile 运行时，exe 所在目录通常只读，
数据文件（event.db / 用户可编辑的 coefficients.yaml / SOUL.md）一律落在
ELF_DATA_DIR（壳传入，默认为 exe 同级的 data/ 或开发时的 elf-brain/data/）。
"""

import os
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]  # elf-brain/（app/core/config.py 上两级）
DATA_DIR = Path(os.getenv("ELF_DATA_DIR", ROOT / "data"))


def _load_dotenv() -> None:
    """数据目录下的 .env 补进环境变量（已有变量优先，不覆盖）。

    key 不落仓库：开发时放 elf-brain/data/.env，生产时放 %APPDATA%/Elf Buddy/data/.env。
    """
    env_file = DATA_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def bundle_resource(rel: str) -> Path:
    """PyInstaller 包内资源路径（onefile 解压目录为 sys._MEIPASS；开发时=仓库）。"""
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base / rel


_load_dotenv()  # 须在 Settings 定义前执行——类字段默认值在类定义时读取 os.getenv


class Settings(BaseModel):
    data_dir: str = str(DATA_DIR)
    event_db_path: str = str(DATA_DIR / "event.db")
    coeff_path: str = str(DATA_DIR / "coefficients.yaml")  # 用户可编辑副本
    soul_path: str = str(DATA_DIR / "SOUL.md")

    # LLM 接入（OpenAI 兼容协议；打标用便宜模型，讲述用强模型——成本分层，见 docs/01 §5）
    llm_base_url: str = os.getenv("ELF_LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_api_key: str = os.getenv("ELF_LLM_API_KEY", "")
    # cheap 默认 deepseek-chat（§3.3 实测：同批打标 4.6s vs v4-pro 65.1s，提速 14 倍）
    llm_model_cheap: str = os.getenv("ELF_LLM_MODEL_CHEAP", "deepseek-chat")
    llm_model_strong: str = os.getenv("ELF_LLM_MODEL_STRONG", "deepseek-v4-pro")

    # Review 管线成本护栏（docs/03 §3.5：单次 Review LLM 预算硬上限，超限降级纯统计）
    review_token_budget: int = int(os.getenv("ELF_REVIEW_TOKEN_BUDGET", "20000"))

    # P0 种子兴趣（docs/04 §2.5）；RSS 默认 26 源（docs/05 §3.4：8 中文源 2026-10-17 实测可达，
    # 知乎热榜反爬 403 弃用；18 英文 AI/科技源 2026-10-28 实测可达，用户指定）。
    # 用户自选源存 data/sources.json，优先于此默认表
    rss_feeds: list[str] = [
        "https://36kr.com/feed",            # 36氪·科技商业
        "https://sspai.com/feed",           # 少数派·效率生活
        "https://www.ifanr.com/feed",       # 爱范儿·消费科技
        "https://www.geekpark.net/rss",     # 极客公园·前沿科技
        "https://www.infoq.cn/feed",        # InfoQ·技术资讯
        "https://www.solidot.org/index.rss",  # 奇客·开源科技
        "https://www.cnblogs.com/rss",      # 博客园·开发者
        "https://www.oschina.net/news/rss",  # 开源中国
        "https://openai.com/news/rss.xml",   # OpenAI 官方博客
        "https://deepmind.google/blog/rss.xml",  # Google DeepMind
        "https://huggingface.co/blog/feed.xml",  # Hugging Face·开源模型
        "https://lilianweng.github.io/index.xml",  # Lilian Weng·LLM 综述
        "https://magazine.sebastianraschka.com/feed",  # Ahead of AI·论文拆解
        "https://jalammar.github.io/feed.xml",  # Jay Alammar·图解原理
        "https://www.latent.space/feed",     # Latent Space·AI 工程
        "https://rss.arxiv.org/rss/cs.AI",   # arXiv 人工智能
        "https://rss.arxiv.org/rss/cs.LG",   # arXiv 机器学习
        "https://www.technologyreview.com/feed/",  # MIT 科技评论
        "https://techcrunch.com/feed/",      # TechCrunch·创投
        "https://www.theverge.com/rss/index.xml",  # The Verge
        "https://arstechnica.com/?feed=rss2",  # Ars Technica·硬核
        "https://www.wired.com/feed/rss",    # Wired·科技文化
        "https://importai.substack.com/feed",  # Import AI·政策治理
        "https://www.therundown.ai/feed",    # The Rundown AI·日报
        "https://tldr.tech/api/rss/ai",      # TLDR AI·摘要
    ]


def bootstrap_data_files() -> None:
    """首次运行：包内默认文件拷到可写数据目录，之后以用户副本为准（可手工调教）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    coeff = DATA_DIR / "coefficients.yaml"
    if not coeff.exists():
        shutil.copy(bundle_resource("app/timing/coefficients.yaml"), coeff)


bootstrap_data_files()
settings = Settings()
