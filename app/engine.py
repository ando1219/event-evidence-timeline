"""规则引擎：从主张计算"截至某时刻"的当前结论。

原则：AI 只负责抽取主张；结论由这里的确定性规则计算，便于测试和解释。
每个结论都带上支撑它的主张 id，前端可以一路点回原文。

处理顺序（对每个事实槽位）：
  1. 只看 effective_at <= as_of 的来源（新版本生效后，旧版本主张作废）
  2. 同一发布方的"更正 / 撤回"作废其更早的说法
  3. "否认"作废更早的、等级不高于否认方的说法；低等级否认高等级说法则记为冲突
  4. 剩余有效说法按来源等级取最高等级；同等级不同发布方取值不同 => 冲突
  5. 只有低等级来源支撑且超过有效期 => 已过期；带期限事项逾期 => 逾期待核实
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from app.models import (
    NATURE_LABEL,
    SOURCE_TYPE_LABEL,
    Action,
    Attribution,
    Claim,
    Event,
    Nature,
    Source,
    SourceType,
)
from app.schemas import slot_defs

# ---------------- 证据权重参数 ----------------

# 来源等级：rank 用于"谁覆盖谁"，base 用于权重分数
SOURCE_RANK = {
    SourceType.announcement: 6,
    SourceType.regulator: 6,
    SourceType.company_reply: 5,
    SourceType.media: 4,
    SourceType.research: 3,
    SourceType.market_note: 2,
    SourceType.self_media: 2,
    SourceType.rumor: 1,
}
SOURCE_BASE = {
    SourceType.announcement: 1.0,
    SourceType.regulator: 0.95,
    SourceType.company_reply: 0.85,
    SourceType.media: 0.6,
    SourceType.research: 0.45,
    SourceType.market_note: 0.3,
    SourceType.self_media: 0.25,
    SourceType.rumor: 0.15,
}
NATURE_FACTOR = {
    Nature.fact: 1.0,
    Nature.opinion: 0.5,
    Nature.speculation: 0.35,
    Nature.rumor: 0.25,
}
ATTRIBUTION_FACTOR = {
    Attribution.official: 1.0,
    Attribution.named: 0.9,
    Attribution.anonymous: 0.7,
}
# 传闻/推测随时间衰减（半衰期），事实不衰减
DECAY_HALF_LIFE_DAYS = 7.0
# 只有低等级来源支撑的说法，超过这么久无人证实即视为过期
RUMOR_TTL = timedelta(days=14)
# 低等级来源的最高 rank（含）
LOW_RANK_MAX = 2
# 抓取延迟超过该值时提示
FETCH_LAG_WARN = timedelta(hours=6)

DECIDING_NATURES = {Nature.fact, Nature.rumor}

STATUS_LABEL = {
    "confirmed": "已确认",
    "reported": "媒体报道·待证实",
    "unverified": "未证实",
    "disputed": "存在冲突",
    "denied": "已否认",
    "expired": "已过期·未证实",
    "missing": "暂无信息",
}

CLAIM_STATUS_LABEL = {
    "active": "有效",
    "supporting": "佐证",
    "superseded_version": "原文已更新",
    "superseded_update": "已被同一发布方新说法替代",
    "superseded_progress": "进展已更新",
    "corrected": "已更正",
    "retracted": "已撤回",
    "denied": "已被否认",
    "overridden": "被更高等级来源覆盖",
    "expired": "已过期",
    "not_deciding": "不参与事实判定",
}


def norm_value(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = re.sub(r"\s+", "", str(v)).lower()
    try:
        f = float(s)
        return ("%.6f" % f).rstrip("0").rstrip(".")
    except ValueError:
        return s


@dataclass
class ClaimView:
    claim: Claim
    source: Source
    root_id: str
    weight: float
    weight_breakdown: dict
    status: str = "active"
    reason: str = ""
    distortion: Optional[str] = None

    root_rank: Optional[int] = None

    @property
    def rank(self) -> int:
        r = SOURCE_RANK[self.source.source_type]
        # 转载不提升证据等级：取自身与原始来源中较低的等级
        return min(r, self.root_rank) if self.root_rank is not None else r

    @property
    def t(self) -> datetime:
        return self.source.effective_at

    def to_dict(self) -> dict:
        c, s = self.claim, self.source
        return {
            "id": c.id,
            "source_id": s.id,
            "slot": c.slot,
            "value": c.display_value or c.value,
            "unit": c.unit,
            "nature": c.nature.value,
            "nature_label": NATURE_LABEL[c.nature],
            "action": c.action.value,
            "attribution": c.attribution.value,
            "quote": c.quote,
            "valid_until": c.valid_until.isoformat() if c.valid_until else None,
            "extraction": c.extraction,
            "confidence": c.confidence,
            "weight": round(self.weight, 3),
            "weight_breakdown": self.weight_breakdown,
            "status": self.status,
            "status_label": CLAIM_STATUS_LABEL.get(self.status, self.status),
            "reason": self.reason,
            "distortion": self.distortion,
            "outlet": s.outlet,
            "source_type": s.source_type.value,
            "source_type_label": SOURCE_TYPE_LABEL[s.source_type],
            "effective_at": s.effective_at.isoformat(),
            "root_id": self.root_id,
        }


@dataclass
class SlotState:
    key: str
    label: str
    unit: Optional[str]
    status: str = "missing"
    value: Optional[str] = None
    deciding_claim_ids: list[str] = field(default_factory=list)
    supporting_claim_ids: list[str] = field(default_factory=list)
    independent_sources: int = 0
    support_score: float = 0.0
    conflict: Optional[dict] = None
    flags: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)  # 该槽位所有主张（含失效的），按时间

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "status": self.status,
            "status_label": STATUS_LABEL[self.status],
            "value": self.value,
            "deciding_claim_ids": self.deciding_claim_ids,
            "supporting_claim_ids": self.supporting_claim_ids,
            "independent_sources": self.independent_sources,
            "support_score": round(self.support_score, 3),
            "conflict": self.conflict,
            "flags": self.flags,
            "history": self.history,
        }


def claim_weight(claim: Claim, source: Source, as_of: datetime) -> tuple[float, dict]:
    base = SOURCE_BASE[source.source_type]
    nature = NATURE_FACTOR[claim.nature]
    attr = ATTRIBUTION_FACTOR[claim.attribution]
    decay = 1.0
    if claim.nature in (Nature.rumor, Nature.speculation):
        age_days = max(0.0, (as_of - source.effective_at).total_seconds() / 86400)
        decay = math.pow(0.5, age_days / DECAY_HALF_LIFE_DAYS)
    # 转载不在单条权重上打折，而是在汇总时按原始来源去重（见 _dedup_scores）
    w = base * nature * attr * decay
    return w, {
        "source_base": base,
        "nature_factor": nature,
        "attribution_factor": attr,
        "decay": round(decay, 3),
        "is_repost": bool(source.reposted_from),
        "formula": "来源等级 × 性质系数 × 出处系数 × 时效衰减；转载按原始来源去重计数",
    }


def _root_of(src: Source, sources: dict[str, Source]) -> str:
    seen = set()
    cur = src
    while cur.reposted_from and cur.reposted_from in sources and cur.id not in seen:
        seen.add(cur.id)
        cur = sources[cur.reposted_from]
    return cur.id


def _fmt(t: datetime) -> str:
    return t.strftime("%m-%d %H:%M")


class EventEngine:
    def __init__(self, event: Event, sources: list[Source], claims: list[Claim]):
        self.event = event
        self.sources = {s.id: s for s in sources}
        self.claims = [c for c in claims if c.source_id in self.sources]
        self.slot_defs = {d.key: d for d in slot_defs(event.event_type)}

    # ---------- 视图构造 ----------
    def _visible_views(self, as_of: datetime) -> list[ClaimView]:
        visible = {sid: s for sid, s in self.sources.items() if s.effective_at <= as_of}
        # 新版本生效后，旧版本作废
        replaced = {s.version_of: s for s in visible.values() if s.version_of}
        views = []
        for c in self.claims:
            s = visible.get(c.source_id)
            if not s:
                continue
            w, br = claim_weight(c, s, as_of)
            root_id = _root_of(s, self.sources)
            v = ClaimView(c, s, root_id, w, br)
            if root_id != s.id:
                v.root_rank = SOURCE_RANK[self.sources[root_id].source_type]
            if s.id in replaced:
                nv = replaced[s.id]
                v.status = "superseded_version"
                v.reason = f"{nv.outlet} 于 {_fmt(nv.effective_at)} 更新了原文"
            views.append(v)
        views.sort(key=lambda v: (v.t, v.claim.id))
        self._mark_distortion(views)
        return views

    def _mark_distortion(self, views: list[ClaimView]) -> None:
        """转载内容与原始来源取值不一致 => 转述走样。"""
        by_src_slot: dict[tuple[str, str], ClaimView] = {}
        for v in views:
            by_src_slot.setdefault((v.source.id, v.claim.slot), v)
        for v in views:
            if not v.source.reposted_from:
                continue
            root = by_src_slot.get((v.root_id, v.claim.slot))
            if root and norm_value(root.claim.value) != norm_value(v.claim.value):
                v.distortion = (
                    f"转载自 {root.source.outlet}，原文为「{root.claim.display_value or root.claim.value}」，"
                    f"转述为「{v.claim.display_value or v.claim.value}」"
                )

    # ---------- 槽位判定 ----------
    def _resolve_slot(self, key: str, views: list[ClaimView], as_of: datetime) -> SlotState:
        d = self.slot_defs[key]
        st = SlotState(key=key, label=d.label, unit=d.unit)
        cs = [v for v in views if v.claim.slot == key]
        if not d.is_fact_slot:
            for v in cs:
                if v.status == "active":
                    v.status, v.reason = "not_deciding", "观点类槽位，不进入事实判定"
            st.history = [v.to_dict() for v in cs]
            return st
        for v in cs:
            if v.status == "active" and v.claim.nature not in DECIDING_NATURES:
                v.status, v.reason = "not_deciding", f"{NATURE_LABEL[v.claim.nature]}不参与事实判定"
            elif v.status == "active" and v.distortion:
                v.status, v.reason = "not_deciding", "转述与原始来源不一致，以原始来源为准"
        live = [v for v in cs if v.status == "active"]
        conflicts: list[dict] = []

        # 2. 更正 / 撤回：作废同一发布方更早的说法
        for c in live:
            if c.claim.action not in (Action.correct, Action.retract):
                continue
            for e in live:
                if e is c or e.status != "active" or e.t >= c.t:
                    continue
                if e.source.issuer == c.source.issuer:
                    if c.claim.action == Action.correct:
                        e.status = "corrected"
                        e.reason = f"{c.source.outlet} 于 {_fmt(c.t)} 更正为「{c.claim.display_value or c.claim.value}」"
                    else:
                        e.status = "retracted"
                        e.reason = f"{c.source.outlet} 于 {_fmt(c.t)} 撤回"
            if c.claim.action == Action.retract:
                c.status, c.reason = "retracted", "撤回声明本身不构成取值"

        # 3. 否认
        for dny in [v for v in live if v.claim.action == Action.deny and v.status == "active"]:
            target = norm_value(dny.claim.value)
            for e in live:
                if e is dny or e.status != "active" or e.claim.action == Action.deny or e.t > dny.t:
                    continue
                if target is not None and norm_value(e.claim.value) != target:
                    continue
                if dny.rank >= e.rank:
                    e.status = "denied"
                    e.reason = f"{dny.source.outlet}（{SOURCE_TYPE_LABEL[dny.source.source_type]}）于 {_fmt(dny.t)} 否认"
                else:
                    conflicts.append({
                        "kind": "low_rank_denial",
                        "severity": "medium",
                        "message": f"{dny.source.outlet} 否认了更高等级来源 {e.source.outlet} 的说法，需人工核实",
                        "claim_ids": [dny.claim.id, e.claim.id],
                    })

        asserts = [v for v in live if v.status == "active" and v.claim.action in (Action.assert_, Action.correct)]
        denies = [v for v in live if v.status == "active" and v.claim.action == Action.deny]

        if not asserts:
            if denies:
                top = max(denies, key=lambda v: (v.rank, v.t))
                st.status = "denied"
                denied_val = top.claim.display_value or top.claim.value
                st.value = f"否认：{denied_val}" if denied_val else "否认存在确定取值"
                st.deciding_claim_ids = [top.claim.id]
                st.independent_sources = len({v.root_id for v in denies})
                st.support_score = top.weight
            st.conflict = conflicts[0] if conflicts else None
            if conflicts:
                st.status = "disputed"
            st.history = [v.to_dict() for v in cs]
            return st

        # 4. 同一发布方：只保留最新说法
        latest_by_issuer: dict[str, ClaimView] = {}
        for v in asserts:
            prev = latest_by_issuer.get(v.source.issuer)
            if prev is None or v.t >= prev.t:
                latest_by_issuer[v.source.issuer] = v
        for v in asserts:
            latest = latest_by_issuer[v.source.issuer]
            if v is not latest and norm_value(v.claim.value) != norm_value(latest.claim.value):
                v.status = "superseded_update"
                v.reason = f"{latest.source.outlet} 于 {_fmt(latest.t)} 给出新说法「{latest.claim.display_value or latest.claim.value}」"
        current = [v for v in asserts if v.status == "active"]
        top_rank = max(v.rank for v in current)
        top = [v for v in current if v.rank == top_rank]
        top_values = {}
        for v in top:
            top_values.setdefault(norm_value(v.claim.value), []).append(v)

        if len(top_values) > 1 and d.kind == "progress":
            # 进展类槽位：同等级来源以最新说法为准，旧说法视为已被新进展替代
            latest = max(top, key=lambda v: v.t)
            for v in top:
                if norm_value(v.claim.value) != norm_value(latest.claim.value):
                    v.status = "superseded_progress"
                    v.reason = f"进展已更新：{latest.source.outlet} 于 {_fmt(latest.t)} 披露「{latest.claim.display_value or latest.claim.value}」"
            top = [v for v in top if v.status == "active"]
            current = [v for v in current if v.status == "active"]
            top_values = {norm_value(latest.claim.value): top}

        if len(top_values) > 1:
            st.status = "disputed"
            st.value = " / ".join(
                sorted({(vs[0].claim.display_value or vs[0].claim.value) for vs in top_values.values()})
            )
            st.deciding_claim_ids = [v.claim.id for v in top]
            st.independent_sources = len({v.root_id for v in top})
            for v in current:
                if v.rank < top_rank:
                    if norm_value(v.claim.value) in top_values:
                        v.status, v.reason = "supporting", "与冲突中的一方一致"
                    else:
                        v.status = "overridden"
                        v.reason = f"与更高等级来源（{SOURCE_TYPE_LABEL[top[0].source.source_type]}）不一致"
            conflicts.insert(0, {
                "kind": "same_rank_conflict",
                "severity": "high",
                "message": f"同等级来源（{SOURCE_TYPE_LABEL[top[0].source.source_type]}）对「{d.label}」说法不一致",
                "claim_ids": [v.claim.id for v in top],
                "sides": [
                    {
                        "value": vs[0].claim.display_value or vs[0].claim.value,
                        "claim_ids": [x.claim.id for x in vs],
                        "outlets": sorted({x.source.outlet for x in vs}),
                        "score": round(sum(_dedup_scores(vs).values()), 3),
                    }
                    for vs in top_values.values()
                ],
            })
        else:
            chosen_norm = next(iter(top_values))
            winner = max(top, key=lambda v: v.t)
            st.value = winner.claim.display_value or winner.claim.value
            st.deciding_claim_ids = [v.claim.id for v in top]
            for v in current:
                if v.rank == top_rank:
                    continue
                if norm_value(v.claim.value) == chosen_norm:
                    v.status, v.reason = "supporting", "与当前结论一致"
                else:
                    v.status = "overridden"
                    v.reason = f"与更高等级来源（{SOURCE_TYPE_LABEL[winner.source.source_type]}）不一致"
                    if v.t > winner.t:
                        conflicts.append({
                            "kind": "late_low_rank_divergence",
                            "severity": "low",
                            "message": f"{v.source.outlet} 在正式口径之后给出不同说法「{v.claim.display_value or v.claim.value}」，暂不改变结论，建议关注",
                            "claim_ids": [winner.claim.id, v.claim.id],
                        })
            agree = [v for v in current if norm_value(v.claim.value) == chosen_norm]
            scores = _dedup_scores(agree)
            st.independent_sources = len(scores)
            st.support_score = sum(scores.values())
            st.supporting_claim_ids = [v.claim.id for v in agree if v.claim.id not in st.deciding_claim_ids]

            if top_rank >= 5:
                st.status = "confirmed"
            elif top_rank == 4:
                st.status = "reported"
            else:
                st.status = "unverified"
            # 5. 过期：只有低等级来源支撑，且最近一次佐证已超过 TTL
            if top_rank <= LOW_RANK_MAX:
                last_t = max(v.t for v in agree)
                if as_of - last_t > RUMOR_TTL:
                    st.status = "expired"
                    for v in agree:
                        v.status = "expired"
                        v.reason = f"超过 {RUMOR_TTL.days} 天无更高等级来源证实"
            # 带期限事项逾期
            for v in agree:
                if v.claim.valid_until and as_of > v.claim.valid_until:
                    st.flags.append(
                        f"「{st.value}」附带期限 {v.claim.valid_until.strftime('%Y-%m-%d')} 已过，仍无新进展披露，逾期待核实"
                    )
            if conflicts and st.status not in ("disputed",):
                # 中等严重度的冲突（低等级否认高等级）也要把状态标为冲突
                if any(c["severity"] in ("high", "medium") for c in conflicts):
                    st.status = "disputed"

        st.conflict = conflicts[0] if conflicts else None
        if len(conflicts) > 1:
            st.flags.extend(c["message"] for c in conflicts[1:])
        st.history = [v.to_dict() for v in cs]
        return st

    # ---------- 对外接口 ----------
    def state(self, as_of: datetime) -> dict:
        views = self._visible_views(as_of)
        slots = {k: self._resolve_slot(k, views, as_of) for k in self.slot_defs}
        fact_slots = [s for k, s in slots.items() if self.slot_defs[k].is_fact_slot]

        buckets = {"confirmed": [], "disputed": [], "unverified": [], "closed": []}
        for s in fact_slots:
            if s.status == "confirmed":
                buckets["confirmed"].append(s.key)
            elif s.status == "disputed":
                buckets["disputed"].append(s.key)
            elif s.status in ("reported", "unverified"):
                buckets["unverified"].append(s.key)
            elif s.status in ("denied", "expired"):
                buckets["closed"].append(s.key)

        opinions = [
            v.to_dict() for v in views
            if v.claim.nature in (Nature.opinion, Nature.speculation) or not self.slot_defs.get(v.claim.slot, None)
            or not self.slot_defs[v.claim.slot].is_fact_slot
        ]
        stage = slots.get("deal_stage")
        stage_text = stage.value if stage and stage.value else self._fallback_stage(views)

        return {
            "as_of": as_of.isoformat(),
            "stage": stage_text,
            "stage_status": stage.status if stage else None,
            "slots": [s.to_dict() for s in fact_slots],
            "buckets": buckets,
            "opinions": opinions,
            "conflicts": [s.conflict | {"slot": s.key, "slot_label": s.label} for s in fact_slots if s.conflict],
            "claims": {v.claim.id: v.to_dict() for v in views},
            "timeline": self._timeline(views, as_of),
            "origin": self._origin(views),
            "summary": self._summary(stage_text, fact_slots),
            "visible_source_count": sum(1 for s in self.sources.values() if s.effective_at <= as_of),
        }

    def _fallback_stage(self, views: list[ClaimView]) -> str:
        if not views:
            return "暂无信息"
        top = max(v.rank for v in views)
        return {6: "已正式披露", 5: "公司已回应", 4: "媒体报道阶段"}.get(top, "传闻阶段")

    def _summary(self, stage: str, slots: list[SlotState]) -> str:
        cnt = {k: 0 for k in STATUS_LABEL}
        for s in slots:
            cnt[s.status] += 1
        parts = [f"当前阶段：{stage}。"]
        parts.append(
            f"已确认 {cnt['confirmed']} 项，存在冲突 {cnt['disputed']} 项，"
            f"待证实 {cnt['reported'] + cnt['unverified']} 项，已否认或过期 {cnt['denied'] + cnt['expired']} 项。"
        )
        return "".join(parts)

    def _timeline(self, views: list[ClaimView], as_of: datetime) -> list[dict]:
        by_src: dict[str, list[ClaimView]] = {}
        for v in views:
            by_src.setdefault(v.source.id, []).append(v)
        # 没抽出主张的来源（如抽取失败）也要出现在时间线上
        visible_ids = {sid for sid, s in self.sources.items() if s.effective_at <= as_of}
        out = []
        for sid in sorted(visible_ids, key=lambda i: self.sources[i].effective_at):
            s = self.sources[sid]
            lag = s.fetched_at - s.effective_at
            out.append({
                "source_id": s.id,
                "title": s.title,
                "outlet": s.outlet,
                "url": s.url,
                "source_type": s.source_type.value,
                "source_type_label": SOURCE_TYPE_LABEL[s.source_type],
                "rank": SOURCE_RANK[s.source_type],
                "event_time": s.event_time.isoformat() if s.event_time else None,
                "published_at": s.published_at.isoformat(),
                "fetched_at": s.fetched_at.isoformat(),
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "effective_at": s.effective_at.isoformat(),
                "fetch_lag_warning": f"抓取晚于披露 {lag.total_seconds() / 3600:.1f} 小时" if lag > FETCH_LAG_WARN else None,
                "version_of": s.version_of,
                "reposted_from": s.reposted_from,
                "root_id": _root_of(s, self.sources),
                "is_constructed": s.is_constructed,
                "origin": s.origin,
                "text": s.text,
                "claim_ids": [v.claim.id for v in by_src.get(sid, [])],
            })
        return out

    def _origin(self, views: list[ClaimView]) -> dict:
        """溯源：每个取值的最早出处 + 转载链。"""
        first_seen: dict[tuple[str, str], ClaimView] = {}
        for v in views:
            if v.claim.value is None:
                continue
            k = (v.claim.slot, norm_value(v.claim.value))
            if k not in first_seen or v.t < first_seen[k].t:
                first_seen[k] = v
        reposts: dict[str, list[str]] = {}
        for s in self.sources.values():
            if s.reposted_from and any(v.source.id == s.id for v in views):
                reposts.setdefault(_root_of(s, self.sources), []).append(s.id)
        earliest = min(views, key=lambda v: v.t) if views else None
        return {
            "earliest_source_id": earliest.source.id if earliest else None,
            "first_seen": [
                {
                    "slot": k[0],
                    "value": v.claim.display_value or v.claim.value,
                    "claim_id": v.claim.id,
                    "source_id": v.source.id,
                    "outlet": v.source.outlet,
                    "source_type_label": SOURCE_TYPE_LABEL[v.source.source_type],
                    "at": v.t.isoformat(),
                }
                for k, v in sorted(first_seen.items(), key=lambda kv: kv[1].t)
            ],
            "repost_tree": reposts,
            "distortions": [v.to_dict() for v in views if v.distortion],
        }

    # ---------- 结论变化（驱动"只推送状态变化"） ----------
    def changes(self, until: datetime) -> list[dict]:
        moments = set()
        for s in self.sources.values():
            if s.effective_at <= until:
                moments.add(s.effective_at)
                moments.add(s.effective_at + RUMOR_TTL + timedelta(seconds=1))
        for c in self.claims:
            if c.valid_until:
                moments.add(c.valid_until + timedelta(seconds=1))
        moments = sorted(m for m in moments if m <= until)
        prev: dict[str, tuple] = {}
        out = []
        for t in moments:
            st = self.state(t)
            for s in st["slots"]:
                cur = (s["status"], s["value"], tuple(s["flags"]))
                old = prev.get(s["key"])
                if old is not None and old == cur:
                    continue
                if old is None and s["status"] == "missing":
                    prev[s["key"]] = cur
                    continue
                trig = [st["claims"][cid] for cid in s["deciding_claim_ids"] if cid in st["claims"]]
                trig_src = max(trig, key=lambda c: c["effective_at"]) if trig else None
                out.append({
                    "at": t.isoformat(),
                    "slot": s["key"],
                    "slot_label": s["label"],
                    "from_status": STATUS_LABEL[old[0]] if old else None,
                    "from_value": old[1] if old else None,
                    "to_status": s["status_label"],
                    "to_value": s["value"],
                    "flags": s["flags"],
                    "trigger": (
                        f"{trig_src['outlet']}（{trig_src['source_type_label']}）" if trig_src and trig_src["effective_at"] == t.isoformat()
                        else "时间推移（过期/逾期规则）"
                    ),
                    "trigger_source_id": trig_src["source_id"] if trig_src and trig_src["effective_at"] == t.isoformat() else None,
                })
                prev[s["key"]] = cur
        return out


def _dedup_scores(views: list[ClaimView]) -> dict[str, float]:
    """按原始来源去重：同一根来源的多次转载只计一次（取最大权重）。"""
    out: dict[str, float] = {}
    for v in views:
        out[v.root_id] = max(out.get(v.root_id, 0.0), v.weight)
    return out
