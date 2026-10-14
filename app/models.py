"""核心数据模型。

设计要点：
- 产品的基本单位是"主张"(Claim)：某个来源在某个时刻对某个事实槽位说了什么。
- 来源(Source)保留四个时间：事件发生时间 / 披露时间 / 抓取时间 / 更新时间。
- 当前结论不由模型直接生成，而是由 engine.py 按规则从主张计算得出。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    announcement = "announcement"    # 交易所公告（正式披露）
    regulator = "regulator"          # 交易所问询函 / 监管文件
    company_reply = "company_reply"  # 公司澄清、互动易/e互动回复
    media = "media"                  # 权威财经媒体
    research = "research"            # 卖方研报
    self_media = "self_media"        # 自媒体 / 财经大V
    rumor = "rumor"                  # 股吧、群聊等市场传闻
    market_note = "market_note"      # 行情异动解读（如扶摇个股异动原因）


SOURCE_TYPE_LABEL = {
    SourceType.announcement: "公司公告",
    SourceType.regulator: "监管/交易所",
    SourceType.company_reply: "公司回应",
    SourceType.media: "财经媒体",
    SourceType.research: "卖方研报",
    SourceType.self_media: "自媒体",
    SourceType.rumor: "市场传闻",
    SourceType.market_note: "异动解读",
}


class Nature(str, Enum):
    """主张性质：题目要求区分的四类。"""
    fact = "fact"                # 事实（可核验的陈述）
    opinion = "opinion"          # 观点（评价、判断）
    speculation = "speculation"  # 推测（对未来/未知的推断）
    rumor = "rumor"              # 传闻（来源不明、未经证实）


NATURE_LABEL = {
    Nature.fact: "事实",
    Nature.opinion: "观点",
    Nature.speculation: "推测",
    Nature.rumor: "传闻",
}


class Action(str, Enum):
    assert_ = "assert"    # 提出/重申一个取值
    deny = "deny"         # 否认某个取值（或否认该槽位存在确定取值）
    correct = "correct"   # 同一发布方更正自己此前的取值
    retract = "retract"   # 撤回此前说法


class Attribution(str, Enum):
    official = "official"    # 署名/官方
    named = "named"          # 有明确出处
    anonymous = "anonymous"  # 匿名知情人士、"据悉"


class Source(BaseModel):
    id: str
    event_id: Optional[str] = None
    title: str
    text: str
    url: Optional[str] = None
    outlet: str                      # 发布方，如"辰星电子"、"财经媒体A"
    issuer: str                      # 归一化发布主体，用于判断"同一发布方更正"
    source_type: SourceType
    event_time: Optional[datetime] = None    # 事件实际发生时间（如签约日）
    published_at: datetime                   # 披露时间
    fetched_at: datetime                     # 抓取时间
    updated_at: Optional[datetime] = None    # 原文更新时间（新版本生效时间）
    version_of: Optional[str] = None         # 本条是哪条来源的新版本
    reposted_from: Optional[str] = None      # 转载自哪条来源
    origin: str = "demo"                     # demo / ingest / ifind / fuyao
    is_constructed: bool = False             # 是否为构造样例数据

    @property
    def effective_at(self) -> datetime:
        """该版本对外可见、开始影响结论的时间。"""
        return self.updated_at or self.published_at


class Claim(BaseModel):
    id: str
    source_id: str
    event_id: str
    slot: str
    value: Optional[str] = None          # 归一化取值；deny 时可为空（否认任何确定取值）
    display_value: Optional[str] = None  # 展示用原样取值
    unit: Optional[str] = None
    nature: Nature
    action: Action = Action.assert_
    attribution: Attribution = Attribution.named
    quote: str                           # 原文片段，必须能在 source.text 中找到
    valid_until: Optional[datetime] = None   # 带期限的事项，过期未兑现需提示
    extraction: str = "manual"           # manual / llm / rule_fallback
    confidence: float = 1.0


class SlotDef(BaseModel):
    key: str
    label: str
    unit: Optional[str] = None
    is_fact_slot: bool = True   # 观点/推测类槽位(如盈利影响)不进入事实账本
    # attribute：静态事实，同等级来源说法不同即为冲突（如交易对价）
    # progress：会随时间推进的状态，同等级来源以最新说法为准（如交易进展、监管问询）
    kind: str = "attribute"


class EventSubject(BaseModel):
    name: str
    thscode: Optional[str] = None
    role: str                   # acquirer / target / related
    aliases: list[str] = Field(default_factory=list)
    is_constructed: bool = False


class Event(BaseModel):
    id: str
    title: str
    event_type: str             # ma（并购重组）等
    subjects: list[EventSubject]
    keywords: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    is_constructed: bool = False
    note: Optional[str] = None


class WatchItem(BaseModel):
    thscode: str
    name: str
    is_constructed: bool = False
    concepts: list[str] = Field(default_factory=list)


class DataStatus(str, Enum):
    ok = "ok"
    stale = "stale"
    missing = "missing"
    error = "error"
    not_configured = "not_configured"


class DataResult(BaseModel):
    """所有外部数据调用的统一返回：失败也必须显式带状态，禁止静默。"""
    status: DataStatus
    source: str                 # 数据源名称，如 "扶摇 /api/a-share/prices/historical"
    message: str = ""
    as_of: Optional[datetime] = None
    data: Optional[object] = None
