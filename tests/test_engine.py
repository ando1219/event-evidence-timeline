"""规则引擎：主链路（传闻→报道→冲突→公司回应→公告→更正）与各类边界。"""
import unittest
from datetime import timedelta

from app.engine import EventEngine, claim_weight
from app.models import Action, Attribution, Claim, Event, EventSubject, Nature, Source, SourceType
from tests.helpers import NOW, demo_engine, demo_store, slot, t


class DemoDataIntegrity(unittest.TestCase):
    def test_every_quote_is_in_source_text(self):
        st = demo_store()
        for c in st.claims.values():
            self.assertIn(c.quote, st.sources[c.source_id].text, c.id)

    def test_demo_is_marked_constructed(self):
        st = demo_store()
        self.assertTrue(all(s.is_constructed for s in st.sources.values()))
        self.assertTrue(all(e.is_constructed for e in st.events.values()))


class MainChain(unittest.TestCase):
    def setUp(self):
        self.eng = demo_engine()

    def test_before_any_source_everything_missing(self):
        st = self.eng.state(t("2026-08-01 00:00"))
        self.assertTrue(all(s["status"] == "missing" for s in st["slots"]))
        self.assertEqual(st["visible_source_count"], 0)

    def test_rumor_stage(self):
        st = self.eng.state(t("2026-09-01 22:00"))
        self.assertEqual(slot(st, "consideration")["status"], "unverified")
        self.assertEqual(slot(st, "consideration")["value"], "约30亿元")
        self.assertEqual(st["stage"], "传闻")

    def test_media_overrides_rumor(self):
        st = self.eng.state(t("2026-09-02 11:00"))
        c = slot(st, "consideration")
        self.assertEqual((c["status"], c["value"]), ("reported", "约25亿元"))
        self.assertEqual(st["claims"]["c02"]["status"], "overridden")

    def test_same_rank_media_conflict(self):
        st = self.eng.state(t("2026-09-02 13:00"))
        c = slot(st, "consideration")
        self.assertEqual(c["status"], "disputed")
        self.assertEqual(c["conflict"]["kind"], "same_rank_conflict")
        self.assertEqual(len(c["conflict"]["sides"]), 2)
        self.assertIn("consideration", st["buckets"]["disputed"])

    def test_company_reply_resolves_conflict(self):
        st = self.eng.state(t("2026-09-02 20:00"))
        c = slot(st, "consideration")
        self.assertEqual((c["status"], c["value"]), ("confirmed", "尚未确定"))
        self.assertEqual(st["claims"]["c10"]["status"], "overridden")
        self.assertEqual(st["claims"]["c12"]["status"], "overridden")
        self.assertEqual(st["conflicts"], [])

    def test_denial_changes_conclusion(self):
        before = slot(self.eng.state(t("2026-09-02 19:00")), "trading_halt")
        after_state = self.eng.state(t("2026-09-02 20:00"))
        after = slot(after_state, "trading_halt")
        self.assertEqual(before["status"], "unverified")
        self.assertEqual(after["status"], "denied")
        self.assertEqual(after_state["claims"]["c03"]["status"], "denied")

    def test_announcement_confirms_and_supersedes_company_reply(self):
        st = self.eng.state(t("2026-09-15 20:30"))
        self.assertEqual(slot(st, "consideration")["value"], "22.8亿元（暂定）")
        self.assertEqual(slot(st, "deal_stage")["value"], "已签意向协议")
        self.assertEqual(slot(st, "trading_halt")["value"], "不停牌")
        self.assertEqual(st["claims"]["c17"]["status"], "superseded_update")

    def test_correction_changes_stake(self):
        before = self.eng.state(t("2026-09-22 19:00"))
        after = self.eng.state(t("2026-09-22 21:00"))
        self.assertEqual(slot(before, "stake")["value"], "51%")
        self.assertEqual(slot(after, "stake")["value"], "55%")
        self.assertEqual(after["claims"]["c22"]["status"], "corrected")

    def test_source_update_supersedes_old_version(self):
        st = self.eng.state(t("2026-09-16 10:00"))
        self.assertEqual(st["claims"]["c10"]["status"], "superseded_version")
        self.assertEqual(st["claims"]["c27"]["status"], "supporting")  # 媒体更正后与公告一致

    def test_only_effective_sources_are_used(self):
        # 更新版在 09-16 09:30 生效，之前只能看到旧版本
        st = self.eng.state(t("2026-09-16 09:00"))
        self.assertNotIn("c27", st["claims"])
        self.assertEqual(st["claims"]["c10"]["status"], "overridden")


