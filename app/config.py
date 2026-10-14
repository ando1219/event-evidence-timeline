"""环境变量配置。所有密钥只从环境变量读取，不写入代码或仓库。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

CN_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _load_dotenv() -> None:
    """本地开发便利：读取项目根目录 .env（已在 .gitignore 中），不覆盖已存在的环境变量。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    fuyao_api_key: str
    fuyao_base_url: str
    llm_api_key: str
    llm_base_url: str
    llm_model: str
    ifind_mcp_url: str
    ifind_mcp_token: str
    ifind_news_tool: str
    http_timeout: float
    demo_now: Optional[datetime]

    @property
    def fuyao_enabled(self) -> bool:
        return bool(self.fuyao_api_key)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    @property
    def ifind_enabled(self) -> bool:
        return bool(self.ifind_mcp_url)


def load_settings() -> Settings:
    demo_now = _env("DEMO_NOW")
    return Settings(
        fuyao_api_key=_env("FUYAO_API_KEY"),
        fuyao_base_url=_env("FUYAO_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        llm_model=_env("LLM_MODEL", "deepseek-chat"),
        ifind_mcp_url=_env("IFIND_MCP_URL"),
        ifind_mcp_token=_env("IFIND_MCP_TOKEN"),
        ifind_news_tool=_env("IFIND_NEWS_TOOL"),
        http_timeout=float(_env("HTTP_TIMEOUT", "12") or 12),
        demo_now=datetime.fromisoformat(demo_now) if demo_now else None,
    )


def now(settings: Optional[Settings] = None) -> datetime:
    s = settings or load_settings()
    return s.demo_now or datetime.now(CN_TZ)
