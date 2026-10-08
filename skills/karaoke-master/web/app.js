/* Karaoke Master — local workflow page.
 * Talks to kmlib/server.py: GET /api/job, SSE /api/events, POST /api/action, /files/<job file>.
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "html") n.innerHTML = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return n;
};
const fileUrl = (rel) => (rel ? "/files/" + rel.split("?")[0].split("/").map(encodeURIComponent).join("/") + (rel.includes("?") ? "?" + rel.split("?")[1] : "") : "");
const fmtT = (t, cs = true) => {
  if (t === null || t === undefined || isNaN(t)) return "—";
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${m}:${(cs ? s.toFixed(2) : Math.floor(s).toString()).padStart(cs ? 5 : 2, "0")}`;
};
const parseT = (txt) => {
  txt = String(txt).trim();
  if (!txt) return null;
  if (txt.includes(":")) { const [m, s] = txt.split(":"); return parseInt(m, 10) * 60 + parseFloat(s); }
  return parseFloat(txt);
};
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

const S = {
  job: null, chat: [], drafts: {}, viewing: 0, pending: 0,
  peaks: null, peaksKey: "", view: null, viewRev: null, ws: null, wsRev: null,
  d1: null, d1Key: "", lastStage: 0, jobDir: "",
};

/* ------------------------------------------------------------------ network */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}
async function act(type, payload = {}, extra = {}) {
  try {
    const res = await api("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type, payload, ...extra }) });
    return res;
  } catch (e) { toast("操作失败：" + e.message); throw e; }
}
function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function boot() {
  if (/[?&]still/.test(location.search)) document.documentElement.classList.add("still"); // static screenshots
  try {
    const data = await api("/api/job");
    applyPayload(data, true);
  } catch (e) {
    $("#stage-0 p").textContent = "无法读取任务：" + e.message;
  }
  connectSSE();
  bindStatic();
  requestAnimationFrame(tick);
}

function connectSSE() {
  const es = new EventSource("/api/events");
  es.addEventListener("job", (ev) => applyPayload(JSON.parse(ev.data)));
  es.addEventListener("log", (ev) => appendLog(JSON.parse(ev.data).line));
  es.addEventListener("closed", (ev) => { es.close(); showClosed(JSON.parse(ev.data).by); });
  es.onerror = () => { if (!S.closed) setStatus("error", "与本地服务的连接中断，正在重连…"); };
  S.es = es;
}

function showClosed(by) {
  S.closed = true;
  if (S.es) S.es.close();
  $("#closed-text").textContent = by === "user" ? "已结束本工程，网页服务已停止。可以关闭这个标签页。"
    : "Agent 已完成本工程并关闭了网页服务。可以关闭这个标签页。";
  $("#closed-hint").textContent = S.jobDir ? `工程文件夹：${S.jobDir}　·　需要再次打开时让 Agent 运行 km.py serve` : "";
  $("#closed-overlay").hidden = false;
  setStatus("idle", "网页服务已关闭");
}

function applyPayload(data, first = false) {
  const prevStage = S.job ? S.job.stage : 0;
  S.job = data.job; S.chat = data.chat || []; S.drafts = data.drafts || {}; S.pending = data.pending_actions || 0; S.jobDir = data.job_dir;
  if (first || S.viewing === 0 || (S.job.stage !== prevStage && S.viewing === prevStage)) S.viewing = S.job.stage;
  const want = first && /^#stage=([123])$/.exec(location.hash || ""); // e.g. #stage=1: open a finished stage
  if (want && +want[1] <= S.job.stage) S.viewing = +want[1];
  renderAll();
}

/* ------------------------------------------------------------------ render root */
function renderAll() {
  const j = S.job; if (!j) return;
  const song = j.song || {};
  $("#job-title").textContent = [song.title, song.artist].filter(Boolean).join(" · ") || j.id;
  document.title = (song.title ? song.title + " · " : "") + "Karaoke Master";
  setStatus(j.status, j.status_text);
  renderStepper();
  for (const n of [0, 1, 2, 3]) $("#stage-" + n).hidden = S.viewing !== n;
  if (S.viewing === 1) renderStage1();
  if (S.viewing === 2) renderStage2();
  if (S.viewing === 3) renderStage3();
  renderChat();
  loadTimingView();
  loadWebStyle();
  loadLayout();
}

function setStatus(status, text) {
  const s = $("#status"); s.className = "status " + (status || "");
  const listening = S.job && S.job.agent && S.job.agent.listening;
  let t = text || "";
  if (status === "awaiting_user") t = (t ? t + " · " : "") + "等待你确认";
  $(".txt", s).textContent = t;
  s.title = (listening ? "Agent 正在等待你的操作" : "") + (t ? "\n" + t : "");
}

function renderStepper() {
  const stage = S.job.stage;
  $$("#stepper button").forEach((b) => {
    const n = +b.dataset.stage;
    b.className = "";
    b.disabled = n > stage;
    if (n <= stage) b.classList.add("reached");
    if (n < stage) b.classList.add("done");
    if (n === stage) b.classList.add("current");
    if (n === S.viewing) b.classList.add("viewing");
  });
}

/* ------------------------------------------------------------------ data loads */
async function loadPeaks() {
  const m = S.job.media || {};
  const rel = m.vocals_peaks || m.peaks;
  if (!rel || rel === S.peaksKey) return;
  S.peaksKey = rel;
  try { S.peaks = await api(fileUrl(rel)); drawWaves(); } catch (e) { S.peaksKey = ""; }
}
async function loadTimingView() {
  const t = S.job.timing || {};
  const rev = (t.view || "") + ":" + (t.revision || "") + ":" + (S.job.updated_at || "");
  if (!t.view && !(S.job.stage >= 2)) return;
  if (rev === S.viewRev) return;
  S.viewRev = rev;
  try {
    const v = await api(fileUrl("timing/timed.json") + "?r=" + Date.now());
    const changed = JSON.stringify(v.lines.map((l) => [l.start, l.end, l.qa && l.qa.flag])) !== JSON.stringify((S.view ? S.view.lines : []).map((l) => [l.start, l.end, l.qa && l.qa.flag]));
    S.view = v;
    if (changed) { if (S.viewing === 2) renderStage2(); if (S.viewing === 3) renderLineTable(); }
  } catch (e) { /* not yet written */ }
}
// Engine-exact overlay data (kmlib/weblayout.py): per-line sprites + sampled wipe keyframes.
async function loadLayout() {
  const r = S.job.render || {};
  if (!r.web_layout || r.layout_rev === S.layoutRev) return;
  S.layoutRev = r.layout_rev;
  try {
    const L = await api(fileUrl(r.web_layout) + "?r=" + r.layout_rev);
    const img = (src) => { const im = new Image(); im.src = fileUrl(src); return im; };
    for (const ln of L.lines) { ln.imgB = img(ln.before); ln.imgA = img(ln.after); }
    if (L.title) L.title.img = img(L.title.src);
    S.layout = L;
  } catch (e) { S.layoutRev = null; }
}
function keyX(keys, t) {
  if (!keys || !keys.length || t < keys[0][0]) return null;
  for (let k = keys.length - 1; k >= 0; k--) {
    if (t >= keys[k][0]) {
      const a = keys[k], b = keys[k + 1];
      if (a[1] === null) return b && t >= b[0] ? b[1] : null;
      if (!b || b[1] === null) return a[1];
      return a[1] + (b[1] - a[1]) * (t - a[0]) / Math.max(1e-6, b[0] - a[0]);
    }
  }
  return null;
}
function drawEngineOverlay(ctx, L, t, bx, by, bw, bh) {
  const s = bw / L.w;
  const fade = (start, end, fin, fout) => clamp(Math.min((t - start) / Math.max(1e-3, fin), (end - t) / Math.max(1e-3, fout), 1), 0, 1);
  if (L.title && L.title.img.complete && t >= L.title.start && t <= L.title.end) {
    ctx.globalAlpha = fade(L.title.start, L.title.end, L.title.fade_in || 0.3, L.title.fade_out || 0.3);
    const b = L.title.box; ctx.drawImage(L.title.img, bx + b[0] * s, by + b[1] * s, b[2] * s, b[3] * s);
  }
  let active = null;
  for (const ln of L.lines) {
    if (t < ln.start || t > ln.end || !ln.imgB.complete) continue;
    const b = ln.box; const X = bx + b[0] * s, Y = by + b[1] * s, Wd = b[2] * s, Ht = b[3] * s;
    ctx.globalAlpha = fade(ln.start, ln.end, L.entry_ms / 1000, L.exit_ms / 1000);
    ctx.drawImage(ln.imgB, X, Y, Wd, Ht);
    if (!ln.imgA.complete) continue;
    const clipDraw = (x0, x1, y0, y1) => {
      if (x1 <= x0) return;
      ctx.save(); ctx.beginPath(); ctx.rect(X + x0 * s, Y + y0 * s, (x1 - x0) * s, (y1 - y0) * s); ctx.clip();
      ctx.drawImage(ln.imgA, X, Y, Wd, Ht); ctx.restore();
    };
    const split = ln.split === null || ln.split === undefined ? 0 : Math.max(0, ln.split);
    const wx = keyX(ln.wipe, t);
    if (wx !== null) clipDraw(0, wx, split, b[3]);
    if (split > 0) for (const r of ln.rubies || []) { const rx = keyX(r.keys, t); if (rx !== null) clipDraw(r.left - 3, rx, 0, split); }
    const k0 = ln.wipe && ln.wipe.length ? ln.wipe[0][0] : ln.start, k1 = ln.wipe && ln.wipe.length ? ln.wipe[ln.wipe.length - 1][0] : ln.end;
    if (t >= k0 && t <= k1) active = ln.i;
  }
  ctx.globalAlpha = 1;
  return active;
}

async function loadWebStyle() {
  const r = S.job.render || {};
  if (!r.web_style || r.rev === S.wsRev) return;
  S.wsRev = r.rev;
  try { S.ws = await api(fileUrl(r.web_style) + "?r=" + r.rev); } catch (e) { S.wsRev = null; }
}

/* ================================================================== STAGE 1 */
function draftKey() {
  const j = S.job; return (j.lyrics.selected || "") + ":" + (j.lyrics.lines || []).length + ":" + (j.lyrics.lines || []).map((l) => l.text.length).join(",");
}
function ensureDraft1() {
  const j = S.job;
  const key = draftKey();
  // untouched fields always follow the agent's latest state; touched ones keep the user's edits
  // once stage 1 is confirmed the job itself is the truth (the agent may have changed options since)
  const saved = j.stage > 1 ? null : (S.d1 && S.d1Key === key) ? S.d1 : (S.drafts.stage1 && S.drafts.stage1.key === key) ? S.drafts.stage1 : null;
  const opts = j.options || {};
  const hasVideo = !!(j.media.source && j.media.source.has_video);
  const base = {
    key,
    song: { ...(j.song || {}) },
    singers: JSON.parse(JSON.stringify((j.song && j.song.singers) || [])),
    segment: j.segment ? { start: j.segment.start, end: j.segment.end } : null,
    lines: (j.lyrics.lines || []).map((l) => ({ include: l.include !== false, singer: l.singer || null })),
    options: {
      template: opts.template || "classic",
      effect: opts.effect || "classic",
      background: opts.background || { type: hasVideo ? "source" : "mv" },
      resolution: opts.resolution || "1920x1080",
      fps: [30, 60].includes(+opts.fps) ? +opts.fps : 60,
      outputs: opts.outputs || ["mp4", "sug", "yurika", "onoff"],
      ruby: opts.ruby !== false,
      title: opts.title !== false,
    },
    touched: {},
  };
  S.d1 = saved ? mergeDraft(base, saved) : base;
  S.d1Key = key;
  return S.d1;
}
function mergeDraft(base, saved) {
  const out = { ...base };
  for (const k of ["song", "singers", "segment", "lines", "options"]) if (saved.touched && saved.touched[k] && saved[k]) out[k] = saved[k];
  out.touched = saved.touched || {};
  if (S.d1 && saved === S.d1) {
    // keep object identity for touched fields so open inputs/menus stay bound
    for (const k of Object.keys(out.touched)) out[k] = S.d1[k];
  }
  return out;
}
let saveTimer = null;
function touch(field) {
  S.d1.touched[field] = true;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => act("save_draft", S.d1, { name: "stage1" }).catch(() => {}), 700);
  renderConfirmSummary();
}

