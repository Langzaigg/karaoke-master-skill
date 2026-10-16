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
// 输出帧率：24 / 30 / 60，或 "source" 跟随素材帧率
const fpsChoice = (v) => {
  const s = String(v === null || v === undefined ? "" : v).trim();
  if (s === "source" || s === "跟随素材") return "source";
  return [24, 30, 60].includes(+s) ? +s : 60;
};
const sourceFps = (j) => { const f = +(((j || {}).media || {}).source || {}).fps; return f > 0 ? Math.round(f) : 0; };
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
  wc: null, wcIdx: null,
  expKinds: null, expSig: null, expTouched: false,   // 导出卡片勾选（本次页面会话内）
  dirCur: null,                                      // 导出目录选择器当前所在文件夹
  masterStale: false,                                // 成品 MP4 的画面落后于当前时间轴
};

/* ------------------------------------------------------------------ network */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}
async function act(type, payload = {}, extra = {}) {
  if ((type === "undo" || type === "redo") && S.expects) S.expects.clear();  // 撤销/重做后的状态由服务端说了算
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
  S.wc = data.wavecheck || null;
  S.masterStale = !!data.master_stale;
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
S.editSeq = 0;   // bumped by every local fine edit; stale refreshes may not overwrite newer local state
S.viewSeq = 0;   // bumped by every view fetch; only the newest in-flight response may apply
S.flushSeq = 0;  // bumped on every edit submit; a fetch that spans a submit is stale by definition
S.flushRev = null;  // revision at the last edit submit; refresh waits until the commit bumps it
S.expects = new Map();  // line:char -> frozen expectation; a body must include our submitted edits
async function loadTimingView() {
  const t = S.job.timing || {};
  const rev = (t.view || "") + ":" + (t.revision || "") + ":" + (S.job.updated_at || "");
  if (!t.view && !(S.job.stage >= 2)) return;
  if (rev === S.viewRev) return;
  // 本地还有未提交的细调编辑（或正在拖动）时不要用服务端快照覆盖 —— 提交后的
  // job 更新会带着新 rev 再触发一次，那时才应用权威数据。
  if (tlDrag || finePending.size) return;
  S.viewRev = rev;
  const seq = ++S.viewSeq;
  const eseq = S.editSeq;
  const fseq = S.flushSeq;
  try {
    const v = await api(fileUrl("timing/timed.json") + "?r=" + Date.now());
    // 应用前再校验：飞行期间如果又发了新请求 / 有新的本地编辑 / 提交过新改动 /
    // 开始了拖动，这份响应就已经过期（可能还是编辑前的旧状态），丢弃等下一次。
    if (seq !== S.viewSeq || eseq !== S.editSeq || fseq !== S.flushSeq || tlDrag || finePending.size) return;
    // 有已提交但还没被引擎消化的细调编辑时，只接受「提交已落地」的刷新
    // （快照事件也会碰 job.json，但它带的还是编辑前的 timed.json）。
    if (S.flushRev !== null && (S.job.timing || {}).revision === S.flushRev
        && Date.now() - (S.flushAt || 0) < 10000) return;
    S.flushRev = null;
    // 还有已提交但未被任何刷新确认的编辑时：刷新体必须已经包含它们（比对冻结的
    // 期望快照），否则就是「前一次提交」的旧状态，丢弃等下一次。
    if (S.expects.size) {
      const now = Date.now();
      for (const [k, ex] of S.expects) {
        if (now - ex.at > 20000) { S.expects.delete(k); continue; }
        const bch = v.lines[ex.line] && v.lines[ex.line].chars[ex.char];
        if (!bch || bch.cp.length !== ex.cp.length || !!bch.se !== ex.se
            || ex.cp.some((t, i) => Math.abs(bch.cp[i] - t) > 0.03)) return;
      }
      S.expects.clear();
    }
    const changed = JSON.stringify(v.lines.map((l) => [l.start, l.end, l.qa && l.qa.flag])) !== JSON.stringify((S.view ? S.view.lines : []).map((l) => [l.start, l.end, l.qa && l.qa.flag]));
    S.view = v;
    if (changed) { if (S.viewing === 2) renderStage2(); if (S.viewing === 3) renderLineTable(); }
  } catch (e) { S.viewRev = null; /* 没读到/半写：让下一次 job 事件重试 */ }
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
    S.tlHold = 0;   // 引擎布局已跟上刚才的编辑，走字预览恢复精确合成
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
      fps: fpsChoice(opts.fps),
      cast_delay_ms: opts.cast_delay_ms === undefined || opts.cast_delay_ms === null ? 200 : +opts.cast_delay_ms,
      outputs: opts.outputs || ["mp4", "sug", "yurika", "onoff"],
      ruby: opts.ruby !== false,
      title: opts.title !== false,
      overrides: JSON.parse(JSON.stringify(opts.overrides || {})),
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
  memo("output", JSON.stringify([d.options, j.media.design_still, j.media.hires, j.media.mv_video, j.media.oped,
    (j.previews || {}).mv_video_candidates]), renderOutput);
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
      const still = (bgKind(S.d1.options.background.type) === "video" && (m.mv_video || {}).still) || ds[designKind() || "mv"] || ds.mv || ds.montage || m.cover;
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
  const dpr = window.devicePixelRatio || 1;
  // 设计高度（CSS px）只在首次记录：位图高度 = 设计高度 × dpr，若每次从已写回的
  // height 属性再乘 dpr，高倍屏上每次重绘画布都会变高一次（页面被无限拉长）。
  if (!cv.dataset.cssh) {
    cv.dataset.cssh = cv.getAttribute("height") || Math.round(cv.getBoundingClientRect().height) || 120;
    cv.style.height = cv.dataset.cssh + "px";
  }
  const r = cv.getBoundingClientRect();
  const w = Math.max(10, Math.round(r.width * dpr)), h = Math.max(10, Math.round(+cv.dataset.cssh * dpr));
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
      el("button", { class: "singer-chip", title: "点击切换演唱者", style: { background: sg ? sg.color : "#3a4166" }, onclick: (ev) => { ev.stopPropagation(); singerMenu(ev.target, i); } }, sg ? sg.name : "未指定"));
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

// 三种背景模式：视频（原视频/选定视频直接铺底）· 混剪（素材按节拍剪辑，可选乐曲可视化）
// · OPED（长视频/官方 PV 分段拼接）。仅字幕是导出用的小工具，不属这三种之一。
const BG_TYPES = [["video", "视频"], ["montage", "混剪"], ["oped", "OPED 拼接"], ["subs", "仅 KTV 字幕"]];
function bgKind(t) { return t === "spectrum" ? "mv" : t === "source" ? "video" : t; }
function setBackground(bg, notify = true) {
  const o = S.d1.options;
  const prev = o.background || {};
  if (bg.visualize === undefined && prev.visualize !== undefined) bg = Object.assign({ visualize: prev.visualize }, bg);
  o.background = bg; touch("options");
  if (bg.type === "subs" && !o.outputs.includes("alpha") && !S.d1.alphaAsked) { o.outputs = o.outputs.concat(["alpha"]); S.d1.alphaAsked = true; }
  renderOutput(); renderMV();
  if (notify) act("set_background", { background: bg });
}
function renderOutput() {
  const j = S.job; const o = S.d1.options; const hasVideo = !!(j.media.source && j.media.source.has_video);
  const cur = bgKind(o.background.type);
  const curBtn = cur === "mv" ? "montage" : cur;   // 旧的 AMV 背景并入「混剪」
  const ctl = $("#bg-type"); ctl.innerHTML = "";
  for (const [v, label0] of BG_TYPES) { const label = v === "video" && !hasVideo ? "MV 视频" : label0; ctl.append(el("button", { class: curBtn === v ? "on" : "", onclick: () => {
    if (v === "video") {
      if (hasVideo) setBackground({ type: "source" });
      else { o.background = { type: "video", path: o.background.path || "" }; touch("options"); renderOutput(); renderMV(); }
    } else if (v === "subs") setBackground({ type: "subs", color: o.background.type === "subs" ? o.background.color : "#000000" });
    else setBackground({ type: v });
  } }, label)); }
  const extra = $("#bg-extra"); extra.innerHTML = "";
  if (cur === "video" && hasVideo) extra.append(el("span", { class: "muted small" }, "使用素材原视频作为背景"));
  if (cur === "video" && !hasVideo) extra.append(renderMvVideo());
  if (cur === "oped") extra.append(renderOped());
  if (["video", "oped", "mv", "montage"].includes(cur)) extra.append(renderVisualizeToggle());
  if (cur === "mv") extra.append(el("span", { class: "muted small" }, "旧版 AMV 背景：点「混剪」可并入混剪模式"));
  if (cur === "montage") extra.append(el("span", { class: "muted small" }, "图片或游戏 / 动画视频片段按节拍剪辑（网上搜集 / 你的图包 / 混合），可叠加乐曲可视化，见下方「混剪设计」"));
  if (cur === "subs") {
    const col = o.background.color || "#000000";
    const sw = (c, label) => el("button", { class: "swatch" + (col.toLowerCase() === c.toLowerCase() ? " on" : ""), title: label, style: { background: c },
      onclick: () => setBackground({ type: "subs", color: c }) });
    extra.append(sw("#000000", "黑底"), sw("#00B140", "绿幕（抠像用）"), sw("#0000FF", "蓝幕"),
      el("input", { type: "color", value: col, onchange: (ev) => setBackground({ type: "subs", color: ev.target.value }) }),
      el("span", { class: "muted small" }, "只有字幕；建议同时勾选「透明字幕层 MOV」，可直接叠加到剪辑软件"));
  }
  if (!["video", "mv", "montage", "oped", "subs"].includes(cur)) extra.append(el("span", { class: "muted small" }, `当前背景：${o.background.type === "image" ? "图片" : "纯色"}（由 Agent 设置）`));
  $("#out-kind-mv").hidden = !(cur === "mv" || cur === "montage");
  $("#out-kind-mv-label").textContent = cur === "montage" ? "混剪（无字幕版）" : "可视化（无字幕版）";
  $("#out-res").value = o.resolution;
  const tplSize = (((j.previews.catalog || {}).templates || []).find((t) => t.id === o.template) || {}).size || 96;
  const fsInp = $("#out-font-size");
  if (document.activeElement !== fsInp) fsInp.value = (o.overrides || {}).font_size_px === undefined ? "" : (o.overrides || {}).font_size_px;
  fsInp.placeholder = `默认 ${tplSize}`;
  const bmInp = $("#out-bottom-margin");
  if (document.activeElement !== bmInp) bmInp.value = (o.overrides || {}).line_y_margin_px === undefined ? "" : (o.overrides || {}).line_y_margin_px;
  bmInp.placeholder = "默认 60";
  if (document.activeElement !== $("#out-cast")) $("#out-cast").value = o.cast_delay_ms === undefined ? 200 : o.cast_delay_ms;
  if (o.fps === undefined || o.fps === null) o.fps = 60;
  o.fps = fpsChoice(o.fps);
  const sfps = sourceFps(j);
  const srcBtn = $('#out-fps button[data-v="source"]');
  if (srcBtn) {
    srcBtn.textContent = sfps ? `跟随素材（${sfps}）` : "跟随素材";
    srcBtn.disabled = !sfps;
    srcBtn.title = sfps ? `跟随素材帧率：输出 ${sfps} fps（源视频帧率）` : "素材没有视频轨，无法跟随素材帧率";
  }
  $$("#out-fps button").forEach((b) => b.classList.toggle("on", String(b.dataset.v) === String(o.fps)));
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
// "MV 视频" background for audio projects: search + download (Lin-K Lyrics' video downloader) or a local file
function renderMvVideo() {
  const j = S.job; const mvv = (j.media || {}).mv_video || null;
  const cands = ((j.previews || {}).mv_video_candidates || {});
  const song = j.song || {};
  const box = el("div", { class: "mvv" });
  if (mvv) {
    box.append(el("div", { class: "mvv-cur" },
      mvv.still ? el("img", { src: fileUrl(mvv.still) }) : null,
      el("div", {}, el("b", {}, mvv.title || "背景视频"),
        el("div", { class: "muted small" }, [mvv.uploader, mvv.source, mvv.format].filter(Boolean).join(" · ")),
        el("div", { class: "small mvv-notes" + ((mvv.notes || []).some((n) => n.startsWith("⚠")) ? " warn" : "") }, (mvv.notes || []).join("\n")))));
  }
  const q = el("input", { type: "text", class: "grow", value: cands.query || [song.artist, song.title, "MV"].filter(Boolean).join(" "), placeholder: "搜索 YouTube，如「歌手 歌名 MV」" });
  box.append(el("div", { class: "row gap" }, q, el("button", { class: "btn ghost sm", onclick: () => {
    if (!q.value.trim()) return; act("mv_video_search", { query: q.value.trim() }); toast("正在搜索 MV…");
  } }, "搜索 MV")));
  const list = el("div", { class: "mvv-list" });
  for (const c of cands.items || []) {
    list.append(el("div", { class: "mvv-item" + (mvv && mvv.url && mvv.url.includes(c.id) ? " on" : "") },
      el("img", { src: c.thumb_local ? fileUrl(c.thumb_local) : c.thumb, referrerpolicy: "no-referrer" }),
      el("div", { class: "mvv-meta" }, el("b", {}, c.title || c.id),
        el("span", { class: "muted small" }, [c.uploader, c.duration ? fmtT(c.duration, false) : "", c.views ? c.views.toLocaleString() + " 次播放" : ""].filter(Boolean).join(" · "))),
      el("button", { class: "btn ghost sm", onclick: () => {
        if (!confirm(`下载「${c.title}」（${c.uploader || ""}，${c.duration ? fmtT(c.duration, false) : ""}）并设为背景？\n只用它的画面，会自动对齐到歌曲。`)) return;
        act("mv_video_use", { url: c.url }); toast("正在下载 MV 并对齐到歌曲…");
      } }, "使用")));
  }
  if ((cands.items || []).length) box.append(list);
  const loc = el("input", { type: "text", class: "grow", placeholder: "或使用本机视频文件路径，如 D:\\video\\mv.mp4" });
  box.append(el("div", { class: "row gap" }, loc, el("button", { class: "btn ghost sm", onclick: () => {
    const p = loc.value.trim().replace(/^"|"$/g, ""); if (!p) return; act("mv_video_local", { path: p }); toast("正在对齐本机视频…");
  } }, "使用本机视频")));
  box.append(el("span", { class: "muted small" }, "只取视频画面，音频始终用你的歌曲文件（无损时 on / off vocal 都是无损源）。"));
  return box;
}

// "OPED 拼接" background: segments added and stitched by the agent (KM oped); the page only shows them
function renderVisualizeToggle() {
  // 乐曲可视化是独立的一层：视频 / 混剪 / OPED 三种背景都能开。
  // 混剪 / 可视化设计里它是设计的一部分；视频 / OPED 上由背景预处理叠加上去，
  // 处理方式默认按素材质量自动决定（高 → 直接叠加，中 → 背景变淡，低 → 模糊毛玻璃）。
  const o = S.d1.options;
  const bg = o.background || {};
  const cur0 = bgKind(bg.type);
  const design = cur0 === "mv" || cur0 === "montage";
  const on = design ? bg.visualize !== false : bg.visualize === true;
  const box = el("div", { class: "row gap", style: { alignItems: "center" } });
  const ctl = el("div", { class: "seg-ctl" });
  for (const [v, label] of [[true, "开"], [false, "关"]]) {
    ctl.append(el("button", { class: on === v ? "on" : "", onclick: () => {
      setBackground(Object.assign({}, bg, { visualize: v }));
      toast(v ? "已开启乐曲可视化（叠加在背景上）" : "已关闭乐曲可视化");
    } }, label));
  }
  box.append(el("span", { class: "muted small", title: "频谱 / 波形，叠加在背景画面上" }, "频谱可视化"), ctl);
  if (bg.visualize === true && !design) {
    const cur = bg.visualize_mode || "auto";
    const c2 = el("div", { class: "seg-ctl" });
    for (const [v, label, tip] of [["auto", "按素材质量自动", "按背景素材的分辨率 / 帧率自动决定"],
                                   ["overlay", "直接叠加", "可视化图案直接叠在画面上"],
                                   ["dim", "背景变淡", "整幅背景压暗一点，凸显可视化"],
                                   ["blur", "毛玻璃", "只模糊可视化图案覆盖到的区域，其余画面保持清晰"]]) {
      c2.append(el("button", { class: cur === v ? "on" : "", title: tip, onclick: () => {
        setBackground(Object.assign({}, bg, { visualize_mode: v }));
        if (v !== "auto") toast("叠加方式：" + label);
      } }, label));
    }
    box.append(el("span", { class: "muted small" }, "叠加方式"), c2);
  }
  return box;
}

function renderOped() {
  const j = S.job; const op = (j.media || {}).oped || null;
  const box = el("div", { class: "mvv" });
  if (!op || !(op.segments || []).length) {
    box.append(el("span", { class: "muted small" }, "由 agent 用 KM oped 命令添加并拼接 OP/ED 视频"));
    return box;
  }
  const D = (j.media.source || {}).duration || 0;
  const card = el("div", { class: "mvv-cur" },
    op.still ? el("img", { src: fileUrl(op.still) }) : null,
    el("div", {}, el("b", {}, op.video ? "OP/ED 拼接背景已生成" : "尚未拼接"),
      ...op.segments.map((s) => {
        const a = Math.max(0, s.shift || 0), b = Math.min(D, (s.shift || 0) + (s.duration || 0));
        const conf = s.confidence === null || s.confidence === undefined ? "手动偏移" : `置信 ${Math.round(s.confidence * 100)}%`;
        const shift = s.shift === null || s.shift === undefined ? "未对齐" : `偏移 ${s.shift >= 0 ? "+" : ""}${(+s.shift).toFixed(2)}s`;
        return el("div", { class: "muted small" }, `${s.id} · ${s.label || s.file}：覆盖 ${fmtT(a)}–${fmtT(b)} · ${shift} · ${conf}`);
      }),
      (op.notes || []).length ? el("div", { class: "small mvv-notes" + (op.notes.some((n) => n.startsWith("⚠")) ? " warn" : "") }, op.notes.join("\n")) : null));
  box.append(card);
  return box;
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
  $("#mv-title").textContent = kind === "montage" ? "混剪设计" : "可视化设计";
  $("#mv-sub").textContent = kind === "montage" ? "素材（图包 / 游戏动画片段）按节拍剪辑，可在上方开关乐曲可视化；选一个风格作起点，或在右侧告诉 Agent 想要的效果"
    : "封面 / MV 图 + 频谱风格的乐曲可视化；也可点「混剪」改用素材剪辑，或在右侧告诉 Agent 想要的意象";
  const labels = ["前奏", "主歌", "副歌高潮"];
  const box = $("#mv-stills"); box.innerHTML = "";
  if (mv.need_assets) box.append(el("div", { class: "muted" }, "素材池还是空的：在下方上传图包，或选择让 Agent 上网搜集图片。"));
  else if (!mv.stills) box.append(el("div", { class: "muted" }, kind === "montage" ? "正在准备混剪画面…" : "Agent 正在设计 MV 画面…"));
  (mv.stills || []).forEach((src, k) => box.append(el("figure", {}, el("img", { src: fileUrl(src) }),
    el("figcaption", {}, `${labels[k] || ""} · ${fmtT((mv.times || [])[k], false)}`))));
  // 混剪模式下把可视化预设也列出来：同一模式的两种起点（有素材 → 剪辑打底；没素材 → 封面打底）
  const cat = (pv.mv_catalog || []).filter((p) => kind === "montage" || (p.kind || "mv") === kind);
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
const ORIGIN_LABEL = { user: "图包", web: "网络", video: "视频", clip: "片段" };
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
    ? `${plan.bpm ? Math.round(plan.bpm) + " BPM · " : ""}${plan.shots.length} 个镜头（平均 ${avg.toFixed(1)} 秒一切） · 素材 ${pool.length} 个（${mix}），用到 ${plan.unique_used} 个` +
      (plan.repeated_sections ? ` · ${plan.repeated_sections} 个镜头在重复段落沿用前面的画面` : "")
    : pool.length ? `素材 ${pool.length} 个（${mix}）` : "素材池是空的";
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
      el("button", { class: "del", title: "移除这个素材", onclick: () => { act("mv_asset_remove", { ids: [a.id] }); toast("已移除，正在重新排剪辑并渲染静帧…"); } }, "×"),
      el("figcaption", {}, a.origin === "user" ? (a.name || "图包") : a.origin === "clip" ? (a.name || a.credit || a.id) : (a.credit || a.source || a.id))));
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
  $("#out-font-size").addEventListener("change", (ev) => {
    const v = Math.round(+ev.target.value || 0);
    const ov = { ...(S.d1.options.overrides || {}) };
    if (v >= 40) ov.font_size_px = v; else delete ov.font_size_px;
    if (Object.keys(ov).length) S.d1.options.overrides = ov; else delete S.d1.options.overrides;
    touch("options"); renderOutput();
  });
  $("#out-font-default").addEventListener("click", () => { $("#out-font-size").value = ""; $("#out-font-size").dispatchEvent(new Event("change")); });
  $("#out-bottom-margin").addEventListener("change", (ev) => {
    const raw = ev.target.value.trim();
    const v = Math.round(+raw || 0);
    const ov = { ...(S.d1.options.overrides || {}) };
    if (raw !== "" && v >= 0) ov.line_y_margin_px = v; else delete ov.line_y_margin_px;
    if (Object.keys(ov).length) S.d1.options.overrides = ov; else delete S.d1.options.overrides;
    touch("options"); renderOutput();
  });
  $("#out-bottom-default").addEventListener("click", () => { $("#out-bottom-margin").value = ""; $("#out-bottom-margin").dispatchEvent(new Event("change")); });
  $("#out-style-apply").addEventListener("click", () => {
    const options = Object.assign({}, S.d1.options, { overrides: S.d1.options.overrides || {} });
    act("preview_styles", { only: ["templates", "effects"], options });
    toast("正在按当前字号 / 底部间距重出模板与特效预览…");
  });
  $("#out-cast").addEventListener("change", (ev) => {
    const v = Math.round(+ev.target.value || 0); S.d1.options.cast_delay_ms = v; touch("options");
    act("set_option", { cast_delay_ms: v });
  });
  $$("#out-fps button").forEach((b) => b.addEventListener("click", () => { S.d1.options.fps = b.dataset.v === "source" ? "source" : +b.dataset.v; touch("options"); renderOutput(); }));
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
  renderWavecheck();
}

