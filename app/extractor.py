"""主张抽取：LLM（OpenAI 兼容接口）优先，规则兜底；所有结果都要过校验。

校验规则（不通过的主张直接丢弃，并记入抽取日志，不会悄悄进入结论）：
- slot 必须在事件类型 schema 内
- nature / action / attribution 必须是合法枚举
- quote 必须能在原文中找到（防止模型编造证据）
- 非否认类主张必须有取值
- 数值类槽位（交易对价、股权比例）取值必须能解析为数字或"未确定"
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional

import requests
from pydantic import ValidationError

from app.config import Settings
from app.models import Action, Attribution, Claim, Nature, Source, SourceType
from app.schemas import slot_defs

NUMERIC_SLOTS = {"consideration", "stake"}

SYSTEM_PROMPT = """你是投资事件信息抽取器。你只做抽取，不做判断、不做预测、不给投资建议。
给定一条关于并购重组事件的原文，抽取其中关于以下槽位的"主张"，输出 JSON：
{{"claims": [{{"slot": 槽位key, "value": 取值, "nature": 性质, "action": 动作, "attribution": 出处, "quote": 原文片段, "valid_until": 期限或null}}],
  "event": {{"acquirer": 收购方或null, "target": 标的或null}}}}

槽位（key：含义）：
{slots}

