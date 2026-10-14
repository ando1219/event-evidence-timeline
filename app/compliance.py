"""合规边界：不输出确定性涨跌预测、收益承诺或买卖建议。

问答不调用模型自由生成：只根据规则引擎算出的当前结论拼装回答，每句都带来源。
"""
from __future__ import annotations

import re

PREDICTION_PATTERNS = [
    r"会不会?涨", r"会不会?跌", r"能涨", r"能跌", r"涨到", r"跌到", r"目标价", r"还能涨", r"还会跌",
    r"明天.*(涨|跌)", r"后市", r"走势.*(怎么|如何)", r"(涨|跌)多少", r"涨停吗", r"跌停吗",
]
ADVICE_PATTERNS = [
    r"(该|要|能|可以|应该)不?(该|要|能|可以)?(买|卖|加仓|减仓|抄底|清仓|持有|止损|上车|跑)",
    r"买入|卖出|建仓|满仓|梭哈|抄底|割肉|仓位", r"值得(买|投资)", r"推荐.*股", r"稳赚|保本|收益率能",
]
DISCLAIMER = "本产品只整理事实与证据，不构成投资建议，不预测涨跌。"

FORBIDDEN_OUTPUT = re.compile(r"(必涨|必跌|稳赚|保本|建议买入|建议卖出|目标价)")


def classify_question(q: str) -> str:
    q = q.strip()
    if any(re.search(p, q) for p in ADVICE_PATTERNS):
        return "advice"
    if any(re.search(p, q) for p in PREDICTION_PATTERNS):
        return "prediction"
    return "fact"


def refusal(kind: str) -> str:
    what = "买卖或仓位建议" if kind == "advice" else "股价涨跌预测"
    return (f"这个问题涉及{what}，本产品不提供。我可以告诉你这件事目前哪些已确认、哪些仍有争议或未证实，"
            f"以及每条结论的出处，帮助你自己判断。{DISCLAIMER}")


def sanitize(text: str) -> str:
    """兜底：任何输出若出现禁用表述，替换为提示。"""
    return FORBIDDEN_OUTPUT.sub("［已屏蔽：涉及预测或建议］", text)


SLOT_KEYWORDS = {
    "target": ["标的", "收购谁", "买谁", "收购对象"],
    "consideration": ["对价", "价格", "多少钱", "估值", "作价", "金额"],
    "stake": ["股权", "比例", "多少股", "控股"],
    "payment": ["支付", "现金", "发股", "怎么付"],
    "deal_stage": ["进展", "到哪", "阶段", "签了", "协议", "状态"],
    "trading_halt": ["停牌"],
    "financing": ["融资", "定增", "募资", "资金来源", "钱从哪"],
    "regulatory": ["问询", "关注函", "监管", "交易所"],
}


def answer(question: str, state: dict) -> dict:
    kind = classify_question(question)
    if kind != "fact":
        return {"kind": kind, "refused": True, "answer": refusal(kind), "citations": []}
    slots = {s["key"]: s for s in state["slots"]}
    keys = [k for k, words in SLOT_KEYWORDS.items() if any(w in question for w in words)]
    if not keys:
        keys = [k for k, s in slots.items() if s["status"] != "missing"]
        lead = "没有识别到具体问的是哪一项，以下是当前全部结论："
    else:
        lead = ""
    lines, cites = [], []
    for k in keys:
        s = slots.get(k)
        if not s:
            continue
        if s["status"] == "missing":
            lines.append(f"「{s['label']}」：目前没有任何来源提及，无法回答。")
            continue
        src_ids = s["deciding_claim_ids"]
        refs = [state["claims"][c] for c in src_ids if c in state["claims"]]
        ref_txt = "；".join(f"{r['outlet']}（{r['source_type_label']}，{r['effective_at'][:16].replace('T', ' ')}）" for r in refs[:3])
        line = f"「{s['label']}」：{s['value']}，状态为「{s['status_label']}」"
        if ref_txt:
            line += f"，依据：{ref_txt}"
        if s.get("conflict"):
            line += f"。注意：{s['conflict']['message']}"
        for f in s.get("flags", []):
            line += f"。提示：{f}"
        lines.append(line + "。")
        cites.extend(src_ids)
    text = (lead + "\n" if lead else "") + "\n".join(lines) + f"\n（截至 {state['as_of'][:16].replace('T', ' ')}。{DISCLAIMER}）"
    return {"kind": kind, "refused": False, "answer": sanitize(text), "citations": cites}