/* --- 对轴证据：KM wavecheck 的每行时频图（有产物时在阶段二展示） ---------- */
function wcLineLabel(i) {
  const l = S.view && S.view.lines ? S.view.lines.find((x) => x.i === i) : null;
  const txt = l && l.text ? String(l.text).trim().slice(0, 16) : "";
  return txt;
}
function renderWavecheck() {
  const card = $("#card-wavecheck");
  if (!card) return;
  const wc = S.wc;
  if (!wc || !wc.lines || !wc.lines.length) { card.hidden = true; return; }
  card.hidden = false;
  if (S.wcIdx === null || !wc.lines.some((l) => l.i === S.wcIdx)) S.wcIdx = wc.lines[0].i;
  const ent = wc.lines.find((l) => l.i === S.wcIdx);
  const sel = $("#wc-sel");
  sel.innerHTML = "";
  for (const l of wc.lines) {
    const txt = wcLineLabel(l.i);
    sel.append(el("option", { value: l.i }, txt ? `行 ${l.i + 1} · ${txt}` : `行 ${l.i + 1}`));
  }
  sel.value = String(S.wcIdx);
  const img = $("#wc-img");
  const key = ent.f + ":" + wc.rev;
  if (img.dataset.key !== key) {
    img.dataset.key = key;
    img.src = fileUrl(`timing/wavecheck/${ent.f}`) + "?r=" + wc.rev;
  }
  $("#wc-open").href = fileUrl(`timing/wavecheck/${ent.f}`) + "?r=" + wc.rev;
  const pos = wc.lines.findIndex((l) => l.i === S.wcIdx) + 1;
  const v = S.view && S.view.lines ? S.view.lines.find((x) => x.i === S.wcIdx) : null;
  const cap = v && v.start !== null && v.end !== null ? `起 ${fmtT(v.start)} → 止 ${fmtT(v.end)}` : "";
  $("#wc-cap").textContent = `行 ${S.wcIdx + 1} · 第 ${pos}/${wc.lines.length} 张${cap ? " · " + cap : ""}`;
  $("#wc-sum").textContent = `${wc.lines.length} 行有图` + (wc.stale ? " · 图可能已过期" : "");
  $("#wc-stale").hidden = !wc.stale;
}
function bindWavecheck() {
  const step = (d) => {
    const wc = S.wc; if (!wc || !wc.lines.length) return;
    const pos = Math.max(0, wc.lines.findIndex((l) => l.i === S.wcIdx));
    S.wcIdx = wc.lines[clamp(pos + d, 0, wc.lines.length - 1)].i;
    renderWavecheck();
  };
  $("#wc-prev").addEventListener("click", () => step(-1));
  $("#wc-next").addEventListener("click", () => step(1));
  $("#wc-sel").addEventListener("change", () => { S.wcIdx = +$("#wc-sel").value; renderWavecheck(); });
  $("#wc-refresh").addEventListener("click", async () => {
    try { applyPayload(await api("/api/job")); toast("已刷新对轴证据"); } catch (e) { toast("刷新失败：" + e.message); }
  });
  $("#wc-regen").addEventListener("click", async () => {
    await act("prompt", { text: "请重新运行 KM wavecheck，刷新本工程的对轴时频图证据（时间轴有改动）", stage: 2 });
    toast("已请求 Agent 重新生成");
  });
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
  const fine = playhead && S.viewing === 3 ? fineLine() : null;
  let x0 = 0, x1 = dur;
  if (playhead && S.zoom) { x0 = S.zoom[0]; x1 = S.zoom[1]; }
  if (!(x1 > x0)) x1 = x0 + 1;
  const X = (t) => ((t - x0) / (x1 - x0)) * w;
  ctx.fillStyle = "#12162a"; ctx.fillRect(0, 0, w, h);
  if (fine) {
    drawFineTrack(ctx, w, h, dpr, X, x0, x1, fine);
  } else {
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
  }
  if (playhead && (S.ab.a !== null || S.ab.b !== null)) {
    if (S.ab.a !== null && S.ab.b !== null && S.ab.a < S.ab.b) {
      ctx.fillStyle = "rgba(124,92,255,.13)"; ctx.fillRect(X(S.ab.a), 0, X(S.ab.b) - X(S.ab.a), h);
      for (const [t, lab] of [[S.ab.a, "A"], [S.ab.b, "B"]]) {
        ctx.fillStyle = "#7c5cff"; ctx.fillRect(X(t), 0, 1.5 * dpr, h);
        ctx.font = `${9 * dpr}px Consolas, monospace`; ctx.fillStyle = "#b9a8ff";
        ctx.fillText(lab, X(t) + 3 * dpr, h - 3 * dpr);
      }
    }
  }
  if (playhead) { const vid = $("#s3-video"); const t = vid ? vid.currentTime : 0; ctx.fillStyle = "#fff"; ctx.fillRect(X(t), 0, 1.5 * dpr, h); }
  if (live && S.job.segment) { ctx.fillStyle = "rgba(0,0,0,.35)"; ctx.fillRect(0, 0, X(S.job.segment.start), h); ctx.fillRect(X(S.job.segment.end), 0, w - X(S.job.segment.end), h); }
}

/* 细调轨泳道（CSS 像素）：标签 / 上下文轨（上行收尾·下行起唱）/ 整行条 / 词轨 / 字块轨 */
const FINE_L = { lab: 12, gh0: 13, gh1: 26, bar0: 29, bar1: 42, w0: 45, w1: 57, by0: 60, tail: 15 };

/* 细调轨：上下文轨（拖粉标=上行收尾、拖紫标=平移下行）+ 整行条（边缘把手=行首/收尾）+ 词轨（拖=平移整词）
   + 字块（拖=平移该字）+ 检查点把手 + 换气点旗标 */
