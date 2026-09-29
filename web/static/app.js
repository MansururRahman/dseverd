"use strict";

const state = { view: null, defaults: {}, watchlist: [], poll: null, pollToken: 0,
  shortlistParams: {}, pendingNote: null };

// ---------------------------------------------------------------- helpers --
function h(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    e.append(k instanceof Node ? k : String(k));
  }
  return e;
}
const missing = v => v === null || v === undefined || Number.isNaN(v);
const num = (v, d = 2) => missing(v) ? "–" : Number(v).toLocaleString("en-US", { maximumFractionDigits: d });
const pct = (v, d = 2) => missing(v) ? "–" : (v * 100).toFixed(d) + "%";

function tone(text) {
  const t = String(text || "").toUpperCase();
  if (t.includes("ARMED BUT")) return "warn";
  if (/NOT |NO TRADE|NO DATA|EXIT|AVOID|WEAK|NEGATIVE|UNFAVORABLE|FAIL/.test(t)) return "bad";
  if (/BUY|TRADE|ROBUST|FAVORABLE|POSITIVE|LEAN \+|PASS/.test(t)) return "good";
  return "warn";
}
const chip = text => h("span", { class: "chip " + tone(text) }, text ?? "–");

async function api(path, body) {
  const init = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = Array.isArray(data.detail)
      ? data.detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : (data.detail || `HTTP ${res.status}`);
    throw Object.assign(new Error(msg), { status: res.status, data });
  }
  return data;
}

function clearInvalid() {
  document.querySelectorAll(".invalid").forEach(e => e.classList.remove("invalid"));
}
function showError(err) {
  if (err.status === 422 && Array.isArray(err.data?.detail)) {
    const form = document.getElementById("form");
    for (const d of err.data.detail) {
      const el = form.elements[(d.loc || [])[1]];
      if (el && el.classList) el.classList.add("invalid");
    }
  }
  document.getElementById("out").replaceChildren(h("div", { class: "error" }, err.message));
}

// ------------------------------------------------------------- UI pieces --
function card(decision, kvs, sub) {
  return h("div", { class: "card" },
    h("div", { class: "card-top" }, chip(decision), sub ? h("span", { class: "muted" }, sub) : null),
    h("dl", { class: "kv" }, Object.entries(kvs).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? "–")])));
}
function gateTable(rows, title = "Gates") {
  return h("div", {}, h("h3", {}, title),
    h("table", { class: "gates" }, h("tbody", {}, rows.map(g => {
      const trClass = g.ok === null ? "info" : (g.ok ? "pass" : "fail");
      const icon = g.ok === null ? "•" : (g.ok ? "✔" : "✘");
      return h("tr", { class: trClass },
        h("td", { class: "icon" }, icon), h("td", {}, g.name),
        h("td", { class: "detail" }, g.detail || ""));
    }))));
}
const boolGates = obj => Object.entries(obj || {}).map(([name, ok]) => ({ name, ok }));
function kvBlock(obj, title) {
  if (!obj) return null;
  return h("div", {}, h("h3", {}, title), h("dl", { class: "kv" }, Object.entries(obj).map(([k, v]) =>
    [h("dt", {}, k), h("dd", {}, typeof v === "number" ? num(v, 3) : (v ?? "–"))])));
}
const cmp = (a, b) => missing(a) ? 1 : missing(b) ? -1
  : (typeof a === "number" && typeof b === "number") ? a - b : String(a).localeCompare(String(b));