function renderS1Banner() {
  const j = S.job; const banner = $("#s1-banner");
  const working = j.stage === 1 && j.status === "working";
  banner.hidden = !working;
  if (!working) return;
  const sep = ((j.progress && j.progress.steps) || []).find((s) => s.id === "separate");
  const an = j.analysis || {};
  let frac = null, label = j.status_text || "Agent 正在准备…";
  if (sep && sep.state === "running") { frac = sep.progress || 0; label = "人声分离：" + (sep.detail || "处理中"); }
  else if (an.asr_progress !== undefined && an.asr_progress < 1 && !an.asr) { frac = an.asr_progress; label = "识别演唱内容：" + (an.asr_detail || ""); }
  else if (j.media && j.media.design_progress !== undefined && j.media.design_progress < 1) { frac = j.media.design_progress; }
  $("#s1-banner-text").textContent = label;
  $("#s1-banner-bar").hidden = frac === null;
  if (frac !== null) $("#s1-banner-bar i").style.width = Math.round(frac * 100) + "%";
  $("#s1-banner-pct").textContent = frac !== null ? Math.round(frac * 100) + "%" : "";
}

// re-render a section only when its inputs changed (keeps scroll / focus / open pickers)
function memo(key, sig, fn) {
  S.memo = S.memo || {};
  if (S.memo[key] === sig) return;
  S.memo[key] = sig; fn();
}

function renderStage1() {
  const j = S.job; const d = ensureDraft1();
  renderS1Banner();
  renderMedia1();
  memo("song", JSON.stringify([d.song, j.song && j.song.sources, j.song && j.song.notes, j.media.cover]), renderSong);
  memo("lyrics", JSON.stringify([j.lyrics, d.lines, d.singers.map((s) => [s.id, s.name, s.color])]), renderLyrics);
  memo("galleries", JSON.stringify([j.previews, d.options.template, d.options.effect]), renderGalleries);
  memo("singers", JSON.stringify([d.singers, (j.previews || {}).singers, d.lines]), renderSingers);
  memo("output", JSON.stringify([d.options, j.media.design_still, j.media.hires]), renderOutput);
  memo("mv", JSON.stringify([d.options.background, (j.previews || {}).designs, (j.previews || {}).design_gallery,
    (j.previews || {}).mv_catalog, (j.previews || {}).montage, (j.previews || {}).mv_assets, (j.options || {}).montage_source,
    (j.segment || {}), d.segment || null]), renderMV);
  renderConfirmSummary();
  const ready = j.status === "awaiting_user" && j.stage === 1;
  const btn = $("#confirm-1");
  btn.disabled = !ready || !(j.lyrics.lines || []).length;
  btn.textContent = j.stage > 1 ? "已确认（可在右侧对话框中要求 Agent 回到本阶段）" : (ready ? "确认，开始自动打轴 →" : "Agent 仍在准备中…");
  loadPeaks();
}

function renderMedia1() {
  const j = S.job, m = j.media || {}, src = m.source || {};
  $("#media-kind").textContent = j.inputs.type === "video" ? `视频 · ${src.width}×${src.height} · ${fmtT(src.duration, false)}` : j.inputs.type === "audio" ? `音频 · ${fmtT(src.duration, false)}` : "歌名检索";
  const box = $("#s1-player");
  const want = m.player || "";
  if (box.dataset.src !== want) {
    box.dataset.src = want; box.innerHTML = "";
    if (!want) box.append(el("div", { class: "audio-only" }, "等待素材…"));
    else if (src.has_video) box.append(el("video", { id: "s1-media", src: fileUrl(want), controls: true, preload: "metadata", poster: m.thumb ? fileUrl(m.thumb) : null }));
    else {
      const ds = m.design_still || {};
      const still = ds[designKind() || "mv"] || ds.mv || ds.montage || m.cover;
      box.append(still ? el("img", { src: fileUrl(still) }) : el("div", { class: "audio-only" }, "♪ 纯音频素材 · 将自动生成频谱动画背景"));
      box.append(el("audio", { id: "s1-media", src: fileUrl(want), controls: true, preload: "metadata" }));
    }
  }
  const d = S.d1;
  const seg = d.segment || j.segment;
  if (document.activeElement !== $("#seg-start")) $("#seg-start").value = seg ? fmtT(seg.start) : "";
  if (document.activeElement !== $("#seg-end")) $("#seg-end").value = seg ? fmtT(seg.end) : "";
  const conf = j.segment && j.segment.confidence;
  $("#seg-note").textContent = j.segment ? (j.segment.by === "match" || j.segment.by === "asr" ? `由语音识别 + 参考时间轴推断（置信 ${Math.round((conf || 0) * 100)}%）` : j.segment.by === "agent" ? "Agent 设定" : "") : "分析中…";
  drawWaves();
}

function drawWaves() {
  if (S.viewing === 1) drawWave1();
  if (S.viewing === 2) drawTimeline($("#s2-timeline"), { live: true });
  if (S.viewing === 3) drawTimeline($("#s3-timeline"), { playhead: true });
}

function prepCanvas(cv) {
  const r = cv.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
  const w = Math.max(10, Math.round(r.width * dpr)), h = Math.max(10, Math.round(cv.height === 150 ? 120 * dpr : (+cv.getAttribute("height")) * dpr));
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
  const ctx = cv.getContext("2d"); ctx.setTransform(1, 0, 0, 1, 0, 0);
  return { ctx, w, h, dpr };
}
function drawPeaks(ctx, w, h, x0, x1, color, top = 0, height = h) {
  const p = S.peaks; if (!p) return;
  const dur = p.duration; const arr = p.peaks; const ps = p.per_second;
  ctx.fillStyle = color;
  const mid = top + height / 2;
  for (let x = 0; x < w; x++) {
    const t0 = x0 + (x / w) * (x1 - x0), t1 = x0 + ((x + 1) / w) * (x1 - x0);
    let a = Math.floor(t0 * ps), b = Math.max(a + 1, Math.floor(t1 * ps)); let v = 0;
    for (let i = a; i < b && i < arr.length; i++) v = Math.max(v, arr[i]);
    const hh = Math.max(1, v * height * 0.46);
    ctx.fillRect(x, mid - hh, 1, hh * 2);
  }
  return dur;
}
function mediaDuration() { return (S.job.media.source && S.job.media.source.duration) || (S.peaks && S.peaks.duration) || 1; }

function drawWave1() {
  const cv = $("#s1-wave"); if (!cv || !S.job) return;
  const { ctx, w, h, dpr } = prepCanvas(cv);
  const dur = mediaDuration(); const X = (t) => (t / dur) * w;
  ctx.fillStyle = "#12162a"; ctx.fillRect(0, 0, w, h);
  for (const [a, b] of (S.job.media.activity || [])) { ctx.fillStyle = "rgba(53,208,255,.10)"; ctx.fillRect(X(a), 0, X(b) - X(a), h); }
  drawPeaks(ctx, w, h, 0, dur, "rgba(160,170,210,.55)", 6 * dpr, h - 22 * dpr);
  const seg = S.d1 && (S.d1.segment || S.job.segment);
  if (seg) {
    ctx.fillStyle = "rgba(0,0,0,.45)"; ctx.fillRect(0, 0, X(seg.start), h); ctx.fillRect(X(seg.end), 0, w - X(seg.end), h);
    ctx.strokeStyle = "#ff5fa2"; ctx.lineWidth = 2 * dpr; ctx.strokeRect(X(seg.start), 1, X(seg.end) - X(seg.start), h - 2);
    for (const t of [seg.start, seg.end]) { ctx.fillStyle = "#ff5fa2"; ctx.fillRect(X(t) - 3 * dpr, h / 2 - 14 * dpr, 6 * dpr, 28 * dpr); }
  }
  (S.job.lyrics.lines || []).forEach((l, i) => {
    const m = l.match; if (!m || m.est === null || m.est === undefined) return;
    const inc = S.d1 ? S.d1.lines[i] && S.d1.lines[i].include : l.include;
    ctx.fillStyle = m.source === "asr" ? (inc ? "rgba(255,194,75,.95)" : "rgba(255,194,75,.35)") : (inc ? "rgba(255,194,75,.55)" : "rgba(255,194,75,.2)");
    ctx.fillRect(X(m.est), h - 14 * dpr, 2 * dpr, 12 * dpr);
  });
  const media = $("#s1-media");
  if (media) { ctx.fillStyle = "#fff"; ctx.fillRect(X(media.currentTime || 0), 0, 1.5 * dpr, h); }
  ctx.fillStyle = "rgba(255,255,255,.4)"; ctx.font = `${10 * dpr}px Segoe UI`;
  for (let t = 0; t < dur; t += 30) ctx.fillText(fmtT(t, false), X(t) + 3 * dpr, 12 * dpr);
}