function drawFineTrack(ctx, w, h, dpr, X, x0, x1, l) {
  drawPeaks(ctx, w, h, x0, x1, "rgba(110,120,165,.35)", 0, h);
  drawTimeGrid(ctx, w, h, dpr, X, x0, x1, l);
  const edit = S.edit || {};
  const sel = edit.chars || new Set();
  const vid = $("#s3-video");
  const t = vid ? vid.currentTime : 0;
  const hCss = h / dpr;
  const M = (y) => y * dpr;
  const [prev, next] = fineNeighbors(l);
  // —— 上下文轨 ——
  ctx.font = `${9.5 * dpr}px Consolas, monospace`;
  if (prev) {
    const gcol = singerColor(S.view, prev.singer);
    ctx.fillStyle = gcol + "3d";
    ctx.fillRect(X(prev.start), M(FINE_L.gh0 + 2), Math.max(1, X(prev.end) - X(prev.start)), M(9));
    const pk = lastSeIdx(prev);
    if (pk !== null) {   // 上行收尾把手：拖 = 改上行释放时间
      const hx = X(prev.chars[pk].e ?? prev.end);
      const hot = !!tlHover && tlHover.kind === "ghost-prev";
      ctx.fillStyle = hot ? "#ffd34d" : "#ff5fa2";
      ctx.fillRect(hx - (hot ? 1.5 : 1) * dpr, M(FINE_L.gh0 + 1), (hot ? 3 : 2) * dpr, M(11));
      ctx.fillRect(hx - (hot ? 4 : 3) * dpr, M(FINE_L.gh1 - 6), (hot ? 8 : 6) * dpr, 5 * dpr);
      ctx.fillStyle = "rgba(255,190,220,.9)";
      ctx.fillText(`▲ 上行收尾 ${fmtT(prev.end)}`, hx + 5 * dpr, M(FINE_L.gh1 - 7));
    }
  }
  if (next) {
    const gcol = singerColor(S.view, next.singer);
    ctx.fillStyle = gcol + "3d";
    ctx.fillRect(X(next.start), M(FINE_L.gh0 + 2), Math.max(1, X(next.end) - X(next.start)), M(9));
    const hx = X(next.start);
    const hotN = !!tlHover && tlHover.kind === "ghost-next";
    ctx.fillStyle = hotN ? "#ffd34d" : "#7c5cff";
    ctx.fillRect(hx - (hotN ? 2.5 : 1.5) * dpr, M(FINE_L.gh0 + 1), (hotN ? 5 : 3) * dpr, M(11));
    ctx.fillStyle = "rgba(200,185,255,.95)";
    ctx.fillText(`▼ 下行起唱 ${fmtT(next.start)}`, hx + 5 * dpr, M(FINE_L.gh1 - 7));
    if (next.start > l.end && X(next.start) - X(l.end) > 24 * dpr) {   // 行距读出
      const gx = (X(l.end) + X(next.start)) / 2;
      ctx.fillStyle = "rgba(255,255,255,.45)";
      ctx.fillText(`+${Math.round((next.start - l.end) * 1000)}ms`, gx - 14 * dpr, M(FINE_L.gh0 + 8));
    }
  }
  // —— 整行条（拖条＝整行平移；两端宽把手＝拉长 / 缩短行首 / 行尾）——
  const rowY = M(FINE_L.bar0), rowH = M(13);
  const col = singerColor(S.view, l.singer);
  const rx0 = X(l.start), rx1 = X(l.end);
  const focus = edit.focus || "char";
  const hovRow = !!tlHover && tlHover.kind === "rowbar";
  const hovEL = !!tlHover && tlHover.kind === "edge-l";
  const hovER = !!tlHover && tlHover.kind === "edge-r";
  if (focus === "row") {   // 调句子模式：整条轨道加底，突出当前操作层
    ctx.fillStyle = "rgba(255,255,255,.045)";
    ctx.fillRect(0, rowY - 4 * dpr, w, rowH + 8 * dpr);
  }
  ctx.fillStyle = col + (hovRow || focus === "row" ? "ee" : "bb");
  ctx.fillRect(rx0, rowY, Math.max(2, rx1 - rx0), rowH);
  const hw = 5 * dpr, hh = rowH + 6 * dpr;
  ctx.fillStyle = hovEL ? "#ffd34d" : "rgba(255,255,255,.92)";
  ctx.fillRect(rx0 - hw / 2, rowY - 3 * dpr, hw, hh);
  ctx.fillStyle = hovER ? "#ffd34d" : "rgba(255,255,255,.92)";
  ctx.fillRect(rx1 - hw / 2, rowY - 3 * dpr, hw, hh);
  ctx.font = `${10 * dpr}px Consolas, monospace`; ctx.fillStyle = "rgba(255,255,255,.85)";
  ctx.fillText(`行 ${l.i + 1}`, Math.max(2, rx0 - 36 * dpr), rowY + 10 * dpr);
  ctx.fillStyle = "rgba(255,255,255,.6)";
  ctx.fillText(`${fmtT(l.start)} → ${fmtT(l.end)}`, rx1 + 8 * dpr, rowY + 10 * dpr);
  // —— 词轨 ——
  const wy0 = M(FINE_L.w0), wy1 = M(FINE_L.w1);
  const wyb = wy0 + (wy1 - wy0) * 0.72;
  fineWords(l).forEach((w) => {
    const ti = wordTimed(l, w);
    if (!ti) return;
    const a = l.chars[ti[0]].s;
    const b = Math.max(...ti.map((i) => l.chars[i].e));
    const wx0 = X(a), wx1 = Math.max(wx0 + 3 * dpr, X(b));
    const selOn = w.length > 1 && w.every((i) => sel.has(i));
    ctx.fillStyle = selOn ? "rgba(53,208,255,.16)" : "rgba(255,255,255,.05)";
    ctx.fillRect(wx0, wy0, wx1 - wx0, wy1 - wy0);
    ctx.strokeStyle = selOn ? "rgba(53,208,255,.9)" : "rgba(255,255,255,.18)";
    ctx.lineWidth = 1 * dpr;
    ctx.strokeRect(wx0 + 0.5, wy0 + 0.5, Math.max(1, wx1 - wx0) - 1, wy1 - wy0 - 1);
    const cw = wx1 - wx0;
    if (w.length > 1 && cw > 10 * dpr) {   // 词内字界线：暗示可拆分
      for (let k = 1; k < w.length; k++) {
        const c = l.chars[w[k]];
        if (c.s === null) continue;
        ctx.strokeStyle = "rgba(255,255,255,.10)";
        ctx.beginPath(); ctx.moveTo(X(c.s), wy0 + 1); ctx.lineTo(X(c.s), wy1 - 1); ctx.stroke();
      }
    }
    if (cw > 16 * dpr) {
      const txt = wordText(l, w), ruby = wordRuby(l, w);
      ctx.font = `600 ${10 * dpr}px "Yu Gothic UI", "Meiryo", "Microsoft YaHei UI", sans-serif`;
      ctx.fillStyle = selOn ? "#fff" : "rgba(233,236,248,.88)";
      ctx.fillText(txt, wx0 + 3 * dpr, wyb);
      const tw = ctx.measureText(txt).width;
      if (ruby && cw - tw > 34 * dpr) {
        ctx.font = `${9 * dpr}px "Yu Gothic UI", "Meiryo", "Microsoft YaHei UI", sans-serif`;
        ctx.fillStyle = "rgba(159,227,255,.8)";
        ctx.fillText(ruby, wx0 + 6 * dpr + tw, wyb);
      }
    }
  });
  // —— 字块轨（按注音分拍：段条 + 圆点把手；拖圆点 = 调这一拍的时刻）——
  const by0 = M(FINE_L.by0), by1 = h - M(FINE_L.tail);
  const bh0 = by1 - by0;
  const segH = Math.min(17 * dpr, bh0 * 0.32);   // 注音段条高度
  l.chars.forEach((c, i) => {
    if (c.s === null || !c.cp.length) return;
    const bx0 = X(c.s), bx1 = Math.max(bx0 + 1, X(c.e));
    const selOn = sel.has(i);
    ctx.fillStyle = selOn ? "rgba(53,208,255,.13)" : "rgba(255,255,255,.045)";
    ctx.fillRect(bx0, by0, bx1 - bx0, bh0);
    const prog = charProgress(c, t);
    if (prog) {   // 已唱进度：与走字预览呼应的紫底
      ctx.fillStyle = "rgba(124,92,255,.22)";
      ctx.fillRect(bx0, by0, (bx1 - bx0) * prog, bh0);
    }
    ctx.strokeStyle = selOn ? "rgba(53,208,255,.9)" : "rgba(255,255,255,.16)";
    ctx.lineWidth = 1 * dpr;
    ctx.strokeRect(bx0 + 0.5, by0 + 0.5, Math.max(1, bx1 - bx0) - 1, bh0 - 1);
    const bw = bx1 - bx0;
    // 按注音切分：拍数与注音段数一致时，每段一个格子（显示该段假名 / 停顿）
    const parts = c.r || [];
    const segOn = parts.length > 1 && c.cp.length === parts.length;
    const textBottom = segOn ? by1 - segH : by1;
    if (bw > 14 * dpr) {
      const fs = Math.min(22 * dpr, bw * 0.58, (textBottom - by0) * 0.5);
      ctx.font = `600 ${fs}px "Yu Gothic UI", "Meiryo", "Microsoft YaHei UI", sans-serif`;
      ctx.fillStyle = selOn ? "#fff" : "rgba(233,236,248,.88)";
      ctx.fillText(c.c.trim() ? c.c : "␣", bx0 + bw / 2 - ctx.measureText(c.c).width / 2, by0 + (textBottom - by0) * 0.62);
    }
    if (segOn) {
      for (let k = 0; k < parts.length; k++) {
        const a = c.cp[k];
        const b = k + 1 < parts.length ? Math.max(c.cp[k + 1], a) : Math.max(c.e ?? a, a);
        const sx0 = Math.max(bx0, X(a)), sx1 = Math.max(sx0 + 1, Math.min(bx1, X(b)));
        const on = t >= a && t < b;   // 正在唱的这一拍
        ctx.fillStyle = on ? "rgba(53,208,255,.30)" : "rgba(53,208,255,.09)";
        ctx.fillRect(sx0, by1 - segH + 1 * dpr, sx1 - sx0, segH - 2 * dpr);
        ctx.strokeStyle = "rgba(53,208,255,.30)";
        ctx.lineWidth = dpr;
        ctx.strokeRect(sx0 + 0.5, by1 - segH + 1 * dpr + 0.5, Math.max(1, sx1 - sx0) - 1, segH - 2 * dpr - 1);
        if (sx1 - sx0 > 11 * dpr && parts[k]) {
          ctx.font = `${9 * dpr}px "Yu Gothic UI", "Meiryo", "Microsoft YaHei UI", sans-serif`;
          ctx.fillStyle = on ? "#dff5ff" : "rgba(159,227,255,.72)";
          ctx.fillText(parts[k], (sx0 + sx1) / 2 - ctx.measureText(parts[k]).width / 2, by1 - segH * 0.28);
        }
      }
    }
    // 检查点把手（每个注音拍一个圆点；拖 = 调这一拍的时刻）
    ctx.globalAlpha = focus === "row" ? 0.45 : 1;   // 调句子模式：淡化拍点，聚焦行条
    c.cp.forEach((ct, k) => {
      const px = X(ct);
      const active = selOn && edit.cp && edit.cp[0] === i && edit.cp[1] === k;
      const hovCp = !!tlHover && tlHover.kind === "cp" && tlHover.char === i && tlHover.cp === k;
      const hot = active || hovCp;
      const colr = hot ? "#ffffff" : (k === 0 ? "#9fe3ff" : "#35d0ff");
      ctx.strokeStyle = colr;
      ctx.lineWidth = (hot ? 2 : 1) * dpr;
      ctx.beginPath(); ctx.moveTo(px, by0 - 5 * dpr); ctx.lineTo(px, by1 + 5 * dpr); ctx.stroke();
      const rr = (hot ? 4.6 : 3.4) * dpr;
      ctx.fillStyle = colr;
      ctx.beginPath(); ctx.arc(px, by0 - 8 * dpr, rr, 0, Math.PI * 2); ctx.fill();
    });
    ctx.globalAlpha = 1;
    if (c.se) {   // 换气点旗标（可拖）
      const px = X(c.e);
      const hovSe = !!tlHover && tlHover.kind === "se" && tlHover.char === i;
      ctx.fillStyle = hovSe ? "#ffd34d" : "#ff5fa2";
      ctx.beginPath();
      ctx.moveTo(px, by1 + 1 * dpr); ctx.lineTo(px - 5 * dpr, by1 + 11 * dpr); ctx.lineTo(px + 5 * dpr, by1 + 11 * dpr);
      ctx.closePath(); ctx.fill();
    }
  });
}

function drawTimeGrid(ctx, w, h, dpr, X, x0, x1, l) {
  const span = x1 - x0;
  const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
  const step = steps.find((s) => (s / span) * w >= 66 * dpr) || 600;
  ctx.font = `${9.5 * dpr}px Consolas, monospace`;
  const t0 = Math.ceil(x0 / step) * step;
  for (let t = t0; t <= x1 + 1e-9; t += step) {
    const px = X(t);
    ctx.strokeStyle = "rgba(255,255,255,.07)";
    ctx.lineWidth = dpr;
    ctx.beginPath(); ctx.moveTo(px, 15 * dpr); ctx.lineTo(px, h); ctx.stroke();
    ctx.fillStyle = "rgba(255,255,255,.45)";
    ctx.fillText(step < 1 ? `${(Math.round(t * 1000) / 1000).toFixed(step < 0.1 ? 3 : step < 0.5 ? 2 : 1)}s` : fmtT(t), px + 3 * dpr, 11 * dpr);
  }
}

/* ================================================================== STAGE 3 */
let s3Bound = false;

/* ------------------------------------------------------------ 细调（字符级时间轴）
 * 选中一行后时间轴放大到该行：[上] 整行条（拖=平移整行）· [中] 字符块（拖=平移该字）
 * · 块内竖线 = 检查点（拖=微调）· 换气点旗标（拖=改释放时间）。键盘：
 * ←→ 跳字 · ↑↓ 跳行 · Alt+↑↓ 微调 ∓10ms（加 Shift 为 ±50ms）· = / − 节奏点 · P 换气
 * · [ ] 循环区间 · , . 逐帧。所有编辑短时间合并成一次提交（一次撤销快照）。
 */
S.ab = { a: null, b: null };   // A/B 循环区间（秒）
let tlDrag = null;             // 进行中的时间轴拖动
let tlHover = null;            // 时间轴悬停命中（高亮 + 光标 + 提示）
let hintBase = "";             // 常规提示（syncFineBar 生成；悬停 / 拖动提示临时覆盖）
let finePending = new Map();   // 待提交的编辑（key 去重；平移类累加）
let fineTimer = null;

function fineLine() { return S.edit && S.view ? S.view.lines[S.edit.line] : null; }
function fineFps() {
  const f = (S.job.options || {}).fps;
  const n = typeof f === "number" ? f : parseFloat(f);
  return n > 0 ? n : (sourceFps(S.job) || 60);
}
const r3 = (v) => Math.round(v * 1000) / 1000;

/* 上下相邻的已打轴行（词/句间精调的上下文） */
function fineNeighbors(l) {
  const v = S.view; let prev = null, next = null;
  if (!v || !l) return [prev, next];
  for (let k = l.i - 1; k >= 0; k--) { const p = v.lines[k]; if (p && p.start !== null) { prev = p; break; } }
  for (let k = l.i + 1; k < v.lines.length; k++) { const p = v.lines[k]; if (p && p.start !== null) { next = p; break; } }
  return [prev, next];
}
/* 词 = linked_to_next 连词链（与引擎 / ruby 呈现同一分组口径） */
function fineWords(l) {
  const out = []; let cur = [];
  l.chars.forEach((c, i) => { cur.push(i); if (!c.link) { out.push(cur); cur = []; } });
  if (cur.length) out.push(cur);
  return out;
}
function wordTimed(l, w) {
  const idxs = w.filter((i) => l.chars[i] && l.chars[i].s !== null && l.chars[i].cp.length);
  return idxs.length ? idxs : null;
}
function wordText(l, w) { return w.map((i) => l.chars[i].c).join(""); }
function wordRuby(l, w) { return w.map((i) => (l.chars[i].r || []).join("")).join(""); }
function wordAtX(l, geo, cx) {
  for (const w of fineWords(l)) {
    const ti = wordTimed(l, w); if (!ti) continue;
    const xa = geo.px(l.chars[ti[0]].s), xb = geo.px(Math.max(...ti.map((i) => l.chars[i].e)));
    if (cx >= xa - 2 && cx <= xb + 2) return w;
  }
  return null;
}
function lastTimedIdx(l) { for (let i = l.chars.length - 1; i >= 0; i--) if (l.chars[i].s !== null && l.chars[i].cp.length) return i; return null; }
function lastSeIdx(l) { for (let i = l.chars.length - 1; i >= 0; i--) if (l.chars[i].se) return i; return null; }
function firstTimedIdx(l) { return l.chars.findIndex((c) => c.s !== null && c.cp.length); }

function clampFineZoom(a, b) {
  const dur = mediaDuration();
  const lo = clamp(a, 0, dur), hi = clamp(b, lo + 0.05, dur);
  return hi - lo < 0.25 ? [lo, Math.min(dur, lo + 0.25)] : [lo, hi];
}

function enterFine(i, opts = {}) {
  const l = S.view && S.view.lines[i];
  if (!l || l.start === null) { toast("这一行还没有时间轴"); return; }
  flushEdits();
  const focus = opts.focus === "row" ? "row" : "char";
  S.edit = { line: i, focus, chars: new Set(focus === "row" ? [] : (opts.chars || [0])), cp: opts.cp || [0, 0] };
  if (focus === "char") {
    const n = l.chars.length;
    if ([...S.edit.chars].some((c) => c >= n || c < 0)) S.edit.chars = new Set([0]);
    if (!S.edit.chars.size || !S.edit.chars.has(S.edit.cp[0])) S.edit.cp = [Math.min(...S.edit.chars), 0];
  }
  if (!opts.keepZoom) {
    const span = Math.max(0.4, l.end - l.start);
    let lo = l.start - Math.min(1, span * 0.3);
    let hi = l.end + Math.min(0.8, span * 0.25);
    const [prev, next] = fineNeighbors(l);   // 邻近行较近时一并纳入窗口：句间把手可直接拖
    if (prev && prev.end < l.start && l.start - prev.end < 2.2) lo = Math.min(lo, prev.end - 0.25);
    if (next && next.start > l.end && next.start - l.end < 2.2) hi = Math.max(hi, next.start + 0.25);
    S.zoom = clampFineZoom(lo, hi);
  }
  renderLineTable(); renderSelBar(); syncFineBar();
  drawTimeline($("#s3-timeline"), { playhead: true });
}
function exitFine() {
  if (!S.edit) return;
  flushEdits();
  S.edit = null; S.zoom = null;
  renderLineTable(); renderSelBar(); syncFineBar();
  drawTimeline($("#s3-timeline"), { playhead: true });
}

