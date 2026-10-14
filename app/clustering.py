"""事件归并：判断一条新来源属于哪个已有事件。

打分（可解释，便于测试）：
- 命中事件主体名称/别名：每个 +3（收购方与标的同时命中 +2 奖励）
- 命中事件关键词：每个 +1（上限 3）
- 与事件最近一条来源的时间距离：90 天内 +1，超过 180 天 -2
- LLM/规则抽到的标的与事件标的一致：+3
得分 >= 5 归入该事件；没有达标事件则建议新建事件（由用户确认）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.models import Event, Source

MATCH_THRESHOLD = 5


def score_event(event: Event, source: Source, last_seen: Optional[datetime], hint_target: Optional[str]) -> dict:
    text = source.title + "\n" + source.text
    score, reasons = 0, []
    roles_hit = set()
    for subj in event.subjects:
        names = [subj.name] + subj.aliases
        if any(n and n in text for n in names):
            score += 3
            roles_hit.add(subj.role)
            reasons.append(f"提到{('收购方' if subj.role == 'acquirer' else '标的' if subj.role == 'target' else '相关方')}「{subj.name}」")
    if {"acquirer", "target"} <= roles_hit:
        score += 2
        reasons.append("收购方与标的同时出现")
    kw = [k for k in event.keywords if k in text][:3]
    if kw:
        score += len(kw)
        reasons.append("关键词：" + "、".join(kw))
    if last_seen:
        gap = abs(source.published_at - last_seen)
        if gap <= timedelta(days=90):
            score += 1
            reasons.append("与该事件最近动态相隔 90 天内")
        elif gap > timedelta(days=180):
            score -= 2
            reasons.append("与该事件最近动态相隔超过 180 天")
    if hint_target:
        tgt = next((s for s in event.subjects if s.role == "target"), None)
        if tgt and (hint_target == tgt.name or hint_target in tgt.aliases):
            score += 3
            reasons.append("抽取出的标的与事件标的一致")
    return {"event_id": event.id, "title": event.title, "score": score, "reasons": reasons}


def match_event(events: list[Event], source: Source, last_seen: dict[str, datetime],
                hint_target: Optional[str] = None) -> dict:
    cands = sorted(
        (score_event(e, source, last_seen.get(e.id), hint_target) for e in events),
        key=lambda x: x["score"], reverse=True,
    )
    best = cands[0] if cands else None
    return {
        "matched": bool(best and best["score"] >= MATCH_THRESHOLD),
        "best": best,
        "candidates": cands[:3],
        "threshold": MATCH_THRESHOLD,
    }
