/* fm-ft8-skimmer page. The data are three JSON documents from the skimmer: status (state, last slot,
   station), recent (latest messages, QSOs, signals) and stats (hourly buckets [t, slots listened, slots
   with any signal, messages, QSOs, slots with FT8, slots with another signal] for 32 days).
   Served by the skimmer itself they are polled from data/*.json. On a static web host, put a
   data/config.json next to the page - {"mqtt": {"url": "wss://broker.example/mqtt", "root": "fmft8"}} -
   and the page reads them live from the broker instead (see the README).
   Everything that came over the air or from the broker is written with textContent, never as HTML.
   All times on the page are UTC. */
"use strict";
const $ = id => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else if (k === "width") e.style.width = v; else e.setAttribute(k, v);
  }
  for (const k of kids) if (k != null) e.append(k);
  return e;
};
const svg = (tag, attrs = {}, text) => {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
};
const pad = n => String(n).padStart(2, "0");
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const hms = t => { const d = new Date(t * 1000); return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`; };
const md = t => { const d = new Date(t * 1000); return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`; };
const when = t => (Math.floor(t / 86400) === Math.floor(Date.now() / 86400e3) ? "" : md(t) + " ") + hms(t);
const ago = s => s < 90 ? `${Math.max(0, Math.round(s))} s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
const db = v => `${v > 0 ? "+" : ""}${Math.round(v)} dB`;
const pill = (id, cls, text) => { const p = $(id); p.querySelector(".dot").className = "dot " + cls; p.querySelector("span:last-child").textContent = text; };

let STATUS = null, RECENT = null, STATS = null, RANGE = "24h";
try { const r = localStorage.getItem("fmft8-range"); if (r === "week" || r === "month") RANGE = r; } catch (e) {}
const RANGES = { "24h": { n: 24, step: 3600, name: "last 24 hours" }, week: { n: 28, step: 21600, name: "last 7 days" }, month: { n: 30, step: 86400, name: "last 30 days" } };

/* ---------- buckets ---------- */
function buckets(range) {
  const { n, step } = RANGES[range], now = Date.now() / 1000;
  const end = Math.ceil(now / step) * step, start = end - n * step;
  const out = Array.from({ length: n }, (_, i) => ({ t: start + i * step, slots: 0, carrier: 0, msgs: 0, qsos: 0, ft8: 0, other: 0 }));
  for (const [t, slots, carrier, msgs, qsos, ft8, other] of (STATS && STATS.hours) || []) {
    const i = Math.floor((t - start) / step);
    if (i >= 0 && i < n) { const b = out[i]; b.slots += slots; b.carrier += carrier; b.msgs += msgs; b.qsos += qsos; b.ft8 += ft8 || 0; b.other += other == null ? carrier : other; }
  }
  for (const b of out) {
    const span = Math.min(now, b.t + step) - b.t;            // the running bucket is judged on the time so far
    b.cover = span > 0 ? Math.min(1, b.slots * 15 / Math.max(span, 60)) : 0;
    b.future = span <= 0;
  }
  return out;
}

/* ---------- station tiles ---------- */
function renderStation() {
  const s = STATUS, now = Date.now() / 1000, box = $("health");
  if (!s) return;
  const stale = s.state !== "offline" && now - (s.ts || 0) > 900;
  const state = stale ? "silent" : s.state;
  const cls = state === "listening" ? "good" : state === "paused" ? "warn" : "bad";
  const label = { listening: "listening", paused: "paused", "no-sdr": "receiver missing", offline: "offline", silent: "no report", starting: "starting" }[state] || state;
  pill("pill-rx", state === "listening" ? "live" : cls === "warn" ? "warn" : "bad", label);
  const day = buckets("24h"), sum = k => day.reduce((a, b) => a + b[k], 0);
  const tile = (l, v, sub, c) => el("div", { class: "tile" }, el("div", { class: "l", text: l }), el("div", { class: "v " + (c || ""), text: v }), sub ? el("div", { class: "s", text: sub }) : null);
  const last = s.last;
  box.replaceChildren(
    tile("Skimmer", label, stale ? `last report ${ago(now - s.ts)}` : s.reason || (s.since ? `since ${when(s.since)} UTC` : ""), cls),
    tile("Frequency", `${(s.freq / 1e6).toFixed(3)} MHz`, "FM, standard FT8"),
    tile("Last slot", last ? hms(last.t) : "—", last ? `${last.n} message${last.n === 1 ? "" : "s"} · ${ago(now - last.t - 15)}` : "none yet"),
    tile("Channel", last ? (last.carrier_s >= 0.5 ? db(last.peak_db) : "clear") : "—", last ? (last.carrier_s >= 0.5 ? `${last.what || "signal"}, ${Math.round(last.carrier_s)} s` : "nothing in the last slot") : ""),
    tile("Messages, 24 h", String(sum("msgs")), `${sum("qsos")} QSO${sum("qsos") === 1 ? "" : "s"}`),
  );
  $("st-age").textContent = [s.sdr, s.antenna].filter(Boolean).join(" · ");
  // who and where: from the skimmer's settings
  const mhz = (s.freq / 1e6).toFixed(3), call = s.call || "";
  $("title").textContent = (call ? call + " // " : "") + "FM-FT8 SKIMMER";
  document.title = (call ? call + " " : "") + "FM-FT8 Skimmer";
  $("subtitle").textContent = `Listening for FT8 via FM on ${mhz} MHz` + (s.loc ? ` from ${s.loc}` : "");
  document.querySelectorAll(".qrg").forEach(e => { e.textContent = mhz; });
  $("foot").textContent = `fm-ft8-skimmer ${s.v || ""}` + (call ? ` · receiving station ${call}` : "") + (s.loc ? `, ${s.loc}` : "") + " · receive only · RTL-SDR, FM demodulator and the FT8 decoder of WSJT-X";
  const psk = !!(s.psk && call);
  $("psk-note").hidden = !psk;
  if (psk) $("psk-link").href = "https://pskreporter.info/pskmap.html?preset&callsign=" + encodeURIComponent(call) + "&txrx=rx&timerange=86400";
}
const QRG = () => STATUS && STATUS.freq ? `${(STATUS.freq / 1e6).toFixed(3)} MHz` : "the frequency";

/* ---------- timeline: two panels on one time axis (messages, QSOs) + listening strip ---------- */
function niceMax(v) { for (const m of [4, 8, 10, 20, 40, 50, 100, 200, 400, 500, 1000, 2000, 5000, 10000]) if (v <= m) return m; return Math.ceil(v / 10000) * 10000; }

function renderTimeline() {
  const root = $("tl"), wrap = $("tl-wrap"), W = Math.max(300, wrap.clientWidth), data = buckets(RANGE), { step, name } = RANGES[RANGE];
  const L = 34, R = 6, plotW = W - L - R, bw = plotW / data.length;
  const panels = [{ key: "msgs", title: "FT8 messages decoded", h: 110 }, { key: "qsos", title: "QSOs", h: 70 }];
  let y = 0;
  for (const p of panels) { p.top = y + 20; y = p.top + p.h + 10; }
  const covTop = y, H = covTop + 6 + 20;
  root.setAttribute("viewBox", `0 0 ${W} ${H}`); root.setAttribute("height", H);
  root.replaceChildren();
  const defs = svg("defs"), pat = svg("pattern", { id: "hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  pat.append(svg("line", { class: "hatch-line", x1: 0, y1: 0, x2: 0, y2: 6 })); defs.append(pat); root.append(defs);
  const x = i => L + i * bw, gap = bw >= 8 ? 2 : bw >= 4 ? 1 : 0;
  for (const p of panels) {
    const max = niceMax(Math.max(...data.map(b => b[p.key]), 1)), ys = v => p.top + p.h - v / max * p.h;
    root.append(svg("text", { class: "ttl", x: L, y: p.top - 8 }, p.title));
    for (const v of [max / 2, max]) {
      root.append(svg("line", { class: "grid", x1: L, x2: W - R, y1: ys(v), y2: ys(v) }));
      root.append(svg("text", { x: L - 6, y: ys(v) + 4, "text-anchor": "end" }, String(v)));
    }
    data.forEach((b, i) => {
      if (STATS && !b.future && b.cover < 0.02) root.append(svg("rect", { class: "off", x: x(i), y: p.top, width: bw, height: p.h }));
      const v = b[p.key];
      if (v > 0) {
        const h = Math.max(2, v / max * p.h), w = Math.min(24, Math.max(1, bw - gap)), r = Math.min(3, w / 2, h);
        const x0 = x(i) + (bw - w) / 2, y0 = p.top + p.h - h;
        root.append(svg("path", { class: "bar", d: `M${x0},${y0 + h}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + w - r}Q${x0 + w},${y0} ${x0 + w},${y0 + r}V${y0 + h}Z` }));
      }
    });
    root.append(svg("line", { class: "base", x1: L, x2: W - R, y1: p.top + p.h, y2: p.top + p.h }));
  }
  // listening strip: bar width = share of the bucket the skimmer listened
  data.forEach((b, i) => { if (b.cover > 0) root.append(svg("rect", { class: "cov", x: x(i) + gap / 2, y: covTop, width: Math.max(1, (bw - gap) * b.cover), height: 4 })); });
  // time axis
  const every = RANGE === "24h" ? 3 : RANGE === "week" ? 4 : 5, minGap = 46;
  let lastX = -1e9;
  data.forEach((b, i) => {
    const d = new Date(b.t * 1000);
    const on = RANGE === "24h" ? d.getUTCHours() % every === 0 : RANGE === "week" ? d.getUTCHours() === 0 : i % every === 0;
    if (!on || x(i) - lastX < minGap) return;
    lastX = x(i);
    const label = RANGE === "24h" ? `${pad(d.getUTCHours())}:00` : RANGE === "week" ? `${DAY[d.getUTCDay()]} ${d.getUTCDate()}` : md(b.t);
    root.append(svg("line", { class: "base", x1: x(i), x2: x(i), y1: covTop + 6, y2: covTop + 10 }));
    root.append(svg("text", { x: x(i) + 3, y: covTop + 22 }, label));
  });
  const total = data.reduce((a, b) => a + b.msgs, 0), tq = data.reduce((a, b) => a + b.qsos, 0);
  if (STATS && total === 0) root.append(svg("text", { class: "empty-t", x: L + plotW / 2, y: panels[0].top + panels[0].h / 2 + 4, "text-anchor": "middle" }, `// no FT8 heard on ${QRG()} in the ${name}`));
  if (!STATS) root.append(svg("text", { class: "empty-t", x: L + plotW / 2, y: panels[0].top + panels[0].h / 2 + 4, "text-anchor": "middle" }, "// waiting for the skimmer…"));
  $("tl-sum").textContent = STATS ? `${name}: ${total} message${total === 1 ? "" : "s"}, ${tq} QSO${tq === 1 ? "" : "s"} · UTC` : "";
  // hover layer: one full-height target per bucket
  const tip = $("tip");
  const span = b => RANGE === "month" ? `${DAY[new Date(b.t * 1000).getUTCDay()]} ${md(b.t)}` : `${md(b.t)} ${pad(new Date(b.t * 1000).getUTCHours())}:00–${pad(new Date((b.t + step) * 1000).getUTCHours())}:00`;
  data.forEach((b, i) => {
    const hit = svg("rect", { class: "hit", x: x(i), y: panels[0].top, width: bw, height: covTop + 6 - panels[0].top, tabindex: 0, "aria-label": `${span(b)} UTC: ${b.msgs} messages, ${b.qsos} QSOs` });
    const show = () => {
      hit.classList.add("on");
      const row = (k, v) => el("div", {}, el("span", { class: "k", text: k + " " }), el("b", { text: v }));
      tip.replaceChildren(el("div", { text: span(b) + " UTC" }),
        ...(b.future ? [el("div", { class: "k", text: "not yet" })] : b.cover < 0.02 ? [el("div", { class: "k", text: "skimmer was not listening" })] : [
          row("messages", String(b.msgs)), row("QSOs", String(b.qsos)), row("other signals", `${b.other} slot${b.other === 1 ? "" : "s"}`), row("listening", `${Math.round(b.cover * 100)} %`)]));
      tip.hidden = false;
      const tw = tip.offsetWidth, left = Math.min(Math.max(0, x(i) + bw / 2 - tw / 2), W - tw);
      tip.style.left = left + "px"; tip.style.top = "0px";
    };
    const hide = () => { hit.classList.remove("on"); tip.hidden = true; };
    hit.addEventListener("pointerenter", show); hit.addEventListener("pointerleave", hide);
    hit.addEventListener("focus", show); hit.addEventListener("blur", hide);
    root.append(hit);
  });
}