/* 本地重算一行字符的窗口（与后端 char_windows 同规则），拖动时保持走字预览实时 */
function reflowLineChars(l) {
  const chars = l.chars;
  const timed = (c) => c.cp && c.cp.length;
  let lineEnd = null;
  for (const c of chars) {
    if (c.se && c.e !== null) lineEnd = Math.max(lineEnd ?? 0, c.e);
    if (timed(c)) lineEnd = Math.max(lineEnd ?? 0, c.cp[c.cp.length - 1]);
  }
  chars.forEach((c, i) => {
    if (!timed(c)) { c.s = null; c.e = null; return; }
    const start = c.cp[0];
    let e;
    if (c.se) e = c.e !== null ? c.e : start + 0.4;
    else {
      let nxt = null;
      for (let j = i + 1; j < chars.length; j++) if (timed(chars[j])) { nxt = chars[j].cp[0]; break; }
      e = nxt !== null ? nxt : (lineEnd !== null && lineEnd > start ? lineEnd : start + 0.4);
    }
    c.s = start;
    c.e = Math.max(e, c.cp[c.cp.length - 1]);
  });
  chars.forEach((c, i) => {   // 无时间戳的字吸附到相邻边界
    if (c.s !== null) return;
    let prev = null, nxt = null;
    for (let j = i - 1; j >= 0; j--) if (chars[j].e !== null) { prev = chars[j].e; break; }
    for (let j = i + 1; j < chars.length; j++) if (chars[j].s !== null) { nxt = chars[j].s; break; }
    const t = prev !== null ? prev : nxt;
    c.s = t; c.e = t;
  });
  const first = chars.find((c) => c.s !== null);
  l.start = first ? first.s : null;
  l.end = chars.reduce((m, c) => (c.e !== null ? Math.max(m ?? -1e9, c.e) : m), null);
}

/* 检查点 k 的拖动/微调边界：与前一点 ≥20ms，不越过下一个字起点 */
function cpBounds(chars, ci, k) {
  const c = chars[ci]; const v = c.cp;
  let lo = 0, hi = Infinity;
  if (k > 0) lo = v[k - 1] + 0.02;
  else for (let j = ci - 1; j >= 0; j--) if (chars[j].cp && chars[j].cp.length) { lo = chars[j].cp[chars[j].cp.length - 1] + 0.02; break; }
  if (k < v.length - 1) hi = v[k + 1] - 0.02;
  else for (let j = ci + 1; j < chars.length; j++) if (chars[j].cp && chars[j].cp.length) { hi = chars[j].cp[0] - 0.02; break; }
  return [lo, hi];
}

/* 提交队列：短时间内的编辑合并为一次 edit_timing（平移类按 key 累加增量） */
let opSeq = 0;
function opKey(op) {
  if (op.op === "set_char") return `c:${op.line}:${op.char}:${op.cp || 0}`;
  if (op.op === "set_pause") return `p:${op.line}:${op.char}`;
  if (op.op === "shift_chars") return `sc:${op.line}:${(op.chars || [0, 0]).join("-")}`;
  if (op.op === "shift_lines") return `sl:${(op.lines || [op.line]).join("-")}`;
  if (op.op === "set_check_count") return `k:${op.line}:${op.char}`;
  if (op.op === "add_checkpoint") return `a:${op.line}:${op.char}:${op.t}`;
  if (op.op === "remove_checkpoint") return `rc:${op.line}:${op.char}:${++opSeq}`;  // 每次都不合并
  if (op.op === "set_word_links") return `wl:${op.line}:${(op.chars || []).join("-")}`;
  if (op.op === "set_word_ruby") return `wr:${op.line}:${(op.chars || []).join("-")}`;
  if (op.op === "set_line_end") return `le:${op.line}`;
  return JSON.stringify(op);
}
function queueOps(ops) {
  S.editSeq++;
  noteExpect(ops);  // 记录期望：任何刷新体都必须已经包含这次本地编辑
  for (const op of ops) {
    const key = opKey(op);
    const cur = finePending.get(key);
    if (cur && (op.op === "shift_chars" || op.op === "shift_lines")) cur.ms += op.ms;
    else finePending.set(key, { ...op });
  }
  clearTimeout(fineTimer);
  fineTimer = setTimeout(flushEdits, 700);
}
/* 提交时冻结受影响字的期望快照：刷新体必须包含它们，否则视为过期（见 loadTimingView） */
function expectTargets(op) {
  const out = [];
  const line = op.line !== undefined ? op.line : (op.lines ? op.lines[0] : null);
  if (line === null || line === undefined || !S.view) return out;
  const l = S.view.lines[line];
  if (!l) return out;
  if (op.op === "shift_lines") l.chars.forEach((c, i) => out.push([line, i]));
  else if (op.op === "shift_chars") {
    const [a, b] = op.chars || [0, l.chars.length - 1];
    for (let i = a; i <= b && i < l.chars.length; i++) out.push([line, i]);
  } else if (op.op === "set_word_links" || op.op === "set_word_ruby") {
    const [a, b] = op.chars || [0, l.chars.length - 1];
    for (let i = a; i <= b && i < l.chars.length; i++) out.push([line, i]);
  } else if (op.op === "set_line_end") {
    for (let i = l.chars.length - 1; i >= 0; i--) if (l.chars[i].se) { out.push([line, i]); break; }
  } else if (op.char !== undefined) out.push([line, op.char]);
  return out;
}
function noteExpect(ops) {
  for (const op of ops) {
    for (const [line, ci] of expectTargets(op)) {
      const c = S.view.lines[line].chars[ci];
      if (!c) continue;
      S.expects.set(`${line}:${ci}`, { line, char: ci, cp: [...c.cp], se: !!c.se, at: Date.now() });
    }
  }
}
function flushEdits() {
  clearTimeout(fineTimer); fineTimer = null;
  if (!finePending.size) return;
  const ops = [...finePending.values()].filter((o) => !("ms" in o && !o.ms));
  finePending.clear();
  if (ops.length) {
    S.tlHold = Date.now() + 5000;
    S.flushSeq++;
    S.flushRev = (S.job.timing || {}).revision ?? null;
    S.flushAt = Date.now();
    act("edit_timing", { ops });
  }
}

/* 拖动 / 悬停反馈统一走 tl-bar 提示行（不在鼠标旁弹浮动数字） */
function setHint(t) { const h = $("#tl-hint"); if (h) h.textContent = t; }
function hintDefault() { setHint(hintBase); }
function cursorForHit(hit) {
  if (!hit) return "default";
  switch (hit.kind) {
    case "cp": case "se": case "edge-l": case "edge-r":
    case "ghost-prev": case "ghost-next": return "ew-resize";
    case "rowbar": case "block": case "word": return "grab";
    default: return "crosshair";
  }
}
function tlHoverInfo(l, hit) {
  if (!hit || hit.kind === "bg") return null;
  if (hit.kind === "cp") {
    const c = l.chars[hit.char]; const part = (c.r || [])[hit.cp] || "";
    return `「${part || (c.c.trim() ? c.c : "␣")}」第 ${hit.cp + 1}/${c.cp.length} 拍 · ${c.cp[hit.cp].toFixed(3)}s — 拖动＝调整这一拍`;
  }
  if (hit.kind === "block") { const c = l.chars[hit.char]; return `字「${c.c.trim() ? c.c : "␣"}」— 拖动＝平移这个字`; }
  if (hit.kind === "se") return `换气点 ${fmtT(l.chars[hit.char].e)} — 拖动＝改释放时间`;
  if (hit.kind === "edge-l") return `行首起唱 ${fmtT(l.start)} — 拖动＝拉长 / 缩短行首`;
  if (hit.kind === "edge-r") return `行尾收尾 ${fmtT(l.end)} — 拖动＝拉长 / 缩短行尾`;
  if (hit.kind === "rowbar") return `整行 ${fmtT(l.start)} → ${fmtT(l.end)}（${(l.end - l.start).toFixed(2)}s）— 拖动＝整体平移`;
  if (hit.kind === "word") {
    const w = hit.word; const r = wordRuby(l, w);
    return `词「${wordText(l, w)}${r ? " " + r : ""}」— 拖动＝整词平移 · 双击＝改读音 / 合并 / 拆分`;
  }
  if (hit.kind === "ghost-prev") return "上行收尾 — 拖动＝改上一行的释放时间";
  if (hit.kind === "ghost-next") { const n2 = fineNeighbors(l)[1]; return `下行起唱 ${n2 ? fmtT(n2.start) : ""} — 拖动＝平移下一行`; }
  return null;
}
function updateTlHover(ev) {
  const tl = $("#s3-timeline"); if (!tl) return;
  const r = tl.getBoundingClientRect();
  const inside = r.width > 0 && ev.clientX >= r.left && ev.clientX <= r.right && ev.clientY >= r.top - 4 && ev.clientY <= r.bottom + 4;
  let hit = null;
  if (inside) { const l = fineLine(); hit = l ? tlHitFine(l, tlGeo(tl), ev) : null; }
  const same = (!hit && !tlHover) || !!hit && !!tlHover && hit.kind === tlHover.kind && hit.char === tlHover.char && hit.cp === tlHover.cp
    && String(hit.word) === String(tlHover.word);
  if (same) return;
  tlHover = hit;
  tl.style.cursor = cursorForHit(hit);
  const l = fineLine();
  const info = l ? tlHoverInfo(l, hit) : null;
  if (info) setHint(info); else hintDefault();
  drawTimeline(tl, { playhead: true });
}

/* 当前聚焦的检查点 [字索引, 检查点索引]（键盘微调 / 节奏点 / 换气点的作用对象） */
function currentCP() {
  const l = fineLine(); if (!l || !S.edit.chars.size) return null;
  const sel = [...S.edit.chars].sort((a, b) => a - b);
  let ci = (S.edit.cp || [sel[0], 0])[0];
  if (!S.edit.chars.has(ci)) ci = sel[0];
  const c = l.chars[ci];
  if (!c || !c.cp || !c.cp.length) return null;
  return [ci, clamp((S.edit.cp || [ci, 0])[1], 0, c.cp.length - 1)];
}

function nudgeCP(ms) {
  const l = fineLine(); const cur = currentCP(); if (!l || !cur) { toast("先点选一个字"); return; }
  const c = l.chars[cur[0]];
  const [lo, hi] = cpBounds(l.chars, cur[0], cur[1]);
  const t = r3(clamp(c.cp[cur[1]] + ms / 1000, lo, hi));
  if (t === c.cp[cur[1]]) return;
  c.cp[cur[1]] = t;
  reflowLineChars(l);
  queueOps([{ op: "set_char", line: S.edit.line, char: cur[0], cp: cur[1], t }]);
  syncFineBar(); drawTimeline($("#s3-timeline"), { playhead: true });
  setHint(`第 ${cur[1] + 1}/${c.cp.length} 拍 · ${t.toFixed(3)}s（${ms > 0 ? "+" : ""}${ms}ms）`);
}

function setCPCount(delta) {
  const l = fineLine(); const cur = currentCP(); if (!l || !cur) { toast("先点选一个字"); return; }
  const c = l.chars[cur[0]];
  if (delta > 0) {
    if (c.cp.length >= 16) return;
    // 新节奏点先落在「末点 → 字尾」的中点，再拖动微调；服务端用相对 op 追加，
    // 本地只做同样的乐观预览（陈旧视图不会把数量改错）。
    const last = c.cp[c.cp.length - 1];
    const t = r3(last + Math.max(0.05, ((c.e ?? last + 0.3) - last) / 2));
    c.cp.push(t);
    S.edit.cp = [cur[0], c.cp.length - 1];
    queueOps([{ op: "add_checkpoint", line: S.edit.line, char: cur[0], t }]);
  } else {
    if (c.cp.length <= 1) return;
    c.cp.pop();
    S.edit.cp = [cur[0], Math.max(0, c.cp.length - 1)];
    queueOps([{ op: "remove_checkpoint", line: S.edit.line, char: cur[0] }]);
  }
  reflowLineChars(l);
  syncFineBar(); drawTimeline($("#s3-timeline"), { playhead: true });
}

function togglePause() {
  const l = fineLine(); const cur = currentCP(); if (!l || !cur) { toast("先点选一个字"); return; }
  const c = l.chars[cur[0]];
  const ops = [];
  if (c.se) {
    c.se = false;
    ops.push({ op: "set_pause", line: S.edit.line, char: cur[0], t: null });
  } else {
    c.se = true;
    c.e = r3(Math.max(c.e ?? c.cp[c.cp.length - 1], c.cp[c.cp.length - 1] + 0.05));
    ops.push({ op: "set_pause", line: S.edit.line, char: cur[0], t: c.e });
  }
  reflowLineChars(l);
  queueOps(ops);
  syncFineBar(); drawTimeline($("#s3-timeline"), { playhead: true });
}

function clearCPs() {   // 清除该字全部节奏点（时间戳丢失，靠撤销兜底）
  const l = fineLine(); const cur = currentCP(); if (!l || !cur) return;
  queueOps([{ op: "set_check_count", line: S.edit.line, char: cur[0], count: 0 }]);
  toast(`已清除第 ${S.edit.line + 1} 行「${l.chars[cur[0]].c}」的节奏点（可撤销）`);
}

/* 均分：把所选字的全部检查点在首尾之间按字宽排布 */
function evenSpread() {
  const l = fineLine(); if (!l) return;
  const sel = [...S.edit.chars].filter((i) => l.chars[i] && l.chars[i].cp.length).sort((a, b) => a - b);
  if (!sel.length) { toast("先选中要均分的字"); return; }
  const wof = (c) => ((c.c || " ").charCodeAt(0) < 0x2e80 ? 0.55 : 1);
  const pts = [];
  let x = 0;
  for (const ci of sel) {
    const c = l.chars[ci]; const w = wof(c);
    for (let k = 0; k < c.cp.length; k++) pts.push({ ci, k, x: x + (w * k) / Math.max(1, c.cp.length) });
    x += w;
  }
  const x0 = l.chars[sel[0]].cp[0];
  const x1 = l.chars[sel[sel.length - 1]].e;
  if (!(x1 > x0)) { toast("这一行的时间范围异常，无法均分"); return; }
  const ops = [];
  for (const p of pts) {
    const t = r3(x0 + (x1 - x0) * (p.x / x));
    l.chars[p.ci].cp[p.k] = t;
    ops.push({ op: "set_char", line: S.edit.line, char: p.ci, cp: p.k, t });
  }
  reflowLineChars(l);
  queueOps(ops);
  syncFineBar(); drawTimeline($("#s3-timeline"), { playhead: true });
  toast(`已按字宽均分 ${sel.length} 个字`);
}

function listenFine() {
  const l = fineLine(); if (!l) return;
  const sel = [...S.edit.chars].sort((a, b) => a - b);
  const c = l.chars[sel[0]]; if (!c || c.s === null) return;
  const vid = $("#s3-video");
  vid.currentTime = Math.max(0, c.s - 0.2);
  vid.play();
}

function syncFineBar() {
  const tools = $("#tl-tools"), exitBtn = $("#tl-exit"), keys = $("#tl-keys"), guide = $("#tl-guide");
  const lbl = $("#tl-lbl"), hint = $("#tl-hint");
  const l = fineLine();
  tools.hidden = !l; exitBtn.hidden = !l; keys.hidden = !l;
  if (guide) guide.hidden = !!l;
  if (!l) {
    lbl.textContent = "时间轴";
    hint.textContent = "";
    hintBase = "";
    return;
  }
  const focus = (S.edit && S.edit.focus) || "char";
  lbl.textContent = (focus === "row" ? "调句子" : "精调") + ` · 第 ${S.edit.line + 1} 行`;
  const sel = [...S.edit.chars].sort((a, b) => a - b);
  const cur = currentCP();
  const multi = sel.length > 1 ? `已选 ${sel.length} 字 · ` : "";
  const wsel = fineWords(l).find((w) => w.length > 1 && w.every((x) => S.edit.chars.has(x)));
  const wpart = wsel ? `词「${wordText(l, wsel)}${wordRuby(l, wsel) ? ` ${wordRuby(l, wsel)}` : ""}」 · ` : "";
  if (cur) {
    const c = l.chars[cur[0]];
    const part = (c.r || [])[cur[1]] || "";
    hint.textContent = `${wpart}${multi}字「${c.c.trim() ? c.c : "␣"}」 · 第 ${cur[1] + 1}/${c.cp.length} 拍` +
      (part ? `「${part}」` : "") + ` · ${c.cp[cur[1]].toFixed(3)}s` + (c.se && cur[1] === c.cp.length - 1 ? " · 换气点" : "");
  } else if (focus === "row") {
    hint.textContent = "拖行条＝平移整行 · 拖两端把手＝拉长 / 缩短 · 粉标＝上行收尾、紫标＝下行起唱 · 点字块转入精调";
  } else {
    hint.textContent = wpart + multi + "点字块 / 词卡选中 · 拖圆点＝调整这一拍的时刻";
  }
  hintBase = hint.textContent;
  $("#tl-pause").classList.toggle("on", !!(cur && l.chars[cur[0]].se));
}