class TimeRules(unittest.TestCase):
    def setUp(self):
        self.eng = demo_engine()

    def test_rumor_expires_after_ttl(self):
        self.assertEqual(slot(self.eng.state(t("2026-09-15 21:00")), "financing")["status"], "unverified")
        self.assertEqual(slot(self.eng.state(t("2026-09-15 22:00")), "financing")["status"], "expired")
        st = self.eng.state(t("2026-09-29 00:00"))
        self.assertEqual(slot(st, "financing")["status"], "confirmed")
        self.assertEqual(slot(st, "financing")["value"], "自有资金及银行并购贷款")

    def test_overdue_flag_then_cleared(self):
        self.assertEqual(slot(self.eng.state(t("2026-09-25 12:00")), "regulatory")["flags"], [])
        flagged = slot(self.eng.state(t("2026-09-26 12:00")), "regulatory")
        self.assertTrue(any("逾期待核实" in f for f in flagged["flags"]))
        cleared = slot(self.eng.state(t("2026-09-29 00:00")), "regulatory")
        self.assertEqual(cleared["flags"], [])
        self.assertEqual(cleared["value"], "已回复关注函")

    def test_progress_slot_does_not_conflict(self):
        st = self.eng.state(NOW)
        self.assertEqual(slot(st, "regulatory")["status"], "confirmed")
        self.assertEqual(st["claims"]["c30"]["status"], "superseded_progress")

    def test_rumor_weight_decays_fact_does_not(self):
        st = demo_store()
        c, s = st.claims["c02"], st.sources["s01"]
        w1, _ = claim_weight(c, s, s.published_at)
        w2, _ = claim_weight(c, s, s.published_at + timedelta(days=7))
        self.assertAlmostEqual(w2, w1 / 2, places=6)
        f, fs = st.claims["c23"], st.sources["s07"]
        self.assertEqual(claim_weight(f, fs, fs.published_at)[0], claim_weight(f, fs, NOW)[0])

    def test_changes_include_time_triggered_expiry(self):
        ch = self.eng.changes(NOW)
        exp = [c for c in ch if c["slot"] == "financing" and c["to_status"] == "已过期·未证实"]
        self.assertEqual(len(exp), 1)
        self.assertIn("时间推移", exp[0]["trigger"])


class ReportsAndOrigin(unittest.TestCase):
    def test_repost_counts_as_one_independent_source(self):
        st = demo_engine().state(t("2026-09-02 09:00"))
        self.assertEqual(slot(st, "trading_halt")["independent_sources"], 1)

    def test_distorted_repost_is_flagged_and_not_deciding(self):
        st = demo_engine().state(t("2026-09-02 09:00"))
        self.assertEqual(slot(st, "consideration")["value"], "约30亿元")
        c07 = st["claims"]["c07"]
        self.assertEqual(c07["status"], "not_deciding")
        self.assertIn("转载自", c07["distortion"])
        self.assertEqual(len(st["origin"]["distortions"]), 1)

    def test_origin_points_to_earliest_rumor(self):
        st = demo_engine().state(NOW)
        self.assertEqual(st["origin"]["earliest_source_id"], "s01")
        self.assertIn("s01", st["origin"]["repost_tree"])

    def test_opinions_and_speculation_kept_out_of_facts(self):
        st = demo_engine().state(NOW)
        ids = {o["id"] for o in st["opinions"]}
        self.assertTrue({"c13", "c19", "c20", "c35"} <= ids)
        self.assertNotIn("earnings_impact", [s["key"] for s in st["slots"]])
        # 媒体C"可能上调至26亿"是推测，不应改变交易对价结论
        self.assertEqual(slot(st, "consideration")["value"], "22.8亿元（暂定）")

    def test_fetch_lag_warning_uses_effective_time(self):
        st = demo_engine().state(NOW)
        tl = {x["source_id"]: x for x in st["timeline"]}
        self.assertIsNotNone(tl["s03"]["fetch_lag_warning"])   # 抓取晚 7.5 小时
        self.assertIsNone(tl["s07"]["fetch_lag_warning"])
        self.assertIsNone(tl["s08"]["fetch_lag_warning"])      # 更新版按更新时间计算，只晚 12 分钟


