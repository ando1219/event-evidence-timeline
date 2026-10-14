"""事件类型的槽位定义。MVP 只做"并购重组"一种类型，槽位清晰、演化链条长。"""
from app.models import SlotDef

EVENT_SCHEMAS: dict[str, list[SlotDef]] = {
    "ma": [
        SlotDef(key="target", label="收购标的"),
        SlotDef(key="consideration", label="交易对价", unit="亿元"),
        SlotDef(key="stake", label="收购股权比例", unit="%"),
        SlotDef(key="payment", label="支付方式"),
        SlotDef(key="deal_stage", label="交易进展", kind="progress"),
        SlotDef(key="trading_halt", label="是否停牌", kind="progress"),
        SlotDef(key="financing", label="配套融资"),
        SlotDef(key="regulatory", label="监管问询", kind="progress"),
        SlotDef(key="earnings_impact", label="盈利影响（观点）", is_fact_slot=False),
    ],
}

EVENT_TYPE_LABEL = {"ma": "并购重组"}

# 交易进展的推进顺序，用于事件阶段展示
DEAL_STAGE_ORDER = ["传闻", "媒体报道", "初步洽谈", "已签意向协议", "已签正式协议", "已获批准", "已完成", "已终止"]


def slot_defs(event_type: str) -> list[SlotDef]:
    return EVENT_SCHEMAS.get(event_type, [])


def slot_label(event_type: str, key: str) -> str:
    for s in slot_defs(event_type):
        if s.key == key:
            return s.label
    return key