function renderStage3() {
  const j = S.job; const m = j.media;
  if (S.edit && (!S.view || !S.view.lines[S.edit.line])) S.edit = null;   // view 换代时兜底
  const vid = $("#s3-video");
  const bg = (j.options && j.options.background) || {};
  const bk = bgKind(bg.type || ((m.source && m.source.has_video) ? "source" : "mv"));
  const dk = bk === "montage" ? "montage" : bk === "mv" || !(m.source && m.source.has_video) ? "mv" : null;
  let src = m.player;
  const dv = dk && ((m.designs || {})[dk] || {}).video;
  if (dv && bk !== "subs") src = dv;
  const poster = bk === "oped" && (m.oped || {}).still ? m.oped.still
    : dk && (m.design_still || {})[dk] ? (m.design_still || {})[dk] : m.thumb;
  if (vid.dataset.src !== src) { vid.dataset.src = src; vid.src = fileUrl(src); vid.poster = poster && bk !== "subs" ? fileUrl(poster) : ""; }
  vid.style.opacity = bk === "subs" ? 0 : 1;
  $("#s3-player").style.background = bk === "subs" ? (bg.color || "#000") : "";
  const ef = (j.previews || {}).engine_frame;
  if (S.engineRev === undefined) S.engineRev = ef ? ef.rev : null; // don't pop up a frame from a previous session
  if (ef && ef.rev !== S.engineRev) { S.engineRev = ef.rev; const img = $("#s3-engine"); img.src = fileUrl(ef.path) + "?r=" + ef.rev; img.hidden = false; $("#s3-engine-badge").hidden = false; $("#s3-engine-badge").textContent = `引擎渲染帧 @ ${fmtT(ef.t)} · 点击关闭`; }
  memo("exports", JSON.stringify([j.exports || [], (j.options || {}).outputs, (j.options || {}).output_dir,
    (j.options || {}).cast_delay_ms, (j.options || {}).hires, S.masterStale]), renderExports);
  memo("linetable", String(S.viewRev) + JSON.stringify((j.timing || {}).qa_summary || {}) + (S.ws ? S.ws.rev : ""), renderLineTable);
  const t3 = j.timing || {};
  memo("s3singers", JSON.stringify([(j.song || {}).singers, S.ws ? S.ws.rev : null, S.brush || [], t3.undo, t3.undo_label, t3.redo, t3.redo_label]), () => { renderS3Singers(); renderSelBar(); });
  syncFineBar();
  loadPeaks();
}

const KIND_LABEL = { mp4: "成品 MP4", sug: "SUG 工程", yurika: "字幕工程", lrc: "逐字 LRC", alpha: "透明字幕层",
  onoff: "on/off vocal", mv: "无字幕版", hires: "Hi-Res" };
function s3KindBoxes() { return $$("#s3-out-kinds input"); }
function s3Kinds() { return s3KindBoxes().filter((c) => c.checked).map((c) => c.value); }

/* ---- 导出目录：页面内的文件夹选择器（不弹本机对话框） ---- */
async function loadDirPick(path, child) {
  try {
    S.dirCur = await act("list_dir", child ? { path, child } : { path });
    renderDirPick();
  } catch (e) { toast("读不到这个文件夹：" + e.message); }
}
function renderDirPick() {
  const r = S.dirCur; if (!r) return;
  $("#dp-path").textContent = r.path;
  $("#dp-up").disabled = !r.parent;
  const dr = $("#dp-drives"); dr.innerHTML = "";
  for (const d of r.drives || [])
    dr.append(el("button", { class: "dp-chip" + (r.path.toLowerCase().startsWith(d.toLowerCase()) ? " on" : ""), onclick: () => loadDirPick(d) }, d));
  const list = $("#dp-list"); list.innerHTML = "";
  if (!(r.dirs || []).length) list.append(el("div", { class: "muted small" }, "（没有子文件夹）"));
  for (const name of r.dirs)
    list.append(el("button", { class: "dp-dir", title: name, onclick: () => loadDirPick(r.path, name) }, "📁 " + name));
}

function renderExports() {
  const j = S.job; const opts = j.options || {};
  const running = (j.exports || []).some((e) => e.state === "running");
  /* 勾选框：默认沿用阶段一确认时选的「导出内容」；本页改动只存在 S 里，
     点「一键导出」时才写回 options.outputs。 */
  const outs = (opts.outputs || []).length ? opts.outputs : ["mp4", "sug", "yurika", "onoff"];
  const sig = JSON.stringify(outs);
  if (!S.expKinds || (!S.expTouched && sig !== S.expSig)) { S.expKinds = new Set(outs); S.expSig = sig; }
  const bgt = bgKind((opts.background || {}).type);
  const mvOK = bgt === "mv" || bgt === "montage";
  $("#s3-kind-mv").hidden = !mvOK;
  $("#s3-kind-mv-label").textContent = bgt === "montage" ? "混剪版" : "可视化版";
  if (!mvOK) S.expKinds.delete("mv");   // 背景不是生成的设计时没有无字幕版可导
  s3KindBoxes().forEach((c) => (c.checked = S.expKinds.has(c.value)));
  $("#s3-hires-box").hidden = !S.expKinds.has("hires");   // 音源输入只在勾了 Hi-Res 时才占位置
  const kinds = s3Kinds();
  const deps = kinds.includes("onoff") || kinds.includes("hires");   // 只复制成品视频流 + 换音轨
  const hasMaster = (j.exports || []).some((e) => e.kind === "mp4" && e.state === "done");
  $("#s3-export").disabled = running || !kinds.length;
  $("#s3-exp-state").textContent = !kinds.length ? "先勾选要导出的内容"
    : deps && !hasMaster ? "还没有成品 MP4，导出时会先渲染一遍"
    : deps && S.masterStale ? "时间轴在上次导出后有改动：会先重新渲染成品 MP4"
    : kinds.includes("mp4") || kinds.includes("alpha") ? "成品渲染需要几分钟" : "";
  const cd = opts.cast_delay_ms;
  const ci = $("#s3-cast");
  if (document.activeElement !== ci) ci.value = cd === undefined || cd === null ? 200 : cd;
  const oi = $("#s3-out-dir");
  if (document.activeElement !== oi) oi.value = opts.output_dir || "";
  oi.placeholder = "（默认）" + (S.jobDir || "") + "\\export";
  const hr = opts.hires || {};
  if (document.activeElement !== $("#s3-hires-on") && !$("#s3-hires-on").value) $("#s3-hires-on").value = hr.on || "";
  if (document.activeElement !== $("#s3-hires-off") && !$("#s3-hires-off").value) $("#s3-hires-off").value = (hr.off || []).join("; ");
  const list = $("#export-list"); list.innerHTML = "";
  for (const e of j.exports || []) {
    const right = e.state === "running" ? el("span", { class: "mono small" }, `${Math.round((e.progress || 0) * 100)}%${e.eta ? " · 剩余约 " + Math.ceil(e.eta) + " 秒" : ""}`)
      : e.state === "error" ? el("span", { class: "chip bad", title: e.error }, "失败")
      : el("button", { class: "btn ghost sm", onclick: () => act("open_folder", { path: e.path }) }, "定位");
    const paths = (e.files && e.files.length ? e.files : [e.path]).filter(Boolean);   // 一行一条记录：文件名 + 悬停看完整路径
    const files = paths.map((p) => String(p).split(/[\\/]/).pop()).join(" · ");
    const item = el("div", { class: "export-item" }, el("span", { class: "k" }, ({ hires: "hi-res", onoff: "on/off", alpha: "alpha", mv: "bg" })[e.kind] || e.kind),
      el("span", { class: "lbl", title: e.label || e.kind }, e.label || e.kind),
      el("span", { class: "p", title: paths.join("\n") }, files), right,
      e.notes && e.notes.length ? el("div", { class: "notes" }, e.notes.join("\n")) : null,
      e.state === "running" ? el("div", { class: "pbar" }, el("i", { style: { width: Math.round((e.progress || 0) * 100) + "%" } })) : null);
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
    S.sel = S.sel || new Set();
    const names = lineSingers(l);
    const fin = S.edit && S.edit.line === i;
    const finMode = fin ? ((S.edit && S.edit.focus) || "char") : null;
    const row = el("div", { class: "lrow" + (S.sel.has(i) ? " sel" : "") + (fin ? " fine" : ""), "data-i": i },
      el("span", { class: "n", title: "点击选中这一行（Shift 连选），再点上方歌手或按数字键分配", onclick: (ev) => { ev.stopPropagation(); toggleSel(i, ev.shiftKey); } }, i + 1),
      el("div", {},
        el("div", { class: "tx", html: rubyFromChars(l.chars, (c) => paintCss(paintOf(sgName(c.sg || l.singer)))) }),
        el("div", { class: "info" },
          names.length ? el("span", { class: "sgs", title: names.join(" / ") }, ...names.map((n) => el("i", { style: { backgroundImage: paintCss(paintOf(n)) } })), names.join(" / ")) : null,
          el("span", { class: "flag " + flag }), `${fmtT(l.start)} → ${fmtT(l.end)}`,
          q.notes && q.notes.length ? el("span", { class: "qa" }, q.notes.join("；")) : null,
          el("span", { class: "nudge" },
            el("button", { class: "mode" + (finMode === "row" ? " on" : ""), title: "句子级调整：放大这一行，拖行条＝平移整行，拖两端把手＝拉长 / 缩短（上下行关系也能拖）", onclick: (ev) => { ev.stopPropagation(); finMode === "row" ? exitFine() : enterFine(i, { focus: "row" }); } }, "调句子"),
            el("button", { class: "mode" + (finMode === "char" ? " on" : ""), title: "字符级精调：逐字 / 逐注音拍拖动圆点（←→ 跳字、Alt+↑↓ 微调）", onclick: (ev) => { ev.stopPropagation(); finMode === "char" ? exitFine() : enterFine(i, { focus: "char" }); } }, "精调"),
            el("button", { title: "让本行从当前播放位置开始", onclick: (ev) => { ev.stopPropagation(); const t = $("#s3-video").currentTime; editLines([{ op: "shift_lines", lines: [i], ms: Math.round((t - l.start) * 1000) }]); } }, "⇤ 播放头"),
            el("button", { title: "修改歌词文本 / 注音", onclick: (ev) => { ev.stopPropagation(); S.editing = S.editing === i ? null : i; S.editDraft = null; renderLineTable(); } }, "✎")))));
    row.addEventListener("click", () => { const vid = $("#s3-video"); vid.currentTime = Math.max(0, l.start - 1.2); vid.play(); });
    box.append(row);
    if (S.editing === i) box.append(lineEditor(l, i));
  });
  box.scrollTop = keepScroll;
  if (S.activeLine !== null && S.activeLine !== undefined) { const a = $(`.lrow[data-i="${S.activeLine}"]`); if (a) a.classList.add("active"); }
}

// UtaTen-style inline ruby for the line editor: 「漢字(かんじ)」 per word (chars joined by ・link)
function inlineRubyOf(l) {
  let out = "", gt = "", gr = "";
  for (const c of l.chars) {
    gt += c.c; gr += (c.r || []).join("").replace(/\^pause\^/g, "");
    if (!c.link) { out += gr && gr !== gt ? `${gt}(${gr})` : gt; gt = ""; gr = ""; }
  }
  return out;
}

