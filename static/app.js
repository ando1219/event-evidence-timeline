/* 事件证据时间线 — 前端（原生 JS，无构建步骤） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const app = $("#app");

const TIER_COLOR = {
  announcement: "var(--t6)", regulator: "var(--t6)", company_reply: "var(--t5)", media: "var(--t4)",
  research: "var(--t3)", market_note: "var(--t2)", self_media: "var(--t2)", rumor: "var(--t1)",
};
const SOURCE_TYPES = [
  ["announcement", "公司公告"], ["regulator", "监管/交易所"], ["company_reply", "公司回应（互动平台/澄清）"],
  ["media", "财经媒体"], ["research", "卖方研报"], ["self_media", "自媒体"], ["rumor", "市场传闻"], ["market_note", "异动解读"],
];
const SLOT_LABEL = { target: "收购标的", consideration: "交易对价", stake: "收购股权比例", payment: "支付方式", deal_stage: "交易进展",
  trading_halt: "是否停牌", financing: "配套融资", regulatory: "监管问询", earnings_impact: "盈利影响（观点）" };
const INACTIVE = new Set(["superseded_version", "superseded_update", "superseded_progress", "corrected", "retracted", "denied", "overridden", "expired"]);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmtShort(iso) { if (!iso) return "—"; const d = new Date(iso); return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function fmtFull(iso) { if (!iso) return "—"; const d = new Date(iso); return `${d.getFullYear()}-${fmtShort(iso)}`; }
function pad(n) { return String(n).padStart(2, "0"); }
function toLocalInput(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }
function isoCN(ms) {
  // 以 +08:00 表示，避免浏览器时区影响后端判断
  const d = new Date(ms + 8 * 3600 * 1000);
  return d.toISOString().slice(0, 19) + "+08:00";
}

async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { /* 非 JSON */ }
  if (!r.ok) throw new Error((data && (data.error || data.detail)) || `请求失败（HTTP ${r.status}）`);
  return data;
}

function stBadge(status, label) { return `<span class="badge st-${esc(status)}">${esc(label)}</span>`; }
function natureBadge(n, label) { return `<span class="badge n-${esc(n)}">${esc(label)}</span>`; }
function tierTag(type, label) { return `<span class="tier"><i style="background:${TIER_COLOR[type] || "var(--t1)"}"></i>${esc(label)}</span>`; }
function constructedBadge(flag) { return flag ? `<span class="badge constructed" title="公司与文本为虚构，用于演示">构造样例</span>` : ""; }
function dataStatus(r) {
  if (!r) return "";
  const cls = r.status === "ok" ? "st-confirmed" : r.status === "not_configured" || r.status === "missing" ? "st-missing" : "st-disputed";
  return `<span class="badge ${cls}">${esc(r.status_label || r.status)}</span> <span class="small muted">${esc(r.message || "")}</span>`;
}
function actionLabel(a) { const m = { assert: "", deny: "否认", correct: "更正", retract: "撤回" }; return a in m ? m[a] : a; }
function weightTip(c) {
  const b = c.weight_breakdown || {};
  return `权重 ${c.weight} = 来源等级 ${b.source_base} × 性质 ${b.nature_factor} × 出处 ${b.attribution_factor} × 时效 ${b.decay}${b.is_repost ? "（转载，按原始来源去重计数）" : ""}`;
}

/* ---------------- 顶部状态 ---------------- */
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const chips = [
      [h.llm.enabled ? "ok" : "off", `抽取：${h.llm.enabled ? "LLM " + h.llm.model : "规则兜底"}`, h.llm.note],
      [h.fuyao.status === "ok" ? "ok" : h.fuyao.status === "not_configured" ? "off" : "err", `扶摇：${h.fuyao.status_label}`, h.fuyao.message || h.fuyao.source],
      [h.ifind.enabled ? "ok" : "off", `iFinD：${h.ifind.enabled ? "已配置" : "未配置"}`, h.ifind.news_tool || "未配置 IFIND_MCP_URL"],
    ];
    $("#status-chips").innerHTML = chips.map(([c, t, tip]) => `<span class="chip" title="${esc(tip)}"><span class="dot ${c}"></span>${esc(t)}</span>`).join("");
  } catch (e) {
    $("#status-chips").innerHTML = `<span class="chip"><span class="dot err"></span>后端不可用</span>`;
  }
}

