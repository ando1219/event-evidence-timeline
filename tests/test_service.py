"""服务层：导入主链路、输入校验、接口失败降级、合规边界。"""
import unittest

import requests

from app.datasources.fuyao import FuyaoClient
from app.service import Service, ServiceError
from tests.helpers import FakeResponse, FakeSession, demo_store, settings, slot, t

FORMAL = {
    "title": "辰星电子：关于签署股权转让协议的公告", "outlet": "辰星电子", "issuer": "company_chenxing",
    "source_type": "announcement", "published_at": "2026-10-13T20:00:00+08:00",
    "text": "公司于近日与海岳光芯股东签署《股权转让协议》，以现金方式收购海岳光芯55%股权，交易对价最终确定为24.5亿元。",
}


def svc(**kw):
    s = settings(**kw)
    return Service(s, demo_store())


class Ingest(unittest.TestCase):
    def test_announcement_is_merged_and_changes_conclusion(self):
        s = svc()
        r = s.ingest(FORMAL)
        self.assertEqual(r["event_id"], "evt_demo_ma")
        self.assertTrue(r["match"]["matched"])
        labels = {c["slot_label"]: c["to"] for c in r["conclusion_changes"]}
        self.assertEqual(labels["交易对价"], "已确认：24.5亿元")
        self.assertEqual(labels["交易进展"], "已确认：已签正式协议")
        # 旧公告的 22.8 被同一发布方的新说法替代，不是冲突
        st = s.event_detail("evt_demo_ma")
        self.assertEqual(slot(st, "consideration")["status"], "confirmed")

    def test_replay_before_ingested_source_is_unchanged(self):
        s = svc()
        s.ingest(FORMAL)
        st = s.event_detail("evt_demo_ma", t("2026-10-01 00:00"))
        self.assertEqual(slot(st, "consideration")["value"], "22.8亿元（暂定）")

    def test_unrelated_rumor_creates_new_event(self):
        s = svc()
        r = s.ingest({"source_type": "rumor", "outlet": "股吧", "published_at": "2026-10-13T10:00:00+08:00",
                      "text": "听说云帆材料要收购北辰新能，估值大概12亿，下周停牌。"})
        self.assertIsNotNone(r["created_event"])
        self.assertEqual(r["created_event"]["title"], "云帆材料拟收购北辰新能")
        st = s.event_detail(r["event_id"])
        self.assertEqual(slot(st, "consideration")["status"], "unverified")

    def test_version_update_inherits_event_and_publish_time(self):
        s = svc()
        r = s.ingest({"source_type": "media", "outlet": "财经媒体B", "issuer": "media_b", "version_of": "s04",
                      "published_at": "2026-10-10T09:00:00+08:00",
                      "text": "【更新】辰星电子收购海岳光芯的交易对价暂定为22.8亿元。"})
        self.assertEqual(r["event_id"], "evt_demo_ma")
        src = s.store.sources[r["source_id"]]
        self.assertEqual(src.published_at, s.store.sources["s04"].published_at)
        self.assertEqual(src.updated_at.isoformat(), "2026-10-10T09:00:00+08:00")

    def test_invalid_inputs(self):
        s = svc()
        cases = [
            {"text": "", "source_type": "media"},
            {"text": "x" * 6000, "source_type": "media"},
            {"text": "abc", "source_type": "blog"},
            {"text": "abc", "source_type": "media", "published_at": "2030-01-01T00:00:00+08:00"},
            {"text": "abc", "source_type": "media", "published_at": "昨天"},
            {"text": "abc", "source_type": "media", "version_of": "nope"},
            {"text": "abc", "source_type": "media", "event_id": "nope"},
        ]
        for c in cases:
            with self.assertRaises(ServiceError, msg=str(c)[:60]):
                s.ingest(c)

    def test_extraction_log_records_fallback(self):
        s = svc()
        s.ingest(FORMAL)
        log = s.event_detail("evt_demo_ma")["extraction_log"]
        self.assertEqual(log[-1]["method"], "rule_fallback")
        self.assertIn("未配置", log[-1]["fallback_reason"])


class EventAndTime(unittest.TestCase):
    def test_future_as_of_is_clamped_to_now(self):
        st = svc().event_detail("evt_demo_ma", t("2027-01-01 00:00"))
        self.assertEqual(st["as_of"][:16], "2026-10-14T12:00")

    def test_unknown_event(self):
        with self.assertRaises(ServiceError) as cm:
            svc().event_detail("nope")
        self.assertEqual(cm.exception.status, 404)

    def test_trade_date_fallback_is_labelled_when_calendar_missing(self):
        st = svc().event_detail("evt_demo_ma")
        self.assertEqual(st["calendar_status"]["status"], "not_configured")
        s07 = next(x for x in st["timeline"] if x["source_id"] == "s07")
        self.assertEqual(s07["trade_date"]["date"], "2026-09-16")  # 周二 20:00 披露 → 周三
        self.assertEqual(s07["trade_date"]["method"], "weekday_estimate")