function bindWave1() {
  const cv = $("#s1-wave"); let drag = null;
  const tAt = (ev) => { const r = cv.getBoundingClientRect(); return clamp(((ev.clientX - r.left) / r.width) * mediaDuration(), 0, mediaDuration()); };
  cv.addEventListener("mousedown", (ev) => {
    if (!S.d1) return; const t = tAt(ev); const seg = S.d1.segment || S.job.segment || { start: 0, end: mediaDuration() };
    const tol = (8 / cv.getBoundingClientRect().width) * mediaDuration();
    if (Math.abs(t - seg.start) < tol) drag = "start"; else if (Math.abs(t - seg.end) < tol) drag = "end";
    else { const m = $("#s1-media"); if (m) m.currentTime = t; drag = null; }
    if (drag) S.d1.segment = { ...seg };
  });
  window.addEventListener("mousemove", (ev) => {
    if (!drag) return; const t = tAt(ev);
    if (drag === "start") S.d1.segment.start = Math.min(t, S.d1.segment.end - 1); else S.d1.segment.end = Math.max(t, S.d1.segment.start + 1);
    drawWave1(); $("#seg-start").value = fmtT(S.d1.segment.start); $("#seg-end").value = fmtT(S.d1.segment.end);
  });
  window.addEventListener("mouseup", () => { if (drag) { drag = null; touch("segment"); } });
  for (const id of ["seg-start", "seg-end"]) $("#" + id).addEventListener("change", (ev) => {
    const v = parseT(ev.target.value); if (v === null || isNaN(v)) return;
    const seg = { ...(S.d1.segment || S.job.segment || { start: 0, end: mediaDuration() }) };
    seg[id === "seg-start" ? "start" : "end"] = v; S.d1.segment = seg; touch("segment"); drawWave1();
  });
  $("#seg-play").addEventListener("click", () => {
    const m = $("#s1-media"); const seg = S.d1 && (S.d1.segment || S.job.segment); if (!m || !seg) return;
    m.currentTime = seg.start; m.play(); const stopAt = seg.end;
    const h = () => { if (m.currentTime >= stopAt) { m.pause(); m.removeEventListener("timeupdate", h); } }; m.addEventListener("timeupdate", h);
  });
}

function renderSong() {
  const s = S.d1.song; const j = S.job;
  for (const k of ["title", "artist", "work", "lyricist", "composer", "arranger"]) {
    const inp = $("#song-" + k); if (document.activeElement !== inp) inp.value = s[k] || "";
  }
  const conf = j.song && j.song.confidence;
  $("#song-confidence").textContent = conf ? `识别置信度 ${Math.round(conf * 100)}%` : "";
  $("#song-confidence").hidden = !conf;
  $("#song-notes").textContent = (j.song && j.song.notes) || "";
  const cover = (j.song && j.song.cover) || j.media.cover || j.media.thumb;
  const img = $("#song-cover"); img.hidden = !cover; if (cover) img.src = fileUrl(cover);
  const src = $("#song-sources"); src.innerHTML = "";
  for (const s2 of (j.song && j.song.sources) || []) src.append(el("a", { href: s2.url, target: "_blank", rel: "noreferrer" }, s2.label || s2.url));
}

function singerById(id) { return (S.d1 ? S.d1.singers : S.job.song.singers || []).find((s) => s.id === id); }

function renderLyrics() {
  const j = S.job; const d = S.d1;
  const tabs = $("#lyric-cands"); tabs.innerHTML = "";
  for (const c of j.lyrics.candidates || []) {
    const b = el("button", { class: "tab" + (c.id === j.lyrics.selected ? " on" : ""), title: `${c.title} / ${c.artist}${c.duration ? " · " + fmtT(c.duration, false) : ""}` },
      ({ utaten: "UtaTen", qm: "QQ 音乐", kg: "酷狗", ne: "网易云", lrclib: "LRCLIB" })[c.provider] || c.provider,
      c.has_ruby ? el("span", { class: "badge ruby" }, "注音") : null,
      c.has_timing ? el("span", { class: "badge" }, "时间轴") : null,
      c.has_translation ? el("span", { class: "badge" }, "翻译") : null,
      el("span", { class: "badge" }, c.line_count + " 行"));
    b.addEventListener("click", () => {
      if (c.id === j.lyrics.selected) return;
      act("use_lyrics", { candidate: c.id }); toast("已请 Agent 切换到该歌词版本");
    });
    tabs.append(b);
  }
  const list = $("#lyric-list"); const keepScroll = list.scrollTop; list.innerHTML = "";
  list.classList.toggle("hide-ruby", !$("#show-ruby").checked);
  list.classList.toggle("only-inc", $("#only-included").checked);
  const lines = j.lyrics.lines || [];
  if (!lines.length) { list.append(el("div", { class: "lline blank" }, el("span"), el("span"), el("span", { class: "txt" }, "Agent 正在检索歌词…"))); return; }
  lines.forEach((l, i) => {
    const dl = d.lines[i] || { include: true };
    const sg = singerById(dl.singer);
    const m = l.match || {};
    const matchTxt = m.est !== undefined && m.est !== null ? `${fmtT(m.est)} ${m.source === "asr" ? "听到" : "推算"}` : (m.note ? "未出现" : "");
    const row = el("div", { class: "lline" + (dl.include ? "" : " off"), title: m.note || (m.heard ? "识别到：" + m.heard : "") },
      el("span", { class: "no" }, i + 1),
      el("input", { type: "checkbox", checked: dl.include, onclick: (ev) => { ev.stopPropagation(); dl.include = ev.target.checked; row.classList.toggle("off", !dl.include); touch("lines"); drawWave1(); } }),
      el("span", { class: "txt", html: rubyHtml(l.text, l.ruby) }),
      el("span", { class: "match" + (m.source === "asr" ? " hi" : m.source === "lrc" ? " mid" : "") }, matchTxt),
      el("button", { class: "singer-chip", style: { background: sg ? sg.color : "#3a4166" }, onclick: (ev) => { ev.stopPropagation(); singerMenu(ev.target, i); } }, sg ? sg.name : "未指定"));
    row.addEventListener("click", () => { const md = $("#s1-media"); if (md && m.est !== null && m.est !== undefined) { md.currentTime = Math.max(0, m.est - 0.5); md.play(); } });
    list.append(row);
  });
  list.scrollTop = keepScroll;
}

function rubyHtml(text, ruby) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const spans = (ruby || []).slice().sort((a, b) => a[0] - b[0]);
  let out = "", pos = 0;
  for (const [s, e, r, auto] of spans) {
    if (s < pos) continue;
    out += esc(text.slice(pos, s));
    out += `<ruby${auto ? ' class="auto"' : ""}>${esc(text.slice(s, e + 1))}<rt${auto ? ' style="opacity:.6"' : ""}>${esc(r)}</rt></ruby>`;
    pos = e + 1;
  }
  return out + esc(text.slice(pos));
}

function singerMenu(anchor, lineIdx) {
  document.querySelectorAll(".menu").forEach((m) => m.remove());
  const r = anchor.getBoundingClientRect();
  const menu = el("div", { class: "menu", style: { position: "fixed", left: r.left + "px", top: r.bottom + 4 + "px", zIndex: 30, background: "var(--panel-2)", border: "1px solid var(--line-2)", borderRadius: "10px", padding: "6px", boxShadow: "var(--shadow)", display: "flex", flexDirection: "column", gap: "4px" } });
  const apply = (sid, range) => {
    const idx = range ? rangeFrom(lineIdx) : [lineIdx];
    for (const i of idx) S.d1.lines[i].singer = sid;
    touch("lines"); renderLyrics(); renderSingers(); menu.remove();
  };
  for (const s of S.d1.singers) {
    menu.append(el("div", { class: "row gap" },
      el("button", { class: "singer-chip", style: { background: s.color }, onclick: () => apply(s.id, false) }, s.name),
      el("button", { class: "btn ghost sm", onclick: () => apply(s.id, true), title: "把本行起直到下一处空白/歌手切换的连续行都设为该歌手" }, "到段落末")));
  }
  document.body.append(menu);
  setTimeout(() => window.addEventListener("click", function off(ev) { if (!menu.contains(ev.target)) { menu.remove(); window.removeEventListener("click", off); } }), 0);
}
function rangeFrom(i) {
  const lines = S.job.lyrics.lines; const cur = S.d1.lines[i].singer; const out = [i];
  for (let k = i + 1; k < lines.length; k++) {
    const gap = lines[k].match && lines[k - 1].match && lines[k].match.est && lines[k - 1].match.est ? lines[k].match.est - lines[k - 1].match.est : 0;
    if (S.d1.lines[k].singer !== cur || gap > 8) break; out.push(k);
  }
  return out;
}