function table(rows, cols, opts = {}) {
  const tbody = h("tbody");
  const t = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, cols.map(c => h("th", { "data-key": c.key }, c.label)))), tbody);
  let sortKey = null, asc = true;
  const draw = () => {
    const rs = [...rows];
    if (sortKey) rs.sort((a, b) => cmp(a[sortKey], b[sortKey]) * (asc ? 1 : -1));
    tbody.replaceChildren(...rs.map(r => {
      const tr = h("tr", { class: opts.rowClass ? opts.rowClass(r) : null },
        cols.map(c => h("td", { class: c.cls }, c.fmt ? c.fmt(r[c.key], r) : (r[c.key] ?? "–"))));
      if (opts.onRow) { tr.classList.add("click"); tr.addEventListener("click", () => opts.onRow(r)); }
      return tr;
    }));
  };
  t.tHead.addEventListener("click", e => {
    const k = e.target.dataset.key;
    if (!k) return;
    asc = sortKey === k ? !asc : true;
    sortKey = k;
    draw();
  });
  draw();
  return t;
}
function backtestBlock(bt, cols, window) {
  const st = bt.stats || {};
  return h("div", {},
    h("h3", {}, "Backtest ", h("span", { class: "muted" }, window || "")),
    h("p", { class: "stats" },
      `Trades ${st.n ?? 0} · Win rate ${pct(st.win_rate, 0)} · Expectancy ${pct(st.expectancy)} / trade`,
      missing(st.total_pnl) ? "" : ` · Total P&L ${num(st.total_pnl, 0)} BDT`),
    bt.note ? h("p", { class: "note" }, bt.note) : null,
    (bt.trades || []).length ? table(bt.trades, cols) : h("p", { class: "muted" }, "No trades."));
}

// -------------------------------------------------------------- renderers --
function renderSwingEntry(r) {
  const e = r.entry, z = e.sizing || {}, s = e.snapshot || {};
  const kv = e.decision === "NO DATA" ? {} : {
    Setup: e.setup, Price: num(s.price), RSI: num(s.rsi, 1), "ATR %": num(s.atr_pct),
    Structure: s.structure, Entry: num(z.entry), Stop: num(z.stop), Target: num(z.target),
    Shares: num(z.shares, 0), RR: num(z.rr) };
  return h("div", {},
    card(e.decision, kv, e.reason),
    (e.gates || []).length ? gateTable(e.gates) : null,
    (e.manual || []).length ? h("div", {}, h("h3", {}, "Manual confirmations"),
      e.manual.map(m => h("label", { class: "check block" }, h("input", { type: "checkbox" }), m))) : null,
    r.backtest ? backtestBlock(r.backtest, [
      { key: "entry_date", label: "Entry date" }, { key: "exit_date", label: "Exit date" },
      { key: "entry", label: "Entry", fmt: v => num(v), cls: "n" },
      { key: "ret", label: "Return", fmt: v => pct(v), cls: "n" },
      { key: "pnl_bdt", label: "P&L BDT", fmt: v => num(v, 0), cls: "n" },
      { key: "hold", label: "Days", cls: "n" }, { key: "legs", label: "Legs", cls: "n" },
      { key: "setup", label: "Setup" }], r.backtest.window) : null);
}
function renderSwingExit(r) {
  const signals = (r.signals || []).map(s => typeof s === "string" ? { action: r.action, why: s } : s);
  return h("div", {},
    card(r.action, { Price: num(r.price), "P&L %": num(r.pnl_pct), Trail: num(r.trail),
      RSI: num(r.snapshot?.rsi, 1), "MACD hist": num(r.snapshot?.macd_hist, 3) }),
    h("h3", {}, "Signals"),
    signals.length ? table(signals, [{ key: "action", label: "Action", fmt: v => chip(v) },
      { key: "why", label: "Why" }]) : h("p", { class: "muted" }, "No exit signals — hold."));
}
function renderClaude(r) {
  const kv = r.tradeable ? { Uptrend: r.uptrend, RR: num(r.rr), Entry: num(r.entry),
    Exit: num(r.exit), Stop: num(r.stop) } : {};
  return h("div", {},
    card(r.tradeable ? "TRADEABLE" : "NOT TRADEABLE", kv, r.reason),
    gateTable((r.ledger || []).map(l => {
      let ok = null;
      let name = l;
      if (l.startsWith("PASS")) {
        ok = true;
        name = l.replace(/^PASS\s+/, "");
      } else if (l.startsWith("FAIL")) {
        ok = false;
        name = l.replace(/^FAIL\s+/, "");
      } else if (l.startsWith("INFO")) {
        ok = null;
        name = l.replace(/^INFO\s+/, "");
      }
      return { ok, name };
    }), "Ledger"));
}
function renderUptrend(r) {
  if (r.decision === "NO DATA") return card(r.decision, {}, r.reason);
  return h("div", {},
    card(r.decision, { Confidence: missing(r.confidence) ? "–" : r.confidence + "%",
      Optional: r.optional_score }, r.reason),
    gateTable(boolGates(r.mandatory), "Mandatory (all required)"),
    gateTable(boolGates(r.optional), "Optional (confidence)"),
    Object.keys(r.guards || {}).length ? gateTable(boolGates(r.guards), "Guards (veto)") : null,
    kvBlock(r.snapshot, "Snapshot"));
}
function renderTechnical(r) {
  const b = r.brief || {};
  if (missing(b.score)) return card("NO DATA", {}, b.note);
  return h("div", {},
    card(b.verdict, { Score: b.score, Price: num(b.price), RSI: num(b.rsi, 1),
      "Pos in 60d range %": num(b.pos, 0), "Avg 20d vol": num(b.avg_vol, 0) }, r.verdict),
    h("h3", {}, "Score breakdown"),
    h("ul", { class: "reasons" }, (r.reasons || []).map(x => h("li", { class: x.startsWith("-") ? "neg" : "pos" }, x))));
}
function renderGate(r) {
  const s = r.snapshot;
  return h("div", {},
    card(r.verdict, s ? { Price: num(s.price), "All gates": s.all_pass ? "PASS" : "blocked",
      Blocker: s.blocker || "–", "Clean days": r.n_clean } : {}, r.verdict_reason),
    s ? gateTable(s.gates) : null,
    backtestBlock({ trades: r.trades, stats: r.stats }, [
      { key: "entry_date", label: "Entry date" }, { key: "exit_date", label: "Exit date" },
      { key: "entry", label: "Entry", fmt: v => num(v), cls: "n" },
      { key: "exit", label: "Exit", fmt: v => num(v), cls: "n" },
      { key: "reason", label: "Reason" }, { key: "ret", label: "Return", fmt: v => pct(v), cls: "n" }], r.window),
    h("pre", { class: "summary" }, r.summary_text));
}

