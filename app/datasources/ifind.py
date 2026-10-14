"""iFinD MCP 接入层（新闻来源）。

iFinD MCP 走 MCP Streamable HTTP（JSON-RPC 2.0）。不同账号开放的工具名和返回字段可能不同，
所以工具名由环境变量 IFIND_NEWS_TOOL 指定，可先调 /api/ifind/tools 列出可用工具再配置。
返回字段做了宽松映射（标题/正文/时间/来源/链接），映射不到的字段原样保留，并在结果里标明。
该模块未在本开发环境中实测（无 iFinD 账号），README 已列为已知边界。
"""
from __future__ import annotations

import itertools
import json
import os
from datetime import datetime
from typing import Any, Optional

import requests

from app.config import CN_TZ, Settings
from app.models import DataResult, DataStatus

FIELD_MAP = {
    "title": ["title", "标题", "news_title", "headline"],
    "text": ["content", "summary", "正文", "摘要", "text", "abstract"],
    "published_at": ["publish_time", "pub_time", "time", "date", "发布时间", "datetime", "publishTime"],
    "outlet": ["source", "来源", "media", "site"],
    "url": ["url", "link", "链接"],
}


def _pick(d: dict, names: list[str]) -> Any:
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    return None


def _parse_time(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        return datetime.fromtimestamp(ts, CN_TZ).isoformat()
    s = str(v).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[: len(datetime.now().strftime(fmt))], fmt).replace(tzinfo=CN_TZ).isoformat()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)).isoformat()
    except ValueError:
        return None


def normalize_news(payload: Any) -> list[dict]:
    """把 MCP 工具返回的内容尽量规整成新闻列表。"""
    rows: list[dict] = []
    if isinstance(payload, dict):
        for k in ("data", "items", "item", "list", "result", "news"):
            if isinstance(payload.get(k), list):
                payload = payload[k]
                break
    if isinstance(payload, list):
        for it in payload:
            if not isinstance(it, dict):
                continue
            rows.append({
                "title": _pick(it, FIELD_MAP["title"]),
                "text": _pick(it, FIELD_MAP["text"]),
                "published_at": _parse_time(_pick(it, FIELD_MAP["published_at"])),
                "outlet": _pick(it, FIELD_MAP["outlet"]),
                "url": _pick(it, FIELD_MAP["url"]),
                "raw": it,
            })
    return rows


class IFindMCP:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None):
        self.s = settings
        self.http = session or requests.Session()
        self.session_id: Optional[str] = None
        self._ids = itertools.count(1)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.s.ifind_mcp_token:
            name = os.environ.get("IFIND_MCP_AUTH_HEADER", "Authorization")
            h[name] = f"Bearer {self.s.ifind_mcp_token}" if name.lower() == "authorization" else self.s.ifind_mcp_token
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, payload: dict) -> Any:
        r = self.http.post(self.s.ifind_mcp_url, data=json.dumps(payload), headers=self._headers(),
                           timeout=self.s.http_timeout)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        r.raise_for_status()
        if "id" not in payload:
            return None
        ctype = r.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:].strip())
                    if msg.get("id") == payload["id"]:
                        return msg
            raise ValueError("SSE 响应中没有对应的 JSON-RPC 结果")
        return r.json()

    def _rpc(self, method: str, params: dict) -> Any:
        if not self.session_id:
            init = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "event-timeline", "version": "0.1"}}})
            if init and init.get("error"):
                raise RuntimeError(f"initialize 失败：{init['error']}")
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        msg = self._post({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params})
        if msg.get("error"):
            raise RuntimeError(str(msg["error"]))
        return msg.get("result")

    def _wrap(self, label: str, fn) -> DataResult:
        src = f"iFinD MCP {label}"
        if not self.s.ifind_enabled:
            return DataResult(status=DataStatus.not_configured, source=src, message="未配置 IFIND_MCP_URL，未调用 iFinD")
        try:
            return fn(src)
        except requests.Timeout:
            return DataResult(status=DataStatus.error, source=src, message="请求超时")
        except requests.HTTPError as e:
            return DataResult(status=DataStatus.error, source=src, message=f"HTTP {e.response.status_code}")
        except (requests.RequestException, ValueError, RuntimeError) as e:
            self.session_id = None
            return DataResult(status=DataStatus.error, source=src, message=f"{type(e).__name__}: {str(e)[:200]}")

    def list_tools(self) -> DataResult:
        def run(src):
            res = self._rpc("tools/list", {})
            tools = [{"name": t.get("name"), "description": (t.get("description") or "")[:200]}
                     for t in (res or {}).get("tools", [])]
            return DataResult(status=DataStatus.ok if tools else DataStatus.missing, source=src, data=tools,
                              as_of=datetime.now(CN_TZ))
        return self._wrap("tools/list", run)

    def search_news(self, query: str, limit: int = 10) -> DataResult:
        def run(src):
            if not self.s.ifind_news_tool:
                return DataResult(status=DataStatus.not_configured, source=src,
                                  message="未配置 IFIND_NEWS_TOOL（新闻检索工具名），请先在 /api/ifind/tools 查看可用工具")
            res = self._rpc("tools/call", {"name": self.s.ifind_news_tool, "arguments": {"query": query, "limit": limit}})
            if (res or {}).get("isError"):
                return DataResult(status=DataStatus.error, source=src, message="工具返回 isError", data=res)
            texts = [c.get("text", "") for c in (res or {}).get("content", []) if c.get("type") == "text"]
            payload: Any = None
            for t in texts:
                try:
                    payload = json.loads(t)
                    break
                except ValueError:
                    continue
            rows = normalize_news(payload) if payload is not None else []
            if not rows:
                return DataResult(status=DataStatus.missing, source=src,
                                  message="未能解析出新闻列表，原始返回见 data", data={"raw_text": texts[:3]})
            return DataResult(status=DataStatus.ok, source=src, data=rows[:limit], as_of=datetime.now(CN_TZ))
        return self._wrap(f"新闻检索（{self.s.ifind_news_tool or '未配置'}）", run)