function renderGalleries() {
  const j = S.job; const cat = (j.previews && j.previews.catalog) || { templates: [], effects: [] };
  const tg = $("#tpl-gallery"); tg.innerHTML = "";
  if (!cat.templates.length) tg.append(el("div", { class: "muted" }, "Agent 正在用渲染引擎生成模板预览…"));
  for (const t of cat.templates) {
    const img = (j.previews.templates || {})[t.id];
    tg.append(optCard(t, img ? { backgroundImage: `url("${fileUrl(img)}")` } : null, S.d1.options.template === t.id, () => {
      S.d1.options.template = t.id; touch("options"); renderGalleries(); act("preview_styles", { only: ["effects", "singers"], options: { template: t.id } });
      toast("已选择模板，正在用新模板刷新特效与歌手预览");
    }));
  }
  const fg = $("#fx-gallery"); fg.innerHTML = "";
  for (const f of cat.effects) {
    const p = (j.previews.effects || {})[f.id];
    const card = optCard(f, null, S.d1.options.effect === f.id, () => { S.d1.options.effect = f.id; touch("options"); renderGalleries(); });
    if (p) {
      const sprite = el("div", { class: "sprite", style: { backgroundImage: `url("${fileUrl(p.sprite)}")`, backgroundSize: `${p.frames * 100}% 100%`, backgroundPosition: `${(3 / (p.frames - 1)) * 100}% 0` } });
      $(".shot", card).append(sprite);
      let timer = null, k = 0;
      card.addEventListener("mouseenter", () => { timer = setInterval(() => { k = (k + 1) % p.frames; sprite.style.backgroundPosition = `${(k / (p.frames - 1)) * 100}% 0`; }, 420); });
      card.addEventListener("mouseleave", () => { clearInterval(timer); sprite.style.backgroundPosition = `${(3 / (p.frames - 1)) * 100}% 0`; });
    }
    fg.append(card);
  }
}
function optCard(item, shotStyle, on, onclick) {
  return el("div", { class: "opt" + (on ? " on" : ""), onclick },
    el("div", { class: "shot", style: shotStyle || {} }, shotStyle ? null : el("div", { class: "ph" }, "预览生成中…")),
    item.recommended ? el("span", { class: "rec" }, "推荐") : null,
    el("div", { class: "meta" }, el("b", {}, item.name), el("span", {}, item.desc)));
}

function renderSingers() {
  const j = S.job; const d = S.d1; const grid = $("#singer-grid"); grid.innerHTML = "";
  const counts = {}; d.lines.forEach((l, i) => { if (l.include) counts[l.singer] = (counts[l.singer] || 0) + 1; });
  $("#singer-hint").textContent = d.singers.length > 1 ? "每位演唱者唱过的字使用自己的颜色；在歌词列表中点击歌手标签分配" : "单人演唱时使用模板配色；添加多位演唱者即可分别配色";
  d.singers.forEach((s) => {
    const img = ((j.previews || {}).singers || {})[s.id];
    grid.append(el("div", { class: "singer-card" },
      el("div", { class: "shot", style: img ? { backgroundImage: `url("${fileUrl(img)}")` } : {} }),
      el("div", { class: "body" },
        el("input", { type: "color", value: s.color, oninput: (ev) => { s.color = ev.target.value; touch("singers"); renderLyrics(); } }),
        el("input", { type: "text", value: s.name, onchange: (ev) => { s.name = ev.target.value; touch("singers"); renderLyrics(); } }),
        el("span", { class: "count" }, `${counts[s.id] || 0} 行`),
        d.singers.length > 1 ? el("button", { class: "btn ghost sm", title: "删除", onclick: () => { d.singers = d.singers.filter((x) => x !== s); d.lines.forEach((l) => { if (l.singer === s.id) l.singer = d.singers[0] && d.singers[0].id; }); touch("singers"); touch("lines"); renderSingers(); renderLyrics(); } }, "×") : null)));
  });
}

const BG_TYPES = [["video", "视频"], ["mv", "AMV"], ["montage", "图片混剪"], ["subs", "仅 KTV 字幕"]];
function bgKind(t) { return t === "spectrum" ? "mv" : t === "source" ? "video" : t; }
function setBackground(bg, notify = true) {
  const o = S.d1.options; o.background = bg; touch("options");
  if (bg.type === "subs" && !o.outputs.includes("alpha") && !S.d1.alphaAsked) { o.outputs = o.outputs.concat(["alpha"]); S.d1.alphaAsked = true; }
  renderOutput(); renderMV();
  if (notify) act("set_background", { background: bg });
}
function renderOutput() {
  const j = S.job; const o = S.d1.options; const hasVideo = !!(j.media.source && j.media.source.has_video);
  const cur = bgKind(o.background.type);
  const ctl = $("#bg-type"); ctl.innerHTML = "";
  for (const [v, label] of BG_TYPES) ctl.append(el("button", { class: cur === v ? "on" : "", onclick: () => {
    if (v === "video") {
      if (hasVideo) setBackground({ type: "source" });
      else { o.background = { type: "video", path: o.background.path || "" }; touch("options"); renderOutput(); renderMV(); }
    } else if (v === "subs") setBackground({ type: "subs", color: o.background.type === "subs" ? o.background.color : "#000000" });
    else setBackground({ type: v });
  } }, label));
  const extra = $("#bg-extra"); extra.innerHTML = "";
  if (cur === "video" && hasVideo) extra.append(el("span", { class: "muted small" }, "使用素材原视频作为背景"));
  if (cur === "video" && !hasVideo) {
    const inp = el("input", { type: "text", class: "grow", value: o.background.path || "", placeholder: "背景视频的本机路径，如 D:\\video\\bg.mp4（只取画面，音频仍用歌曲）" });
    extra.append(inp, el("button", { class: "btn ghost sm", onclick: () => {
      const p = inp.value.trim().replace(/^"|"$/g, ""); if (!p) return;
      setBackground({ type: "video", path: p }); toast("已设置背景视频");
    } }, "使用"));
  }
  if (cur === "mv") extra.append(el("span", { class: "muted small" }, "Agent 按歌曲主题设计画面 + 频谱 / 音频可视化，见下方「AMV 设计」"));
  if (cur === "montage") extra.append(el("span", { class: "muted small" }, "图片按节拍剪辑（网上搜集 / 你的图包 / 混合），见下方「图片混剪」"));
  if (cur === "subs") {
    const col = o.background.color || "#000000";
    const sw = (c, label) => el("button", { class: "swatch" + (col.toLowerCase() === c.toLowerCase() ? " on" : ""), title: label, style: { background: c },
      onclick: () => setBackground({ type: "subs", color: c }) });
    extra.append(sw("#000000", "黑底"), sw("#00B140", "绿幕（抠像用）"), sw("#0000FF", "蓝幕"),
      el("input", { type: "color", value: col, onchange: (ev) => setBackground({ type: "subs", color: ev.target.value }) }),
      el("span", { class: "muted small" }, "只有字幕；建议同时勾选「透明字幕层 MOV」，可直接叠加到剪辑软件"));
  }
  if (!["video", "mv", "montage", "subs"].includes(cur)) extra.append(el("span", { class: "muted small" }, `当前背景：${o.background.type === "image" ? "图片" : "纯色"}（由 Agent 设置）`));
  $("#out-kind-mv").hidden = !(cur === "mv" || cur === "montage");
  $("#out-kind-mv-label").textContent = cur === "montage" ? "图片混剪（无字幕版）" : "AMV（无字幕版）";
  $("#out-res").value = o.resolution;
  if (![30, 60].includes(+o.fps)) o.fps = 60;
  $$("#out-fps button").forEach((b) => b.classList.toggle("on", +b.dataset.v === +o.fps));
  $$("#out-kinds input").forEach((c) => (c.checked = o.outputs.includes(c.value)));
  $("#opt-ruby").checked = o.ruby; $("#opt-title").checked = o.title;
  const hr = o.hires || {};
  const reg = (j.media || {}).hires || {};
  const notes = $("#s1-hires-notes");
  notes.textContent = (reg.notes || []).join("\n");
  notes.classList.toggle("warn", (reg.notes || []).some((n) => n.startsWith("⚠")));
  if (document.activeElement !== $("#s1-hires-on")) $("#s1-hires-on").value = hr.on || "";
  if (document.activeElement !== $("#s1-hires-off")) $("#s1-hires-off").value = (hr.off || []).join("; ");
}
function splitPaths(v) { return String(v || "").split(/[;\r\n]+/).map((x) => x.trim().replace(/^"|"$/g, "")).filter(Boolean); }

function designKind() { const t = S.d1 && bgKind(S.d1.options.background.type); return t === "mv" || t === "montage" ? t : null; }
function renderMV() {
  const j = S.job; const card = $("#card-mv");
  const kind = designKind();
  card.hidden = !kind;
  if (!kind) return;
  const pv = j.previews || {};
  const mv = (pv.designs || {})[kind] || {};
  $("#mv-title").textContent = kind === "montage" ? "图片混剪设计" : "AMV 设计（静态 MV）";
  $("#mv-sub").textContent = kind === "montage" ? "图片按节拍剪辑，叠加可视化特效；选一个风格作起点，或在右侧告诉 Agent 想要的效果"
    : "Agent 根据歌曲主题设计画面；可选预设作起点，或在右侧告诉 Agent 想要的意象";
  const labels = ["前奏", "主歌", "副歌高潮"];
  const box = $("#mv-stills"); box.innerHTML = "";
  if (mv.need_assets) box.append(el("div", { class: "muted" }, "素材池还是空的：在下方上传图包，或选择让 Agent 上网搜集图片。"));
  else if (!mv.stills) box.append(el("div", { class: "muted" }, kind === "montage" ? "正在准备混剪画面…" : "Agent 正在设计 MV 画面…"));
  (mv.stills || []).forEach((src, k) => box.append(el("figure", {}, el("img", { src: fileUrl(src) }),
    el("figcaption", {}, `${labels[k] || ""} · ${fmtT((mv.times || [])[k], false)}`))));
  const cat = (pv.mv_catalog || []).filter((p) => (p.kind || "mv") === kind);
  const pre = cat.find((p) => p.id === mv.preset);
  $("#mv-theme").textContent = mv.theme || mv.name || (pre ? "预设：" + pre.name : "");
  $("#mv-notes").textContent = mv.notes || "想换风格 / 配色 / 频谱样式，直接在右侧对话框告诉 Agent。";
  const pal = $("#mv-palette"); pal.innerHTML = "";
  if (Array.isArray(mv.palette)) mv.palette.forEach((c) => pal.append(el("i", { style: { background: c }, title: c })));
  renderMontage(kind);
  const gal = $("#mv-gallery"); gal.innerHTML = "";
  const shots = (pv.design_gallery || {})[kind] || {};
  for (const p of cat) {
    gal.append(optCard({ name: p.name, desc: p.desc }, shots[p.id] ? { backgroundImage: `url("${fileUrl(shots[p.id])}")` } : null,
      mv.preset === p.id, () => { act("mv_preset", { preset: p.id }); toast("已切换预设，正在渲染静帧…"); }));
  }
}