字段规则：
- nature：fact=可核验的陈述（包括公告、正式回应、媒体援引的具体事实）；opinion=评价判断；speculation=对未来或未知的推测（含"可能、预计、或将、有望"）；rumor=来源不明的传闻（"听说、网传、据传"）
- action：assert=提出取值；deny=否认某个取值（value 填被否认的说法，若否认任何确定取值则为 null）；correct=发布方更正自己此前的取值（value 填更正后的取值）；retract=撤回
- attribution：official=公司/监管官方；named=有明确出处；anonymous=知情人士、据悉等匿名来源
- quote：必须逐字摘自原文，尽量短（不超过40字），不得改写
- value：交易对价只填数字，单位亿元（如 22.8），未确定填"未确定"；股权比例只填数字（如 55）；交易进展从 [传闻, 初步洽谈, 已签意向协议, 已签正式协议, 已获批准, 已完成, 已终止] 中选
- valid_until：原文中明确给出的截止日期（ISO 格式，如 2026-11-30T23:59:59+08:00），没有则 null
- 原文没提到的槽位不要输出；不要输出原文之外的任何信息"""


def _norm_text(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", "", s)


def quote_in_text(quote: str, text: str) -> bool:
    return bool(quote) and _norm_text(quote) in _norm_text(text)


def validate_claims(raw: list[dict], source: Source, event_id: str, event_type: str, method: str,
                    confidence: float) -> tuple[list[Claim], list[dict]]:
    defs = {d.key: d for d in slot_defs(event_type)}
    ok: list[Claim] = []
    rejected: list[dict] = []
    for r in raw:
        reason = None
        slot = r.get("slot")
        value = r.get("value")
        value = None if value in (None, "", "null") else str(value).strip()
        try:
            nature = Nature(r.get("nature", "fact"))
            action = Action(r.get("action", "assert"))
            attribution = Attribution(r.get("attribution", "named"))
        except ValueError as e:
            nature = action = attribution = None
            reason = f"枚举值非法：{e}"
        if reason is None and slot not in defs:
            reason = f"未知槽位 {slot}"
        elif reason is None and not quote_in_text(r.get("quote", ""), source.text):
            reason = "引用片段在原文中找不到（疑似编造）"
        elif reason is None and value is None and action != Action.deny:
            reason = "缺少取值"
        elif reason is None and slot in NUMERIC_SLOTS and value is not None and value != "未确定":
            m = re.search(r"\d+(?:\.\d+)?", value)
            if not m:
                reason = f"数值槽位取值无法解析：{value}"
            else:
                value = m.group(0)
        if reason:
            rejected.append({"raw": r, "reason": reason})
            continue
        d = defs[slot]
        display = value
        if value is not None and d.unit and value != "未确定":
            display = f"{value}{d.unit}"
        valid_until = r.get("valid_until") or None
        try:
            ok.append(Claim(
                id=f"c_{uuid.uuid4().hex[:10]}", source_id=source.id, event_id=event_id, slot=slot,
                value=value, display_value=display, unit=d.unit, nature=nature, action=action,
                attribution=attribution, quote=r["quote"], valid_until=valid_until,
                extraction=method, confidence=confidence,
            ))
        except ValidationError as e:
            rejected.append({"raw": r, "reason": f"字段格式错误：{e.errors()[0].get('msg')}"})
    return ok, rejected


# ---------------- LLM ----------------

def _extract_json(content: str) -> dict:
    content = content.strip()
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("模型输出中没有 JSON")
    return json.loads(m.group(0))


def llm_extract(source: Source, event_type: str, settings: Settings,
                session: Optional[requests.Session] = None) -> dict:
    """返回 {"claims": [...], "event": {...}}；调用失败抛异常，由上层降级。"""
    http = session or requests
    slots = "\n".join(f"- {d.key}：{d.label}" for d in slot_defs(event_type))
    body = {
        "model": settings.llm_model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.format(slots=slots)},
            {"role": "user", "content": f"来源类型：{source.source_type.value}\n发布方：{source.outlet}\n"
                                        f"披露时间：{source.published_at.isoformat()}\n标题：{source.title}\n原文：{source.text}"},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    url = settings.llm_base_url + "/chat/completions"
    r = http.post(url, json=body, headers=headers, timeout=max(settings.http_timeout, 40))
    if r.status_code == 400:  # 部分兼容接口不支持 response_format
        body.pop("response_format")
        r = http.post(url, json=body, headers=headers, timeout=max(settings.http_timeout, 40))
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    data = _extract_json(content)
    if not isinstance(data.get("claims"), list):
        raise ValueError("模型输出缺少 claims 列表")
    return data


# ---------------- 规则兜底 ----------------

RUMOR_WORDS = r"听说|网传|据传|传闻|小道消息|坊间"
ANON_WORDS = r"知情人士|接近交易|据悉|消息人士|多位人士|有消息称"
SPEC_WORDS = r"可能|预计|或将|有望|或达|料将"
DENY_WORDS = r"不属实|否认|并非|不存在|子虚乌有"
CORRECT_WORDS = r"更正|有误|正确表述"


def _clauses(text: str) -> list[str]:
    return [c for c in re.split(r"[。；;！!？?\n]", text) if c.strip()]


def rule_extract(source: Source, known_target: Optional[str] = None) -> dict:
    """关键词规则抽取，覆盖面有限，置信度低；仅在未配置 LLM 或 LLM 失败时使用。"""
    text = source.text
    st = source.source_type
    official = st in (SourceType.announcement, SourceType.regulator, SourceType.company_reply)
    base_nature = "rumor" if st in (SourceType.rumor, SourceType.self_media) else "fact"
    attribution = "official" if st in (SourceType.announcement, SourceType.regulator, SourceType.company_reply) \
        else ("anonymous" if re.search(ANON_WORDS, text) else "named")
    claims: list[dict] = []
    target = known_target
    acquirer = None
    m = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,10}?)(?:要|拟|将|正在|正|计划|筹划)?(?:以现金方式)?(?:收购|洽购)", text)
    if m:
        acquirer = re.sub(r"^(听说|据传|网传|据悉|传闻|有消息称|据|称|公司)+", "", m.group(1)) or None
        if acquirer and re.search(r"现金|方式|股权|股东|协议|近日|于", acquirer):
            acquirer = None  # 规则误抓的非主体片段
    for cl in _clauses(text):
        if re.search(r"初稿称|此前称|原称|曾称|前次报道称", cl):
            continue  # 引述旧说法的句子不当作新主张
        nature = base_nature
        if not official and re.search(RUMOR_WORDS, cl):
            nature = "rumor"
        if re.search(SPEC_WORDS, cl):
            nature = "speculation"
        if st == SourceType.research:
            nature = "speculation" if re.search(SPEC_WORDS, cl) else "opinion"
        action = "assert"
        if re.search(DENY_WORDS, cl):
            action = "deny"
        elif re.search(CORRECT_WORDS, cl):
            action = "correct"

        def add(slot, value, quote):
            claims.append({"slot": slot, "value": value, "nature": "fact" if action == "deny" else nature,
                           "action": action, "attribution": attribution, "quote": quote})

        m = re.search(r"收购([一-龥A-Za-z]{2,8}?)(?:\d|控股权|股权|事宜|的|，|$)", cl)
        if m and not target:
            target = m.group(1)
        if target and target in cl and re.search(r"收购|并购|洽谈|洽购", cl) and action == "assert":
            add("target", target, cl.strip()[:60])
        ms = list(re.finditer(r"(?:对价|估值|价格|作价)[^，。；]{0,10}?(\d+(?:\.\d+)?)\s*亿", cl))
        m = (ms[-1] if action == "correct" else ms[0]) if ms else None
        if m:
            add("consideration", m.group(1), m.group(0))
        elif re.search(r"(?:对价|价格)[^，。；]{0,6}(?:尚未确定|未确定)", cl):
            q = re.search(r"(?:对价|价格)[^，。；]{0,6}(?:尚未确定|未确定)", cl).group(0)
            claims.append({"slot": "consideration", "value": "未确定", "nature": "fact", "action": "assert",
                           "attribution": attribution, "quote": q})
        ms = list(re.finditer(r"(\d+(?:\.\d+)?)%\s*股权", cl))
        m = (ms[-1] if action == "correct" else ms[0]) if ms else None
        if m:
            add("stake", m.group(1), m.group(0))
        if re.search(r"现金方式|现金收购|以现金", cl) and action == "assert":
            q = re.search(r"现金方式|现金收购|以现金", cl).group(0)
            add("payment", "现金", q)
        if re.search(r"不停牌", cl):
            claims.append({"slot": "trading_halt", "value": "不停牌", "nature": "fact", "action": "assert",
                           "attribution": attribution, "quote": "不停牌"})
        elif re.search(r"停牌", cl):
            q = re.search(r"[^，。；]{0,6}停牌", cl).group(0)
            add("trading_halt", None if action == "deny" else q.strip(), q)
        if re.search(r"股权转让协议|正式协议|收购协议", cl) and re.search(r"签署|签订", cl) and action == "assert" \
                and not re.search(r"预计|将于|拟于", cl):
            add("deal_stage", "已签正式协议", re.search(r"股权转让协议|正式协议|收购协议", cl).group(0))
        elif re.search(r"意向协议", cl) and re.search(r"签署|签订", cl) and action == "assert":
            add("deal_stage", "已签意向协议", re.search(r"签[署订][^，。；]{0,12}意向协议", cl).group(0)
                if re.search(r"签[署订][^，。；]{0,12}意向协议", cl) else "意向协议")
        elif re.search(r"终止", cl) and (re.search(r"收购|交易|重组", cl) or action == "deny"):
            # 否认"终止"时只否认"已终止"这一说法，不否认整个槽位
            add("deal_stage", "已终止", re.search(r"[^，。；]{0,8}终止[^，。；]{0,8}", cl).group(0))
        elif re.search(r"洽谈|筹划", cl) and action == "assert":
            add("deal_stage", "初步洽谈", re.search(r"洽谈|筹划", cl).group(0))
        if re.search(r"关注函|问询函", cl):
            if re.search(r"回复", cl) and st == SourceType.announcement:
                add("regulatory", "已回复问询/关注函", re.search(r"[^，。；]{0,10}回复", cl).group(0))
            elif st == SourceType.regulator:
                add("regulatory", "已收到问询/关注函", re.search(r"关注函|问询函", cl).group(0))
        elif st == SourceType.regulator and re.search(r"请你公司说明|请说明|请补充披露", cl):
            add("regulatory", "已收到问询/关注函", re.search(r"请你公司说明|请说明|请补充披露", cl).group(0))
        m = re.search(r"(?:定增|募资|募集资金)[^，。；]{0,8}?(\d+(?:\.\d+)?)\s*亿", cl)
        if m and action == "assert":
            add("financing", f"定增募资{m.group(1)}亿元", m.group(0))
    return {"claims": claims, "event": {"acquirer": acquirer, "target": target}}


def extract(source: Source, event_type: str, event_id: str, settings: Settings,
            known_target: Optional[str] = None, session: Optional[requests.Session] = None) -> dict:
    """抽取入口：返回 claims、被拒绝的候选、使用的方法和降级原因。"""
    method, confidence, fallback_reason = "llm", 0.8, None
    data = None
    if settings.llm_enabled:
        try:
            data = llm_extract(source, event_type, settings, session=session)
        except Exception as e:  # noqa: BLE001 — 任何失败都降级，但记录原因
            fallback_reason = f"LLM 调用失败（{type(e).__name__}: {str(e)[:120]}），已降级为规则抽取"
    else:
        fallback_reason = "未配置 LLM_API_KEY，使用规则抽取（覆盖有限，置信度低）"
    if data is None:
        method, confidence = "rule_fallback", 0.5
        data = rule_extract(source, known_target)
    claims, rejected = validate_claims(data.get("claims", []), source, event_id, event_type, method, confidence)
    return {
        "claims": claims,
        "rejected": rejected,
        "method": method,
        "fallback_reason": fallback_reason,
        "event_hint": data.get("event") or {},
    }