/* ---------------- 路由 ---------------- */
function route() {
  const h = location.hash || "#/";
  $$(".nav a").forEach((a) => a.classList.toggle("active", (h.startsWith("#/ingest") ? "ingest" : "home") === a.dataset.nav && !h.startsWith("#/event")));
  closeDrawer();
  let m;
  if ((m = h.match(/^#\/event\/([\w-]+)/))) return renderEvent(m[1]);
  if (h.startsWith("#/ingest")) return renderIngest();
  return renderHome();
}
window.addEventListener("hashchange", route);

/* ---------------- 首页：关注 + 事件 ---------------- */
async function renderHome() {
  app.innerHTML = `<div class="grid-home">
    <section class="panel" id="watch-panel"><div class="loading">加载关注列表…</div></section>
    <section id="events-panel"><div class="panel"><div class="loading">加载事件…</div></div></section>
  </div>`;
  renderWatch();
  try {
    const { items } = await api("/api/events");
    $("#events-panel").innerHTML = `<div class="panel"><div class="panel-head"><h2>事件</h2><span class="small muted">按最近更新排序 · 只展示结论变化</span></div>
      ${items.length ? items.map(eventCard).join("") : `<div class="empty">暂无事件，可在「导入新消息」中添加。</div>`}</div>`;
  } catch (e) {
    $("#events-panel").innerHTML = `<div class="error-box">事件加载失败：${esc(e.message)}</div>`;
  }
}

function eventCard(e) {
  const b = e.buckets;
  return `<a class="event-card" href="#/event/${esc(e.id)}">
    <div class="meta"><span>${esc(e.event_type_label)}</span>${constructedBadge(e.is_constructed)}
      <span>${e.source_count} 条来源</span><span>首次出现 ${fmtFull(e.first_seen)}</span><span>最近更新 ${fmtFull(e.last_update)}</span></div>
    <h3>${esc(e.title)}</h3>
    <div class="stage">当前阶段：<b>${esc(e.stage)}</b></div>
    <div class="counts">
      <span class="badge st-confirmed">已确认<b>${b.confirmed}</b></span>
      <span class="badge st-disputed">有冲突<b>${b.disputed}</b></span>
      <span class="badge st-unverified">待证实<b>${b.unverified}</b></span>
      <span class="badge st-denied">已否认/过期<b>${b.closed}</b></span>
    </div>
    ${e.latest_changes.map(changeLine).join("")}
  </a>`;
}

function changeLine(c) {
  if (c.from_status === c.to_status && c.from_value === c.to_value && c.flags && c.flags.length) {
    return `<div class="change-line"><time>${fmtShort(c.at)}</time>${esc(c.slot_label)}：<span style="color:var(--warn)">⚠ ${esc(c.flags[c.flags.length - 1])}</span></div>`;
  }
  const from = c.from_status ? `${esc(c.from_status)}${c.from_value ? "「" + esc(c.from_value) + "」" : ""}` : "新出现";
  const to = `${esc(c.to_status)}${c.to_value ? "「" + esc(c.to_value) + "」" : ""}`;
  return `<div class="change-line"><time>${fmtShort(c.at)}</time>${esc(c.slot_label)}：${from}<span class="arrow">→</span>${to}<span class="muted"> · ${esc(c.trigger)}</span></div>`;
}

async function renderWatch() {
  const box = $("#watch-panel");
  try {
    const { items } = await api("/api/watchlist");
    box.innerHTML = `<div class="panel-head"><h2>关注标的</h2><span class="small muted">匿名构造持仓</span></div>
      ${items.map(watchItem).join("") || `<div class="empty">关注列表为空</div>`}
      <form class="add-watch" id="add-watch"><input type="text" name="q" placeholder="添加：600519.SH 或 名称" aria-label="添加关注标的"><button class="btn" type="submit">添加</button></form>
      <div id="watch-msg"></div>`;
    $("#add-watch").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const q = ev.target.q.value.trim();
      if (!q) return;
      try {
        const r = await api("/api/watchlist", { method: "POST", body: { query: q } });
        if (r.warning) $("#watch-msg").innerHTML = `<div class="warn-box">${esc(r.warning)}</div>`;
        setTimeout(renderWatch, r.warning ? 1800 : 0);
      } catch (e) { $("#watch-msg").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    });
    $$("[data-unwatch]", box).forEach((b) => b.addEventListener("click", async () => {
      try { await api(`/api/watchlist/${encodeURIComponent(b.dataset.unwatch)}`, { method: "DELETE" }); renderWatch(); }
      catch (e) { $("#watch-msg").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
    }));
  } catch (e) {
    box.innerHTML = `<div class="error-box">关注列表加载失败：${esc(e.message)}</div>`;
  }
}

function watchItem(w) {
  const q = w.quote;
  const quote = q.status === "ok"
    ? `<span class="quote num">最新 <b>${q.last_price}</b> · 涨跌幅 <b>${q.price_change_ratio_pct?.toFixed?.(2) ?? q.price_change_ratio_pct}%</b> · ${esc(fmtShort(q.as_of))} · 扶摇快照</span>`
    : `<span class="quote">行情：${esc(q.status_label || "")} ${esc(q.message || "")}</span>`;
  const rels = w.events.length ? w.events.map((r) => `<div class="rel-line">
      <span class="badge ${r.relation === "direct" ? "st-reported" : "st-missing"}">${esc(r.relation_label)}</span>
      <a href="#/event/${esc(r.event_id)}">${esc(r.title)}</a>
      <div class="small muted">${esc(r.detail)}</div>
      ${r.latest_changes.slice(0, 2).map(changeLine).join("")}</div>`).join("")
    : `<div class="small muted" style="margin-top:6px">未发现与已跟踪事件的关联</div>`;
  return `<div class="watch-item">
    <div class="watch-row"><div><span class="watch-name">${esc(w.name)}</span> <span class="watch-code">${esc(w.thscode)}</span> ${constructedBadge(w.is_constructed)}</div>
    ${w.is_constructed ? "" : `<button class="link-btn small" data-unwatch="${esc(w.thscode)}">移除</button>`}</div>
    ${quote}${rels}</div>`;
}

/* ---------------- 事件详情 ---------------- */
const evState = { id: null, data: null, asOf: null, open: new Set(), timer: null, req: 0 };

async function renderEvent(id) {
  evState.id = id; evState.asOf = null; evState.open = new Set();
  app.innerHTML = `<div class="loading">加载事件…</div>`;
  try {
    const params = new URLSearchParams(location.hash.split("?")[1] || "");
    const t = params.get("t");
    if (t) evState.asOf = t;
    const data = await fetchEvent();
    buildEventShell(data);
    updateEvent(data);
    loadRelations(data);
  } catch (e) {
    app.innerHTML = `<div class="error-box">事件加载失败：${esc(e.message)}</div><p><a href="#/">返回</a></p>`;
  }
}

async function fetchEvent() {
  const q = evState.asOf ? `?as_of=${encodeURIComponent(evState.asOf)}` : "";
  const my = ++evState.req;
  const data = await api(`/api/events/${encodeURIComponent(evState.id)}${q}`);
  if (my !== evState.req) return null; // 过期响应
  evState.data = data;
  return data;
}

function buildEventShell(d) {
  const ev = d.event;
  const min = Date.parse(d.time_range.min) - 3600 * 1000;
  const max = Date.parse(d.time_range.max);
  const span = max - min || 1;
  const ticks = d.time_range.moments.map((m) => {
    const src = d.timeline.find((x) => x.effective_at === m) || {};
    const left = ((Date.parse(m) - min) / span) * 100;
    return `<span class="tick" data-t="${esc(m)}" title="${esc(fmtFull(m) + " " + (src.title || ""))}" style="left:${left}%;background:${TIER_COLOR[src.source_type] || "var(--t1)"}"></span>`;
  }).join("");
  const subjects = ev.subjects.map((s) => `${esc(s.name)}${s.thscode ? `<span class="watch-code"> ${esc(s.thscode)}</span>` : ""}（${{ acquirer: "收购方", target: "标的", related: "相关方" }[s.role] || s.role}）`).join("、");
  app.innerHTML = `
  <div class="ev-head"><div>
      <div class="meta"><a href="#/">← 返回</a><span>${esc(d.event_type_label)}</span>${constructedBadge(ev.is_constructed)}<span>主体：${subjects || "待确认"}</span></div>
      <h1>${esc(ev.title)}</h1>
      ${ev.note ? `<div class="small muted">${esc(ev.note)}</div>` : ""}
    </div>
    <div><a class="btn" href="#/ingest?event=${esc(ev.id)}">＋ 为该事件导入新消息</a></div>
  </div>
  <div class="timebar">
    <div class="timebar-top">
      <span class="asof-label">截至 <b id="asof-text"></b> 的认知 <span class="muted small" id="asof-hint"></span></span>
      <div class="timebar-btns">
        <button class="btn small" id="prev-ev" title="跳到上一条证据">◀ 上一条证据</button>
        <button class="btn small" id="next-ev" title="跳到下一条证据">下一条证据 ▶</button>
        <button class="btn small primary" id="now-ev">回到现在</button>
      </div>
    </div>
    <div class="track">${ticks}<input type="range" id="asof-range" min="${min}" max="${max}" step="60000" aria-label="时间回放"></div>
    <div class="track-labels"><span>${fmtFull(new Date(min).toISOString())}</span><span>现在 ${fmtFull(d.time_range.max)}</span></div>
    <div class="summary-line" id="summary-line"></div>
  </div>
  <div class="status-card" id="status-card"></div>
  <div class="grid-2">
    <section class="panel"><div class="panel-head"><h2>事实账本</h2><span class="small muted">点击一行查看该事实的全部说法与变化</span></div><div id="ledger"></div></section>
    <section class="panel"><div class="panel-head"><h2>证据时间线</h2><span class="small muted">按生效时间 · 颜色=来源等级</span></div><div id="timeline"></div></section>
  </div>
  <div class="grid-3" style="margin-top:16px">
    <section class="panel"><h2>证据冲突</h2><div id="conflicts"></div></section>
    <section class="panel"><h2>溯源</h2><div id="origin"></div></section>
    <section class="panel"><h2>观点与推测 <span class="small muted">（不参与事实判定）</span></h2><div id="opinions"></div></section>
  </div>
  <div class="grid-2" style="margin-top:16px">
    <section class="panel"><div class="panel-head"><h2>与关注标的的关系</h2></div><div id="relations"><div class="loading">加载中…</div></div></section>
    <section>
      <div class="panel"><h2>结论变化记录</h2><ul class="list-plain changes" id="changes"></ul></div>
      <div class="panel"><h2>问一问 <span class="small muted">只基于当前结论回答，附出处</span></h2>
        <form class="ask-row" id="ask-form"><input type="text" name="q" placeholder="例如：交易对价现在是多少？停牌了吗？" aria-label="提问"><button class="btn primary">提问</button></form>
        <div id="answer"></div></div>
    </section>
  </div>
  <section class="panel" style="margin-top:16px"><details><summary>数据口径与状态</summary><div id="data-status" style="margin-top:8px"></div></details></section>`;

  const range = $("#asof-range");
  range.value = evState.asOf ? Date.parse(evState.asOf) : max;
  range.addEventListener("input", () => {
    evState.asOf = Number(range.value) >= max - 60000 ? null : isoCN(Number(range.value));
    $("#asof-text").textContent = fmtFull(evState.asOf || d.time_range.max);
    clearTimeout(evState.timer);
    evState.timer = setTimeout(refreshEvent, 120);
  });
  $$(".tick").forEach((t) => t.addEventListener("click", () => jumpTo(t.dataset.t)));
  $("#now-ev").addEventListener("click", () => { evState.asOf = null; range.value = max; refreshEvent(); });
  $("#prev-ev").addEventListener("click", () => stepMoment(-1));
  $("#next-ev").addEventListener("click", () => stepMoment(1));
  $("#ask-form").addEventListener("submit", onAsk);
}

function jumpTo(iso) {
  evState.asOf = iso;
  $("#asof-range").value = Date.parse(iso);
  refreshEvent();
}
function stepMoment(dir) {
  const ms = evState.data.time_range.moments.map(Date.parse);
  const cur = evState.asOf ? Date.parse(evState.asOf) : Date.parse(evState.data.time_range.max);
  const next = dir < 0 ? [...ms].reverse().find((m) => m < cur) : ms.find((m) => m > cur);
  if (next === undefined) { if (dir > 0) $("#now-ev").click(); return; }
  jumpTo(isoCN(next));
}

async function refreshEvent() {
  try {
    const d = await fetchEvent();
    if (d) updateEvent(d);
  } catch (e) {
    $("#summary-line").innerHTML = `<div class="error-box">刷新失败：${esc(e.message)}</div>`;
  }
}

function updateEvent(d) {
  $("#asof-text").textContent = fmtFull(d.as_of);
  $("#asof-hint").textContent = evState.asOf ? "（时间回放中：只使用此刻之前已披露的证据）" : "（实时）";
  $("#summary-line").innerHTML = `${esc(d.summary)} <span class="muted small">已纳入 ${d.visible_source_count} 条来源</span>`;
  $$(".tick").forEach((t) => { t.style.opacity = Date.parse(t.dataset.t) <= Date.parse(d.as_of) ? 1 : 0.3; });
  renderStatusCard(d);
  renderLedger(d);
  renderTimeline(d);
  renderConflicts(d);
  renderOrigin(d);
  renderOpinions(d);
  renderChanges(d);
  renderDataStatus(d);
}

function slotByKey(d, k) { return d.slots.find((s) => s.key === k); }

function renderStatusCard(d) {
  const cols = [
    ["confirmed", "已确认", "公告/监管/公司正式口径"],
    ["disputed", "存在冲突", "同等级来源说法不一"],
    ["unverified", "待证实", "仅媒体报道或传闻"],
    ["closed", "已否认 / 已过期", "被否认，或长期无人证实"],
  ];
  $("#status-card").innerHTML = cols.map(([k, title, tip]) => {
    const items = d.buckets[k].map((key) => slotByKey(d, key)).filter(Boolean);
    return `<div class="sc-col ${k}"><h3 title="${esc(tip)}"><span>${title}</span><span class="num">${items.length}</span></h3>
      ${items.length ? items.map((s) => `<div class="sc-item" data-slot="${esc(s.key)}" title="${esc(s.status_label)}"><span class="lbl">${esc(s.label)}</span><span class="val">${esc(s.value)}</span></div>`).join("")
        : `<div class="sc-empty">无</div>`}</div>`;
  }).join("");
  $$(".sc-item", $("#status-card")).forEach((el) => el.addEventListener("click", () => {
    evState.open.add(el.dataset.slot);
    renderLedger(evState.data);
    const row = $(`tr[data-slot="${el.dataset.slot}"]`);
    if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
  }));
}

function renderLedger(d) {
  const rows = d.slots.map((s) => {
    const open = evState.open.has(s.key);
    const deciders = s.deciding_claim_ids.map((id) => d.claims[id]).filter(Boolean);
    const basis = deciders.length ? [...new Set(deciders.map((c) => c.outlet))].map(esc).join("、") : "—";
    const flags = [...(s.flags || []), ...(s.conflict && s.status !== "disputed" ? [s.conflict.message] : [])];
    const main = `<tr class="slot-row ${open ? "open" : ""}" data-slot="${esc(s.key)}">
      <td>${esc(s.label)}</td>
      <td><div class="val">${s.value ? esc(s.value) : `<span class="muted">—</span>`}</div>${flags.map((f) => `<div class="flag">⚠ ${esc(f)}</div>`).join("")}</td>
      <td>${stBadge(s.status, s.status_label)}</td>
      <td class="small">${basis}<div class="muted num" title="同一原始来源的转载只计一次">独立来源 ${s.independent_sources}</div></td>
      <td class="small muted num">${s.history.length}</td></tr>`;
    if (!open) return main;
    const hist = s.history.length ? s.history.map((c) => histItem(c)).join("") : `<div class="muted small">暂无任何来源提及</div>`;
    return main + `<tr><td colspan="5" class="hist">${hist}</td></tr>`;
  }).join("");
  $("#ledger").innerHTML = `<table class="ledger"><thead><tr><th style="width:96px">事实</th><th>当前结论</th><th style="width:110px">状态</th><th style="width:130px">依据</th><th style="width:44px" title="该事实被提及的次数">说法</th></tr></thead><tbody>${rows}</tbody></table>`;
  $$(".slot-row", $("#ledger")).forEach((tr) => tr.addEventListener("click", () => {
    const k = tr.dataset.slot;
    evState.open.has(k) ? evState.open.delete(k) : evState.open.add(k);
    renderLedger(evState.data);
  }));
  $$("[data-src]", $("#ledger")).forEach(bindSrc);
}

function histItem(c) {
  const inactive = INACTIVE.has(c.status);
  const csCls = c.status === "denied" || c.status === "overridden" ? "bad" : inactive ? "mute" : "";
  const act = actionLabel(c.action);
  return `<div class="hist-item ${inactive ? "inactive" : ""}"><time>${fmtShort(c.effective_at)}</time><div>
    <div>${tierTag(c.source_type, c.source_type_label)} <button class="link-btn" data-src="${esc(c.source_id)}">${esc(c.outlet)}</button>
      ${natureBadge(c.nature, c.nature_label)} ${act ? `<span class="badge st-unverified">${act}</span>` : ""}
      <span class="hv"><b>${esc(c.value ?? "（否认确定取值）")}</b></span>
      <span class="weight" title="${esc(weightTip(c))}">w=${c.weight}</span></div>
    <div class="q">原文：「${esc(c.quote)}」</div>
    <div class="cs ${csCls}">${esc(c.status_label)}${c.reason ? "：" + esc(c.reason) : ""}</div>
    ${c.distortion ? `<div class="cs bad">⚠ 转述走样：${esc(c.distortion)}</div>` : ""}
    ${c.extraction !== "manual" ? `<div class="cs">抽取方式：${c.extraction === "llm" ? "LLM" : "规则兜底"}，置信度 ${c.confidence}</div>` : ""}
  </div></div>`;
}

function renderTimeline(d) {
  const items = [...d.timeline].reverse();
  $("#timeline").innerHTML = items.length ? `<div class="tl">${items.map((s) => {
    const claims = s.claim_ids.map((id) => d.claims[id]).filter(Boolean);
    const td = s.trade_date || {};
    return `<div class="tl-item" style="--c:${TIER_COLOR[s.source_type]}">
      <div class="tl-time">${fmtFull(s.effective_at)}${s.updated_at ? "（更新）" : ""}</div>
      <div class="tl-title"><button data-src="${esc(s.source_id)}">${esc(s.title)}</button></div>
      <div class="tl-tags">${tierTag(s.source_type, s.source_type_label)}<span class="small muted">${esc(s.outlet)}</span>
        ${s.version_of ? `<span class="badge st-reported">原文更新版</span>` : ""}
        ${s.reposted_from ? `<span class="badge st-missing">转载</span>` : ""}
        ${s.origin === "ingest" ? `<span class="badge st-reported">用户导入</span>` : ""}
        <span class="small muted" title="${esc(td.note || "")}">→ 交易日 ${esc(td.date || "—")}${td.method === "weekday_estimate" ? "（估算）" : ""}</span></div>
      ${s.fetch_lag_warning ? `<div class="tl-note">⏱ ${esc(s.fetch_lag_warning)}</div>` : ""}
      <div class="tl-claims">${claims.length ? claims.map((c) => `<div>${natureBadge(c.nature, c.nature_label)} ${esc(c.slot_label || slotLabel(d, c.slot))}：<span class="${INACTIVE.has(c.status) ? "cs mute" : ""}">${actionLabel(c.action) ? actionLabel(c.action) + " " : ""}${esc(c.value ?? "")}</span>${INACTIVE.has(c.status) ? ` <span class="cs">（${esc(c.status_label)}）</span>` : ""}</div>`).join("") : `<div class="muted">未抽取到结构化主张</div>`}</div>
    </div>`;
  }).join("")}</div>` : `<div class="empty">此时刻之前没有任何来源</div>`;
  $$("[data-src]", $("#timeline")).forEach(bindSrc);
}

function slotLabel(d, key) {
  const s = slotByKey(d, key);
  if (s) return s.label;
  return SLOT_LABEL[key] || key;
}

function renderConflicts(d) {
  const box = $("#conflicts");
  if (!d.conflicts.length) { box.innerHTML = `<div class="muted small">此刻没有未解决的冲突。</div>`; return; }
  box.innerHTML = d.conflicts.map((c) => `<div class="conflict sev-${esc(c.severity)}">
    <div><b>${esc(c.slot_label)}</b>：${esc(c.message)}</div>
    ${c.sides ? `<div class="sides">${c.sides.map((s) => `<div class="side"><b>${esc(s.value)}</b><div class="small muted">${esc(s.outlets.join("、"))}</div><div class="small muted num">证据分 ${s.score}</div></div>`).join("")}</div>` : ""}
    <div class="small muted" style="margin-top:6px">产品不替你选边：等待更高等级来源（公司回应/公告）出现后，结论会自动更新。</div>
  </div>`).join("");
}

function renderOrigin(d) {
  const o = d.origin;
  const earliest = d.timeline.find((s) => s.source_id === o.earliest_source_id);
  const fs = o.first_seen.filter((f) => ["target", "consideration", "deal_stage", "stake", "financing", "trading_halt"].includes(f.slot)).slice(0, 8);
  const tree = Object.entries(o.repost_tree);
  $("#origin").innerHTML = `
    ${earliest ? `<dl class="kv"><dt>最早出处</dt><dd><button class="link-btn" data-src="${esc(earliest.source_id)}">${esc(earliest.outlet)}</button> ${tierTag(earliest.source_type, earliest.source_type_label)}<div class="small muted">${fmtFull(earliest.effective_at)}</div></dd></dl>` : `<div class="muted small">暂无来源</div>`}
    ${fs.length ? `<h3 style="margin-top:12px">各说法首次出现</h3><ul class="list-plain">${fs.map((f) => `<li>${esc(slotLabel(d, f.slot))}「${esc(f.value)}」<div class="small muted">${fmtShort(f.at)} · <button class="link-btn" data-src="${esc(f.source_id)}">${esc(f.outlet)}</button>（${esc(f.source_type_label)}）</div></li>`).join("")}</ul>` : ""}
    ${tree.length ? `<h3 style="margin-top:12px">转载链</h3>${tree.map(([root, kids]) => `<div class="small">${esc(srcName(d, root))} → ${kids.map((k) => esc(srcName(d, k))).join("、")}<div class="muted">转载按原始来源计为 1 个独立来源</div></div>`).join("")}` : ""}
    ${o.distortions.length ? `<h3 style="margin-top:12px">转述走样</h3>${o.distortions.map((c) => `<div class="warn-box">${esc(c.distortion)}</div>`).join("")}` : ""}`;
  $$("[data-src]", $("#origin")).forEach(bindSrc);
}
function srcName(d, id) { const s = d.timeline.find((x) => x.source_id === id); return s ? s.outlet : id; }

function renderOpinions(d) {
  const ops = d.opinions;
  $("#opinions").innerHTML = ops.length ? ops.map((c) => `<div class="opinion">
      ${natureBadge(c.nature, c.nature_label)} <b>${esc(c.value)}</b>
      <div class="small muted">${tierTag(c.source_type, c.source_type_label)} <button class="link-btn" data-src="${esc(c.source_id)}">${esc(c.outlet)}</button> · ${fmtShort(c.effective_at)} · <span class="weight" title="${esc(weightTip(c))}">w=${c.weight}</span></div>
      <div class="small" style="color:var(--ink-2)">「${esc(c.quote)}」</div></div>`).join("")
    : `<div class="muted small">此刻没有观点或推测类内容。</div>`;
  $$("[data-src]", $("#opinions")).forEach(bindSrc);
}

function renderChanges(d) {
  const all = [...d.changes].reverse();
  const LIMIT = 8;
  const ch = evState.showAllChanges ? all : all.slice(0, LIMIT);
  $("#changes").innerHTML = (ch.length ? ch.map((c) => `<li><time>${fmtShort(c.at)}</time>${esc(c.slot_label)}：${c.from_status ? `${esc(c.from_status)}${c.from_value ? "「" + esc(c.from_value) + "」" : ""}` : "新出现"}<span class="arrow">→</span><b>${esc(c.to_status)}${c.to_value ? "「" + esc(c.to_value) + "」" : ""}</b>
      <div class="small muted">触发：${c.trigger_source_id ? `<button class="link-btn" data-src="${esc(c.trigger_source_id)}">${esc(c.trigger)}</button>` : esc(c.trigger)}${c.flags.length && c.from_value === c.to_value ? " · ⚠ " + esc(c.flags[c.flags.length - 1]) : ""}</div></li>`).join("")
    : `<li class="muted">暂无变化</li>`) + (all.length > LIMIT ? `<li><button class="link-btn small" id="toggle-changes">${evState.showAllChanges ? "收起" : `展开全部 ${all.length} 条`}</button></li>` : "");
  $$("[data-src]", $("#changes")).forEach(bindSrc);
  const tg = $("#toggle-changes");
  if (tg) tg.addEventListener("click", () => { evState.showAllChanges = !evState.showAllChanges; renderChanges(evState.data); });
}

function renderDataStatus(d) {
  const log = d.extraction_log || [];
  $("#data-status").innerHTML = `<dl class="kv">
    <dt>交易日历</dt><dd>${dataStatus(d.calendar_status)}<div class="small muted">披露时间按「15:00 前当日、之后顺延」换算为首个可交易日；日历不可用时按工作日估算并标注。</div></dd>
    <dt>证据权重</dt><dd class="small">来源等级 × 性质系数 × 出处系数 × 时效衰减（传闻/推测半衰期 7 天）；同一原始来源的转载只计一次。</dd>
    <dt>过期规则</dt><dd class="small">只有传闻/自媒体支撑的说法，14 天内无更高等级来源证实即标为「已过期·未证实」；带截止日期的事项逾期未更新会提示「逾期待核实」。</dd>
    <dt>四类时间</dt><dd class="small">事件发生时间 / 披露时间 / 抓取时间 / 更新时间分别记录；结论按"生效时间"（有更新则取更新时间）计算；抓取晚于披露 6 小时以上会提示。</dd>
    <dt>抽取记录</dt><dd class="small">${log.length ? log.map((l) => `${fmtShort(l.at)} ${l.method === "llm" ? "LLM" : "规则兜底"}：接受 ${l.accepted} 条，拒绝 ${l.rejected.length} 条${l.fallback_reason ? "（" + esc(l.fallback_reason) + "）" : ""}${l.rejected.map((r) => `<div class="muted">· 拒绝：${esc(r.reason)}</div>`).join("")}`).join("<br>") : "本事件样例主张为人工标注（作为抽取基准）；用户导入的来源会在此显示抽取与校验记录。"}</dd>
  </dl>`;
}

async function onAsk(ev) {
  ev.preventDefault();
  const q = ev.target.q.value.trim();
  if (!q) return;
  $("#answer").innerHTML = `<div class="answer muted">查询中…</div>`;
  try {
    const r = await api(`/api/events/${encodeURIComponent(evState.id)}/ask`, { method: "POST", body: { question: q, as_of: evState.asOf } });
    $("#answer").innerHTML = `<div class="answer ${r.refused ? "refused" : ""}">${esc(r.answer)}</div>`;
  } catch (e) { $("#answer").innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
}

/* 关系 + 行情 */
async function loadRelations(d) {
  const box = $("#relations");
  const rels = d.relations;
  const subjectRows = d.event.subjects.filter((s) => s.thscode).map((s) => ({ thscode: s.thscode, name: s.name, relation_label: "事件主体", detail: { acquirer: "收购方", target: "标的", related: "相关方" }[s.role] || s.role, relation: "subject" }));
  const seen = new Set();
  const rows = [...rels, ...subjectRows].filter((r) => { if (seen.has(r.thscode)) return false; seen.add(r.thscode); return true; });
  if (!rows.length) { box.innerHTML = `<div class="muted small">关注列表为空。</div>`; return; }
  box.innerHTML = rows.map((r, i) => `<div class="rel">
      <div><b>${esc(r.name)}</b> <span class="watch-code">${esc(r.thscode)}</span>
        <span class="badge ${r.relation === "direct" ? "st-reported" : r.relation === "none" ? "st-missing" : "st-unverified"}">${esc(r.relation_label)}</span></div>
      <div class="small muted">${esc(r.detail)}${r.basis ? ` · 依据：${esc(r.basis)}` : ""}</div>
      ${r.relation === "none" ? "" : `<div id="mk-${i}" class="small muted">行情加载中…</div>`}</div>`).join("");
  rows.forEach(async (r, i) => {
    if (r.relation === "none") return;
    const el = $(`#mk-${i}`);
    try {
      const m = await api(`/api/events/${encodeURIComponent(d.event.id)}/market?thscode=${encodeURIComponent(r.thscode)}`);
      el.innerHTML = marketView(m);
    } catch (e) { el.innerHTML = `<span class="cs bad">行情请求失败：${esc(e.message)}</span>`; }
  });
}

function marketView(m) {
  if (m.result.status !== "ok" || !m.bars.length) {
    return `行情：${dataStatus(m.result)}`;
  }
  const W = 520, H = 120, P = 6;
  const xs = m.bars.map((_, i) => P + (i / Math.max(1, m.bars.length - 1)) * (W - 2 * P));
  const cl = m.bars.map((b) => b.close);
  const lo = Math.min(...cl), hi = Math.max(...cl), rg = hi - lo || 1;
  const ys = cl.map((c) => H - P - ((c - lo) / rg) * (H - 2 * P));
  const path = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join("");
  const idx = Object.fromEntries(m.bars.map((b, i) => [b.date, i]));
  const marks = m.markers.filter((k) => idx[k.trade_date.date] !== undefined).map((k) => {
    const i = idx[k.trade_date.date];
    return `<line x1="${xs[i]}" x2="${xs[i]}" y1="0" y2="${H}" stroke="${TIER_COLOR[k.source_type]}" stroke-dasharray="3 3"><title>${esc(k.trade_date.date + " " + k.title + (k.day_change_pct !== undefined ? ` 当日涨跌 ${k.day_change_pct}%` : ""))}</title></line>`;
  }).join("");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="事件期间收盘价走势"><path d="${path}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>${marks}</svg>
    <div class="small muted">${esc(m.caliber)}。虚线=证据对应的首个可交易日。${esc(m.note)}</div>
    <div class="small muted">${esc(m.result.source)} · 数据时间 ${fmtFull(m.result.as_of)}</div>`;
}

/* 来源抽屉 */
function bindSrc(el) { el.addEventListener("click", (ev) => { ev.stopPropagation(); openSource(el.dataset.src); }); }
function openSource(id) {
  const d = evState.data;
  const s = d && d.timeline.find((x) => x.source_id === id);
  if (!s) return;
  const claims = s.claim_ids.map((cid) => d.claims[cid]).filter(Boolean);
  let html = esc(s.text);
  const seenQ = new Set();
  claims.forEach((c) => {
    const q = esc(c.quote);
    if (!q || seenQ.has(q)) return;
    seenQ.add(q);
    html = html.split(q).join(`<mark class="${INACTIVE.has(c.status) ? "m-inactive" : ""}" title="${esc(slotLabel(d, c.slot) + "：" + (c.value ?? "") + "（" + c.status_label + "）")}">${q}</mark>`);
  });
  const td = s.trade_date || {};
  $("#drawer-body").innerHTML = `
    <div class="small muted">${tierTag(s.source_type, s.source_type_label)} · ${esc(s.outlet)} ${constructedBadge(s.is_constructed)}</div>
    <h2 style="font-size:18px;margin-top:6px">${esc(s.title)}</h2>
    <dl class="kv">
      <dt>事件发生时间</dt><dd>${s.event_time ? fmtFull(s.event_time) : `<span class="muted">原文未给出</span>`}</dd>
      <dt>披露时间</dt><dd>${fmtFull(s.published_at)}</dd>
      <dt>抓取时间</dt><dd>${fmtFull(s.fetched_at)} ${s.fetch_lag_warning ? `<span class="cs bad">${esc(s.fetch_lag_warning)}</span>` : ""}</dd>
      <dt>更新时间</dt><dd>${s.updated_at ? fmtFull(s.updated_at) : `<span class="muted">无</span>`}</dd>
      <dt>首个可交易日</dt><dd>${esc(td.date || "—")} <span class="small muted">${esc(td.note || "")}</span></dd>
      ${s.version_of ? `<dt>版本</dt><dd>是 <button class="link-btn" data-src2="${esc(s.version_of)}">${esc(srcName(d, s.version_of))} 原文</button> 的更新版；旧版本中的说法已作废</dd>` : ""}
      ${s.reposted_from ? `<dt>转载自</dt><dd><button class="link-btn" data-src2="${esc(s.reposted_from)}">${esc(srcName(d, s.reposted_from))}</button></dd>` : ""}
      ${s.url ? `<dt>原文链接</dt><dd><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a></dd>` : ""}
    </dl>
    <div class="src-text">${html}</div>
    <h3>从这条来源抽出的主张</h3>
    ${claims.length ? claims.map(histItem).join("") : `<div class="muted small">没有抽取到结构化主张。</div>`}`;
  $$("[data-src2]", $("#drawer-body")).forEach((b) => b.addEventListener("click", () => openSource(b.dataset.src2)));
  $$("[data-src]", $("#drawer-body")).forEach(bindSrc);
  $("#drawer").hidden = false; $("#drawer-backdrop").hidden = false;
}
function closeDrawer() { $("#drawer").hidden = true; $("#drawer-backdrop").hidden = true; }
$("#drawer-close").addEventListener("click", closeDrawer);
$("#drawer-backdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ---------------- 导入 ---------------- */
const SAMPLES = [
  { label: "示例：签署正式协议公告", data: { title: "辰星电子：关于签署股权转让协议的公告", outlet: "辰星电子", issuer: "company_chenxing", source_type: "announcement",
    text: "公司于近日与海岳光芯股东签署《股权转让协议》，以现金方式收购海岳光芯55%股权，交易对价最终确定为24.5亿元。本次交易尚需提交公司股东大会审议。" } },
  { label: "示例：媒体报道不同对价", data: { title: "财经媒体D：辰星电子收购对价敲定", outlet: "财经媒体D", issuer: "media_d", source_type: "media",
    text: "据接近交易的人士透露，辰星电子收购海岳光芯的交易对价已敲定为24亿元，双方将于近期签约。" } },
  { label: "示例：新传闻（另一事件）", data: { title: "股吧：云帆材料要收购北辰新能？", outlet: "某股吧用户", issuer: "guba_user_2", source_type: "rumor",
    text: "听说云帆材料要收购北辰新能，估值大概12亿，下周停牌。" } },
];

async function renderIngest() {
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const preEvent = params.get("event") || "";
  app.innerHTML = `<div class="grid-2">
    <section class="panel">
      <div class="panel-head"><h2>导入一条新消息</h2><span class="small muted">粘贴原文 → 抽取主张 → 校验 → 归并到事件 → 重算结论</span></div>
      <div class="sample-btns">${SAMPLES.map((s, i) => `<button class="btn small" data-sample="${i}">${esc(s.label)}</button>`).join("")}<span class="small muted">（示例均为构造文本）</span></div>
      <form id="ingest-form" class="form-grid">
        <div class="full"><label>标题</label><input type="text" name="title" placeholder="可留空，默认取原文前 30 字"></div>
        <div class="full"><label>原文 *</label><textarea name="text" required placeholder="粘贴公告、新闻、研报观点或传闻原文"></textarea></div>
        <div><label>来源类型 *</label><select name="source_type">${SOURCE_TYPES.map(([v, l]) => `<option value="${v}" ${v === "media" ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div><label>发布方</label><input type="text" name="outlet" placeholder="如：财经媒体A / 某公司"></div>
        <div><label>披露时间 *</label><input type="datetime-local" name="published_at"></div>
        <div><label>事件发生时间（可选）</label><input type="datetime-local" name="event_time"></div>
        <div class="full"><label>原文链接（可选）</label><input type="url" name="url" placeholder="https://"></div>
        <div><label>归属事件</label><select name="event_id" id="ing-event"><option value="">自动归并</option></select></div>
        <div><label>与已有来源的关系（可选）</label><select name="rel" id="ing-rel"><option value="">无</option></select></div>
        <input type="hidden" name="issuer">
        <div class="full"><button class="btn primary" type="submit" id="ing-submit">导入并重算</button>
          <span class="small muted">未配置 LLM 时使用规则抽取（覆盖有限）。所有抽取结果必须能在原文中找到引用，否则会被拒绝。</span></div>
      </form>
    </section>
    <section>
      <div class="panel" id="ingest-result"><h2>导入结果</h2><div class="muted small">提交后在这里查看：抽到了哪些主张、哪些被拒绝、归入哪个事件、结论变了什么。</div></div>
      <div class="panel"><h2>从 iFinD 检索新闻</h2>
        <form class="ask-row" id="ifind-form"><input type="text" name="q" placeholder="关键词，如公司名"><button class="btn">检索</button></form>
        <div id="ifind-result" class="small muted" style="margin-top:8px">需要配置 IFIND_MCP_URL 与 IFIND_NEWS_TOOL；未配置时会明确提示，不会返回空列表冒充正常。</div>
      </div>
    </section></div>`;
  const form = $("#ingest-form");
  form.published_at.value = toLocalInput(new Date());
  try {
    const { items } = await api("/api/events");
    $("#ing-event").innerHTML = `<option value="">自动归并</option>` + items.map((e) => `<option value="${esc(e.id)}" ${e.id === preEvent ? "selected" : ""}>${esc(e.title)}</option>`).join("");
    if (preEvent) loadRelOptions(preEvent);
  } catch (_) { /* 忽略：保留自动归并 */ }
  $("#ing-event").addEventListener("change", (e) => loadRelOptions(e.target.value));
  $$("[data-sample]").forEach((b) => b.addEventListener("click", () => {
    const s = SAMPLES[Number(b.dataset.sample)].data;
    for (const [k, v] of Object.entries(s)) if (form[k]) form[k].value = v;
    form.published_at.value = toLocalInput(new Date());
  }));
  form.addEventListener("submit", onIngest);
  $("#ifind-form").addEventListener("submit", onIFind);
}

async function loadRelOptions(eventId) {
  const sel = $("#ing-rel");
  sel.innerHTML = `<option value="">无</option>`;
  if (!eventId) return;
  try {
    const d = await api(`/api/events/${encodeURIComponent(eventId)}`);
    sel.innerHTML += d.timeline.map((s) => `<option value="version_of:${esc(s.source_id)}">是「${esc(s.title.slice(0, 18))}」的更新版本</option><option value="reposted_from:${esc(s.source_id)}">转载自「${esc(s.title.slice(0, 18))}」</option>`).join("");
  } catch (_) { /* 忽略 */ }
}

async function onIngest(ev) {
  ev.preventDefault();
  const f = ev.target;
  const body = {
    title: f.title.value, text: f.text.value, source_type: f.source_type.value, outlet: f.outlet.value,
    issuer: f.issuer.value || f.outlet.value, url: f.url.value,
    published_at: f.published_at.value ? f.published_at.value + ":00+08:00" : null,
    event_time: f.event_time.value ? f.event_time.value + ":00+08:00" : null,
    event_id: f.event_id.value || null,
  };
  const rel = f.rel.value;
  if (rel) { const [k, v] = rel.split(":"); body[k] = v; }
  const btn = $("#ing-submit");
  btn.disabled = true; btn.textContent = "抽取与重算中…";
  const box = $("#ingest-result");
  try {
    const r = await api("/api/ingest", { method: "POST", body });
    const ex = r.extraction;
    box.innerHTML = `<h2>导入结果</h2>
      <div>${esc(r.message)} · <a href="#/event/${esc(r.event_id)}">查看事件 →</a></div>
      ${r.created_event ? `<div class="warn-box">未匹配到已有事件，已新建「${esc(r.created_event.title)}」，归属待人工确认。</div>` : ""}
      ${r.match ? `<div class="small muted" style="margin-top:6px">归并依据：${esc(r.match.best ? r.match.best.title : "")} 得分 ${r.match.best ? r.match.best.score : 0}（阈值 ${r.match.threshold}）${r.match.best ? "：" + esc(r.match.best.reasons.join("；")) : ""}</div>` : `<div class="small muted">已按指定事件归并</div>`}
      <h3 style="margin-top:12px">抽取方式：${ex.method === "llm" ? "LLM" : "规则兜底"}</h3>
      ${ex.fallback_reason ? `<div class="warn-box">${esc(ex.fallback_reason)}</div>` : ""}
      <ul class="list-plain result-claims">${ex.claims.map((c) => `<li>${natureBadge(c.nature, { fact: "事实", opinion: "观点", speculation: "推测", rumor: "传闻" }[c.nature])} ${esc(SLOT_LABEL[c.slot] || c.slot)}：<b>${esc(c.display_value ?? c.value ?? "（否认）")}</b> ${actionLabel(c.action) ? `<span class="badge st-unverified">${actionLabel(c.action)}</span>` : ""}<div class="small muted">「${esc(c.quote)}」</div></li>`).join("") || `<li class="muted">没有抽取到可用主张</li>`}</ul>
      ${ex.rejected.length ? `<h3 style="margin-top:10px">被拒绝的候选（不会进入结论）</h3><ul class="list-plain">${ex.rejected.map((x) => `<li class="small">${esc(x.reason)}：<span class="muted">${esc(JSON.stringify(x.raw))}</span></li>`).join("")}</ul>` : ""}
      <h3 style="margin-top:12px">结论变化</h3>
      ${r.conclusion_changes.length ? `<ul class="list-plain">${r.conclusion_changes.map((c) => `<li>${esc(c.slot_label)}：${esc(c.from)}<span class="arrow">→</span><b>${esc(c.to)}</b></li>`).join("")}</ul>` : `<div class="muted small">当前结论未变化（可能被更高等级来源覆盖，或未抽取到新事实）。</div>`}`;
  } catch (e) {
    box.innerHTML = `<h2>导入结果</h2><div class="error-box">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "导入并重算";
  }
}

async function onIFind(ev) {
  ev.preventDefault();
  const q = ev.target.q.value.trim();
  const box = $("#ifind-result");
  if (!q) return;
  box.innerHTML = "检索中…";
  try {
    const r = await api(`/api/ifind/news?q=${encodeURIComponent(q)}`);
    if (r.status !== "ok") { box.innerHTML = dataStatus(r); return; }
    box.innerHTML = r.data.map((n, i) => `<div class="news-item"><b>${esc(n.title || "（无标题）")}</b>
      <div class="muted">${esc(n.outlet || "来源未知")} · ${n.published_at ? fmtFull(n.published_at) : "时间未知"}</div>
      <div>${esc((n.text || "").slice(0, 120))}</div><button class="btn small" data-news="${i}">填入表单</button></div>`).join("");
    $$("[data-news]", box).forEach((b) => b.addEventListener("click", () => {
      const n = r.data[Number(b.dataset.news)];
      const f = $("#ingest-form");
      f.title.value = n.title || ""; f.text.value = n.text || ""; f.outlet.value = n.outlet || ""; f.url.value = n.url || "";
      if (n.published_at) f.published_at.value = toLocalInput(new Date(n.published_at));
      f.source_type.value = "media";
      window.scrollTo({ top: 0, behavior: "smooth" });
    }));
  } catch (e) { box.innerHTML = `<div class="error-box">${esc(e.message)}</div>`; }
}

/* ---------------- 启动 ---------------- */
loadHealth();
route();