// ------------------------------------------------------------------ tools --
const SYMBOL = { name: "symbol", label: "Symbol", type: "symbol" };
const DAYS = { name: "days", label: "Days", type: "number", min: 20 };
const LIVE = { name: "live", label: "Live bar", type: "checkbox" };
const CAPITAL = { name: "capital", label: "Capital (BDT)", type: "number", adv: true };
const RISK = { name: "risk", label: "Risk % / trade", type: "number", adv: true };
const SCORE = { name: "score_gate", label: "Score gate", type: "number", adv: true };
const MIN_TURNOVER = { name: "min_turnover", label: "Min avg 20d volume", type: "number", adv: true, optional: true };
const INDEX = { name: "index_symbol", label: "Index symbol (e.g. DS30)", type: "text", adv: true, optional: true };

const TOOLS = {
  shortlist: { title: "Shortlist funnel", path: "/api/shortlist", fields: [
    { name: "source", label: "Tickers", type: "source" }, DAYS, LIVE,
    { name: "raw_volume", label: "Raw volume (no projection)", type: "checkbox" },
    { ...MIN_TURNOVER, adv: false }, { ...INDEX, adv: false },
    CAPITAL, RISK, SCORE] },
  swing_entry: { title: "Swing entry", path: "/api/swing/entry", render: renderSwingEntry, fields: [
    SYMBOL, DAYS, { name: "backtest", label: "Include backtest", type: "checkbox" },
    CAPITAL, RISK, { name: "min_rr", label: "Min RR (0 = off)", type: "number", adv: true }, SCORE] },
  swing_exit: { title: "Swing exit", path: "/api/swing/exit", render: renderSwingExit, fields: [
    SYMBOL, DAYS, { name: "entry", label: "Entry", type: "number", required: true },
    { name: "stop", label: "Stop", type: "number", required: true },
    { name: "target", label: "Target", type: "number", required: true }] },
  claude: { title: "dse_claude", path: "/api/claude", render: renderClaude, fields: [
    SYMBOL, DAYS, LIVE, { name: "min_rr", label: "Min RR", type: "number", adv: true },
    { name: "max_ext", label: "Max % above SMA20", type: "number", adv: true },
    { name: "max_day_gain", label: "Max signal-day gain %", type: "number", adv: true }] },
  uptrend: { title: "Uptrend gate", path: "/api/uptrend", render: renderUptrend, fields: [
    SYMBOL, DAYS, MIN_TURNOVER, INDEX] },
  technical: { title: "Technical read", path: "/api/technical", render: renderTechnical, fields: [
    SYMBOL, DAYS, LIVE] },
  gate: { title: "Gate strategy", path: "/api/gate", render: renderGate, fields: [
    SYMBOL, DAYS, CAPITAL,
    { name: "tp", label: "Take profit (fraction)", type: "number", adv: true },
    { name: "stop_mult", label: "ATR stop ×", type: "number", adv: true },
    { name: "cost", label: "Round-trip cost (fraction)", type: "number", adv: true },
    { name: "rsi_min", label: "RSI min", type: "number", adv: true },
    { name: "rsi_max", label: "RSI max", type: "number", adv: true },
    { name: "disabled_gates", label: "Disable gates", type: "multi", adv: true,
      options: ["trend", "sma_cross", "macd", "rsi", "volume", "atr"] }] },
};

