"""抽取与校验：编造引用、非法字段被拒绝；LLM 失败时降级且留痕。"""
import unittest

import requests

from app.extractor import extract, llm_extract, quote_in_text, rule_extract, validate_claims
from app.models import Source, SourceType
from tests.helpers import NOW, FakeResponse, FakeSession, settings


def src(text, stype=SourceType.media):
    return Source(id="x", title="t", text=text, outlet="o", issuer="o", source_type=stype, published_at=NOW, fetched_at=NOW)


TEXT = "公司于2026年9月15日签署《股权收购意向协议》，拟以现金方式收购海岳光芯51%股权，交易对价暂定为22.8亿元。"


class Validation(unittest.TestCase):
    def run_v(self, raw):
        return validate_claims(raw, src(TEXT, SourceType.announcement), "e", "ma", "llm", 0.8)

    def test_accepts_good_claim_and_formats_unit(self):
        ok, rej = self.run_v([{"slot": "consideration", "value": "22.8", "nature": "fact", "action": "assert",
                               "attribution": "official", "quote": "交易对价暂定为22.8亿元"}])
        self.assertEqual(rej, [])
        self.assertEqual(ok[0].display_value, "22.8亿元")

    def test_rejects_fabricated_quote(self):
        ok, rej = self.run_v([{"slot": "consideration", "value": "30", "nature": "fact", "quote": "交易对价为30亿元"}])
        self.assertEqual(ok, [])
        self.assertIn("找不到", rej[0]["reason"])

    def test_rejects_unknown_slot_bad_enum_missing_value(self):
        ok, rej = self.run_v([
            {"slot": "ceo", "value": "张三", "nature": "fact", "quote": "签署"},
            {"slot": "stake", "value": "51", "nature": "certain", "quote": "51%股权"},
            {"slot": "stake", "value": "", "nature": "fact", "quote": "51%股权"},
            {"slot": "stake", "value": "一半", "nature": "fact", "quote": "51%股权"},
            {"slot": "deal_stage", "value": "已签意向协议", "nature": "fact", "quote": "签署", "valid_until": "下个月"},
        ])
        self.assertEqual(ok, [])
        self.assertEqual(len(rej), 5)

    def test_numeric_value_is_normalized(self):
        ok, _ = self.run_v([{"slot": "stake", "value": "51%", "nature": "fact", "quote": "51%股权"}])
        self.assertEqual(ok[0].value, "51")

    def test_quote_matching_ignores_whitespace_and_quote_style(self):
        self.assertTrue(quote_in_text("网传“下周停牌”不属实", '网传"下周停牌" 不属实'))


class LLMPath(unittest.TestCase):
    def test_llm_output_is_parsed_and_validated(self):
        content = '```json\n{"claims":[{"slot":"stake","value":"51","nature":"fact","action":"assert","attribution":"official","quote":"收购海岳光芯51%股权"},' \
                  '{"slot":"consideration","value":"99","nature":"fact","quote":"对价99亿元"}],"event":{"acquirer":"辰星电子","target":"海岳光芯"}}\n```'
        sess = FakeSession([FakeResponse(200, {"choices": [{"message": {"content": content}}]})])
        res = extract(src(TEXT, SourceType.announcement), "ma", "e", settings(llm_api_key="k"), session=sess)
        self.assertEqual(res["method"], "llm")
        self.assertEqual([c.slot for c in res["claims"]], ["stake"])
        self.assertEqual(len(res["rejected"]), 1)  # 编造的 99 亿被拒
        self.assertEqual(res["event_hint"]["target"], "海岳光芯")
        body = sess.calls[0][2]["json"]
        self.assertEqual(body["temperature"], 0)

    def test_llm_failure_falls_back_to_rules_with_reason(self):
        sess = FakeSession([requests.Timeout("slow")])
        res = extract(src(TEXT, SourceType.announcement), "ma", "e", settings(llm_api_key="k"), session=sess)
        self.assertEqual(res["method"], "rule_fallback")
        self.assertIn("LLM 调用失败", res["fallback_reason"])
        self.assertIn("consideration", [c.slot for c in res["claims"]])

    def test_retry_without_response_format_on_400(self):
        ok = FakeResponse(200, {"choices": [{"message": {"content": '{"claims": []}'}}]})
        sess = FakeSession([FakeResponse(400, {"error": "unsupported"}), ok])
        llm_extract(src(TEXT), "ma", settings(llm_api_key="k"), session=sess)
        self.assertNotIn("response_format", sess.calls[1][2]["json"])

    def test_no_llm_configured_uses_rules(self):
        res = extract(src(TEXT, SourceType.announcement), "ma", "e", settings())
        self.assertEqual(res["method"], "rule_fallback")
        self.assertIn("未配置", res["fallback_reason"])


class RuleFallback(unittest.TestCase):
    def slots(self, text, stype=SourceType.media):
        d = rule_extract(src(text, stype))
        ok, _ = validate_claims(d["claims"], src(text, stype), "e", "ma", "rule_fallback", 0.5)
        return {(c.slot, c.value, c.nature.value, c.action.value) for c in ok}

    def test_correction_takes_corrected_value(self):
        s = self.slots("“收购海岳光芯51%股权”表述有误，正确表述为“收购海岳光芯55%股权”。", SourceType.announcement)
        self.assertIn(("stake", "55", "fact", "correct"), s)

    def test_denial_of_termination_only_targets_that_value(self):
        s = self.slots("公司注意到网传交易终止的消息，该消息不属实。", SourceType.company_reply)
        self.assertEqual(s, {("deal_stage", "已终止", "fact", "deny")})

    def test_rumor_words_mark_rumor(self):
        s = self.slots("听说云帆材料要收购北辰新能，估值大概12亿。", SourceType.rumor)
        self.assertIn(("consideration", "12", "rumor", "assert"), s)

    def test_speculation_words(self):
        s = self.slots("据接近交易人士，交易对价或达28亿元。")
        self.assertIn(("consideration", "28", "speculation", "assert"), s)

    def test_quoted_old_value_is_skipped(self):
        s = self.slots("本文初稿称交易对价约25亿元。根据公告，交易对价暂定为22.8亿元，现予更正。")
        values = {v for (slot, v, *_rest) in s if slot == "consideration"}
        self.assertEqual(values, {"22.8"})


if __name__ == "__main__":
    unittest.main()