function openLineEditor(i) {
  S.editing = i; S.editDraft = null; S.wordDraft = null;
  renderLineTable();
  const row = $(`.lrow[data-i="${i}"]`);
  if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
}
function lineEditor(l, i) {
  const origRuby = inlineRubyOf(l);
  const iniRuby = (S.editDraft && S.editDraft.i === i) ? S.editDraft.ruby : origRuby;
  const iniText = (S.editDraft && S.editDraft.i === i) ? S.editDraft.text : l.text;
  const txt = el("input", { type: "text", class: "grow", value: iniText, oninput: () => { S.editDraft = { i, text: txt.value, ruby: rub.value }; } });
  const rub = el("input", { type: "text", class: "grow mono", value: iniRuby, placeholder: "漢字(かんじ)", oninput: () => { S.editDraft = { i, text: txt.value, ruby: rub.value }; } });
  const close = () => { S.editing = null; S.editDraft = null; S.wordDraft = null; renderLineTable(); };
  /* —— 词表：逐词读音 + 合并 / 拆分 / 清除（即注音↔歌词对应关系）—— */
  if (!S.wordDraft || S.wordDraft.i !== i) S.wordDraft = { i, vals: {} };
  const words = fineWords(l);
  const wrows = [];
  words.forEach((w, wi) => {
    const txtw = wordText(l, w);
    if (!txtw.trim()) return;   // 纯空格不列
    const key = `${w[0]}-${w[w.length - 1]}`;
    const cur = wordRuby(l, w);
    const val = key in S.wordDraft.vals ? S.wordDraft.vals[key] : cur;
    const wprev = wi > 0 ? words[wi - 1] : null;
    const adjPrev = wprev && wprev[wprev.length - 1] === w[0] - 1;
    const inp = el("input", { type: "text", class: "grow mono", value: val, placeholder: "（无注音）" });
    const commit = () => {
      const v = (S.wordDraft && key in S.wordDraft.vals ? S.wordDraft.vals[key] : val).trim();
      delete S.wordDraft.vals[key];
      if (v === cur) return;
      act("edit_timing", { ops: [{ op: "set_word_ruby", line: i, chars: [w[0], w[w.length - 1]], reading: v }] });
      toast(`「${txtw}」读音已更新`);
    };
    inp.addEventListener("input", () => { S.wordDraft.vals[key] = inp.value; });
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); inp.blur(); } });
    inp.addEventListener("blur", commit);
    const midK = w.length > 1 ? w[0] + Math.max(0, Math.ceil((w.length - 1) / 2) - 1) : null;
    wrows.push(el("div", { class: "wrow" },
      el("span", { class: "wrg mono" }, `${w[0] + 1}${w.length > 1 ? "–" + (w[w.length - 1] + 1) : ""}`),
      el("span", { class: "wtx" }, txtw),
      inp,
      el("button", { class: "btn ghost sm sq", disabled: !adjPrev, title: "与上一个词合并（读音相接）",
        onclick: () => act("edit_timing", { ops: [{ op: "set_word_links", line: i, chars: [wprev[0], w[w.length - 1]] }] }) }, "⇤"),
      el("button", { class: "btn ghost sm sq", disabled: w.length < 2, title: "对半拆成两个词（读音已按字分开，可分别改）",
        onclick: () => act("edit_timing", { ops: [{ op: "set_word_links", line: i, chars: [w[0], midK] }, { op: "set_word_links", line: i, chars: [midK + 1, w[w.length - 1]] }] }) }, "✂"),
      el("button", { class: "btn ghost sm sq", disabled: !cur, title: "清除这个词的注音",
        onclick: () => act("edit_timing", { ops: [{ op: "set_word_ruby", line: i, chars: [w[0], w[w.length - 1]], reading: "" }] }) }, "⌫")));
  });
  return el("div", { class: "ledit", onclick: (ev) => ev.stopPropagation() },
    el("div", { class: "row gap" }, el("span", { class: "muted small" }, "歌词"), txt),
    el("div", { class: "wlist" },
      el("div", { class: "muted small" }, "词 / 注音（读音框回车或点开别处保存；⇤ 并入前词 · ✂ 对半拆开 · ⌫ 清注音）"),
      ...wrows),
    el("details", { class: "raw" }, el("summary", { class: "muted small" }, "原始注音行（高级）"),
      el("div", { class: "row gap" }, el("span", { class: "muted small" }, "注音"), rub),
      el("div", { class: "muted small" }, "语法：在要注音的字后面跟 (かな)，如 違(たが)う；留空 = 清除该行全部注音")),
    el("div", { class: "row gap" },
      el("button", { class: "btn primary sm", onclick: async (ev) => {
        ev.stopPropagation();
        const ops = [];
        const t = txt.value;
        if (t !== l.text) ops.push({ op: "set_text", line: i, text: t });
        const r = rub.value.trim();
        if (r !== origRuby.trim()) {
          if (!r && origRuby.trim() && !confirm("注音框留空：清除该行的全部注音？")) return;
          ops.push({ op: "set_ruby", line: i, inline: r });
        }
        if (!ops.length) { toast("没有改动"); close(); return; }
        await act("edit_timing", { ops });
        close();
        toast(`已更新第 ${i + 1} 行`);
      } }, "保存歌词/原始注音"),
      el("button", { class: "btn ghost sm", onclick: (ev) => { ev.stopPropagation(); close(); } }, "关闭")));
}
function rubyFromChars(chars, paint = null) {
  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  let out = "", grp = "", rb = "", first = null;
  for (const c of chars) {
    if (first === null) first = c;
    grp += c.c; rb += (c.r || []).join("").replace(/\^pause\^/g, "");
    if (!c.link) {
      const h = rb && rb !== grp ? `<ruby>${esc(grp)}<rt>${esc(rb)}</rt></ruby>` : esc(grp);
      out += paint && grp.trim() ? `<span class="sg" style="background-image:${esc(paint(first))}">${h}</span>` : h;
      grp = ""; rb = ""; first = null;
    }
  }
  return out + esc(grp);
}
/* ------------------------------------------------------------------ stage-3 singers (歌割り) */
function paintOf(name) {
  const w = S.ws && S.ws.by_name && S.ws.by_name[name];
  const s = ((S.job.song || {}).singers || []).find((x) => x.name === name);
  return { color: (w && w.color) || (s && s.color) || "#888888", bands: w && w.bands && w.bands.length > 1 ? w.bands : null };
}
function paintCss(p) {
  if (p.bands) return "linear-gradient(to bottom, " + p.bands.map((c, i, a) => `${c} ${(i / a.length * 100).toFixed(1)}% ${((i + 1) / a.length * 100).toFixed(1)}%`).join(", ") + ")";
  return `linear-gradient(${p.color}, ${p.color})`;
}
function sgName(sid) { const s = S.view && S.view.singers.find((x) => x.id === sid); return s ? s.name : null; }
function lineSingers(l) { const out = []; for (const c of l.chars) { const n = sgName(c.sg || l.singer); if (n && c.c.trim() && !out.includes(n)) out.push(n); } return out; }
function selLines() { S.sel = S.sel || new Set(); if (S.sel.size) return [...S.sel].sort((a, b) => a - b); return S.activeLine !== null && S.activeLine !== undefined ? [S.activeLine] : []; }
function fmtLines(ls) {
  const out = []; let a = null, b = null;
  for (const i of ls) { if (a !== null && i === b + 1) { b = i; continue; } if (a !== null) out.push(a === b ? `${a + 1}` : `${a + 1}-${b + 1}`); a = b = i; }
  if (a !== null) out.push(a === b ? `${a + 1}` : `${a + 1}-${b + 1}`);
  return out.join(", ");
}
function toggleSel(i, range) {
  S.sel = S.sel || new Set();
  if (range && S.selAnchor !== undefined) { for (let k = Math.min(S.selAnchor, i); k <= Math.max(S.selAnchor, i); k++) S.sel.add(k); }
  else { if (S.sel.has(i)) S.sel.delete(i); else S.sel.add(i); S.selAnchor = i; }
  renderLineTable(); renderSelBar();
}
function toggleBrush(name) {
  S.brush = S.brush || [];
  S.brush = S.brush.includes(name) ? S.brush.filter((n) => n !== name) : S.brush.concat([name]);
  renderS3Singers(); renderSelBar();
}
function assignSingers(names) {
  const lines = selLines();
  if (!names.length) { toast("先点上方的歌手（可以多选，多选 = 合唱拼色）"); return; }
  if (!lines.length) { toast("先点行号选中要修改的行（Shift 连选），或在播放中直接按"); return; }
  const sg = names.length === 1 && S.view.singers.find((s) => s.name === names[0]);
  if (sg) { for (const i of lines) { const l = S.view.lines[i]; l.singer = sg.id; l.chars.forEach((c) => (c.sg = sg.id)); } renderLineTable(); }
  act("singers", { assign: { lines, singers: names } });
  toast(`第 ${fmtLines(lines)} 行 → ${names.join("＆")}${names.length > 1 ? "（合唱拼色）" : ""}`);
}
function renderS3Singers() {
  const bar = $("#s3-singers"); if (!bar || !S.job) return;
  const singers = (S.job.song || {}).singers || [];
  S.brush = (S.brush || []).filter((n) => singers.some((s) => s.name === n));
  bar.innerHTML = "";
  bar.append(el("span", { class: "muted small" }, "歌手"));
  singers.forEach((s, k) => {
    const p = paintOf(s.name);
    const sw = el("label", { class: "sw", style: { backgroundImage: paintCss(p) }, title: p.bands ? "合唱：颜色来自各位歌手" : "点击改颜色", onclick: (ev) => { if (!p.bands) ev.stopPropagation(); } },
      p.bands ? null : el("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(s.color || "") ? s.color : "#888888",
        onchange: (ev) => { act("singers", { colors: { [s.name]: ev.target.value } }); toast(`正在更新 ${s.name} 的颜色`); } }));
    bar.append(el("span", { class: "sgchip" + (S.brush.includes(s.name) ? " on" : ""),
      title: (k < 9 ? `按 ${k + 1}：把选中行直接设为「${s.name}」。` : "") + "点击加入 / 移出画笔，选多位 = 合唱（拼色）",
      onclick: () => toggleBrush(s.name) }, sw, k < 9 ? el("kbd", {}, k + 1) : null, s.name));
  });
  bar.append(el("button", { class: "btn ghost sm", title: "新增歌手", onclick: () => {
    const name = (prompt("新歌手的名字") || "").trim();
    if (name) { act("singers", { add: [{ name }] }); toast(`正在新增歌手 ${name}（点色块可改颜色）`); }
  } }, "＋ 歌手"));
}
function renderSelBar() {
  const bar = $("#s3-selbar"); if (!bar || !S.job) return;
  S.sel = S.sel || new Set();
  const n = S.sel.size, brush = S.brush || [], t = S.job.timing || {};
  bar.innerHTML = "";
  bar.append(el("span", { class: "muted small" }, n ? `已选 ${n} 行` : "未选行时作用于正在播放的行"));
  bar.append(el("button", { class: "btn primary sm", disabled: !brush.length, title: brush.length ? "" : "先点上方的歌手（可多选）",
    onclick: () => assignSingers(brush) }, brush.length ? `设为 ${brush.join("＆")}${brush.length > 1 ? "（拼色）" : ""}` : "设为…"));
  bar.append(el("button", { class: "btn ghost sm", disabled: !S.view, onclick: () => { S.view.lines.forEach((_, i) => S.sel.add(i)); renderLineTable(); renderSelBar(); } }, "全选"));
  bar.append(el("button", { class: "btn ghost sm", disabled: !n, onclick: () => { S.sel.clear(); renderLineTable(); renderSelBar(); } }, "清除选择"));
  bar.append(el("button", { class: "btn ghost sm", title: "让相邻字的走字速度更均匀：每个检查点最多挪 80 ms，行首、停顿和长音不动；选中行时只处理选中的行，可多点几次",
    onclick: () => { const ls = n ? selLines() : null; act("edit_timing", { ops: [ls ? { op: "smooth", lines: ls } : { op: "smooth" }] }); toast(`正在平滑走字（${ls ? "第 " + fmtLines(ls) + " 行" : "全部歌词"}）`); } }, "平滑走字"));
  bar.append(el("button", { class: "btn ghost sm", disabled: !t.undo, title: t.undo ? `撤销：${t.undo_label || "上一次修改"}` : "没有可撤销的修改",
    onclick: () => { act("undo", {}); toast("正在撤销：" + (t.undo_label || "上一次修改")); } }, "↶ 撤销"));
  bar.append(el("button", { class: "btn ghost sm", disabled: !t.redo, title: t.redo ? `重做：${t.redo_label || "被撤销的修改"}` : "没有可重做的修改",
    onclick: () => { act("redo", {}); toast("正在重做：" + (t.redo_label || "被撤销的修改")); } }, "↷ 重做"));
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

/* ------------------------------------------------------------ 时间轴交互工具 */
function tlGeo(cv) {
  const r = cv.getBoundingClientRect();
  const dur = mediaDuration();
  let x0 = 0, x1 = dur;
  if (S.zoom) { x0 = S.zoom[0]; x1 = S.zoom[1]; }
  if (!(x1 > x0)) x1 = x0 + 1;
  return { rect: r, x0, x1, t: (cx) => x0 + ((cx - r.left) / Math.max(1, r.width)) * (x1 - x0),
           px: (tt) => ((tt - x0) / (x1 - x0)) * r.width };
}

/* x 位置的拍点把手（返回 {char, cp} 或 null；bd = 命中半径，px） */
function cpHandleAt(l, geo, t, cx, bd) {
  let ci = -1;
  for (let i = 0; i < l.chars.length; i++) {
    const c = l.chars[i];
    if (c.s === null || !c.cp.length) continue;
    if (t < c.s - 0.03 || t > c.e + 0.03) continue;
    ci = i; break;
  }
  if (ci < 0) return null;
  const c = l.chars[ci];
  let bk = -1, best = bd;
  for (let k = 0; k < c.cp.length; k++) {
    const d = Math.abs(geo.px(c.cp[k]) - cx);
    if (d <= best) { best = d; bk = k; }
  }
  return bk >= 0 ? { char: ci, cp: bk } : null;
}

/* 细调轨命中（按泳道）：ghost-prev / ghost-next（上下文轨）· edge-l / edge-r / rowbar（行条轨）
   · cp 圆点（把手区，优先）· word（词轨）· se / cp / block（字块轨）· bg */
function tlHitFine(l, geo, ev) {
  const r = geo.rect;
  const cx = ev.clientX - r.left, cy = ev.clientY - r.top;
  const t = geo.t(ev.clientX);
  const hCss = r.height;
  if (cy < FINE_L.lab) return { kind: "bg", t };
  const [prev, next] = fineNeighbors(l);
  if (cy < FINE_L.gh1) {   // 上下文轨
    if (prev && lastSeIdx(prev) !== null && Math.abs(geo.px(prev.end) - cx) <= 9) return { kind: "ghost-prev", t };
    if (next && Math.abs(geo.px(next.start) - cx) <= 9) return { kind: "ghost-next", t };
    return { kind: "bg", t };
  }
  if (cy < FINE_L.bar1 + 2) {   // 整行条轨
    const L = geo.px(l.start), R = geo.px(l.end);
    if (R - L >= 16) {
      if (Math.abs(cx - L) <= 8) return { kind: "edge-l", t };
      if (Math.abs(cx - R) <= 8) return { kind: "edge-r", t };
    }
    if (cx >= L - 5 && cx <= R + 5) return { kind: "rowbar", t };
    return { kind: "bg", t };
  }
  if (cy < FINE_L.w1 + 2) {   // 把手区（拍点圆点画在词轨下缘）+ 词轨
    const h = cpHandleAt(l, geo, t, cx, 6.5);
    if (h) return { kind: "cp", char: h.char, cp: h.cp, t };
    const w = wordAtX(l, geo, cx);
    return w ? { kind: "word", word: w, t } : { kind: "bg", t };
  }
  // 字块轨
  const h2 = cpHandleAt(l, geo, t, cx, 9);
  if (h2) return { kind: "cp", char: h2.char, cp: h2.cp, t };
  let ci = -1;
  for (let i = 0; i < l.chars.length; i++) {
    const c = l.chars[i];
    if (c.s === null || !c.cp.length) continue;
    if (t < c.s - 0.03 || t > c.e + 0.03) continue;
    ci = i; break;
  }
  if (ci < 0) return { kind: "bg", t };
  const c = l.chars[ci];
  if (c.se && cy > hCss - 28 && Math.abs(geo.px(c.e) - cx) <= 9) return { kind: "se", char: ci, t };
  return { kind: "block", char: ci, t };
}

function mergeRuns(sorted) {
  const out = [];
  for (const v of sorted) {
    const last = out[out.length - 1];
    if (last && v === last[1] + 1) last[1] = v;
    else out.push([v, v]);
  }
  return out;
}

/* ------------------------------------------------------------ A/B 循环 · 帧步进 */
function setAB(which) {
  const vid = $("#s3-video");
  if (!vid) return;
  S.ab[which] = r3(vid.currentTime);
  renderAB();
  toast(`循环 ${which.toUpperCase()} 点 = ${fmtT(S.ab[which])}（播放到 B 自动跳回 A）`);
}
function renderAB() {
  const ctl = $("#ab-ctl"); if (!ctl) return;
  const on = S.ab.a !== null || S.ab.b !== null;
  ctl.classList.toggle("on", on);
  $("#ab-a").classList.toggle("set", S.ab.a !== null);
  $("#ab-b").classList.toggle("set", S.ab.b !== null);
  const f = (v) => (v === null ? "–" : v.toFixed(2));
  $("#ab-txt").textContent = on ? `${f(S.ab.a)} – ${f(S.ab.b)}` : "—";
  $("#ab-clear").hidden = !on;
}
function stepFrame(dir) {
  const vid = $("#s3-video"); if (!vid) return;
  vid.pause();
  vid.currentTime = Math.max(0, vid.currentTime + dir / fineFps());
}

/* ------------------------------------------------------------ 细调键盘 */
function moveFineChar(dir) {
  const l = fineLine(); if (!l) return;
  const sel = [...S.edit.chars].sort((a, b) => a - b);
  let ci = clamp((sel[0] ?? 0) + dir, 0, l.chars.length - 1);
  while (ci > 0 && ci < l.chars.length - 1 && !l.chars[ci].cp.length) ci += dir;
  S.edit.chars = new Set([ci]);
  S.edit.cp = [ci, 0];
  const c = l.chars[ci];
  if (c.s !== null) {   // 确保选中字在可视窗口内
    const [a, b] = S.zoom || [0, mediaDuration()];
    if (c.s < a || (c.e ?? c.s) > b) S.zoom = clampFineZoom(Math.max(0, c.s - 0.8), (c.e ?? c.s) + 0.5);
  }
  syncFineBar(); drawTimeline($("#s3-timeline"), { playhead: true });
}
function moveFineLine(dir) {
  const n = S.view ? S.view.lines.length : 0;
  let i = (S.edit ? S.edit.line : 0) + dir;
  while (i >= 0 && i < n && S.view.lines[i].start === null) i += dir;
  if (i < 0 || i >= n) return;
  enterFine(i, { focus: (S.edit && S.edit.focus) || "char" });
}
/* 平移所选字（词卡选中即整词）：Alt+←/→ ±10ms，加 Shift ±50ms */
function shiftFineSel(ms) {
  const l = fineLine();
  if (!l || !S.edit.chars.size) { toast("先点选字或词"); return; }
  const sel = [...S.edit.chars].sort((a, b) => a - b).filter((i) => l.chars[i] && l.chars[i].cp.length);
  if (!sel.length) return;
  let lo = -Infinity, hi = Infinity;
  for (const ci of sel) {
    const v = l.chars[ci].cp;
    let plo = 0, phi = Infinity;
    for (let j = ci - 1; j >= 0; j--) if (l.chars[j].cp && l.chars[j].cp.length) { plo = l.chars[j].cp[l.chars[j].cp.length - 1] + 0.02; break; }
    for (let j = ci + 1; j < l.chars.length; j++) if (l.chars[j].cp && l.chars[j].cp.length) { phi = l.chars[j].cp[0] - 0.02; break; }
    lo = Math.max(lo, plo - v[0]);
    hi = Math.min(hi, phi - v[v.length - 1]);
  }
  const dt = clamp(ms / 1000, lo, hi);
  const step = Math.round(dt * 1000);
  if (!step) return;
  for (const ci of sel) {
    const c = l.chars[ci];
    c.cp = c.cp.map((x) => r3(x + dt));
    if (c.se && c.e !== null) c.e = r3(c.e + dt);
  }
  reflowLineChars(l);
  for (const [a, b] of mergeRuns(sel)) queueOps([{ op: "shift_chars", line: S.edit.line, chars: [a, b], ms: step }]);
  syncFineBar(); drawTimeline($("#s3-timeline"), { playhead: true });
  setHint(`所选 ${sel.length} 字 · ${step >= 0 ? "+" : ""}${step}ms`);
}
function keyFine(ev) {
  if (S.viewing !== 3 || S.closed) return;
  const tag = (document.activeElement || {}).tagName || "";
  if (["TEXTAREA", "INPUT", "SELECT"].includes(tag)) return;
  if (ev.ctrlKey || ev.metaKey) return;
  const key = ev.key;
  if (key === "[") { ev.preventDefault(); setAB("a"); return; }
  if (key === "]") { ev.preventDefault(); setAB("b"); return; }
  if (key === "," || key === ".") { ev.preventDefault(); stepFrame(key === "," ? -1 : 1); return; }
  const l = fineLine();
  if (!l) return;
  if (ev.altKey && (key === "ArrowLeft" || key === "ArrowRight")) {
    ev.preventDefault();
    shiftFineSel((key === "ArrowRight" ? 1 : -1) * (ev.shiftKey ? 50 : 10));
    return;
  }
  if (key === "ArrowLeft" || key === "ArrowRight") { ev.preventDefault(); moveFineChar(key === "ArrowRight" ? 1 : -1); return; }
  if (key === "ArrowUp" || key === "ArrowDown") {
    ev.preventDefault();
    if (ev.altKey) nudgeCP((key === "ArrowUp" ? 1 : -1) * (ev.shiftKey ? 50 : 10));
    else moveFineLine(key === "ArrowDown" ? 1 : -1);
    return;
  }
  if (key === "=" || key === "+") { ev.preventDefault(); setCPCount(1); return; }
  if (key === "-" || key === "_") { ev.preventDefault(); setCPCount(-1); return; }
  if (key === "p" || key === "P") { ev.preventDefault(); togglePause(); return; }
  if (key === "Escape" && !(S.sel && S.sel.size)) exitFine();
}

/* ------------------------------------------------------------ 右键菜单 */
let ctxMenuEl = null;
function closeCtxMenu() { if (ctxMenuEl) { ctxMenuEl.remove(); ctxMenuEl = null; } }
function openCtxMenu(ev, l, hit) {
  closeCtxMenu();
  const c = l.chars[hit.char];
  const menu = el("div", { class: "ctx-menu" });
  const add = (label, fn, disabled) => menu.append(el("button", { disabled, onclick: () => { closeCtxMenu(); fn(); } }, label));
  add(`▶ 试听${hit.kind === "cp" ? "这个检查点" : "这个字"}`, () => {
    const vid = $("#s3-video");
    const tt = hit.kind === "cp" ? c.cp[hit.cp] : c.s;
    if (tt !== null && tt !== undefined) { vid.currentTime = Math.max(0, tt - 0.2); vid.play(); }
  });
  add("＋ 节奏点（加一拍）", () => setCPCount(1), c.cp.length >= 16);
  add("− 节奏点", () => setCPCount(-1), c.cp.length <= 1);
  add(c.se ? "取消换气点" : "设为换气点", togglePause);
  menu.append(el("div", { class: "sep" }));
  add("按字宽均分所选", evenSpread, !S.edit || !S.edit.chars.size);
  add("清除这个字的节奏点…", () => {
    if (confirm(`清除「${c.c}」的全部节奏点？时间戳会丢失（可撤销恢复）`)) clearCPs();
  });
  menu.append(el("div", { class: "sep" }));
  add("把播放头移到这个字", () => { const vid = $("#s3-video"); vid.pause(); vid.currentTime = Math.max(0, (c.s ?? 0) - 0.05); });
  document.body.append(menu);
  menu.style.left = Math.min(ev.clientX, window.innerWidth - 215) + "px";
  menu.style.top = Math.min(ev.clientY, window.innerHeight - menu.offsetHeight - 8) + "px";
  ctxMenuEl = menu;
}

/* ------------------------------------------------------------ 词级弹窗（改读音 / 合并 / 拆分 / 清除）
   由词轨双击 / 右键、行编辑器词表共用同一套 op：set_word_ruby / set_word_links */
let wordPopEl = null;
function closeWordPop() { if (wordPopEl) { wordPopEl.remove(); wordPopEl = null; } }
function wordSplitAtFromX(l, w, geo, cx) {   // 词内离点击处最近的字界线（返回其左侧字索引）
  let k = null, bd = Infinity;
  for (let j = 0; j + 1 < w.length; j++) {
    const nx = l.chars[w[j + 1]];
    if (nx.s === null) continue;
    const d = Math.abs(geo.px(nx.s) - cx);
    if (d < bd) { bd = d; k = w[j]; }
  }
  return k;
}
function openWordPop(li, a, b, ev, splitK) {
  closeWordPop(); closeCtxMenu();
  const l = S.view && S.view.lines[li]; if (!l) return;
  const w = []; for (let i = a; i <= b; i++) w.push(i);
  const txt = wordText(l, w), cur = wordRuby(l, w);
  const words = fineWords(l);
  const wi = words.findIndex((x) => x[0] === a && x[x.length - 1] === b);
  const prevW = wi > 0 ? words[wi - 1] : null;
  const nextW = wi >= 0 && wi < words.length - 1 ? words[wi + 1] : null;
  const adjPrev = prevW && prevW[prevW.length - 1] === a - 1;
  const adjNext = nextW && nextW[0] === b + 1;
  const input = el("input", { type: "text", class: "grow mono", value: cur, placeholder: "かな（留空 = 清除注音）" });
  const run = (ops, msg) => { closeWordPop(); act("edit_timing", { ops }); if (msg) toast(msg); };
  const save = () => {
    const v = input.value.trim();
    if (v === cur) { toast("读音没有变化"); return; }
    run([{ op: "set_word_ruby", line: li, chars: [a, b], reading: v }], `「${txt}」读音 → ${v || "（无注音）"}`);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); save(); }
    else if (e.key === "Escape") { e.preventDefault(); closeWordPop(); }
    e.stopPropagation();
  });
  const pop = el("div", { class: "wordpop", onclick: (e2) => e2.stopPropagation() },
    el("div", { class: "wp-h" }, `词「${txt}」 · 第 ${a + 1}–${b + 1} 字 · ${cur ? `读音 ${cur}` : "无注音"}`),
    el("div", { class: "wp-r" }, el("span", { class: "muted small" }, "读音"), input,
      el("button", { class: "btn primary sm", onclick: save }, "保存")),
    el("div", { class: "wp-b" },
      el("button", { class: "btn ghost sm", disabled: !adjPrev, title: adjPrev ? "与上一个词合并为一个词（读音相接）" : "与前面的词之间隔着空格或未打轴的字",
        onclick: () => run([{ op: "set_word_links", line: li, chars: [prevW[0], b] }], `已并入前词（第 ${prevW[0] + 1}–${b + 1} 字）`) }, "⇤ 并入前词"),
      el("button", { class: "btn ghost sm", disabled: !adjNext, title: "与下一个词合并（读音相接）",
        onclick: () => run([{ op: "set_word_links", line: li, chars: [a, nextW[nextW.length - 1]] }], `已并入后词（第 ${a + 1}–${nextW[nextW.length - 1] + 1} 字）`) }, "并入后词 ⇥"),
      (splitK !== null && splitK !== undefined) ? el("button", { class: "btn ghost sm", title: "在点击位置拆成两个词（读音各半，随后可分别改）",
        onclick: () => run([
          { op: "set_word_links", line: li, chars: [a, splitK] },
          { op: "set_word_links", line: li, chars: [splitK + 1, b] },
        ], `已拆成「${wordText(l, w.filter((x) => x <= splitK))}」+「${wordText(l, w.filter((x) => x > splitK))}」`) }, `✂ 在第 ${splitK + 1} 字后拆开`) : null,
      el("button", { class: "btn ghost sm", disabled: !cur, onclick: () => run([{ op: "set_word_ruby", line: li, chars: [a, b], reading: "" }], `已清除「${txt}」的注音`) }, "⌫ 清除注音")));
  document.body.append(pop);
  pop.style.left = Math.min(ev.clientX, window.innerWidth - pop.offsetWidth - 10) + "px";
  pop.style.top = Math.min(ev.clientY + 10, window.innerHeight - pop.offsetHeight - 10) + "px";
  wordPopEl = pop;
  setTimeout(() => { input.focus(); input.select(); }, 0);
}