// ------------------------------------------------------------------ forms --
function field(f, defs) {
  const d = defs[f.name];
  if (f.type === "checkbox")
    return h("label", { class: "check" }, h("input", { type: "checkbox", name: f.name, checked: !!d }), f.label);
  if (f.type === "multi")
    return h("fieldset", {}, h("legend", {}, f.label), f.options.map(o =>
      h("label", { class: "check" }, h("input", { type: "checkbox", name: f.name, value: o,
        checked: (d || []).includes(o) }), o)));
  if (f.type === "source") {
    const n = state.watchlist.length;
    return h("fieldset", { class: "source" }, h("legend", {}, f.label),
      n ? h("label", { class: "check" }, h("input", { type: "radio", name: "source", value: "watchlist", checked: true }), `Watchlist (${n})`) : null,
      h("label", { class: "check" }, h("input", { type: "radio", name: "source", value: "paste", checked: !n }), "Paste codes"),
      h("textarea", { name: "symbols", rows: 2, placeholder: "ACMEPL KBPPWBIL, LHB" }));
  }
  const isNum = f.type === "number";
  return h("label", { class: "field" }, h("span", {}, f.label),
    h("input", { type: isNum ? "number" : "text", name: f.name, step: isNum ? "any" : null, min: f.min,
      required: f.required || f.type === "symbol", list: f.type === "symbol" ? "watchlist" : null,
      value: d ?? "" }));
}
function buildForm(view) {
  const tool = TOOLS[view], defs = state.defaults[view] || {};
  const basic = tool.fields.filter(f => !f.adv).map(f => field(f, defs));
  const adv = tool.fields.filter(f => f.adv).map(f => field(f, defs));
  const form = document.getElementById("form");
  form.noValidate = true;  // let the API's 422 drive field highlighting
  form.replaceChildren(
    h("h2", {}, tool.title),
    h("div", { class: "row" }, basic, h("button", { type: "submit", class: "primary" }, "Run")),
    adv.length ? h("details", {}, h("summary", {}, "Advanced"), h("div", { class: "row" }, adv)) : null);
  form.onsubmit = ev => { ev.preventDefault(); view === "shortlist" ? startShortlist() : runTool(view); };
}
function readForm(view) {
  const form = document.getElementById("form"), body = {};
  for (const f of TOOLS[view].fields) {
    if (f.type === "checkbox") body[f.name] = form.elements[f.name].checked;
    else if (f.type === "multi")
      body[f.name] = [...form.querySelectorAll(`input[name="${f.name}"]:checked`)].map(i => i.value);
    else if (f.type === "source") {
      const src = form.querySelector('input[name="source"]:checked');
      body.use_watchlist = !!src && src.value === "watchlist";
      body.symbols = body.use_watchlist ? [] : form.elements.symbols.value.split(/[\s,;]+/).filter(Boolean);
    } else {
      const raw = form.elements[f.name].value.trim();
      if (raw === "") { if (f.optional) body[f.name] = null; continue; }
      body[f.name] = f.type === "number" ? Number(raw) : raw;
    }
  }
  return body;
}

// ------------------------------------------------------ single-stock runs --
async function runTool(view) {
  clearInvalid();
  const out = document.getElementById("out");
  out.replaceChildren(h("p", { class: "muted" }, "Fetching from DSE…"));
  const pendingNote = state.pendingNote;
  state.pendingNote = null;  // one-shot: only the run it was set for shows it
  try {
    const data = await api(TOOLS[view].path, readForm(view));
    if (state.view !== view) return;
    out.replaceChildren(
      h("div", { class: "head" }, h("strong", {}, data.symbol), h("span", { class: "muted" }, data.fetched_at),
        data.live_merged ? h("span", { class: "chip warn" }, "LIVE (provisional)") : null),
      pendingNote ? h("p", { class: "note" }, pendingNote) : null,
      data.note ? h("p", { class: "note" }, data.note) : null,
      TOOLS[view].render(data.result));
  } catch (err) { showError(err); }
}

