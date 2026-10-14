"""扶摇（同花顺金融数据 API）REST 客户端。

文档：https://fuyao.aicubes.cn/docs/ ，鉴权请求头 X-api-key。
所有方法返回 DataResult：成功、缺失、限流、鉴权失败、超时都显式带状态，前端据此展示，
绝不在失败时返回空列表冒充"正常"。
注意：扶摇"资讯事件库"接口暂未开放外部接入，因此新闻正文不从扶摇获取。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Optional

import requests

from app.config import CN_TZ, Settings
from app.models import DataResult, DataStatus

ERROR_MESSAGES = {
    1001: "缺少必填参数",
    1002: "参数格式错误",
    1003: "参数取值越界",
    1004: "参数冲突",
    2001: "API Key 缺失或无效",
    2003: "API Key 无权访问该能力",
    3001: "标的不存在",
    3002: "数据未就绪",
    3004: "标的类型不支持该能力",
    4001: "触发限流，请稍后重试",
    5001: "服务内部错误",
    5002: "上游服务超时",
    5003: "数据源不可用",
}
MISSING_CODES = {3001, 3002, 3004}


class FuyaoClient:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None,
                 clock: Callable[[], float] = time.time):
        self.s = settings
        self.http = session or requests.Session()
        self.clock = clock
        self._cache: dict[str, tuple[float, DataResult]] = {}

    def _get(self, path: str, params: dict[str, Any], label: str, ttl: float = 60) -> DataResult:
        src = f"扶摇 {label}（{path}）"
        if not self.s.fuyao_enabled:
            return DataResult(status=DataStatus.not_configured, source=src,
                              message="未配置 FUYAO_API_KEY，未调用扶摇接口")
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < ttl:
            return hit[1]
        try:
            r = self.http.get(self.s.fuyao_base_url + path, params=params,
                              headers={"X-api-key": self.s.fuyao_api_key}, timeout=self.s.http_timeout)
        except requests.Timeout:
            return DataResult(status=DataStatus.error, source=src, message="请求超时")
        except requests.RequestException as e:
            return DataResult(status=DataStatus.error, source=src, message=f"网络错误：{type(e).__name__}")
        if r.status_code == 429:
            return DataResult(status=DataStatus.error, source=src, message="HTTP 429 限流，请稍后重试")
        try:
            body = r.json()
        except ValueError:
            return DataResult(status=DataStatus.error, source=src, message=f"HTTP {r.status_code}，响应不是 JSON")
        code = body.get("code")
        if code != 0:
            msg = ERROR_MESSAGES.get(code, body.get("message") or "未知错误")
            status = DataStatus.missing if code in MISSING_CODES else DataStatus.error
            return DataResult(status=status, source=src,
                              message=f"code={code}：{msg}（request_id={body.get('request_id')}）")
        data = body.get("data")
        ts = (data or {}).get("timestamp") if isinstance(data, dict) else None
        res = DataResult(
            status=DataStatus.ok, source=src, data=data,
            as_of=datetime.fromtimestamp(ts / 1000, CN_TZ) if isinstance(ts, (int, float)) and ts > 0 else None,
        )
        items = (data or {}).get("item") if isinstance(data, dict) else None
        if isinstance(items, list) and not items:
            res.status = DataStatus.missing
            res.message = "接口调用成功但返回为空"
        self._cache[key] = (self.clock(), res)
        return res

    # ---- 接口封装 ----
    def search_ticker(self, q: str, limit: int = 10) -> DataResult:
        return self._get("/api/meta/tickers/search", {"q": q, "asset_type": "a-share", "limit": limit}, "标的检索", ttl=3600)

    def snapshot(self, thscodes: list[str]) -> DataResult:
        return self._get("/api/a-share/prices/snapshot", {"thscodes": ",".join(thscodes)}, "行情快照", ttl=30)

    def historical(self, thscode: str, start: datetime, end: datetime, adjust: str = "forward") -> DataResult:
        return self._get("/api/a-share/prices/historical", {
            "thscode": thscode, "interval": "1d",
            "start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000), "adjust": adjust,
        }, "历史K线", ttl=600)

    def trading_days(self) -> DataResult:
        return self._get("/api/a-share/calendar/trading-days", {}, "交易日历", ttl=6 * 3600)

    def anomaly(self, thscodes: list[str]) -> DataResult:
        return self._get("/api/a-share/special-data/anomaly-analysis-stock",
                         {"thscodes": ",".join(thscodes)}, "个股异动原因", ttl=300)

    def concept_list(self, tag: str = "cn_concept") -> DataResult:
        return self._get("/api/a-share-index/catalog/ths-index-list", {"tag": tag}, "同花顺指数列表", ttl=6 * 3600)

    def constituents(self, index_thscode: str) -> DataResult:
        return self._get("/api/a-share-index/constituents/ths-stock-list", {"thscode": index_thscode},
                         "指数成分股", ttl=3600)