const WHY_LABEL = { new: "新素材", repeat: "重复段落沿用前面的画面", pin: "指定画面", fallback: "素材不足，复用", missing: "缺少素材" };
const ORIGIN_LABEL = { user: "图包", web: "网络", video: "视频" };
const SOURCE_OPTS = [["web", "Agent 上网搜集"], ["user", "我上传图包"], ["mixed", "两者混合"]];
async function uploadPack(files) {
  const list = Array.from(files || []); if (!list.length) return;
  const dir = "pack_" + Date.now(); const paths = [];
  for (let k = 0; k < list.length; k++) {
    toast(`正在上传 ${k + 1}/${list.length}：${list[k].name}`);
    const r = await api(`/api/upload?dir=${dir}&name=` + encodeURIComponent(list[k].name), { method: "POST", body: list[k] });
    paths.push(r.path);
  }
  await act("mv_assets_import", { paths, origin: "user" });
  toast(`已上传 ${list.length} 个文件，正在导入图包…`);
}
function renderMontage(kind) {
  const pv = S.job.previews || {}; const plan = pv.montage; const pool = pv.mv_assets || [];
  const box = $("#mv-montage");
  box.hidden = kind !== "montage";
  if (box.hidden) return;
  const srcSel = (S.job.options || {}).montage_source || "";
  const sc = $("#mv-source"); sc.innerHTML = "";
  for (const [v, label] of SOURCE_OPTS) sc.append(el("button", { class: srcSel === v ? "on" : "", onclick: () => {
    act("montage_source", { source: v });
    toast(v === "user" ? "请在右侧上传图包（图片或 .zip）或填写文件夹路径" : "已通知 Agent 上网搜集相关图片");
  } }, label));
  $("#mv-upload-box").hidden = srcSel === "web";
  const dur = (S.job.media && S.job.media.source && S.job.media.source.duration) || 1;
  const thumb = (id) => { const a = pool.find((x) => x.id === id); return a ? fileUrl(a.thumb) : ""; };
  const uses = {};
  const showPlan = plan && pool.length;
  (showPlan ? plan.shots : []).forEach((s) => { if (s.asset) uses[s.asset] = (uses[s.asset] || 0) + 1; });
  const strip = $("#mv-strip"); strip.innerHTML = ""; strip.hidden = !showPlan;
  if (showPlan) {
    for (const s of plan.shots) {
      const why = s.why || "new"; const url = thumb(s.asset);
      strip.append(el("div", { class: "shot " + why, title: `${fmtT(s.t0, false)}–${fmtT(s.t1, false)} · ${WHY_LABEL[why] || why}`,
        style: { left: (100 * s.t0 / dur) + "%", width: (100 * (s.t1 - s.t0) / dur) + "%", backgroundImage: url ? `url("${url}")` : "none" } }));
    }
    for (const r of plan.repeats || []) strip.append(el("div", { class: "rep", title: `重复段落：沿用 ${fmtT(r.src_start, false)}–${fmtT(r.src_end, false)} 的画面`,
      style: { left: (100 * r.start / dur) + "%", width: (100 * (r.end - r.start) / dur) + "%" } }));
    const seg = (S.d1 && S.d1.segment) || S.job.segment;
    if (seg) {
      strip.append(el("div", { class: "seg", style: { left: 0, width: (100 * seg.start / dur) + "%" } }));
      strip.append(el("div", { class: "seg", style: { left: (100 * seg.end / dur) + "%", right: 0 } }));
    }
  }
  const counts = {}; pool.forEach((a) => (counts[a.origin || "user"] = (counts[a.origin || "user"] || 0) + 1));
  const mix = Object.entries(counts).map(([o, n]) => `${ORIGIN_LABEL[o] || o} ${n}`).join(" / ");
  const need = showPlan ? (plan.needed || plan.shots.length) : 0;
  const avg = showPlan && plan.shots.length ? plan.shots.reduce((a, s) => a + s.t1 - s.t0, 0) / plan.shots.length : 0;
  $("#mv-montage-sum").textContent = showPlan
    ? `${plan.bpm ? Math.round(plan.bpm) + " BPM · " : ""}${plan.shots.length} 个镜头（平均 ${avg.toFixed(1)} 秒一切） · 素材 ${pool.length} 张（${mix}），用到 ${plan.unique_used} 张` +
      (plan.repeated_sections ? ` · ${plan.repeated_sections} 个镜头在重复段落沿用前面的画面` : "")
    : pool.length ? `素材 ${pool.length} 张（${mix}）` : "素材池是空的";
  const note = $("#mv-montage-note");
  const short = showPlan && pool.length < need;
  const slow = showPlan && plan.ideal_unique && pool.length < plan.ideal_unique;
  note.textContent = showPlan
    ? (plan.note || (short ? `还差约 ${need - pool.length} 张不重复的图片才能做到全程不重复，可以再上传或让 Agent 再找一些。`
      : slow ? `按设计节奏需要约 ${plan.ideal_unique} 张不重复的图，目前 ${pool.length} 张，镜头已自动拉长；素材越多切得越快。`
      : "剪辑点对齐小节拍；除重复段落外不重复使用图片。")) + (counts.user && Object.keys(counts).length > 1 ? "  混合素材时优先使用你的图包，并均匀穿插。" : "") + "  鼠标悬停缩略图可移除不想要的图片。"
    : srcSel ? "" : "先选择素材来源：Agent 上网搜集相关作品图片、上传你自己的图包，或两者混合。";
  note.classList.toggle("warn", !!(showPlan && (plan.fallback_repeats || short)));
  const grid = $("#mv-pool"); grid.innerHTML = "";
  for (const a of pool) {
    const n = uses[a.id] || 0;
    grid.append(el("figure", { class: showPlan && !n ? "unused" : "", title: [a.name, a.credit, a.source, (a.tags || []).join(", ")].filter(Boolean).join("\n") || a.id },
      el("img", { src: fileUrl(a.thumb), loading: "lazy" }),
      n ? el("span", { class: "uses" }, n > 1 ? `×${n}` : "✓") : null,
      el("span", { class: "origin o-" + (a.origin || "user") }, ORIGIN_LABEL[a.origin || "user"] || ""),
      el("button", { class: "del", title: "移除这张图", onclick: () => { act("mv_asset_remove", { ids: [a.id] }); toast("已移除，正在重新排剪辑并渲染静帧…"); } }, "×"),
      el("figcaption", {}, a.origin === "user" ? (a.name || "图包") : (a.credit || a.source || a.id))));
  }
}

function renderConfirmSummary() {
  if (!S.d1) return;
  const inc = S.d1.lines.filter((l) => l.include).length;
  const tpl = ((S.job.previews.catalog || {}).templates || []).find((t) => t.id === S.d1.options.template);
  const fx = ((S.job.previews.catalog || {}).effects || []).find((t) => t.id === S.d1.options.effect);
  const seg = S.d1.segment || S.job.segment;
  $("#confirm-summary").textContent = `${inc} 行歌词 · ${tpl ? tpl.name : ""} · ${fx ? fx.name : ""} · ${S.d1.singers.length} 位演唱者` + (seg ? ` · 区间 ${fmtT(seg.start, false)}–${fmtT(seg.end, false)}` : "");
}

function bindStage1() {
  bindWave1();
  for (const k of ["title", "artist", "work", "lyricist", "composer", "arranger"]) $("#song-" + k).addEventListener("input", (ev) => { S.d1.song[k] = ev.target.value; touch("song"); });
  $("#show-ruby").addEventListener("change", renderLyrics);
  $("#only-included").addEventListener("change", renderLyrics);
  $("#inc-all").addEventListener("click", () => { S.d1.lines.forEach((l) => (l.include = true)); touch("lines"); renderLyrics(); drawWave1(); });
  $("#inc-suggest").addEventListener("click", () => { S.job.lyrics.lines.forEach((l, i) => (S.d1.lines[i].include = l.suggest !== false)); touch("lines"); renderLyrics(); drawWave1(); });
  $("#paste-lyrics").addEventListener("click", () => { $("#paste-box").hidden = !$("#paste-box").hidden; });
  $("#mv-gallery-btn").addEventListener("click", () => { act("mv_gallery", { kind: designKind() }); toast("正在为每个预设渲染一张副歌静帧…"); });
  $("#mv-upload").addEventListener("change", (ev) => { uploadPack(ev.target.files).catch((e) => toast("上传失败：" + e.message)); ev.target.value = ""; });
  $("#mv-folder-btn").addEventListener("click", () => {
    const p = $("#mv-folder").value.trim().replace(/^"|"$/g, ""); if (!p) return;
    act("mv_assets_import", { paths: [p], origin: "user" }); toast("正在从文件夹 / 压缩包导入图片…"); $("#mv-folder").value = "";
  });
  $("#mv-plan-btn").addEventListener("click", () => { act("mv_plan", {}); toast("正在按节拍重新计算剪辑…"); });
  $("#paste-cancel").addEventListener("click", () => { $("#paste-box").hidden = true; });
  $("#paste-send").addEventListener("click", async () => {
    const text = $("#paste-text").value.trim(); if (!text) return;
    const blob = new Blob([text], { type: "text/plain" });
    const r = await api("/api/upload?name=pasted_lyrics.txt", { method: "POST", body: blob });
    await act("custom_lyrics", { path: r.path }); $("#paste-box").hidden = true; toast("已交给 Agent：将使用你提供的歌词");
  });
  $("#singer-add").addEventListener("click", () => {
    const palette = ["#FF5FA2", "#4FC3F7", "#FFD54F", "#81C784", "#B39DDB", "#FF8A65", "#4DD0E1", "#F48FB1"];
    const n = S.d1.singers.length; S.d1.singers.push({ id: "s" + (Date.now() % 100000), name: "演唱者 " + (n + 1), color: palette[n % palette.length] });
    touch("singers"); renderSingers();
  });
  $("#singer-refresh").addEventListener("click", () => { act("preview_styles", { only: ["singers"], singers: S.d1.singers, options: { template: S.d1.options.template } }); toast("正在用渲染引擎刷新歌手样式预览"); });
  $("#out-res").addEventListener("change", (ev) => { S.d1.options.resolution = ev.target.value; touch("options"); });
  $$("#out-fps button").forEach((b) => b.addEventListener("click", () => { S.d1.options.fps = +b.dataset.v; touch("options"); renderOutput(); }));
  $$("#out-kinds input").forEach((c) => c.addEventListener("change", () => { S.d1.options.outputs = $$("#out-kinds input").filter((x) => x.checked).map((x) => x.value); touch("options"); renderOutput(); }));
  const regHires = () => {
    const h = S.d1.options.hires || {};
    act("set_hires_source", { on: h.on || null, off: h.off || [] });
    toast(h.on || (h.off || []).length ? "正在对齐 Hi-Res 音源到素材时间轴…" : "已取消 Hi-Res 音源");
  };
  $("#s1-hires-on").addEventListener("change", (ev) => { S.d1.options.hires = { ...(S.d1.options.hires || {}), on: ev.target.value.trim().replace(/^"|"$/g, "") }; touch("options"); regHires(); });
  $("#s1-hires-off").addEventListener("change", (ev) => { S.d1.options.hires = { ...(S.d1.options.hires || {}), off: splitPaths(ev.target.value) }; touch("options"); regHires(); });
  $("#opt-ruby").addEventListener("change", (ev) => { S.d1.options.ruby = ev.target.checked; touch("options"); });
  $("#opt-title").addEventListener("change", (ev) => { S.d1.options.title = ev.target.checked; touch("options"); });
  $("#confirm-1").addEventListener("click", async () => {
    const d = S.d1;
    const payload = { song: { ...d.song, singers: d.singers }, segment: d.segment || S.job.segment, lines: d.lines, options: d.options };
    $("#confirm-1").disabled = true;
    await act("confirm_stage1", payload);
    toast("已确认，Agent 将开始自动打轴");
  });
}