/* ---------- frequency usage: how clear the channel is ---------- */
const pct = v => v === 0 ? "0 %" : v === 1 ? "100 %" : v < 0.0005 ? "< 0.1 %" : v > 0.9995 ? "> 99.9 %" : v < 0.1 || v > 0.9 ? `${(v * 100).toFixed(1)} %` : `${Math.round(v * 100)} %`;
function renderUsage() {
  if (!STATS) return;
  const rows = [];
  for (const [range, label] of [["24h", "24 h"], ["week", "7 days"], ["month", "30 days"]]) {
    const b = buckets(range), sum = k => b.reduce((a, x) => a + x[k], 0);
    const slots = sum("slots"), ft8 = sum("ft8"), other = sum("other");
    const hours = slots * 15 / 3600, listened = hours < 1 ? `${Math.round(hours * 60)} min` : hours < 48 ? `${hours.toFixed(1)} h` : `${(hours / 24).toFixed(1)} days`;
    if (!slots) { rows.push(el("div", { class: "use" }, el("span", { class: "p", text: label }), el("span", { class: "bar" }), el("span", { class: "v muted", text: "no data" }))); continue; }
    const f = ft8 / slots, o = other / slots, clear = Math.max(0, 1 - f - o);
    const bar = el("span", { class: "bar", role: "img", "aria-label": `${label}: ${pct(clear)} clear, FT8 via FM ${pct(f)}, anything else ${pct(o)}` });
    if (ft8) bar.append(el("i", { class: "ft8", width: `${f * 100}%` }));
    if (other) bar.append(el("i", { class: "other", width: `${o * 100}%` }));
    bar.title = `${ft8} slot${ft8 === 1 ? "" : "s"} with FT8 via FM, ${other} with another signal, of ${slots} slots of 15 s`;
    rows.push(el("div", { class: "use" }, el("span", { class: "p", text: label }), bar, el("span", { class: "v", text: `${pct(clear)} clear` }),
      el("span", {}), el("span", { class: "d", text: `FT8 via FM ${pct(f)} · anything else ${pct(o)} · listened ${listened}` })));
  }
  $("usage").replaceChildren(...rows);
  const last = ((RECENT && RECENT.carriers) || []).find(c => !c.n);
  $("use-last").textContent = last ? `Last signal that was not FT8 via FM: ${when(last.t)} UTC, ${last.label || "not identified"}.` : RECENT ? "Nothing but FT8 via FM has been heard so far." : "";
}