class ExternalDataFailures(unittest.TestCase):
    def test_watchlist_without_fuyao_key_is_explicit(self):
        s = svc()
        s.store.watchlist.clear()
        r = s.add_watch("600519.SH")
        self.assertFalse(r["verified"])
        self.assertIn("未能通过扶摇校验", r["warning"])
        q = s.watchlist()["items"][0]["quote"]
        self.assertEqual(q["status"], "not_configured")

    def test_watch_name_without_fuyao_is_rejected(self):
        with self.assertRaises(ServiceError):
            svc().add_watch("贵州茅台")

    def test_fuyao_auth_error_surfaces_in_market(self):
        s = svc(fuyao_api_key="bad")
        s.fuyao = FuyaoClient(s.settings, session=FakeSession([FakeResponse(200, {"code": 2001, "message": "unauth", "request_id": "r1"})]))
        m = s.market("evt_demo_ma", "600519.SH")
        self.assertEqual(m["result"]["status"], "error")
        self.assertIn("2001", m["result"]["message"])
        self.assertEqual(m["bars"], [])

    def test_fuyao_timeout_surfaces_in_watchlist(self):
        s = svc(fuyao_api_key="k")
        s.fuyao = FuyaoClient(s.settings, session=FakeSession([requests.Timeout()]))
        s.store.add_watch(__import__("app.models", fromlist=["WatchItem"]).WatchItem(thscode="600519.SH", name="贵州茅台"))
        q = next(i for i in s.watchlist()["items"] if i["thscode"] == "600519.SH")["quote"]
        self.assertEqual(q["status"], "error")
        self.assertEqual(q["message"], "请求超时")

    def test_market_for_constructed_ticker_has_no_fake_data(self):
        m = svc().market("evt_demo_ma", "DEMO01.DEMO")
        self.assertEqual(m["result"]["status"], "missing")
        self.assertEqual(m["bars"], [])

    def test_market_ok_computes_marker_change(self):
        s = svc(fuyao_api_key="k")
        bars = {"code": 0, "data": {"timestamp": 1, "item": [
            {"date_ms": 1789401600000, "close_price": 10.0, "volume": 1},   # 2026-09-15
            {"date_ms": 1789488000000, "close_price": 11.0, "volume": 1},   # 2026-09-16
        ]}}
        cal = {"code": 0, "data": {"timestamp": 1, "item": [{"date_ms": 0, "date": d} for d in
                                                         ["20260914", "20260915", "20260916", "20260917"]]}}
        s.fuyao = FuyaoClient(s.settings, session=FakeSession([FakeResponse(200, bars), FakeResponse(200, cal)]))
        m = s.market("evt_demo_ma", "600519.SH")
        mk = next(x for x in m["markers"] if x["source_id"] == "s07")
        self.assertEqual(mk["trade_date"]["date"], "2026-09-16")
        self.assertEqual(mk["day_change_pct"], 10.0)
        self.assertIn("不构成预测", m["note"])

    def test_ifind_not_configured(self):
        r = svc().ifind_news("辰星电子")
        self.assertEqual(r["status"], "not_configured")


class Compliance(unittest.TestCase):
    def test_prediction_and_advice_refused(self):
        s = svc()
        for q in ["辰星电子明天会涨吗", "现在能不能买入", "目标价多少", "要不要加仓", "还能涨多少"]:
            r = s.ask("evt_demo_ma", q)
            self.assertTrue(r["refused"], q)
            self.assertIn("不构成投资建议", r["answer"])

    def test_fact_question_answered_with_citations(self):
        r = svc().ask("evt_demo_ma", "交易对价现在是多少？")
        self.assertFalse(r["refused"])
        self.assertIn("22.8亿元", r["answer"])
        self.assertIn("辰星电子（公司公告", r["answer"])
        self.assertTrue(r["citations"])

    def test_answer_respects_replay_time_and_conflict(self):
        r = svc().ask("evt_demo_ma", "对价多少", t("2026-09-02 13:00"))
        self.assertIn("存在冲突", r["answer"])

    def test_missing_fact_is_not_invented(self):
        r = svc().ask("evt_demo_ma", "股权比例是多少", t("2026-09-03 00:00"))
        self.assertIn("没有任何来源提及", r["answer"])


if __name__ == "__main__":
    unittest.main()