/* ================================================================== STAGE 2 */
const STEP_W = { prepare: 2, separate: 18, pronounce: 5, align: 55, refine: 3, qa: 12, project: 5 };
function renderStage2() {
  const steps = (S.job.progress && S.job.progress.steps) || [];
  const ol = $("#steps"); ol.innerHTML = "";
  let tot = 0, done = 0;
  steps.forEach((s, i) => {
    const w = STEP_W[s.id] || 5; tot += w; done += w * (s.state === "done" || s.state === "skipped" ? 1 : (s.progress || 0));
    const ico = s.state === "done" ? "✓" : s.state === "error" ? "!" : s.state === "running" ? "" : String(i + 1);
    const eta = s.state === "running" && s.eta ? ` · 约 ${Math.ceil(s.eta)} 秒` : "";
    ol.append(el("li", { class: s.state },
      el("span", { class: "ico" }, ico),
      el("div", {},
        el("div", { class: "lbl" }, el("b", {}, s.label), el("span", { class: "muted small mono" }, s.state === "running" ? Math.round((s.progress || 0) * 100) + "%" + eta : s.state === "done" && s.finished_at && s.started_at ? `${Math.round(s.finished_at - s.started_at)} 秒` : "")),
        s.detail ? el("div", { class: "det" }, s.detail) : null,
        s.error ? el("div", { class: "err" }, s.error) : null,
        s.state === "running" ? el("div", { class: "pbar" }, el("i", { style: { width: Math.round((s.progress || 0) * 100) + "%" } })) : null)));
  });
  const pct = tot ? Math.round((done / tot) * 100) : 0;
  $("#overall-ring").style.setProperty("--p", pct); $("#overall-pct").textContent = pct + "%";
  const v = S.view; const live = $("#live-lines"); live.innerHTML = "";
  if (v) {
    const timed = v.lines.filter((l) => l.start !== null);
    $("#s2-count").textContent = `${timed.length} / ${v.lines.length} 行已对齐`;
    for (const l of timed.slice().reverse().slice(0, 14)) {
      const f = (l.qa && l.qa.flag) || "ok";
      live.append(el("div", { class: "ll" }, el("span", { class: "t" }, `${fmtT(l.start)}`), el("span", { class: "x" }, l.text), el("span", { class: "flag " + f, title: ((l.qa && l.qa.notes) || []).join("；") })));
    }
  }
  loadPeaks(); drawTimeline($("#s2-timeline"), { live: true });
}

function singerColor(view, sid) {
  const sg = view && view.singers.find((s) => s.id === sid);
  if (S.ws && sg && S.ws.by_name && S.ws.by_name[sg.name]) return S.ws.by_name[sg.name].color;
  return sg ? sg.color : "#ff5fa2";
}

function drawTimeline(cv, { live = false, playhead = false } = {}) {
  if (!cv || !S.job) return;
  const { ctx, w, h, dpr } = prepCanvas(cv);
  const dur = mediaDuration();
  let x0 = 0, x1 = dur;
  if (playhead && S.zoom) { x0 = S.zoom[0]; x1 = S.zoom[1]; }
  const X = (t) => ((t - x0) / (x1 - x0)) * w;
  ctx.fillStyle = "#12162a"; ctx.fillRect(0, 0, w, h);
  drawPeaks(ctx, w, h, x0, x1, "rgba(150,160,200,.45)", 0, h * 0.62);
  const v = S.view;
  if (v) {
    const laneY = h * 0.66, laneH = h * 0.3;
    v.lines.forEach((l, i) => {
      if (l.start === null) return;
      const flag = (l.qa && l.qa.flag) || "ok";
      const col = singerColor(v, l.singer);
      const y = laneY + (i % 2) * (laneH / 2);
      ctx.fillStyle = col + "cc"; ctx.fillRect(X(l.start), y, Math.max(2, X(l.end) - X(l.start)), laneH / 2 - 2 * dpr);
      if (flag !== "ok") { ctx.fillStyle = flag === "bad" ? "#ff5d6c" : "#ffc24b"; ctx.fillRect(X(l.start), y - 3 * dpr, Math.max(2, X(l.end) - X(l.start)), 2 * dpr); }
      if (S.activeLine === i && playhead) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5 * dpr; ctx.strokeRect(X(l.start), y, X(l.end) - X(l.start), laneH / 2 - 2 * dpr); }
      for (const c of l.chars) { if (c.s === null || !c.cp.length) continue; ctx.fillStyle = "rgba(255,255,255,.55)"; ctx.fillRect(X(c.s), y, 1, laneH / 2 - 2 * dpr); }
    });
  }
  if (playhead) { const vid = $("#s3-video"); const t = vid ? vid.currentTime : 0; ctx.fillStyle = "#fff"; ctx.fillRect(X(t), 0, 1.5 * dpr, h); }
  if (live && S.job.segment) { ctx.fillStyle = "rgba(0,0,0,.35)"; ctx.fillRect(0, 0, X(S.job.segment.start), h); ctx.fillRect(X(S.job.segment.end), 0, w - X(S.job.segment.end), h); }
}

/* ================================================================== STAGE 3 */
let s3Bound = false;
function renderStage3() {
  const j = S.job; const m = j.media;
  const vid = $("#s3-video");
  const bg = (j.options && j.options.background) || {};
  const bk = bgKind(bg.type || ((m.source && m.source.has_video) ? "source" : "mv"));
  const dk = bk === "montage" ? "montage" : bk === "mv" || !(m.source && m.source.has_video) ? "mv" : null;
  let src = m.player;
  const dv = dk && ((m.designs || {})[dk] || {}).video;
  if (dv && bk !== "subs") src = dv;
  const poster = dk && (m.design_still || {})[dk] ? (m.design_still || {})[dk] : m.thumb;
  if (vid.dataset.src !== src) { vid.dataset.src = src; vid.src = fileUrl(src); vid.poster = poster && bk !== "subs" ? fileUrl(poster) : ""; }
  vid.style.opacity = bk === "subs" ? 0 : 1;
  $("#s3-player").style.background = bk === "subs" ? (bg.color || "#000") : "";
  const ef = (j.previews || {}).engine_frame;
  if (S.engineRev === undefined) S.engineRev = ef ? ef.rev : null; // don't pop up a frame from a previous session
  if (ef && ef.rev !== S.engineRev) { S.engineRev = ef.rev; const img = $("#s3-engine"); img.src = fileUrl(ef.path) + "?r=" + ef.rev; img.hidden = false; $("#s3-engine-badge").hidden = false; $("#s3-engine-badge").textContent = `引擎渲染帧 @ ${fmtT(ef.t)} · 点击关闭`; }
  memo("exports", JSON.stringify(j.exports || []), renderExports);
  memo("linetable", String(S.viewRev) + JSON.stringify((j.timing || {}).qa_summary || {}), renderLineTable);
  loadPeaks();
}