/* ---------- tables ---------- */
const STAGE = { call: ["call", "calling"], report: ["part", "report sent"], "r-report": ["part", "reports exchanged"], confirmed: ["good", "complete"] };

function renderTables() {
  const r = RECENT;
  if (!r) return;
  const empty = (n, text) => el("tr", {}, el("td", { colspan: n, class: "muted", text }));
  const qs = r.qsos || [];
  $("qso-count").textContent = qs.length ? `${qs.length} shown` : "";
  $("qsos").tBodies[0].replaceChildren(...(qs.length ? qs.map(q => {
    const who = c => el("span", { class: q.heard.includes(c) ? "heard" : "unheard", title: q.heard.includes(c) ? "decoded here" : "not heard here (known from the other station's messages)", text: c });
    const pairs = o => [q.a, q.b].filter(c => o && o[c] != null).map(c => `${c} ${o[c]}`).join(" · ") || "—";
    const [cls, label] = STAGE[q.stage] || ["call", q.stage];
    const snr = Math.max(...Object.values(q.snr || {}), -99);
    return el("tr", {},
      el("td", { text: when(q.t1) }), el("td", {}, who(q.a), " → ", who(q.b)), el("td", {}, el("span", { class: "badge " + cls, text: label })),
      el("td", { text: pairs(q.grid) }), el("td", { text: pairs(q.rpt) }), el("td", { class: "num", text: String(q.n) }), el("td", { class: "num", text: snr > -99 ? db(snr) : "—" }));
  }) : [empty(7, `No QSO heard yet. Call CQ on ${QRG()} FM and you will show up here.`)]));
  const ms = r.decodes || [];
  $("msg-count").textContent = ms.length ? `latest ${ms.length}` : "";
  $("msgs").tBodies[0].replaceChildren(...(ms.length ? ms.map(m => el("tr", {},
    el("td", { text: when(m.t) }), el("td", { class: "msg", text: m.msg }), el("td", { class: "num", text: db(m.snr) }), el("td", { class: "num", text: (m.dt >= 0 ? "+" : "") + m.dt.toFixed(1) + " s" }),
    el("td", { class: "num", text: m.f + " Hz" }), el("td", { class: "num", text: m.rf != null ? db(m.rf) : "—" }))) : [empty(6, "No FT8 message decoded yet.")]));
  const cs = (r.carriers || []).filter(c => !c.n);
  $("cars").tBodies[0].replaceChildren(...(cs.length ? cs.map(c => el("tr", {},
    el("td", { text: when(c.t) }), el("td", { class: "num", text: c.dur_s < 1 ? "< 1 s" : `${Math.round(c.dur_s)} s` }), el("td", { class: "num", text: db(c.peak_db) }),
    el("td", { class: "wrap", text: c.label || "not identified" }))) : [empty(4, "Nothing else heard on the frequency so far.")]));
}

