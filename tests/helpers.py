"""测试工具：固定时间、内存仓库、伪造 HTTP 会话。"""
from __future__ import annotations

import json
from datetime import datetime

import requests

from app.config import CN_TZ, Settings
from app.engine import EventEngine
from app.store import DATA_DIR, Store

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=CN_TZ)


def t(s: str) -> datetime:
    """'2026-09-02 13:00' -> 带 +08:00 的 datetime"""
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CN_TZ)


def settings(**kw) -> Settings:
    base = dict(
        fuyao_api_key="", fuyao_base_url="https://fuyao.example", llm_api_key="", llm_base_url="https://llm.example/v1",
        llm_model="test-model", ifind_mcp_url="", ifind_mcp_token="", ifind_news_tool="", http_timeout=3, demo_now=NOW,
    )
    base.update(kw)
    return Settings(**base)


def demo_store() -> Store:
    return Store(DATA_DIR / "demo_events.json", None)


def demo_engine(store: Store | None = None) -> EventEngine:
    st = store or demo_store()
    ev = st.events["evt_demo_ma"]
    return EventEngine(ev, st.event_sources(ev.id), st.event_claims(ev.id))


def slot(state: dict, key: str) -> dict:
    return next(s for s in state["slots"] if s["key"] == key)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None, text=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}
        self.text = text if text is not None else json.dumps(body, ensure_ascii=False)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self
            raise err


class FakeSession:
    """按顺序返回预置响应；元素为异常时抛出。记录所有调用。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)