function renderExports() {
  const j = S.job; const row = $("#export-row"); row.innerHTML = "";
  const kinds = [["sug", "SUG 打轴工程"], ["yurika", "字幕工程 .yurika"], ["lrc", "逐字 LRC"], ["mp4", "成品 MP4"]];
  const running = (j.exports || []).some((e) => e.state === "running");
  const outs = (j.options || {}).outputs || [];
  const wantHires = outs.includes("hires"), wantOnOff = outs.includes("onoff"), wantAlpha = outs.includes("alpha");
  const bgt = bgKind(((j.options || {}).background || {}).type);
  const wantMV = outs.includes("mv") && (bgt === "mv" || bgt === "montage");
  const extrasLabel = [wantAlpha ? "透明字幕" : "", wantOnOff ? "on/off vocal" : "", wantMV ? (bgt === "montage" ? "混剪" : "AMV") : "", wantHires ? "Hi-Res" : ""].filter(Boolean).join(" + ");
  for (const [k, label] of kinds) row.append(el("button", { class: "btn " + (k === "mp4" ? "primary" : "ghost"), disabled: running, onclick: () => {
    const ks = k === "mp4" ? ["sug", "yurika", "mp4"].concat(wantAlpha ? ["alpha"] : [], wantOnOff ? ["onoff"] : [], wantMV ? ["mv"] : [], wantHires ? ["hires"] : []) : [k];
    act("export", { kinds: ks, hires: wantHires ? hiresPayload() : undefined });
    toast(k === "mp4" ? "开始渲染成品 MP4（需要数分钟）" + (extrasLabel ? "，之后生成 " + extrasLabel : "") : "正在导出 " + label);
  } }, (k === "mp4" ? "▶ 导出" : "导出 ") + label + (k === "mp4" && extrasLabel ? " + " + extrasLabel : "")));
  row.append(el("button", { class: "btn ghost", disabled: running, title: "只渲染开头 25 秒，快速确认最终画面", onclick: () => {
    act("export", { kinds: ["mp4"], clip: 25 }); toast("正在渲染 25 秒试看片段");
  } }, "试看 25 秒"));
  const hasMaster = (j.exports || []).some((e) => e.kind === "mp4" && e.state === "done");
  row.append(el("button", { class: "btn ghost", disabled: running || !hasMaster, title: hasMaster ? "复制视频流，只替换音轨" : "请先导出成品 MP4", onclick: () => {
    act("export", { kinds: ["onoff"], hires: hiresPayload() }); toast("正在生成 on / off vocal 版本");
  } }, "on / off vocal"));
  row.append(el("button", { class: "btn ghost", disabled: running, title: "只有字幕、带透明通道的 ProRes 4444 MOV，导入剪辑软件叠加使用", onclick: () => {
    act("export", { kinds: ["alpha"] }); toast("正在渲染透明字幕层（整首歌，体积较大）");
  } }, "透明字幕层 MOV"));
  $("#s3-hires").disabled = running || !(j.exports || []).some((e) => e.kind === "mp4" && e.state === "done");
  $("#s3-hires").title = $("#s3-hires").disabled ? "请先导出成品 MP4" : "";
  const hr = (j.options || {}).hires || {};
  if (document.activeElement !== $("#s3-hires-on") && !$("#s3-hires-on").value) $("#s3-hires-on").value = hr.on || "";
  if (document.activeElement !== $("#s3-hires-off") && !$("#s3-hires-off").value) $("#s3-hires-off").value = (hr.off || []).join("; ");
  const list = $("#export-list"); list.innerHTML = "";
  for (const e of j.exports || []) {
    const right = e.state === "running" ? el("span", { class: "mono small" }, `${Math.round((e.progress || 0) * 100)}%${e.eta ? " · 剩余约 " + Math.ceil(e.eta) + " 秒" : ""}`)
      : e.state === "error" ? el("span", { class: "chip bad", title: e.error }, "失败")
      : el("button", { class: "btn ghost sm", onclick: () => act("open_folder", { path: e.path }) }, "定位");
    const paths = e.files && e.files.length ? e.files : [e.path];
    const item = el("div", { class: "export-item" }, el("span", { class: "k" }, ({ hires: "hi-res", onoff: "on/off", alpha: "alpha", mv: "bg" })[e.kind] || e.kind),
      el("div", {}, el("div", {}, e.label || e.kind), ...paths.map((p) => el("div", { class: "p" }, p)),
        e.notes && e.notes.length ? el("div", { class: "notes" }, e.notes.join("\n")) : null,
        e.state === "running" ? el("div", { class: "pbar" }, el("i", { style: { width: Math.round((e.progress || 0) * 100) + "%" } })) : null), right);
    list.append(item);
    if (e.state === "running" && e.preview) list.append(el("img", { src: fileUrl(e.preview) + "?r=" + (e.frames || e.progress), style: { width: "100%", borderRadius: "10px", border: "1px solid var(--line)" } }));
  }
}

function renderLineTable() {
  const v = S.view; const box = $("#line-table"); if (!box) return;
  const keepScroll = box.scrollTop; box.innerHTML = "";
  if (!v) { box.append(el("div", { class: "muted" }, "尚未生成时间轴")); return; }
  const qa = (S.job.timing || {}).qa_summary;
  $("#qa-summary").textContent = qa ? `正常 ${qa.ok} · 注意 ${qa.warn} · 可疑 ${qa.bad}` : "";
  v.lines.forEach((l, i) => {
    const q = l.qa || {}; const flag = q.flag || "ok";
    const nud = (ms) => el("button", { onclick: (ev) => { ev.stopPropagation(); editLines([{ op: "shift_lines", lines: [i], ms }]); }, title: `整行${ms > 0 ? "推后" : "提前"} ${Math.abs(ms)} ms` }, (ms > 0 ? "+" : "−") + Math.abs(ms) / 1000);
    const row = el("div", { class: "lrow", "data-i": i },
      el("span", { class: "n" }, i + 1),
      el("div", {},
        el("div", { class: "tx", html: rubyFromChars(l.chars) }),
        el("div", { class: "info" },
          el("span", { class: "flag " + flag }), `${fmtT(l.start)} → ${fmtT(l.end)}`,
          q.notes && q.notes.length ? el("span", { class: "qa" }, q.notes.join("；")) : null,
          el("span", { class: "nudge" }, nud(-100), nud(-20), nud(20), nud(100),
            el("button", { title: "让本行从当前播放位置开始", onclick: (ev) => { ev.stopPropagation(); const t = $("#s3-video").currentTime; editLines([{ op: "shift_lines", lines: [i], ms: Math.round((t - l.start) * 1000) }]); } }, "⇤ 播放头")))));
    row.addEventListener("click", () => { const vid = $("#s3-video"); vid.currentTime = Math.max(0, l.start - 1.2); vid.play(); });
    box.append(row);
  });
  box.scrollTop = keepScroll;
  if (S.activeLine !== null && S.activeLine !== undefined) { const a = $(`.lrow[data-i="${S.activeLine}"]`); if (a) a.classList.add("active"); }
}
function rubyFromChars(chars) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  let out = "", grp = "", rb = "";
  for (const c of chars) {
    grp += c.c; rb += (c.r || []).join("").replace(/\^pause\^/g, "");
    if (!c.link) { out += rb && rb !== grp ? `<ruby>${esc(grp)}<rt>${esc(rb)}</rt></ruby>` : esc(grp); grp = ""; rb = ""; }
  }
  return out + esc(grp);
}
async function editLines(ops) {
  // optimistic update of the local view
  for (const op of ops) if (op.op === "shift_lines") for (const i of op.lines) {
    const l = S.view.lines[i]; const d = op.ms / 1000;
    l.start += d; l.end += d; for (const c of l.chars) { if (c.s !== null) { c.s += d; c.e += d; c.cp = c.cp.map((t) => t + d); } }
  }
  renderLineTable();
  await act("edit_timing", { ops });
}