function renderAll() { renderStation(); renderTimeline(); renderUsage(); renderTables(); }

/* ---------- wiring ---------- */
document.querySelectorAll(".seg button").forEach(b => {
  b.setAttribute("aria-pressed", String(b.dataset.range === RANGE));
  b.addEventListener("click", () => {
    RANGE = b.dataset.range;
    try { localStorage.setItem("fmft8-range", RANGE); } catch (e) {}
    document.querySelectorAll(".seg button").forEach(o => o.setAttribute("aria-pressed", String(o === b)));
    renderTimeline();
  });
});
let resizeT = 0;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(renderTimeline, 120); });
setInterval(renderStation, 5000);
setInterval(() => { renderTimeline(); renderUsage(); }, 60000);
renderTimeline();

function take(name, obj) {
  if (obj == null || typeof obj !== "object") return;
  if (name === "status") { STATUS = obj; renderStation(); renderTables(); }
  else if (name === "recent") { RECENT = obj; renderTables(); renderUsage(); }
  else if (name === "stats") { STATS = obj; renderStation(); renderTimeline(); renderUsage(); }
}
const safely = (name, obj) => { try { take(name, obj); } catch (e) { console.error(name, e); } };

/* footer links for a public installation (legal notice, privacy ...): only plain web links are taken */
function showLinks(links) {
  const ok = (Array.isArray(links) ? links : []).filter(l => l && typeof l.text === "string" && /^(https?:\/\/|\/)/.test(l.href || "")).slice(0, 6);
  $("links").replaceChildren(...ok.flatMap((l, i) => [i ? " · " : "", el("a", { href: l.href, rel: "noopener", text: l.text.slice(0, 40) })]));
}