def _mini(sources, claims):
    ev = Event(id="e", title="x", event_type="ma", subjects=[EventSubject(name="甲", role="acquirer")])
    return EventEngine(ev, sources, claims)


def _src(sid, stype, at, issuer=None, **kw):
    return Source(id=sid, event_id="e", title=sid, text="原文" + sid, outlet=issuer or sid, issuer=issuer or sid,
                  source_type=stype, published_at=at, fetched_at=at, **kw)


def _cl(cid, sid, slot_, value, nature=Nature.fact, action=Action.assert_):
    return Claim(id=cid, source_id=sid, event_id="e", slot=slot_, value=value, nature=nature, action=action,
                 attribution=Attribution.named, quote="原文" + sid)


class SyntheticEdgeCases(unittest.TestCase):
    def test_low_rank_denial_of_high_rank_is_conflict(self):
        eng = _mini(
            [_src("a", SourceType.announcement, t("2026-09-01 10:00")), _src("m", SourceType.media, t("2026-09-02 10:00"))],
            [_cl("1", "a", "consideration", "20"), _cl("2", "m", "consideration", "20", action=Action.deny)],
        )
        s = slot(eng.state(t("2026-09-03 00:00")), "consideration")
        self.assertEqual(s["status"], "disputed")
        self.assertEqual(s["conflict"]["kind"], "low_rank_denial")

    def test_same_issuer_latest_wins_over_all_earlier(self):
        eng = _mini(
            [_src("a1", SourceType.announcement, t("2026-09-01 10:00"), issuer="co"),
             _src("a2", SourceType.announcement, t("2026-09-02 10:00"), issuer="co"),
             _src("a3", SourceType.announcement, t("2026-09-03 10:00"), issuer="co")],
            [_cl("1", "a1", "consideration", "20"), _cl("2", "a2", "consideration", "20"), _cl("3", "a3", "consideration", "24")],
        )
        st = eng.state(t("2026-09-04 00:00"))
        self.assertEqual((slot(st, "consideration")["status"], slot(st, "consideration")["value"]), ("confirmed", "24"))
        self.assertEqual(st["claims"]["1"]["status"], "superseded_update")

    def test_late_low_rank_divergence_is_flagged_but_not_overriding(self):
        eng = _mini(
            [_src("a", SourceType.announcement, t("2026-09-01 10:00")), _src("m", SourceType.media, t("2026-09-05 10:00"))],
            [_cl("1", "a", "consideration", "20"), _cl("2", "m", "consideration", "25")],
        )
        s = slot(eng.state(t("2026-09-06 00:00")), "consideration")
        self.assertEqual((s["status"], s["value"]), ("confirmed", "20"))
        self.assertEqual(s["conflict"]["severity"], "low")

    def test_retraction_removes_claim(self):
        eng = _mini(
            [_src("m1", SourceType.media, t("2026-09-01 10:00"), issuer="m"),
             _src("m2", SourceType.media, t("2026-09-02 10:00"), issuer="m")],
            [_cl("1", "m1", "consideration", "20"), _cl("2", "m2", "consideration", None, action=Action.retract)],
        )
        st = eng.state(t("2026-09-03 00:00"))
        self.assertEqual(slot(st, "consideration")["status"], "missing")
        self.assertEqual(st["claims"]["1"]["status"], "retracted")


if __name__ == "__main__":
    unittest.main()