function bindStage3() {
  const vid = $("#s3-video");
  $("#s3-play").addEventListener("click", () => (vid.paused ? vid.play() : vid.pause()));
  vid.addEventListener("play", () => ($("#s3-play").textContent = "❚❚"));
  vid.addEventListener("pause", () => ($("#s3-play").textContent = "▶"));
  $("#s3-seek").addEventListener("input", (ev) => { vid.currentTime = (ev.target.value / 1000) * (vid.duration || mediaDuration()); });
  $("#s3-rate").addEventListener("change", (ev) => (vid.playbackRate = +ev.target.value));
  $("#s3-frame").addEventListener("click", () => { vid.pause(); act("preview_frame", { t: vid.currentTime }); toast("渲染引擎正在输出当前帧…"); });
  const hide = () => { $("#s3-engine").hidden = true; $("#s3-engine-badge").hidden = true; };
  $("#s3-engine").addEventListener("click", hide); $("#s3-engine-badge").addEventListener("click", hide);
  vid.addEventListener("play", hide);
  $("#open-export").addEventListener("click", () => act("open_folder", { path: (S.job.options || {}).output_dir || "export" }));
  $("#close-page").addEventListener("click", async () => {
    const running = (S.job.exports || []).some((e) => e.state === "running") || S.job.status === "working";
    const msg = running ? "Agent 仍在处理中，确定要结束本工程并关闭网页吗？" : "结束本工程并关闭网页服务？（成品与工程文件都会保留）";
    if (!confirm(msg)) return;
    try { await api("/api/action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ type: "close_page", payload: {} }) }); } catch (e) { /* server already gone */ }
    showClosed("user");
  });
  $("#open-app").addEventListener("click", async () => {
    const has = (S.job.exports || []).some((e) => (e.kind === "yurika" || e.kind === "sug") && e.state === "done");
    if (!has) { toast("请先导出 SUG 或字幕工程"); return; }
    await act("open_app", {}); toast("正在启动 Lin-K Lyrics 并载入工程…");
  });
  $("#s3-hires").addEventListener("click", () => { act("export", { kinds: ["hires"], hires: hiresPayload() }); toast("开始 Hi-Res 混流（对齐 + 分离伴奏可能需要几分钟）"); });
  const tl = $("#s3-timeline");
  let drag = null;
  const tAt = (ev) => { const r = tl.getBoundingClientRect(); const x0 = S.zoom ? S.zoom[0] : 0, x1 = S.zoom ? S.zoom[1] : mediaDuration(); return x0 + ((ev.clientX - r.left) / r.width) * (x1 - x0); };
  tl.addEventListener("mousedown", (ev) => {
    const t = tAt(ev); const r = tl.getBoundingClientRect(); const yRel = (ev.clientY - r.top) / r.height;
    if (yRel > 0.64 && S.view) {
      const lane = yRel > 0.81 ? 1 : 0;
      const i = S.view.lines.findIndex((l, k) => k % 2 === lane && l.start !== null && t >= l.start && t <= l.end);
      if (i >= 0) { drag = { i, t0: t, moved: 0 }; return; }
    }
    vid.currentTime = Math.max(0, t);
  });
  window.addEventListener("mousemove", (ev) => {
    if (!drag) return; const t = tAt(ev); const d = t - drag.t0; const l = S.view.lines[drag.i];
    l.start += d - drag.moved; l.end += d - drag.moved; for (const c of l.chars) if (c.s !== null) { c.s += d - drag.moved; c.e += d - drag.moved; c.cp = c.cp.map((x) => x + d - drag.moved); }
    drag.moved = d;
  });
  window.addEventListener("mouseup", () => { if (drag) { const ms = Math.round(drag.moved * 1000); if (Math.abs(ms) >= 10) act("edit_timing", { ops: [{ op: "shift_lines", lines: [drag.i], ms }] }); drag = null; } });
  tl.addEventListener("wheel", (ev) => {
    ev.preventDefault(); const t = tAt(ev); const dur = mediaDuration(); const [a, b] = S.zoom || [0, dur];
    const k = ev.deltaY > 0 ? 1.25 : 0.8; const na = clamp(t - (t - a) * k, 0, dur), nb = clamp(t + (b - t) * k, 0, dur);
    S.zoom = nb - na >= dur * 0.98 ? null : [na, nb];
  }, { passive: false });
  document.addEventListener("keydown", (ev) => { if (S.viewing === 3 && ev.code === "Space" && !["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) { ev.preventDefault(); vid.paused ? vid.play() : vid.pause(); } });
}

function hiresPayload() {
  const on = $("#s3-hires-on").value.trim().replace(/^"|"$/g, "");
  return { on: on || null, off: splitPaths($("#s3-hires-off").value) };
}

/* ------------------------------------------------------------ overlay renderer */
function lineWindows(view) {
  // Approximation of the engine's 2-row page logic: rows alternate, a line
  // appears lead_in before its first char (but not before its row is free)
  // and leaves tail after its end (or when the next line on its row needs it).
  const lead = 1.8, tail = 1.0;
  const out = []; const rowFree = [-1e9, -1e9];
  const lines = view.lines.filter((l) => l.start !== null);
  lines.forEach((l, k) => {
    const row = k % 2;
    const show = Math.max(l.start - lead, rowFree[row] + 0.05);
    const next = lines[k + 2];
    const hide = Math.min(l.end + tail, next ? next.start - lead + 0.05 > l.end ? next.start - lead + 0.05 : l.end + 0.1 : l.end + tail);
    rowFree[row] = Math.max(hide, l.end);
    out.push({ l, row, show, hide: Math.max(hide, l.end + 0.15) });
  });
  return out;
}

function charProgress(c, t) {
  if (c.s === null) return null;
  if (t <= c.s) return 0; if (t >= c.e) return 1;
  const cps = c.cp.length ? c.cp : [c.s]; const n = cps.length;
  for (let k = 0; k < n; k++) {
    const a = cps[k], b = k + 1 < n ? cps[k + 1] : c.e;
    if (t < b) return (k + clamp((t - a) / Math.max(1e-3, b - a), 0, 1)) / n;
  }
  return 1;
}

function drawOverlay() {
  const cv = $("#s3-overlay"); const vid = $("#s3-video");
  if (!cv || S.viewing !== 3 || !S.view) return;
  const r = cv.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(r.width * dpr)) { cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr); }
  const ctx = cv.getContext("2d"); ctx.clearRect(0, 0, cv.width, cv.height);
  // video content box (object-fit: contain)
  const vw = vid.videoWidth || 1920, vh = vid.videoHeight || 1080;
  const scale = Math.min(cv.width / vw, cv.height / vh); const bw = vw * scale, bh = vh * scale; const bx = (cv.width - bw) / 2, by = (cv.height - bh) / 2;
  if (S.layout) {
    const active = drawEngineOverlay(ctx, S.layout, vid.currentTime, bx, by, bw, bh);
    if (active !== S.activeLine) { S.activeLine = active; $$(".lrow").forEach((n) => n.classList.toggle("active", +n.dataset.i === active)); const a = $(`.lrow[data-i="${active}"]`); if (a && !vid.paused) a.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    return;
  }
  const ws = S.ws || { font: "Yu Gothic UI", size: 96, weight: 700, spacing: 0, stroke: 10, stroke2: 4, before: { top: "#fff", bottom: "#fff", stroke: "#123", stroke2: "#fff" }, after: { top: "#2f6bff", bottom: "#2f6bff", stroke: "#fff", stroke2: "#123" }, by_name: {}, ruby: true };
  const k = bh / 1080; const t = vid.currentTime;
  const fs = ws.size * k, rfs = fs * 0.45, margin = 50 * k * (bw / bh) / (16 / 9);
  const baseY = by + bh - 60 * k; const gap = 45 * k;
  let active = null;
  for (const w of lineWindows(S.view)) {
    if (t < w.show || t > w.hide) continue;
    const l = w.l; if (t >= l.start && t <= l.end) active = l.i;
    let alpha = 1; if (t < w.show + 0.3) alpha = (t - w.show) / 0.3; if (t > w.hide - 0.3) alpha = (w.hide - t) / 0.3;
    ctx.globalAlpha = clamp(alpha, 0, 1);
    ctx.font = `${ws.weight} ${fs}px "${ws.font}", "Yu Gothic UI", "Meiryo", sans-serif`;
    const widths = l.chars.map((c) => ctx.measureText(c.c).width + ws.spacing * k);
    const total = widths.reduce((a, b) => a + b, 0);
    const y = w.row === 0 ? baseY - fs - gap - (ws.ruby ? rfs : 0) : baseY;
    const x = w.row === 0 ? bx + margin : bx + bw - margin - total;
    const sg = S.view.singers.find((s) => s.id === l.singer); const scol = sg && ws.by_name && ws.by_name[sg.name];
    const after = scol ? { ...ws.after, top: scol.top, bottom: scol.bottom } : ws.after;
    drawKaraLine(ctx, l, widths, x, y, fs, rfs, ws, ws.before, null, t, k);
    // wipe clip
    ctx.save(); ctx.beginPath(); let cx = x;
    l.chars.forEach((c, i) => { const p = charProgress(c, t); if (p) ctx.rect(cx - 2, y - fs - rfs * 1.4, widths[i] * p + 2, fs * 1.6 + rfs * 1.4); cx += widths[i]; });
    ctx.clip(); drawKaraLine(ctx, l, widths, x, y, fs, rfs, ws, after, ws.after.glow ? after.top : null, t, k); ctx.restore();
  }
  ctx.globalAlpha = 1;
  if (active !== S.activeLine) { S.activeLine = active; $$(".lrow").forEach((n) => n.classList.toggle("active", +n.dataset.i === active)); const a = $(`.lrow[data-i="${active}"]`); if (a && !vid.paused) a.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
}

function drawKaraLine(ctx, l, widths, x, y, fs, rfs, ws, col, glow, t, k) {
  const grad = ctx.createLinearGradient(0, y - fs * 0.85, 0, y + fs * 0.1); grad.addColorStop(0, col.top); grad.addColorStop(1, col.bottom);
  ctx.lineJoin = "round"; ctx.textBaseline = "alphabetic";
  const strokeW = ws.stroke * k, stroke2W = (ws.stroke2 || 0) * k;
  const passes = (font, size, yy, text, cx) => {
    ctx.font = font;
    if (stroke2W > 0) { ctx.strokeStyle = col.stroke2; ctx.lineWidth = (strokeW + stroke2W) * 2; ctx.strokeText(text, cx, yy); }
    ctx.strokeStyle = col.stroke; ctx.lineWidth = strokeW * 2; ctx.strokeText(text, cx, yy);
    if (glow) { ctx.shadowColor = glow; ctx.shadowBlur = (ws.glow || 10) * k * 2; }
    ctx.fillStyle = grad; ctx.fillText(text, cx, yy); ctx.shadowBlur = 0;
  };
  const font = `${ws.weight} ${fs}px "${ws.font}", "Yu Gothic UI", "Meiryo", sans-serif`;
  const rfont = `${ws.weight} ${rfs}px "${ws.font}", "Yu Gothic UI", "Meiryo", sans-serif`;
  let cx = x;
  l.chars.forEach((c, i) => { passes(font, fs, y, c.c, cx); cx += widths[i]; });
  if (!ws.ruby) return;
  // ruby: group by word
  cx = x; let gx = x, gw = 0, rt = "", gt = "";
  const saveSW = ws.stroke; ws.stroke = Math.max(2, ws.stroke * 0.6);
  l.chars.forEach((c, i) => {
    gt += c.c; rt += (c.r || []).join("").replace(/\^pause\^/g, ""); gw += widths[i];
    if (!c.link) {
      if (rt && rt !== gt) { ctx.font = rfont; const rw = ctx.measureText(rt).width; passes(rfont, rfs, y - fs * 0.92, rt, gx + (gw - rw) / 2); }
      gx += gw; gw = 0; rt = ""; gt = "";
    }
  });
  ws.stroke = saveSW;
}

/* ------------------------------------------------------------------ chat & log */
function renderChat() {
  const box = $("#chat"); const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = "";
  if (!S.chat.length) box.append(el("div", { class: "msg system" }, "这里会显示 Agent 的说明与你的指令。随时可以用文字告诉 Agent 你想改什么。"));
  for (const m of S.chat) box.append(el("div", { class: "msg " + m.role }, el("div", { class: "meta" }, m.role === "user" ? "你" : m.role === "agent" ? "Agent" : "系统", el("span", {}, (m.time || "").slice(11, 16))), m.text));
  if (S.pending) box.append(el("div", { class: "msg system" }, S.job.agent && S.job.agent.listening ? "Agent 已收到，正在处理…" : `有 ${S.pending} 条操作等待 Agent 处理（Agent 正忙于当前任务）`));
  if (atBottom) box.scrollTop = box.scrollHeight;
}
function appendLog(line) { const pre = $("#log"); pre.textContent += line + "\n"; if (pre.textContent.length > 60000) pre.textContent = pre.textContent.slice(-40000); pre.scrollTop = pre.scrollHeight; }

/* ------------------------------------------------------------------ binding & loop */
function bindStatic() {
  $$("#stepper button").forEach((b) => b.addEventListener("click", () => { if (b.disabled) return; S.viewing = +b.dataset.stage; renderAll(); }));
  $$(".side-tabs button").forEach((b) => b.addEventListener("click", () => {
    $$(".side-tabs button").forEach((x) => x.classList.toggle("on", x === b));
    $("#side-chat").hidden = b.dataset.tab !== "chat"; $("#side-log").hidden = b.dataset.tab !== "log";
    if (b.dataset.tab === "log" && !$("#log").textContent) api("/api/log").then((r) => { $("#log").textContent = r.lines.join("\n") + "\n"; });
  }));
  $("#prompt-form").addEventListener("submit", async (ev) => {
    ev.preventDefault(); const text = $("#prompt").value.trim(); if (!text) return;
    const vid = $("#s3-video");
    await act("prompt", { text, stage: S.viewing, t: S.viewing === 3 && vid ? +vid.currentTime.toFixed(2) : null, active_line: S.activeLine });
    $("#prompt").value = "";
  });
  $("#prompt").addEventListener("keydown", (ev) => { if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) $("#prompt-form").requestSubmit(); });
  bindStage1(); bindStage3();
  window.addEventListener("resize", drawWaves);
}

let lastDraw = 0;
function tick(ts) {
  if (S.job) {
    if (S.viewing === 3) {
      drawOverlay();
      if (ts - lastDraw > 50) { drawTimeline($("#s3-timeline"), { playhead: true }); lastDraw = ts; const vid = $("#s3-video"); $("#s3-time").textContent = fmtT(vid.currentTime); if (document.activeElement !== $("#s3-seek")) $("#s3-seek").value = Math.round((vid.currentTime / (vid.duration || mediaDuration())) * 1000); }
    } else if (S.viewing === 1 && ts - lastDraw > 80) { const m = $("#s1-media"); if (m && !m.paused) drawWave1(); lastDraw = ts; }
  }
  requestAnimationFrame(tick);
}

boot();