// ------------------------------------------------------------- shortlist --
async function startShortlist() {
  clearInvalid();
  try {
    const body = readForm("shortlist");
    const { job_id } = await api("/api/shortlist", body);
    pollJob(job_id);
  } catch (err) { showError(err); }
}
function pollJob(id) {
  const token = ++state.pollToken;
  clearTimeout(state.poll);
  let errors = 0;
  const MAX_RETRIES = 3;
  const tick = async () => {
    if (token !== state.pollToken) return;
    let job;
    try {
      job = await api(`/api/jobs/${id}`);
    } catch (err) {
      errors++;
      if (errors > MAX_RETRIES) {
        if (state.view === "shortlist") showError(err);
        return;
      }
      state.poll = setTimeout(tick, 1000);  // transient failure -- retry, same interval
      return;
    }
    errors = 0;
    if (token !== state.pollToken) return;
    if (state.view === "shortlist") renderJob(job);
    if (job.status === "queued" || job.status === "running") state.poll = setTimeout(tick, 1000);
  };
  tick();
}
function openSwing(row) {
  showView("swing_entry");
  const form = document.getElementById("form");
  form.elements.symbol.value = row.symbol;
  const p = state.shortlistParams || {};
  if (p.days !== undefined) form.elements.days.value = p.days;
  if (p.capital !== undefined && form.elements.capital) form.elements.capital.value = p.capital;
  if (p.risk !== undefined && form.elements.risk) form.elements.risk.value = p.risk;
  if (p.score_gate !== undefined && form.elements.score_gate) form.elements.score_gate.value = p.score_gate;
  if (row.src === "LIVE") {
    state.pendingNote = "Stage 1 used today's provisional live bar; Swing entry is "
      + "archive-only (last close), so its verdict can differ.";
  }
  runTool("swing_entry");
}
function renderJob(job) {
  const p = job.partial || {}, res = job.result || {};
  const st1 = res.stage1 || p.stage1, st2 = res.stage2 || p.stage2, st3 = res.stage3 || p.stage3;
  const st4 = res.stage4 || p.stage4, live = res.live || p.live, pr = job.progress;
  const running = job.status === "queued" || job.status === "running";
  state.shortlistParams = job.params || {};
  const parts = [h("div", { class: "jobbar" }, chip(job.status.toUpperCase()),
    pr && running ? h("span", {}, `Stage ${pr.stage} · ${pr.i + 1}/${pr.n} · ${pr.symbol}`) : null,
    pr && running ? h("progress", { max: pr.n, value: pr.i + 1 }) : null,
    running ? h("button", { type: "button", onclick: () => api(`/api/jobs/${job.id}/cancel`, {}).catch(showError) }, "Cancel") : null),
    job.error ? h("div", { class: "error" }, job.error) : null];
  if (live) parts.push(h("p", { class: "note" }, !live.enabled ? "Live snapshot disabled — day-end archive only."
    : live.error !== null ? `Live snapshot unavailable (${live.error}); using archive only.`
    : `Live snapshot: ${live.n_tickers} tickers as of ${live.session_date}.`));
  if (st1) {
    parts.push(h("h3", {}, `Stage 1 — Swing screen · ${st1.n_screened} screened, ${st1.n_passed} CONDITIONAL BUY, ${st1.n_skipped} skipped, ${st1.n_live} live`),
      table(st1.rows, [
        { key: "symbol", label: "Symbol" }, { key: "src", label: "Src" },
        { key: "decision", label: "Decision", fmt: v => chip(v) }, { key: "setup", label: "Setup" },
        { key: "price", label: "Price", fmt: v => num(v), cls: "n" },
        { key: "rsi", label: "RSI", fmt: v => num(v, 0), cls: "n" },
        { key: "gates_passed", label: "Gates", fmt: (v, r) => missing(v) ? "–" : `${v}/${r.gates_total}`, cls: "n" },
        { key: "rr", label: "RR", fmt: v => num(v), cls: "n" },
        { key: "blocker", label: "Blocker / reason", fmt: (v, r) => v || r.reason || "" }], { onRow: openSwing }));
    if ((st1.notes || []).length) parts.push(h("pre", { class: "notes" }, st1.notes.join("\n")));
  }
  if (st2) parts.push(h("h3", {}, `Stage 2 — Dual backtest · ROBUST: ${st2.robust.join(", ") || "none"}`),
    table(st2.rows, [
      { key: "symbol", label: "Symbol" },
      { key: "swing_exp", label: "Swing exp", fmt: v => pct(v), cls: "n" },
      { key: "swing_wr", label: "Swing WR", fmt: v => pct(v, 0), cls: "n" },
      { key: "swing_n", label: "N", cls: "n" },
      { key: "gate_exp", label: "Gate exp", fmt: v => pct(v), cls: "n" },
      { key: "gate_wr", label: "Gate WR", fmt: v => pct(v, 0), cls: "n" },
      { key: "gate_n", label: "N", cls: "n" },
      { key: "verdict", label: "Verdict", fmt: v => chip(v) }],
      { onRow: openSwing, rowClass: r => st2.robust.includes(r.symbol) ? "hl" : null }));
  if (st3) parts.push(h("h3", {}, `Stage 3 — Uptrend gate · CONFIRMED: ${st3.confirmed.join(", ") || "none"}`),
    st3.warning ? h("p", { class: "note" }, st3.warning) : null,
    st3.n_robust ? table(st3.rows, [
      { key: "symbol", label: "Symbol" }, { key: "decision", label: "Decision", fmt: v => chip(v) },
      { key: "price", label: "Price", fmt: v => num(v), cls: "n" },
      { key: "rsi", label: "RSI", fmt: v => num(v, 1), cls: "n" },
      { key: "adx", label: "ADX", fmt: v => num(v, 1), cls: "n" },
      { key: "passed", label: "Mandatory", fmt: (v, r) => missing(v) ? "–" : `${v}/${r.total}`, cls: "n" },
      { key: "optional_score", label: "Optional" },
      { key: "confidence", label: "Conf %", fmt: v => num(v, 1), cls: "n" },
      { key: "fail", label: "Fail", fmt: (v, r) => v || r.note || "" }],
      { onRow: openSwing, rowClass: r => st3.confirmed.includes(r.symbol) ? "hl" : null })
      : h("p", { class: "muted" }, "No ROBUST names from stage 2."));
  if (st4) {
    const tradeable = st4.rows.filter(r => r.tradeable);
    parts.push(h("h3", {}, `Stage 4 — dse_claude pullback · ${tradeable.length} tradeable of ${st4.rows.length}`),
      tradeable.length ? table(tradeable, [
        { key: "symbol", label: "Symbol" }, { key: "uptrend", label: "Uptrend" },
        { key: "rr", label: "RR", fmt: v => num(v), cls: "n" },
        { key: "entry", label: "Entry", fmt: v => num(v), cls: "n" },
        { key: "exit", label: "Exit", fmt: v => num(v), cls: "n" },
        { key: "stop", label: "Stop", fmt: v => num(v), cls: "n" }], { onRow: openSwing })
        : h("p", { class: "muted" }, "No tradeable pullback setups."));
  }
  if (job.status === "done" && st2) parts.push(h("p", { class: "note" },
    "Stage-2 backtests use automated gates only (manual confirmations assumed) — an optimistic upper bound."));
  document.getElementById("out").replaceChildren(...parts);
}

// ------------------------------------------------------------------- nav --
function showView(view) {
  state.view = view;
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.view === view);
  buildForm(view);
  document.getElementById("out").replaceChildren();
  if (view === "shortlist") {
    api("/api/jobs/latest").then(job => {
      if (state.view !== "shortlist") return;
      renderJob(job);
      if (job.status === "queued" || job.status === "running") pollJob(job.id);
    }).catch(() => {});
  }
}

async function init() {
  try {
    const [defaults, wl] = await Promise.all([api("/api/defaults"), api("/api/watchlist")]);
    state.defaults = defaults;
    state.watchlist = wl.symbols;
  } catch (err) { showError(err); }
  document.getElementById("watchlist").replaceChildren(...state.watchlist.map(s => h("option", { value: s })));
  for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => showView(b.dataset.view));
  showView("shortlist");
}
init();
