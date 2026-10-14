"""业务服务层：被 FastAPI 路由调用。不依赖 Web 框架，便于单元测试。"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

from app import compliance
from app.clustering import match_event
from app.config import CN_TZ, Settings, now as settings_now
from app.datasources.calendar import effective_trade_date, parse_trading_days
from app.datasources.fuyao import FuyaoClient
from app.datasources.ifind import IFindMCP
from app.engine import EventEngine
from app.extractor import extract
from app.models import (
    DataResult,
    DataStatus,
    Event,
    EventSubject,
    Source,
    SourceType,
    WatchItem,
)
from app.schemas import EVENT_TYPE_LABEL
from app.store import Store

THSCODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)
MAX_TEXT_LEN = 5000


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _dr(r: DataResult) -> dict:
    d = r.model_dump(mode="json")
    d["status_label"] = {
        "ok": "正常", "stale": "数据过期", "missing": "数据缺失", "error": "调用失败", "not_configured": "未配置",
    }[r.status.value]
    return d


class Service:
    def __init__(self, settings: Settings, store: Store, fuyao: Optional[FuyaoClient] = None,
                 ifind: Optional[IFindMCP] = None):
        self.settings = settings
        self.store = store
        self.fuyao = fuyao or FuyaoClient(settings)
        self.ifind = ifind or IFindMCP(settings)

    # ---------------- 基础 ----------------
    def now(self) -> datetime:
        return settings_now(self.settings)

    def _engine(self, event_id: str) -> EventEngine:
        ev = self.store.events.get(event_id)
        if not ev:
            raise ServiceError("事件不存在", 404)
        return EventEngine(ev, self.store.event_sources(event_id), self.store.event_claims(event_id))

    def _trading_days(self) -> tuple[list[date], DataResult]:
        r = self.fuyao.trading_days()
        return (parse_trading_days(r.data) if r.status == DataStatus.ok else []), r

    def health(self) -> dict:
        cal = self.fuyao.trading_days()
        return {
            "now": self.now().isoformat(),
            "llm": {"enabled": self.settings.llm_enabled, "model": self.settings.llm_model if self.settings.llm_enabled else None,
                    "note": "已配置，抽取走 LLM + 原文校验" if self.settings.llm_enabled else "未配置，导入时使用规则抽取（覆盖有限）"},
            "fuyao": _dr(cal),
            "ifind": {"enabled": self.settings.ifind_enabled, "news_tool": self.settings.ifind_news_tool or None},
            "disclaimer": compliance.DISCLAIMER,
        }

    # ---------------- 事件 ----------------
    def list_events(self) -> list[dict]:
        t = self.now()
        out = []
        for ev in self.store.events.values():
            eng = self._engine(ev.id)
            st = eng.state(t)
            ch = eng.changes(t)
            srcs = self.store.event_sources(ev.id)
            out.append({
                "id": ev.id,
                "title": ev.title,
                "event_type": ev.event_type,
                "event_type_label": EVENT_TYPE_LABEL.get(ev.event_type, ev.event_type),
                "is_constructed": ev.is_constructed,
                "stage": st["stage"],
                "summary": st["summary"],
                "buckets": {k: len(v) for k, v in st["buckets"].items()},
                "conflict_count": len(st["conflicts"]),
                "source_count": len(srcs),
                "first_seen": min((s.published_at for s in srcs), default=t).isoformat(),
                "last_update": max((s.effective_at for s in srcs), default=t).isoformat(),
                "latest_changes": ch[-3:][::-1],
                "subjects": [s.model_dump() for s in ev.subjects],
            })
        out.sort(key=lambda e: e["last_update"], reverse=True)
        return out

    def event_detail(self, event_id: str, as_of: Optional[datetime] = None) -> dict:
        ev = self.store.events.get(event_id)
        if not ev:
            raise ServiceError("事件不存在", 404)
        t_now = self.now()
        t = as_of or t_now
        if t > t_now:
            t = t_now  # 不允许"看未来"
        eng = self._engine(event_id)
        st = eng.state(t)
        srcs = self.store.event_sources(event_id)
        days, cal = self._trading_days()
        for item in st["timeline"]:
            item["trade_date"] = effective_trade_date(datetime.fromisoformat(item["effective_at"]), days)
        st["changes"] = eng.changes(t)
        st["event"] = ev.model_dump()
        st["event_type_label"] = EVENT_TYPE_LABEL.get(ev.event_type, ev.event_type)
        st["time_range"] = {
            "min": min(s.effective_at for s in srcs).isoformat() if srcs else t.isoformat(),
            "max": t_now.isoformat(),
            "moments": sorted({s.effective_at.isoformat() for s in srcs}),
        }
        st["relations"] = [self._relation(ev, w) for w in self.store.watchlist.values()]
        st["calendar_status"] = _dr(cal)
        st["extraction_log"] = [l for l in self.store.extraction_log if l.get("event_id") == event_id][-20:]
        st["disclaimer"] = compliance.DISCLAIMER
        return st

    def ask(self, event_id: str, question: str, as_of: Optional[datetime] = None) -> dict:
        if not question.strip():
            raise ServiceError("问题不能为空")
        st = self._engine(event_id).state(min(as_of or self.now(), self.now()))
        return compliance.answer(question, st)

    # ---------------- 关注标的与关系 ----------------
    def _relation(self, ev: Event, w: WatchItem) -> dict:
        for s in ev.subjects:
            if s.thscode and s.thscode.upper() == w.thscode.upper():
                role = {"acquirer": "收购方", "target": "收购标的", "related": "相关方"}.get(s.role, s.role)
                if s.role == "related":
                    return {"thscode": w.thscode, "name": w.name, "relation": "indirect", "relation_label": "间接关联",
                            "detail": f"事件中列为相关方（{ev.note or '同概念/产业链'}）", "basis": "事件主体表"}
                return {"thscode": w.thscode, "name": w.name, "relation": "direct", "relation_label": "直接当事方",
                        "detail": f"本事件的{role}", "basis": "事件主体表"}
        common = [c for c in ev.concepts if c in w.concepts]
        if common:
            return {"thscode": w.thscode, "name": w.name, "relation": "concept", "relation_label": "同概念",
                    "detail": "与事件主体同属概念：" + "、".join(common) + "（仅表示板块归属，不代表受影响方向）",
                    "basis": "关注列表中的概念标签"}
        return {"thscode": w.thscode, "name": w.name, "relation": "none", "relation_label": "未发现关联",
                "detail": "未在事件主体或概念中找到该标的", "basis": "事件主体表与概念标签"}

    def watchlist(self) -> dict:
        t = self.now()
        items = list(self.store.watchlist.values())
        real = [w.thscode for w in items if not w.is_constructed and THSCODE_RE.match(w.thscode)]
        quotes = self.fuyao.snapshot(real) if real else None
        qmap = {}
        if quotes and quotes.status == DataStatus.ok:
            for q in quotes.data.get("item", []):
                qmap[q["thscode"].upper()] = q
        out = []
        for w in items:
            rels = []
            for ev in self.store.events.values():
                r = self._relation(ev, w)
                if r["relation"] != "none":
                    ch = self._engine(ev.id).changes(t)
                    rels.append({"event_id": ev.id, "title": ev.title, **r, "latest_changes": ch[-3:][::-1]})
            if w.is_constructed:
                quote = {"status": "missing", "status_label": "无行情", "message": "构造标的，无真实行情数据"}
            elif quotes is None:
                quote = {"status": "missing", "status_label": "无行情", "message": "无可查询代码"}
            elif quotes.status != DataStatus.ok:
                quote = _dr(quotes)
            elif w.thscode.upper() not in qmap:
                quote = {"status": "missing", "status_label": "数据缺失", "message": "快照中没有该标的"}
            else:
                q = qmap[w.thscode.upper()]
                quote = {"status": "ok", "status_label": "正常", "source": quotes.source,
                         "as_of": quotes.as_of.isoformat() if quotes.as_of else None,
                         "last_price": q.get("last_price"), "price_change_ratio_pct": q.get("price_change_ratio_pct"),
                         "unit": "CNY / %（涨跌幅为百分数值）"}
            out.append({**w.model_dump(), "quote": quote, "events": rels})
        return {"items": out, "as_of": t.isoformat()}

    def add_watch(self, query: str) -> dict:
        q = query.strip()
        if not q:
            raise ServiceError("请输入股票代码或名称")
        r = self.fuyao.search_ticker(q)
        if r.status == DataStatus.ok:
            hits = r.data.get("item", [])
            hit = next((h for h in hits if h["thscode"].upper() == q.upper()), hits[0])
            item = WatchItem(thscode=hit["thscode"], name=hit["name"])
            self.store.add_watch(item)
            return {"added": item.model_dump(), "verified": True, "lookup": _dr(r)}
        if THSCODE_RE.match(q):
            item = WatchItem(thscode=q.upper(), name=q.upper())
            self.store.add_watch(item)
            return {"added": item.model_dump(), "verified": False, "lookup": _dr(r),
                    "warning": f"未能通过扶摇校验代码（{r.message}），已按输入添加，名称与行情待核实"}
        raise ServiceError(f"无法解析该标的：{r.message or '请输入如 600519.SH 的完整代码'}")

    def remove_watch(self, thscode: str) -> dict:
        if not self.store.remove_watch(thscode):
            raise ServiceError("关注列表中没有该标的", 404)
        return {"removed": thscode}

    # ---------------- 行情（只做描述） ----------------
    def market(self, event_id: str, thscode: str) -> dict:
        ev = self.store.events.get(event_id)
        if not ev:
            raise ServiceError("事件不存在", 404)
        if not THSCODE_RE.match(thscode):
            return {"result": {"status": "missing", "status_label": "无行情",
                               "message": "构造标的或非 A 股代码，无真实行情数据"}, "bars": [], "markers": []}
        srcs = sorted(self.store.event_sources(event_id), key=lambda s: s.effective_at)
        t_now = self.now()
        start = (srcs[0].effective_at if srcs else t_now) - timedelta(days=20)
        r = self.fuyao.historical(thscode, start, t_now)
        days, _ = self._trading_days()
        markers = []
        for s in srcs:
            if s.source_type in (SourceType.announcement, SourceType.company_reply, SourceType.regulator, SourceType.media):
                markers.append({"source_id": s.id, "title": s.title, "source_type": s.source_type.value,
                                "trade_date": effective_trade_date(s.effective_at, days)})
        bars = []
        if r.status == DataStatus.ok:
            for b in r.data.get("item", []):
                d = datetime.fromtimestamp(b["date_ms"] / 1000, CN_TZ).date().isoformat()
                bars.append({"date": d, "close": b["close_price"], "volume": b["volume"]})
            closes = {b["date"]: b["close"] for b in bars}
            ordered = [b["date"] for b in bars]
            for m in markers:
                d = m["trade_date"]["date"]
                if d in closes:
                    i = ordered.index(d)
                    if i > 0:
                        prev = closes[ordered[i - 1]]
                        m["day_change_pct"] = round((closes[d] - prev) / prev * 100, 2)
        return {
            "result": _dr(r),
            "bars": bars,
            "markers": markers,
            "caliber": "日线收盘价，前复权（adjust=forward），单位 CNY；标记日涨跌幅 = 当日收盘 / 前一交易日收盘 - 1",
            "note": "仅描述已发生的历史行情，股价变动可能受多种因素影响，不代表事件影响方向，不构成预测。",
        }

    # ---------------- 导入新来源 ----------------
    def ingest(self, payload: dict) -> dict:
        text = (payload.get("text") or "").strip()
        title = (payload.get("title") or "").strip() or text[:30]
        if not text:
            raise ServiceError("原文不能为空")
        if len(text) > MAX_TEXT_LEN:
            raise ServiceError(f"原文过长（{len(text)} 字），请控制在 {MAX_TEXT_LEN} 字以内")
        try:
            stype = SourceType(payload.get("source_type") or "media")
        except ValueError:
            raise ServiceError("来源类型不合法")
        t_now = self.now()
        try:
            published = datetime.fromisoformat(payload["published_at"]) if payload.get("published_at") else t_now
            if published.tzinfo is None:
                published = published.replace(tzinfo=CN_TZ)
            event_time = datetime.fromisoformat(payload["event_time"]) if payload.get("event_time") else None
            if event_time and event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=CN_TZ)
        except ValueError:
            raise ServiceError("时间格式不正确，请使用 YYYY-MM-DDTHH:MM")
        if published > t_now + timedelta(minutes=5):
            raise ServiceError("披露时间晚于当前时间，请检查")
        version_of = payload.get("version_of") or None
        if version_of and version_of not in self.store.sources:
            raise ServiceError("所选原始版本不存在")
        reposted_from = payload.get("reposted_from") or None
        if reposted_from and reposted_from not in self.store.sources:
            raise ServiceError("所选转载来源不存在")
        outlet = (payload.get("outlet") or "").strip() or "未注明来源"
        src = Source(
            id=f"s_{uuid.uuid4().hex[:10]}", title=title, text=text, url=payload.get("url") or None,
            outlet=outlet, issuer=(payload.get("issuer") or outlet).strip(), source_type=stype,
            event_time=event_time,
            published_at=self.store.sources[version_of].published_at if version_of else published,
            updated_at=published if version_of else None,
            fetched_at=t_now, version_of=version_of, reposted_from=reposted_from,
            origin=payload.get("origin") or "ingest",
        )

        # 1) 抽取（先抽取，标的可用于事件归并）
        forced = payload.get("event_id") or None
        if forced and forced not in self.store.events:
            raise ServiceError("指定的事件不存在")
        if version_of:
            forced = self.store.sources[version_of].event_id
        known_target = None
        for ev in self.store.events.values():
            tgt = next((s for s in ev.subjects if s.role == "target"), None)
            if tgt and tgt.name in text:
                known_target = tgt.name
                break
        ext = extract(src, "ma", "pending", self.settings, known_target=known_target)

        # 2) 事件归并
        last_seen = {}
        for s in self.store.sources.values():
            if s.event_id:
                last_seen[s.event_id] = max(last_seen.get(s.event_id, s.effective_at), s.effective_at)
        match = None
        created = None
        if forced:
            event_id = forced
        else:
            match = match_event(list(self.store.events.values()), src, last_seen, ext["event_hint"].get("target"))
            if match["matched"]:
                event_id = match["best"]["event_id"]
            else:
                hint = ext["event_hint"]
                subjects = []
                if hint.get("acquirer"):
                    subjects.append(EventSubject(name=hint["acquirer"], role="acquirer"))
                if hint.get("target"):
                    subjects.append(EventSubject(name=hint["target"], role="target"))
                title_ev = (f"{hint['acquirer']}拟收购{hint['target']}" if hint.get("acquirer") and hint.get("target")
                            else f"待确认事件：{title[:24]}")
                created = Event(id=f"evt_{uuid.uuid4().hex[:8]}", title=title_ev, event_type="ma",
                                subjects=subjects, keywords=["收购", "并购", "股权", "协议"],
                                note="由导入自动创建，事件归属待人工确认")
                event_id = created.id

        before = self._engine(event_id).state(t_now) if event_id in self.store.events else None
        src.event_id = event_id
        for c in ext["claims"]:
            c.event_id = event_id
        log = {
            "at": t_now.isoformat(), "event_id": event_id, "source_id": src.id, "method": ext["method"],
            "fallback_reason": ext["fallback_reason"], "accepted": len(ext["claims"]),
            "rejected": ext["rejected"],
        }
        self.store.add(created, src, ext["claims"], log)
        after = self._engine(event_id).state(t_now)

        diffs = []
        bmap = {s["key"]: s for s in (before["slots"] if before else [])}
        for s in after["slots"]:
            b = bmap.get(s["key"])
            if not b or (b["status"], b["value"]) != (s["status"], s["value"]):
                if s["status"] == "missing" and (not b or b["status"] == "missing"):
                    continue
                diffs.append({
                    "slot_label": s["label"],
                    "from": f"{b['status_label']}：{b['value']}" if b and b["status"] != "missing" else "暂无信息",
                    "to": f"{s['status_label']}：{s['value']}",
                })
        return {
            "event_id": event_id,
            "source_id": src.id,
            "created_event": created.model_dump() if created else None,
            "match": match,
            "extraction": {
                "method": ext["method"], "fallback_reason": ext["fallback_reason"],
                "claims": [c.model_dump(mode="json") for c in ext["claims"]], "rejected": ext["rejected"],
            },
            "conclusion_changes": diffs,
            "message": "已导入；" + (f"当前结论有 {len(diffs)} 项变化" if diffs else "当前结论未变化"),
        }

    # ---------------- iFinD ----------------
    def ifind_tools(self) -> dict:
        return _dr(self.ifind.list_tools())

    def ifind_news(self, query: str) -> dict:
        if not query.strip():
            raise ServiceError("请输入检索关键词")
        return _dr(self.ifind.search_news(query.strip()))