function bindStage3() {
  const vid = $("#s3-video");
  $("#export-head").addEventListener("click", (ev) => { if (ev.target.closest("button")) return; $("#export-card").classList.toggle("open"); });
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
  /* ---- 导出：勾选 + 一键导出 · 投屏延迟 · 导出目录 ---- */
  s3KindBoxes().forEach((c) => c.addEventListener("change", () => {
    S.expTouched = true; S.expKinds = new Set(s3Kinds()); renderExports();
  }));
  $("#s3-export").addEventListener("click", () => {
    const kinds = s3Kinds();
    if (!kinds.length) { toast("先勾选要导出的内容"); return; }
    S.expTouched = false;
    act("export", { kinds, hires: kinds.includes("hires") ? hiresPayload() : undefined, remember: true });
    const deps = kinds.includes("onoff") || kinds.includes("hires");
    const heavy = deps || kinds.includes("mp4") || kinds.includes("alpha");
    toast("开始导出：" + kinds.map((k) => KIND_LABEL[k] || k).join(" + ")
      + (deps && S.masterStale ? "（会先重新渲染成品 MP4，需要几分钟）" : heavy ? "（需要几分钟）" : ""));
  });
  $("#s3-preview25").addEventListener("click", () => { act("export", { kinds: ["mp4"], clip: 25 }); toast("正在渲染 25 秒试看片段"); });
  $("#s3-cast").addEventListener("change", (ev) => { act("set_option", { cast_delay_ms: Math.round(+ev.target.value || 0) }); toast("投屏延迟已更新，下次导出生效"); });
  const outDir = $("#s3-out-dir");
  const saveOutDir = (v) => { act("set_option", { output_dir: v || null }); toast(v ? "导出目录：" + v : "导出目录已改回工程内的 export 文件夹"); };
  outDir.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); outDir.blur(); } });
  outDir.addEventListener("change", () => saveOutDir(outDir.value.trim()));
  $("#s3-out-dir-default").addEventListener("click", () => { outDir.value = ""; saveOutDir(""); });
  $("#s3-out-dir-pick").addEventListener("click", () => {
    $("#s3-dirpick").hidden = false;
    loadDirPick(outDir.value.trim() || (S.job.options || {}).output_dir || "");
  });
  $("#dp-cancel").addEventListener("click", () => ($("#s3-dirpick").hidden = true));
  $("#dp-up").addEventListener("click", () => { if (S.dirCur && S.dirCur.parent) loadDirPick(S.dirCur.parent); });
  $("#dp-use").addEventListener("click", () => {
    if (!S.dirCur) return;
    outDir.value = S.dirCur.path; saveOutDir(S.dirCur.path); $("#s3-dirpick").hidden = true;
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") $("#s3-dirpick").hidden = true; });
  /* ---- A/B 循环 · 帧步进 · 细调工具条 ---- */
  $("#ab-a").addEventListener("click", () => setAB("a"));
  $("#ab-b").addEventListener("click", () => setAB("b"));
  $("#ab-clear").addEventListener("click", () => { S.ab = { a: null, b: null }; renderAB(); });
  $("#s3-fprev").addEventListener("click", () => stepFrame(-1));
  $("#s3-fnext").addEventListener("click", () => stepFrame(1));
  $("#tl-exit").addEventListener("click", exitFine);
  $("#tl-cpadd").addEventListener("click", () => setCPCount(1));
  $("#tl-cpsub").addEventListener("click", () => setCPCount(-1));
  $("#tl-pause").addEventListener("click", togglePause);
  $("#tl-even").addEventListener("click", evenSpread);
  $("#tl-listen").addEventListener("click", listenFine);
  $("#tl-edit").addEventListener("click", () => { if (fineLine()) openLineEditor(S.edit.line); });
  renderAB();
  /* ---- 时间轴：拖动 / 缩放 / 右键 ---- */
  const tl = $("#s3-timeline");
  let drag = null;
  tl.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0) return;
    const geo = tlGeo(tl);
    const t = geo.t(ev.clientX);
    const yRel = (ev.clientY - geo.rect.top) / geo.rect.height;
    const l = fineLine();
    if (l) {
      const hit = tlHitFine(l, geo, ev);
      if (hit.kind === "cp") {
        if (!S.edit.chars.has(hit.char)) S.edit.chars = new Set([hit.char]);
        S.edit.cp = [hit.char, hit.cp];
        drag = { kind: "cp", char: hit.char, cp: hit.cp, t0: hit.t, base: l.chars[hit.char].cp[hit.cp], dt: 0 };
      } else if (hit.kind === "se") {
        S.edit.chars = new Set([hit.char]);
        S.edit.cp = [hit.char, l.chars[hit.char].cp.length - 1];
        drag = { kind: "se", char: hit.char, t0: hit.t, base: l.chars[hit.char].e, dt: 0 };
      } else if (hit.kind === "block") {
        if (!S.edit.chars.has(hit.char)) S.edit.chars = new Set([hit.char]);
        if (!S.edit.cp || !S.edit.chars.has(S.edit.cp[0])) S.edit.cp = [hit.char, 0];
        const base = {}, seBase = {};
        for (const ci of S.edit.chars) { base[ci] = [...l.chars[ci].cp]; seBase[ci] = l.chars[ci].se ? l.chars[ci].e : null; }
        drag = { kind: "char", t0: hit.t, base, seBase, dt: 0 };
      } else if (hit.kind === "word") {   // 词卡：点=选中该词，拖=平移整词
        S.edit.chars = new Set(hit.word);
        const ti = wordTimed(l, hit.word) || hit.word;
        if (!S.edit.cp || !S.edit.chars.has(S.edit.cp[0])) S.edit.cp = [ti[0], 0];
        const base = {}, seBase = {};
        for (const ci of ti) { base[ci] = [...l.chars[ci].cp]; seBase[ci] = l.chars[ci].se ? l.chars[ci].e : null; }
        drag = { kind: "char", t0: hit.t, base, seBase, dt: 0, word: wordText(l, hit.word) };
      } else if (hit.kind === "rowbar") {
        drag = { kind: "line", line: l.i, t0: hit.t, base: l.chars.map((c) => [...c.cp]),
                 seB: l.chars.map((c) => (c.se ? c.e : null)), dt: 0 };
      } else if (hit.kind === "edge-l") {   // 行首把手：只动首字起唱
        const k = firstTimedIdx(l);
        if (k >= 0) drag = { kind: "edge-l", char: k, t0: hit.t, base: l.chars[k].cp[0], dt: 0 };
      } else if (hit.kind === "edge-r") {   // 收尾把手：改行尾释放
        const sk = lastSeIdx(l);
        if (sk !== null) drag = { kind: "edge-r", char: sk, viaSe: true, t0: hit.t, base: l.chars[sk].e, dt: 0 };
        else {
          const k = lastTimedIdx(l);
          if (k !== null) drag = { kind: "edge-r", char: k, viaSe: false, t0: hit.t, base: l.chars[k].cp[l.chars[k].cp.length - 1], dt: 0 };
        }
      } else if (hit.kind === "ghost-prev") {   // 上行收尾：拖 = 改上行释放
        const p = fineNeighbors(l)[0]; const pk = p && lastSeIdx(p);
        if (p && pk !== null) drag = { kind: "ghost-prev", line: p.i, char: pk, t0: hit.t, base: p.chars[pk].e, dt: 0 };
      } else if (hit.kind === "ghost-next") {   // 下行起唱：拖 = 平移下行整行
        const n2 = fineNeighbors(l)[1];
        if (n2) drag = { kind: "ghost-next", line: n2.i, t0: hit.t, base: n2.chars.map((c) => [...c.cp]),
                         seB: n2.chars.map((c) => (c.se ? c.e : null)), dt: 0 };
      } else {
        drag = { kind: "pan", t0: t, x0: ev.clientX, zoom0: [...(S.zoom || [0, mediaDuration()])], rectW: geo.rect.width, moved: 0 };
      }
    } else if (S.view) {
      if (yRel > 0.64) {   // 全局模式：拖行条 = 平移整行
        const lane = yRel > 0.81 ? 1 : 0;
        const i = S.view.lines.findIndex((lk, k) => k % 2 === lane && lk.start !== null && t >= lk.start && t <= lk.end);
        if (i >= 0) drag = { kind: "line", line: i, t0: t, base: S.view.lines[i].chars.map((c) => [...c.cp]),
                             seB: S.view.lines[i].chars.map((c) => (c.se ? c.e : null)), dt: 0 };
      }
      if (!drag) drag = { kind: "pan", t0: t, x0: ev.clientX, zoom0: [...(S.zoom || [0, mediaDuration()])], rectW: geo.rect.width, moved: 0 };
    }
    if (drag) { tlDrag = drag; tl.style.cursor = "grabbing"; if (drag.kind !== "pan") { syncFineBar(); drawTimeline(tl, { playhead: true }); } ev.preventDefault(); }
  });
  window.addEventListener("mousemove", (ev) => {
    if (!drag) { updateTlHover(ev); return; }
    const geo = tlGeo(tl);
    const t = geo.t(ev.clientX);
    if (drag.kind === "pan") {
      const dx = ev.clientX - drag.x0;
      if (Math.abs(dx) > 3) drag.moved = 1;
      if (drag.moved) {
        const shift = -(dx / drag.rectW) * (drag.zoom0[1] - drag.zoom0[0]);
        let a = drag.zoom0[0] + shift, b = drag.zoom0[1] + shift;
        const dur = mediaDuration();
        if (a < 0) { b -= a; a = 0; }
        if (b > dur) { a -= b - dur; b = dur; }
        S.zoom = [Math.max(0, a), b];
      }
      return;
    }
    let dt = t - drag.t0;
    if (drag.kind === "cp") {
      const ln = fineLine(); if (!ln) return;
      const c = ln.chars[drag.char];
      const [lo, hi] = cpBounds(ln.chars, drag.char, drag.cp);
      const nv = clamp(drag.base + dt, lo, hi);
      c.cp[drag.cp] = r3(nv);
      reflowLineChars(ln);
      drag.dt = nv - drag.base;
      const part = (c.r || [])[drag.cp] || "";
      setHint(`「${part || (c.c.trim() ? c.c : "␣")}」第 ${drag.cp + 1}/${c.cp.length} 拍 · ${nv.toFixed(3)}s`);
    } else if (drag.kind === "se") {
      const ln = fineLine(); if (!ln) return;
      const c = ln.chars[drag.char];
      let hi = Infinity;
      for (let j = drag.char + 1; j < ln.chars.length; j++) if (ln.chars[j].cp && ln.chars[j].cp.length) { hi = ln.chars[j].cp[0] - 0.02; break; }
      const nv = clamp(drag.base + dt, c.cp[c.cp.length - 1] + 0.02, hi);
      c.e = r3(nv);
      reflowLineChars(ln);
      drag.dt = nv - drag.base;
      setHint(`换气点 · ${nv.toFixed(3)}s`);
    } else if (drag.kind === "ghost-prev") {   // 上行收尾
      const ln = S.view.lines[drag.line]; if (!ln) return;
      const c = ln.chars[drag.char];
      const lc = c.cp.length ? c.cp[c.cp.length - 1] : 0;
      const nv = Math.max(lc + 0.02, drag.base + dt);
      c.e = r3(nv);
      reflowLineChars(ln);
      drag.dt = nv - drag.base;
      setHint(`上行收尾 · ${nv.toFixed(3)}s`);
    } else if (drag.kind === "ghost-next") {   // 下行整行平移
      const ln = S.view.lines[drag.line]; if (!ln) return;
      let lo = -Infinity;
      const firstc = drag.base.find((v) => v && v.length);
      if (firstc) lo = Math.max(lo, -firstc[0]);
      dt = Math.max(dt, lo);
      ln.chars.forEach((c, i) => {
        const v = drag.base[i]; if (!v || !v.length) return;
        c.cp = v.map((x) => r3(x + dt));
        if (drag.seB[i] !== null && drag.seB[i] !== undefined) c.e = r3(drag.seB[i] + dt);
      });
      reflowLineChars(ln);
      drag.dt = dt;
      if (Math.abs(dt) > 2e-4) setHint(`下行整行 · ${dt >= 0 ? "+" : ""}${Math.round(dt * 1000)}ms`);
    } else if (drag.kind === "edge-l") {   // 行首起唱
      const ln = fineLine(); if (!ln) return;
      const c = ln.chars[drag.char];
      let hi = Infinity;
      for (let j = drag.char + 1; j < ln.chars.length; j++) if (ln.chars[j].cp && ln.chars[j].cp.length) { hi = ln.chars[j].cp[0] - 0.02; break; }
      const nv = r3(clamp(drag.base + dt, 0, hi));
      c.cp[0] = nv;
      reflowLineChars(ln);
      drag.dt = nv - drag.base;
      setHint(`行首起唱 · ${nv.toFixed(3)}s`);
    } else if (drag.kind === "edge-r") {   // 行尾收尾
      const ln = fineLine(); if (!ln) return;
      const c = ln.chars[drag.char];
      const lo = c.cp[c.cp.length - 1] + 0.02;
      const nv = r3(Math.max(lo, drag.base + dt));
      if (!c.se) c.se = true;
      c.e = nv;
      reflowLineChars(ln);
      drag.dt = nv - drag.base;
      setHint(`行尾收尾 · ${nv.toFixed(3)}s`);
    } else if (drag.kind === "char") {
      const ln = fineLine(); if (!ln) return;
      let lo = -Infinity, hi = Infinity;
      for (const ci of S.edit.chars) {
        const v = drag.base[ci]; if (!v || !v.length) continue;
        let plo = 0, phi = Infinity;
        for (let j = ci - 1; j >= 0; j--) if (ln.chars[j].cp && ln.chars[j].cp.length) { plo = ln.chars[j].cp[ln.chars[j].cp.length - 1] + 0.02; break; }
        for (let j = ci + 1; j < ln.chars.length; j++) if (ln.chars[j].cp && ln.chars[j].cp.length) { phi = ln.chars[j].cp[0] - 0.02; break; }
        lo = Math.max(lo, plo - v[0]);
        hi = Math.min(hi, phi - v[v.length - 1]);
      }
      dt = clamp(dt, lo, hi);
      for (const ci of S.edit.chars) {
        const c = ln.chars[ci]; const v = drag.base[ci]; if (!v || !v.length) continue;
        c.cp = v.map((x) => r3(x + dt));
        if (drag.seBase[ci] !== null && drag.seBase[ci] !== undefined) c.e = r3(drag.seBase[ci] + dt);
      }
      reflowLineChars(ln);
      drag.dt = dt;
      if (Math.abs(dt) > 2e-4) setHint(`${drag.word ? `词「${drag.word}」· ` : `${S.edit.chars.size} 字 · `}${dt >= 0 ? "+" : ""}${Math.round(dt * 1000)}ms`);
    } else if (drag.kind === "line") {
      const ln = S.view.lines[drag.line]; if (!ln) return;
      let lo = -Infinity;
      const firstc = drag.base.find((v) => v && v.length);
      if (firstc) lo = Math.max(lo, -firstc[0]);
      dt = clamp(dt, lo, Infinity);
      ln.chars.forEach((c, i) => {
        const v = drag.base[i]; if (!v || !v.length) return;
        c.cp = v.map((x) => r3(x + dt));
        if (drag.seB[i] !== null && drag.seB[i] !== undefined) c.e = r3(drag.seB[i] + dt);
      });
      reflowLineChars(ln);
      drag.dt = dt;
      if (Math.abs(dt) > 2e-4) setHint(`整行 · ${dt >= 0 ? "+" : ""}${Math.round(dt * 1000)}ms`);
    }
  });
  window.addEventListener("mouseup", () => {
    if (!drag) return;
    const d = drag; drag = null; tlDrag = null;
    tl.style.cursor = cursorForHit(tlHover);
    if (d.kind === "pan") {
      if (!d.moved) { const v0 = $("#s3-video"); v0.currentTime = clamp(d.t0, 0, mediaDuration()); }
      return;
    }
    if (d.kind === "ghost-prev") {   // 上行收尾：写回上行释放时间
      const ln = S.view.lines[d.line];
      if (ln && Math.abs(d.dt || 0) > 2e-4) queueOps([{ op: "set_pause", line: d.line, char: d.char, t: ln.chars[d.char].e }]);
      syncFineBar();
      return;
    }
    if (d.kind === "ghost-next") {   // 下行起唱：整体平移下行
      const ms = Math.round((d.dt || 0) * 1000);
      if (ms) queueOps([{ op: "shift_lines", lines: [d.line], ms }]);
      syncFineBar();
      return;
    }
    const line = fineLine() ? S.edit.line : d.line;
    const l = S.view.lines[line];
    if (!l) return;
    if (d.kind === "cp") {
      const c = l.chars[d.char];
      if (Math.abs(d.dt || 0) > 2e-4) queueOps([{ op: "set_char", line, char: d.char, cp: d.cp, t: c.cp[d.cp] }]);
    } else if (d.kind === "se") {
      if (Math.abs(d.dt || 0) > 2e-4) queueOps([{ op: "set_pause", line, char: d.char, t: l.chars[d.char].e }]);
    } else if (d.kind === "char") {
      const ms = Math.round((d.dt || 0) * 1000);
      if (ms) for (const [a, b] of mergeRuns([...S.edit.chars].sort((x, y) => x - y)))
        queueOps([{ op: "shift_chars", line, chars: [a, b], ms }]);
    } else if (d.kind === "edge-l") {
      if (Math.abs(d.dt || 0) > 2e-4) queueOps([{ op: "set_char", line, char: d.char, cp: 0, t: l.chars[d.char].cp[0] }]);
    } else if (d.kind === "edge-r") {
      if (Math.abs(d.dt || 0) > 2e-4) queueOps([d.viaSe
        ? { op: "set_line_end", line, t: l.chars[d.char].e }
        : { op: "set_pause", line, char: d.char, t: l.chars[d.char].e }]);
    } else if (d.kind === "line") {
      const ms = Math.round((d.dt || 0) * 1000);
      if (ms) queueOps([{ op: "shift_lines", lines: [line], ms }]);
    }
    syncFineBar();
  });
  tl.addEventListener("wheel", (ev) => {
    if (tlDrag) return;
    ev.preventDefault();
    const geo = tlGeo(tl);
    const t = geo.t(ev.clientX); const dur = mediaDuration(); const [a, b] = S.zoom || [0, dur];
    const k = ev.deltaY > 0 ? 1.25 : 0.8; const na = clamp(t - (t - a) * k, 0, dur), nb = clamp(t + (b - t) * k, 0, dur);
    S.zoom = nb - na >= dur * 0.98 ? null : [na, nb];
    drawTimeline(tl, { playhead: true });
  }, { passive: false });
  tl.addEventListener("dblclick", (ev) => {
    const l = fineLine(); if (!l) return;
    const geo = tlGeo(tl);
    const hit = tlHitFine(l, geo, ev);
    if (hit.kind === "word") {   // 词卡双击：改读音 / 合并 / 拆分 / 清除注音
      const w = hit.word;
      const splitK = w.length > 1 ? wordSplitAtFromX(l, w, geo, ev.clientX - geo.rect.left) : null;
      openWordPop(l.i, w[0], w[w.length - 1], ev, splitK);
      return;
    }
    if (hit.kind === "rowbar") { openLineEditor(l.i); return; }   // 行条双击：改歌词 / 注音
    if (hit.kind === "block" || hit.kind === "cp") {
      S.edit.chars = new Set([hit.char]);
      S.edit.cp = [hit.char, hit.cp || 0];
      syncFineBar(); drawTimeline(tl, { playhead: true });
      listenFine();
    }
  });
  tl.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    const l = fineLine(); if (!l) return;
    const geo = tlGeo(tl);
    const hit = tlHitFine(l, geo, ev);
    if (hit.kind === "bg") { exitFine(); return; }
    if (hit.kind === "word") {
      const w = hit.word;
      const splitK = w.length > 1 ? wordSplitAtFromX(l, w, geo, ev.clientX - geo.rect.left) : null;
      if (w.length > 1) { S.edit.chars = new Set(w); syncFineBar(); drawTimeline(tl, { playhead: true }); }
      openWordPop(l.i, w[0], w[w.length - 1], ev, splitK);
      return;
    }
    if (hit.kind === "rowbar" || hit.kind === "edge-l" || hit.kind === "edge-r") { openLineEditor(l.i); return; }
    if (hit.kind === "ghost-prev" || hit.kind === "ghost-next") return;
    S.edit.chars = new Set([hit.char]);
    if (hit.kind === "cp") S.edit.cp = [hit.char, hit.cp];
    syncFineBar();
    openCtxMenu(ev, l, hit);
  });
  document.addEventListener("mousedown", (ev) => { if (ctxMenuEl && !ev.target.closest(".ctx-menu")) closeCtxMenu(); if (wordPopEl && !ev.target.closest(".wordpop")) closeWordPop(); });
  document.addEventListener("keydown", keyFine);
  document.addEventListener("keydown", (ev) => { if (S.viewing === 3 && ev.code === "Space" && !["TEXTAREA", "INPUT"].includes(document.activeElement.tagName)) { ev.preventDefault(); vid.paused ? vid.play() : vid.pause(); } });
  document.addEventListener("keydown", (ev) => {  // 1–9: singer k for the selected lines (or the playing line) · Esc: clear selection
    if (S.viewing !== 3 || ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName) || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const k = /^Digit[1-9]$/.test(ev.code) ? +ev.code.slice(5) : 0;
    const s = k && ((S.job.song || {}).singers || [])[k - 1];
    if (s) { ev.preventDefault(); assignSingers([s.name]); }
    else if (ev.key === "Escape" && S.sel && S.sel.size) { S.sel.clear(); renderLineTable(); renderSelBar(); }
  });
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
  if (S.layout && Date.now() > (S.tlHold || 0)) {
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
    const after = scol ? { ...ws.after, top: scol.top, bottom: scol.bottom, bands: scol.bands } : ws.after;
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
  const grad = ctx.createLinearGradient(0, y - fs * 0.85, 0, y + fs * 0.1);
  if (col.bands && col.bands.length > 1) col.bands.forEach((c, i, a) => { grad.addColorStop(i / a.length, c); grad.addColorStop(Math.max(i / a.length, (i + 1) / a.length - 0.001), c); }); // 拼色
  else { grad.addColorStop(0, col.top); grad.addColorStop(1, col.bottom); }
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
  bindStage1(); bindWavecheck(); bindStage3();
  window.addEventListener("resize", drawWaves);
}

let lastDraw = 0;
function tick(ts) {
  if (S.job) {
    if (S.viewing === 3) {
      drawOverlay();
      const thr = tlDrag ? 0 : 50;   // 拖动中每帧重绘，跟手
      if (ts - lastDraw > thr) {
        drawTimeline($("#s3-timeline"), { playhead: true }); lastDraw = ts;
        const vid = $("#s3-video"); $("#s3-time").textContent = fmtT(vid.currentTime);
        if (document.activeElement !== $("#s3-seek")) $("#s3-seek").value = Math.round((vid.currentTime / (vid.duration || mediaDuration())) * 1000);
      }
      // A/B 循环：播到 B 自动跳回 A
      const vid = $("#s3-video"); const ab = S.ab || {};
      if (!vid.paused && ab.a !== null && ab.b !== null && ab.a < ab.b && vid.currentTime >= ab.b) vid.currentTime = ab.a;
    } else if (S.viewing === 1 && ts - lastDraw > 80) { const m = $("#s1-media"); if (m && !m.paused) drawWave1(); lastDraw = ts; }
  }
  requestAnimationFrame(tick);
}

boot();
