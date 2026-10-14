"""数据源：扶摇错误码映射、交易日换算、iFinD MCP 解析。"""
import json
import unittest
from datetime import date

import requests

from app.datasources.calendar import effective_trade_date
from app.datasources.fuyao import FuyaoClient
from app.datasources.ifind import IFindMCP, normalize_news
from app.models import DataStatus
from tests.helpers import FakeResponse, FakeSession, settings, t


def client(*responses, **kw):
    s = settings(fuyao_api_key="k", **kw)
    return FuyaoClient(s, session=FakeSession(list(responses)), clock=lambda: 1000.0)


class Fuyao(unittest.TestCase):
    def test_not_configured(self):
        r = FuyaoClient(settings()).trading_days()
        self.assertEqual(r.status, DataStatus.not_configured)

    def test_ok_and_header(self):
        c = client(FakeResponse(200, {"code": 0, "data": {"timestamp": 1748275200000, "item": [{"date": "20250527"}]}}))
        r = c.trading_days()
        self.assertEqual(r.status, DataStatus.ok)
        self.assertEqual(r.as_of.isoformat()[:10], "2025-05-27")
        self.assertEqual(c.http.calls[0][2]["headers"]["X-api-key"], "k")

    def test_error_codes(self):
        for code, status in [(2001, DataStatus.error), (2003, DataStatus.error), (3001, DataStatus.missing),
                             (3002, DataStatus.missing), (4001, DataStatus.error), (5002, DataStatus.error)]:
            r = client(FakeResponse(200, {"code": code, "message": "x", "request_id": "rid"})).snapshot(["600519.SH"])
            self.assertEqual(r.status, status, code)
            self.assertIn(str(code), r.message)

    def test_http_429_timeout_and_non_json(self):
        self.assertIn("429", client(FakeResponse(429, None)).snapshot(["600519.SH"]).message)
        self.assertEqual(client(requests.Timeout()).snapshot(["600519.SH"]).message, "请求超时")
        self.assertIn("不是 JSON", client(FakeResponse(502, None)).snapshot(["600519.SH"]).message)

    def test_empty_items_is_missing_not_ok(self):
        r = client(FakeResponse(200, {"code": 0, "data": {"timestamp": 1, "item": []}})).snapshot(["600519.SH"])
        self.assertEqual(r.status, DataStatus.missing)

    def test_cache(self):
        c = client(FakeResponse(200, {"code": 0, "data": {"timestamp": 1, "item": [{"x": 1}]}}))
        c.trading_days()
        c.trading_days()
        self.assertEqual(len(c.http.calls), 1)


DAYS = [date(2026, 9, d) for d in (14, 15, 16, 17, 18, 21, 22)]


class TradeDate(unittest.TestCase):
    def test_after_close_rolls_to_next_trading_day(self):
        r = effective_trade_date(t("2026-09-15 20:00"), DAYS)
        self.assertEqual((r["date"], r["method"]), ("2026-09-16", "calendar"))

    def test_intraday_and_premarket_same_day(self):
        self.assertEqual(effective_trade_date(t("2026-09-15 10:30"), DAYS)["date"], "2026-09-15")
        self.assertIn("盘前", effective_trade_date(t("2026-09-15 08:00"), DAYS)["note"])

    def test_friday_evening_skips_weekend(self):
        self.assertEqual(effective_trade_date(t("2026-09-18 19:00"), DAYS)["date"], "2026-09-21")

    def test_fallback_is_labelled(self):
        r = effective_trade_date(t("2026-09-19 10:00"), [])
        self.assertEqual((r["date"], r["method"]), ("2026-09-21", "weekday_estimate"))
        self.assertIn("未剔除法定节假日", r["note"])

    def test_out_of_calendar_range_is_labelled(self):
        r = effective_trade_date(t("2026-12-01 10:00"), DAYS)
        self.assertEqual(r["method"], "weekday_estimate")
        self.assertIn("超出", r["note"])


class IFind(unittest.TestCase):
    def test_normalize_news_maps_common_fields(self):
        rows = normalize_news({"data": [{"标题": "A", "摘要": "B", "发布时间": "2026-09-02 10:05:00", "来源": "C"}]})
        self.assertEqual((rows[0]["title"], rows[0]["text"], rows[0]["outlet"]), ("A", "B", "C"))
        self.assertTrue(rows[0]["published_at"].startswith("2026-09-02T10:05:00"))

    def test_search_without_tool_name(self):
        s = settings(ifind_mcp_url="https://mcp.example")
        sess = FakeSession([FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"Mcp-Session-Id": "sid"})])
        r = IFindMCP(s, session=sess).search_news("x")
        self.assertEqual(r.status, DataStatus.not_configured)

    def test_search_parses_sse_result(self):
        s = settings(ifind_mcp_url="https://mcp.example", ifind_news_tool="news_search")
        news = json.dumps({"items": [{"title": "辰星电子公告", "content": "正文", "publish_time": 1789488000000}]}, ensure_ascii=False)
        sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": news}]}}, ensure_ascii=False)
        sess = FakeSession([
            FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": {}}, headers={"Mcp-Session-Id": "sid", "Content-Type": "application/json"}),
            FakeResponse(202, None, headers={}, text=""),
            FakeResponse(200, None, headers={"Content-Type": "text/event-stream"}, text=sse),
        ])
        r = IFindMCP(s, session=sess).search_news("辰星")
        self.assertEqual(r.status, DataStatus.ok)
        self.assertEqual(r.data[0]["title"], "辰星电子公告")
        self.assertEqual(sess.calls[2][2]["headers"]["Mcp-Session-Id"], "sid")

    def test_http_error_is_reported(self):
        s = settings(ifind_mcp_url="https://mcp.example", ifind_news_tool="news_search")
        r = IFindMCP(s, session=FakeSession([FakeResponse(401, {"error": "unauthorized"})])).search_news("x")
        self.assertEqual(r.status, DataStatus.error)
        self.assertIn("401", r.message)


if __name__ == "__main__":
    unittest.main()