function viaBroker(url, root) {
  root = root.replace(/\/+$/, "") + "/";
  const client = mqtt.connect(url, { clientId: "fmft8-web-" + Math.random().toString(16).slice(2, 10), clean: true, reconnectPeriod: 5000 });
  client.on("connect", () => { pill("pill-conn", "ok", "live"); client.subscribe(["status", "recent", "stats"].map(n => root + n)); });
  client.on("reconnect", () => pill("pill-conn", "warn", "reconnecting"));
  client.on("offline", () => pill("pill-conn", "bad", "offline"));
  client.on("message", (topic, buf) => { try { safely(topic.slice(root.length), JSON.parse(buf.toString())); } catch (e) { console.error(topic, e); } });
}

function viaPolling() {
  const get = name => fetch(`data/${name}.json`, { cache: "no-cache" }).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
  const round = () => Promise.all(["status", "recent", "stats"].map(n => get(n).then(o => safely(n, o))))
    .then(() => pill("pill-conn", "ok", "live"), () => pill("pill-conn", "bad", "offline"));
  round();
  setInterval(round, 5000);
}

fetch("data/config.json", { cache: "no-cache" }).then(r => r.ok ? r.json() : null).catch(() => null).then(cfg => {
  cfg = cfg || {};
  showLinks(cfg.links);
  const m = cfg.mqtt;
  if (m && typeof m.url === "string" && /^wss?:\/\//.test(m.url) && typeof m.root === "string" && m.root) viaBroker(m.url, m.root);
  else viaPolling();
});
