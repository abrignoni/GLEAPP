"use strict";
/* GLEAPP review gallery client */

const $ = (s, r = document) => r.querySelector(s);
const api = (u, opt) => fetch(u, opt).then(r => r.json());
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = {
  files: [], total: 0, page: 1, pageSize: 200,
  sel: new Set(), lastClick: null, focus: null,
  similarOf: null, vstack: null, ctxIds: [],
  cats: [],                       // [{code,name,color,notable,position,active}]
  keyframeCache: new Map(),
  metaOpen: false,
};

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.style.display = "block";
  clearTimeout(toast._t); toast._t = setTimeout(() => t.style.display = "none", 1800);
}
const fmtDur = s => {
  if (!s && s !== 0) return "";
  s = Math.round(s); const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
};
const fmtSize = b => !b ? "" : b > 1e6 ? (b / 1048576).toFixed(1) + " MB"
  : (b / 1024).toFixed(0) + " KB";
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ---------- categories ---------- */
const catByCode = c => state.cats.find(x => x.code === +c);
const activeCats = () => state.cats
  .filter(c => c.code !== 0 && c.active)
  .sort((a, b) => a.position - b.position);
const catName = c => (catByCode(c) || {}).name || (c ? "Category " + c : "Uncategorized");
const catColor = c => (catByCode(c) || {}).color || (c ? "#888" : "#3a3f4b");

async function refreshCats() {
  state.cats = await api("/api/categories");
  // filter dropdown
  const sel = $("#fcat"), cur = sel.value;
  sel.innerHTML = '<option value="any">Any</option>';
  state.cats.sort((a, b) => a.position - b.position).forEach(c =>
    sel.insertAdjacentHTML("beforeend",
      `<option value="${c.code}">${esc(c.name)}${c.active ? "" : " (hidden)"}</option>`));
  sel.value = cur || "any";
  // selection bar buttons
  $("#selCats").innerHTML = activeCats().map((c, i) =>
    `<button class="btn sm" data-cat="${c.code}" title="key ${i + 1}"
      style="border-color:${c.color}">${esc(c.name || "Category " + c.code)}</button>`).join("");
}

/* ---------- filters ---------- */
function filterParams() {
  const p = new URLSearchParams();
  const q = $("#fq").value.trim(); if (q) p.set("q", q);
  if ($("#fkind").value) p.set("kind", $("#fkind").value);
  if ($("#fcat").value !== "any") p.set("category", $("#fcat").value);
  if ($("#frev").value) p.set("reviewed", $("#frev").value);
  if ($("#fsrc").value) p.set("source", $("#fsrc").value);
  if ($("#fclu").value) p.set("cluster", $("#fclu").value);
  if ($("#ffaces").checked) p.set("faces", "1");
  if ($("#fgps").checked) p.set("has_gps", "1");
  if ($("#fhit").checked) p.set("hashset", "1");
  if ($("#ferr").checked) p.set("error", "1");
  if (state.vstack) p.set("vstack", state.vstack);
  if (+$("#fskin").value > 0) p.set("min_skin", $("#fskin").value);
  if ($("#fcollapse").checked) p.set("dupes", "collapse");
  p.set("sort", $("#fsort").value);
  p.set("limit", state.pageSize);
  p.set("offset", (state.page - 1) * state.pageSize);
  return p;
}

/* load the current page (call reload() to also reset to page 1) */
async function load() {
  state.similarOf = null;
  if (!state.vstack) $("#simBanner").style.display = "none";
  const d = await api("/api/files?" + filterParams());
  state.total = d.total;
  const pages = Math.max(1, Math.ceil(d.total / state.pageSize));
  if (state.page > pages) { state.page = pages; return load(); }
  state.files = d.files;
  $("#grid").innerHTML = "";
  render(d.files);
  renderPager();
  updateStat();
  $("#main").scrollTop = 0;
}
function reload() { state.page = 1; state.vstack = null; state.sel.clear(); load(); }

function renderPager() {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  const p = state.page;
  const from = state.total ? (p - 1) * state.pageSize + 1 : 0;
  const to = Math.min(p * state.pageSize, state.total);
  const hide = !!state.similarOf || pages <= 1;
  const html = hide ? "" : `
    <button data-p="1" ${p <= 1 ? "disabled" : ""}>&laquo; First</button>
    <button data-p="${p - 1}" ${p <= 1 ? "disabled" : ""}>&lsaquo; Prev</button>
    <span>Page <input id="pageIn" type="number" min="1" max="${pages}" value="${p}"> of <b>${pages}</b></span>
    <button data-p="${p + 1}" ${p >= pages ? "disabled" : ""}>Next &rsaquo;</button>
    <button data-p="${pages}" ${p >= pages ? "disabled" : ""}>Last &raquo;</button>
    <span style="margin-left:8px"><b>${from.toLocaleString()}–${to.toLocaleString()}</b> of <b>${state.total.toLocaleString()}</b></span>`;
  for (const el of [$("#pagerTop"), $("#pagerBot")]) {
    el.innerHTML = html;
    el.classList.toggle("hidden", hide);
    el.querySelectorAll("[data-p]").forEach(b =>
      b.onclick = () => gotoPage(+b.dataset.p));
    const inp = el.querySelector("#pageIn");
    if (inp) {
      inp.id = "";                       // avoid dup ids across the two pagers
      inp.addEventListener("change", () => gotoPage(+inp.value));
      inp.addEventListener("keydown", e => {
        if (e.key === "Enter") gotoPage(+inp.value);
      });
    }
  }
}
function gotoPage(n) {
  const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
  n = Math.min(pages, Math.max(1, n || 1));
  if (n === state.page) return;
  state.page = n;
  load();
}

async function showSimilar(id) {
  const d = await api(`/api/similar/${id}?threshold=14`);
  state.similarOf = id;
  state.files = d.files;
  $("#grid").innerHTML = "";
  render(d.files);
  renderPager();
  $("#simBanner").style.display = "flex";
  $("#simId").textContent = `files similar to #${id}`;
  updateStat();
}

function updateStat() {
  $("#statLine").innerHTML = state.similarOf
    ? `<b>${state.files.length}</b> similar files`
    : `<b>${state.total}</b> files · <b>${state.sel.size}</b> selected`;
}

/* ---------- rendering ---------- */
function tileEl(f) {
  const el = document.createElement("div");
  el.className = "tile"
    + (state.sel.has(f.id) ? " sel" : "")
    + (state.focus === f.id ? " focus" : "");
  el.dataset.id = f.id;
  el.style.borderColor = f.category ? catColor(f.category) : "";
  const nExact = f.stack_count || 1;
  const nVis = f.vstack_count || 0;
  const dist = f.distance != null ? `<span class="b">${f.similarity}%</span>` : "";
  const faces = f.faces ? `<span class="b">${f.faces}\u{1F464}</span>` : "";
  const hit = f.hashset_hit ? `<span class="b hit">HASH</span>` : "";
  const gps = f.gps_lat != null ? `<span class="b">\u{1F4CD}</span>` : "";
  const err = f.error && !f.thumb ? `<span class="b hit">ERR</span>` : "";
  let stack = "";
  if (nVis > 1) {
    el.classList.add("vstacked");
    stack = `<span class="stackn vis" title="${nVis} visually similar files">≈ ${nVis}</span>`;
  } else if (nExact > 1) {
    el.classList.add("stacked");
    stack = `<span class="stackn" title="${nExact} identical copies">⬚ ${nExact}</span>`;
  }
  const catbar = f.category
    ? `<div class="catbar" style="background:${catColor(f.category)}">${esc(catName(f.category))}</div>`
    : `<div class="catbar none">Uncategorized</div>`;
  const media = f.thumb
    ? `<img loading="lazy" src="/thumb/${f.thumb}" alt=""
         onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'noimg',textContent:'no preview'}))">`
    : `<div class="noimg">${esc((f.ext || "").replace(".", "").toUpperCase() || "?")}${
         f.error ? "<br><small>not decoded</small>" : ""}</div>`;
  el.innerHTML = `
    ${media}
    ${f.kind === "video" ? `<div class="scrub"><i></i></div>
      <span class="vid">▶ ${fmtDur(f.duration)}</span>` : ""}
    <div class="badges">${hit}${err}${dist}${faces}${gps}</div>
    ${stack}
    ${f.reviewed ? `<span class="rev">✓</span>` : ""}
    ${catbar}`;
  if (f.kind === "video" && f.has_keyframes) enableScrub(el, f);
  return el;
}

function render(files) {
  const frag = document.createDocumentFragment();
  files.forEach(f => frag.appendChild(tileEl(f)));
  $("#grid").appendChild(frag);
}

function refreshTiles(ids) {
  ids.forEach(id => {
    const t = document.querySelector(`.tile[data-id="${id}"]`);
    const f = state.files.find(x => x.id === id);
    if (t && f) t.replaceWith(tileEl(f));
  });
  syncSel();
}
function refreshAllTiles() {
  document.querySelectorAll(".tile").forEach(t => {
    const f = state.files.find(x => x.id === +t.dataset.id);
    if (f) t.replaceWith(tileEl(f));
  });
}

/* ---------- video scrubbing ---------- */
function enableScrub(el, f) {
  const img = el.querySelector("img");
  const bar = el.querySelector(".scrub i");
  let frames = null, base = img.getAttribute("src"), loading = false;
  async function ensure() {
    if (frames || loading) return;
    if (state.keyframeCache.has(f.id)) { frames = state.keyframeCache.get(f.id); return; }
    loading = true;
    frames = await api(`/api/file/${f.id}/keyframes`);
    state.keyframeCache.set(f.id, frames); loading = false;
  }
  el.addEventListener("mouseenter", ensure);
  el.addEventListener("mousemove", e => {
    if (!frames || !frames.length) return;
    const r = el.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    const i = Math.min(frames.length - 1, Math.floor(x * frames.length));
    img.src = frames[i].thumb;
    bar.style.width = (x * 100) + "%";
  });
  el.addEventListener("mouseleave", () => { img.src = base; bar.style.width = "0"; });
}

/* ---------- selection / focus ---------- */
function toggleSel(id, additive, range) {
  if (range && state.lastClick != null) {
    const ids = state.files.map(f => f.id);
    let a = ids.indexOf(state.lastClick), b = ids.indexOf(id);
    if (a > b)[a, b] = [b, a];
    for (let i = a; i <= b; i++) state.sel.add(ids[i]);
  } else if (additive) {
    state.sel.has(id) ? state.sel.delete(id) : state.sel.add(id);
  } else {
    const only = state.sel.size === 1 && state.sel.has(id);
    state.sel.clear(); if (!only) state.sel.add(id);
  }
  state.lastClick = id;
  setFocus(id);
  syncSel();
}
function setFocus(id) {
  state.focus = id;
  document.querySelectorAll(".tile.focus").forEach(t => t.classList.remove("focus"));
  const t = document.querySelector(`.tile[data-id="${id}"]`);
  if (t) t.classList.add("focus");
  if (state.metaOpen) showMeta(id);
}
function syncSel() {
  document.querySelectorAll(".tile").forEach(t =>
    t.classList.toggle("sel", state.sel.has(+t.dataset.id)));
  $("#selCount").textContent = state.sel.size;
  $("#selbar").style.display = state.sel.size ? "flex" : "none";
  updateStat();
}
const selIds = () => state.sel.size ? [...state.sel]
  : (state.focus != null ? [state.focus] : []);

/* ---------- save state ---------- */
let saveTimer = null, pendingSaves = 0;
function setSaveState(s, detail) {
  const el = $("#saveState");
  el.className = s === "saving" ? "saving" : s === "error" ? "error" : "";
  el.title = detail || "";
  if (s === "saving") el.textContent = "Saving…";
  else if (s === "error") el.textContent = "⚠ Save failed — click to retry";
  else {
    const t = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    el.textContent = "All changes saved · " + t;
  }
}
/* wrap a mutating request so the header reflects it */
async function save(url, body, method = "POST") {
  pendingSaves++;
  setSaveState("saving");
  try {
    const r = await fetch(url, {
      method,
      headers: body != null ? { "Content-Type": "application/json" } : undefined,
      body: body != null ? JSON.stringify(body) : undefined,
    });
    const j = await r.json().catch(() => ({}));
    pendingSaves--;
    if (!r.ok || j.error) { setSaveState("error", j.message || r.statusText); return j; }
    if (pendingSaves === 0) setSaveState("saved");
    return j;
  } catch (e) {
    pendingSaves--; setSaveState("error", String(e)); throw e;
  }
}
$("#saveState").onclick = () => { if ($("#saveState").className === "error") setSaveState("saved"); };

/* ---------- mutations ---------- */
async function categorize(ids, cat) {
  await save("/api/categorize", { ids, category: cat });
  ids.forEach(id => { const f = state.files.find(x => x.id === id); if (f) f.category = cat; });
  refreshTiles(ids);
  if (state.metaOpen && ids.includes(state.focus)) showMeta(state.focus);
  toast(`${cat ? catName(cat) : "Uncategorized"} → ${ids.length} file(s)`);
}
async function review(ids, val = true) {
  await save("/api/review", { ids, reviewed: val });
  ids.forEach(id => { const f = state.files.find(x => x.id === id); if (f) f.reviewed = val ? 1 : 0; });
  refreshTiles(ids);
  if (state.metaOpen && ids.includes(state.focus)) showMeta(state.focus);
  toast(`${val ? "Reviewed" : "Unreviewed"} → ${ids.length}`);
}
async function tagIds(ids, preset) {
  const t = preset ?? prompt("Add tag(s), comma separated:");
  if (!t) return;
  await save("/api/tag", { ids, add: t.split(",").map(s => s.trim()).filter(Boolean) });
  if (state.metaOpen && ids.includes(state.focus)) showMeta(state.focus);
  toast("Tagged " + ids.length);
}

/* ---------- metadata pane ---------- */
function toggleMeta(force) {
  state.metaOpen = force ?? !state.metaOpen;
  try { localStorage.setItem("gleapp.meta", state.metaOpen ? "1" : "0"); } catch (e) {}
  $("#meta").classList.toggle("hidden", !state.metaOpen);
  $("#btnMeta").style.borderColor = state.metaOpen ? "var(--accent)" : "";
  if (state.metaOpen && state.focus != null) showMeta(state.focus);
}

let pendingNoteFlush = null, noteCtx = null;
window.addEventListener("pagehide", () => {
  if (!noteCtx || !noteCtx.ta || !noteCtx.ta.isConnected) return;
  try {
    navigator.sendBeacon(`/api/file/${noteCtx.id}/note`,
      new Blob([JSON.stringify({ notes: noteCtx.ta.value })],
        { type: "application/json" }));
  } catch (e) {}
});
async function showMeta(id) {
  if (pendingNoteFlush) { try { await pendingNoteFlush(); } catch (e) {} pendingNoteFlush = null; }
  const f = await api("/api/file/" + id);
  if (!f || f.id == null) {           // f.error here is the file's *processing*
    $("#meta").innerHTML = `<div class="empty">Could not load file #${id}.</div>`;
    return;                           // error column, not an API failure
  }
  const m = $("#meta");
  const isVid = f.kind === "video";
  let flags = "";
  try {
    const vf = f.vic_flags ? JSON.parse(f.vic_flags) : null;
    if (vf) flags = Object.entries(vf).filter(([, v]) => v === true)
      .map(([k]) => k.replace(/_/g, " ")).join(", ");
  } catch (e) {}
  const rows = [
    ["Source", f.source], ["Type", f.kind],
    ["Original name", f.orig_name || ""],
    ["Original path", f.orig_path || ""],
    ["MIME", f.mime || ""],
    ["VIC MediaID", f.media_id ?? ""],
    ["VIC flags", flags],
    ["Size", fmtSize(f.size)],
    ["Dimensions", f.width ? `${f.width}×${f.height}` : ""],
    ["Duration", f.duration ? fmtDur(f.duration) : ""],
    ["Captured", f.created_dt || ""], ["Camera", f.camera || ""],
    ["Faces", f.faces || 0], ["Skin ratio", f.skin_ratio ?? ""],
    ["Reviewed", f.reviewed ? `yes — ${f.reviewed_by || ""}` : "no"],
    ["Known hash", f.hashset_hit || ""],
    ["Exact copies", f.stack && f.stack.length > 1 ? `${f.stack.length}` : "none"],
    ["Visually similar", f.vstack && f.vstack.length > 1 ? `${f.vstack.length} files` : "none"],
    ["Similar-group", f.cluster_id ? `#${f.cluster_id} (${f.cluster_size})` : "—"],
    ["Error", f.error || ""],
  ].filter(r => r[1] !== "" && r[1] != null);
  const hashes = [["MD5", f.md5], ["SHA1", f.sha1], ["SHA256", f.sha256], ["pHash", f.phash]]
    .filter(r => r[1]);
  const gps = f.gps_lat != null
    ? `<tr><td>GPS</td><td>${f.gps_lat}, ${f.gps_lon}
       <a target="_blank" href="https://www.openstreetmap.org/?mlat=${f.gps_lat}&mlon=${f.gps_lon}#map=15/${f.gps_lat}/${f.gps_lon}">map</a></td></tr>` : "";

  const preview = f.thumb
    ? `<img src="/thumb/${f.thumb}" alt=""
         onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'noimg',textContent:'no preview'}))">
       <button class="btn sm full" id="mFull">View full size</button>`
    : `<div class="noimg">no preview<br><small>${esc(f.error || "not processed")}</small></div>`;
  const nm = (f.orig_name || f.rel_path || f.path || "").split(/[\\/]/).pop();
  m.innerHTML = `
    <div class="preview">${preview}</div>
    <div class="body">
      <h2>${esc(nm || "file #" + f.id)}</h2>
      <div class="path">${esc(f.path || f.rel_path || "")}</div>
      ${f.error ? `<div class="path" style="color:var(--danger)">⚠ ${esc(f.error)}</div>` : ""}

      <div>Category:
        <span class="catnow" style="background:${catColor(f.category)}">
          ${esc(catName(f.category))}</span></div>
      <div class="row" id="mCats"></div>
      <div class="row">
        <button class="btn sm" id="mRev">${f.reviewed ? "Unmark reviewed" : "Mark reviewed"}</button>
        <button class="btn sm" id="mSim">Find similar</button>
        <button class="btn sm" id="mTag">Add tag</button>
      </div>
      <div>${(f.tags || []).map(t =>
        `<span class="pill">${esc(t)} <b data-t="${esc(t)}">×</b></span>`).join("")}</div>

      ${f.keyframes && f.keyframes.length ? `<div class="muted" style="margin-top:8px">Key frames</div>
        <div class="film">${f.keyframes.map(k =>
          `<img src="${k.thumb}" title="${fmtDur(k.ts)}" data-ts="${k.ts}">`).join("")}</div>` : ""}

      <table>
        ${rows.map(r => `<tr><td>${r[0]}</td><td>${esc(r[1])}</td></tr>`).join("")}
        ${gps}
        ${hashes.map(r => `<tr><td>${r[0]}</td><td class="mono">${esc(r[1])}</td></tr>`).join("")}
      </table>

      <div class="muted" style="margin-top:8px">Notes <span id="mNoteState" class="muted" style="font-size:11px"></span></div>
      <textarea id="mNotes" placeholder="autosaves as you type">${esc(f.notes || "")}</textarea>

      ${f.stack && f.stack.length > 1 ? `<div class="muted">Exact copies (${f.stack.length})</div>
        <div class="film">${f.stack.filter(s => s.thumb).map(s =>
          `<img src="/thumb/${s.thumb}" title="${esc(s.rel_path)}" data-open="${s.id}">`).join("")}</div>` : ""}
      ${f.vstack && f.vstack.length > 1 ? `<div class="muted" style="color:var(--accent)">Visually similar (${f.vstack.length})</div>
        <div class="film">${f.vstack.filter(s => s.thumb || !s.error).map(s =>
          `<img src="/thumb/${s.thumb || ""}" title="${esc(s.orig_name || s.rel_path)}" data-open="${s.id}"
             onerror="this.style.visibility='hidden'">`).join("")}</div>
        <div class="row"><button class="btn sm" id="mVstack">Show only this group</button></div>` : ""}
    </div>`;

  $("#mCats").innerHTML = activeCats().map((c, i) =>
    `<button class="btn sm" data-c="${c.code}" style="border-color:${c.color}"
      title="key ${i + 1}">${esc(c.name || "Cat " + c.code)}</button>`).join("")
    + `<button class="btn sm" data-c="0">Clear</button>`;
  $("#mCats").querySelectorAll("[data-c]").forEach(b =>
    b.onclick = () => categorize([id], +b.dataset.c));
  $("#mRev").onclick = () => review([id], !f.reviewed);
  $("#mSim").onclick = () => showSimilar(id);
  $("#mTag").onclick = () => tagIds([id]);
  if ($("#mFull")) $("#mFull").onclick = () => openViewer(id);
  if ($("#mVstack")) $("#mVstack").onclick = () => {
    state.vstack = f.vstack_id; state.page = 1; load();
    $("#simBanner").style.display = "flex";
    $("#simId").textContent = `visual-match group (${f.vstack.length})`;
  };
  m.querySelectorAll("[data-t]").forEach(b => b.onclick = () =>
    save("/api/tag", { ids: [id], remove: [b.dataset.t] }).then(() => showMeta(id)));

  // notes: autosave ~0.9s after typing stops, and immediately on blur
  const ta = $("#mNotes");
  noteCtx = { id, ta };
  let noteT = null, lastSent = ta.value;
  const flush = async () => {
    clearTimeout(noteT);
    if (ta.value === lastSent) return;
    lastSent = ta.value;
    $("#mNoteState").textContent = "saving…";
    await save(`/api/file/${id}/note`, { notes: ta.value });
    const f2 = state.files.find(x => x.id === id); if (f2) f2.notes = ta.value;
    $("#mNoteState").textContent = "saved";
    setTimeout(() => { if ($("#mNoteState")) $("#mNoteState").textContent = ""; }, 1500);
  };
  ta.addEventListener("input", () => {
    $("#mNoteState").textContent = "editing…";
    clearTimeout(noteT); noteT = setTimeout(flush, 900);
  });
  ta.addEventListener("blur", flush);
  pendingNoteFlush = flush;
  m.querySelectorAll(".film img[data-ts]").forEach(im =>
    im.onclick = () => openViewer(id, +im.dataset.ts));
  m.querySelectorAll(".film img[data-open]").forEach(im =>
    im.onclick = () => { setFocus(+im.dataset.open); });
}

/* ---------- full-size viewer ---------- */
function openViewer(id, ts) {
  const f = state.files.find(x => x.id === id) || {};
  const w = $("#vWrap");
  if (f.kind === "video") {
    w.innerHTML = `<video src="/media/${id}" controls autoplay></video>`;
    if (ts) w.querySelector("video").currentTime = ts;
  } else {
    // /view transcodes HEIC/TIFF/RAW that the browser can't render
    w.innerHTML = `<img src="/view/${id}" alt=""
      onerror="this.replaceWith(Object.assign(document.createElement('div'),
        {className:'noimg',style:'padding:40px',
         innerHTML:'This file can\\'t be displayed<br><small>GPU texture / proprietary format — try the original file</small>'}))">`;
  }
  $("#viewer").style.display = "block";
}
function closeViewer() { $("#viewer").style.display = "none"; $("#vWrap").innerHTML = ""; }
$("#vClose").onclick = closeViewer;
$("#viewer").addEventListener("click", e => { if (e.target.id === "viewer") closeViewer(); });

/* ---------- context menu ---------- */
function openCtx(x, y, ids) {
  state.ctxIds = ids;
  const many = ids.length > 1 ? ` (${ids.length})` : "";
  const catBtns = activeCats().map((c, i) =>
    `<button data-a="c${c.code}"><span class="dot" style="background:${c.color}"></span>
      ${esc(c.name || "Category " + c.code)}${many} <span class="muted">${i + 1}</span></button>`).join("");
  $("#ctx").innerHTML = `
    <button data-a="similar">\u{1F50D} Find similar images</button>
    <button data-a="meta">ℹ Show details</button>
    <button data-a="full">⤢ View full size</button>
    <div class="sep"></div>
    ${catBtns}
    <button data-a="c0"><span class="dot" style="background:#3a3f4b"></span>Clear category${many} <span class="muted">0</span></button>
    <div class="sep"></div>
    <button data-a="rev">✓ Mark reviewed${many}</button>
    <button data-a="unrev">Mark not reviewed${many}</button>
    <button data-a="tag">\u{1F3F7} Add tag${many}</button>
    <div class="sep"></div>
    <button data-a="md5">#️⃣ Export MD5s${many}</button>
    <button data-a="mediafile">Open original file</button>`;
  const c = $("#ctx");
  c.style.display = "block";
  c.style.left = Math.min(x, innerWidth - c.offsetWidth - 6) + "px";
  c.style.top = Math.min(y, innerHeight - c.offsetHeight - 6) + "px";
}
function closeCtx() { $("#ctx").style.display = "none"; }

$("#ctx").addEventListener("click", e => {
  const btn = e.target.closest("button"); if (!btn) return;
  const a = btn.dataset.a; const ids = state.ctxIds;
  closeCtx();
  if (a === "similar") return showSimilar(ids[0]);
  if (a === "meta") { toggleMeta(true); return setFocus(ids[0]); }
  if (a === "full") return openViewer(ids[0]);
  if (a === "mediafile") return window.open("/media/" + ids[0], "_blank");
  if (a === "md5") return exportMd5(ids);
  if (a[0] === "c") return categorize(ids, +a.slice(1));
  if (a === "rev") return review(ids, true);
  if (a === "unrev") return review(ids, false);
  if (a === "tag") return tagIds(ids);
});

/* ---------- category editor ---------- */
function renderCatEd() {
  const rows = state.cats.filter(c => c.code !== 0).sort((a, b) => a.position - b.position);
  $("#catRows").innerHTML = rows.map(c => `
    <div class="cat" draggable="true" data-code="${c.code}">
      <span class="grip">☰</span>
      <span class="dot" style="background:${c.color}"></span>
      <input type="text" value="${esc(c.name)}" placeholder="Category ${c.code} (unnamed)">
      <button class="btn sm danger" data-del="${c.code}">Delete</button>
    </div>`).join("") || `<div class="muted">No categories yet — add one below.</div>`;

  $("#catRows").querySelectorAll("input").forEach(inp => {
    inp.addEventListener("change", async () => {
      const code = +inp.closest(".cat").dataset.code;
      await save("/api/categories/" + code, { name: inp.value.trim() }, "PATCH");
      await refreshCats(); renderCatEd(); refreshAllTiles();
      if (state.metaOpen && state.focus != null) showMeta(state.focus);
    });
  });
  $("#catRows").querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    const code = +b.dataset.del;
    const used = state.files.filter(f => f.category === code).length;
    let q = `?`;
    if (used && !confirm(
      `${used} loaded file(s) use this category.\nOK = keep their label but hide the category.\nCancel = abort.`))
      return;
    await save("/api/categories/" + code + q, null, "DELETE");
    await refreshCats(); renderCatEd(); refreshAllTiles();
  });

  // drag reorder
  let dragCode = null;
  $("#catRows").querySelectorAll(".cat").forEach(row => {
    row.addEventListener("dragstart", () => { dragCode = +row.dataset.code; row.classList.add("drag"); });
    row.addEventListener("dragend", () => row.classList.remove("drag"));
    row.addEventListener("dragover", e => e.preventDefault());
    row.addEventListener("drop", async e => {
      e.preventDefault();
      const target = +row.dataset.code;
      if (dragCode == null || dragCode === target) return;
      const order = rows.map(c => c.code).filter(c => c !== dragCode);
      order.splice(order.indexOf(target), 0, dragCode);
      await save("/api/categories/reorder", { codes: order });
      await refreshCats(); renderCatEd();
    });
  });
}
$("#btnCats").onclick = () => { renderCatEd(); $("#catEd").style.display = "block"; };
$("#catClose").onclick = () => { $("#catEd").style.display = "none"; refreshAllTiles(); };
$("#catEd").addEventListener("click", e => { if (e.target.id === "catEd") $("#catClose").click(); });
$("#catAdd").onclick = async () => {
  await save("/api/categories", { name: "" });
  await refreshCats(); renderCatEd();
};

/* ---------- grid events ---------- */
$("#grid").addEventListener("click", e => {
  const t = e.target.closest(".tile"); if (!t) return;
  toggleSel(+t.dataset.id, e.ctrlKey || e.metaKey, e.shiftKey);
});
$("#grid").addEventListener("dblclick", e => {
  const t = e.target.closest(".tile"); if (t) openViewer(+t.dataset.id);
});
$("#grid").addEventListener("contextmenu", e => {
  const t = e.target.closest(".tile"); if (!t) return;
  e.preventDefault();
  const id = +t.dataset.id;
  const ids = state.sel.has(id) && state.sel.size > 1 ? [...state.sel] : [id];
  if (ids.length === 1) { state.sel.clear(); state.sel.add(id); setFocus(id); syncSel(); }
  openCtx(e.clientX, e.clientY, ids);
});
document.addEventListener("click", e => { if (!e.target.closest("#ctx")) closeCtx(); });
document.addEventListener("scroll", closeCtx, true);

/* ---------- keyboard ---------- */
function moveFocus(delta) {
  const ids = state.files.map(f => f.id);
  let i = ids.indexOf(state.focus);
  if (i < 0) { i = 0; }
  else {
    const next = i + delta;
    if (next < 0 && state.page > 1) return gotoPage(state.page - 1);
    if (next >= ids.length && !state.similarOf
        && state.page < Math.ceil(state.total / state.pageSize))
      return gotoPage(state.page + 1);
    i = Math.min(ids.length - 1, Math.max(0, next));
  }
  const id = ids[i];
  state.sel.clear(); state.sel.add(id); setFocus(id); syncSel();
  document.querySelector(`.tile[data-id="${id}"]`)?.scrollIntoView({ block: "nearest" });
}
document.addEventListener("keydown", e => {
  if (/input|textarea|select/i.test(e.target.tagName)) return;
  if (e.key === "Escape") {
    closeCtx(); closeViewer();
    $("#catEd").style.display = "none"; $("#reportDlg").style.display = "none";
    return;
  }
  if (e.key === "PageDown") { e.preventDefault(); return gotoPage(state.page + 1); }
  if (e.key === "PageUp") { e.preventDefault(); return gotoPage(state.page - 1); }
  if (e.key === "Home" && e.ctrlKey) { e.preventDefault(); return gotoPage(1); }
  if (e.key === "End" && e.ctrlKey) {
    e.preventDefault();
    return gotoPage(Math.ceil(state.total / state.pageSize));
  }
  if (e.key === "ArrowRight") { e.preventDefault(); return moveFocus(1); }
  if (e.key === "ArrowLeft") { e.preventDefault(); return moveFocus(-1); }
  const ids = selIds();
  if (e.key >= "1" && e.key <= "9") {
    const c = activeCats()[+e.key - 1];
    if (c && ids.length) categorize(ids, c.code);
  } else if (e.key === "0" && ids.length) categorize(ids, 0);
  else if (e.key.toLowerCase() === "r" && ids.length) review(ids, true);
  else if (e.key.toLowerCase() === "f" && ids.length) showSimilar(ids[0]);
  else if (e.key.toLowerCase() === "i") toggleMeta();
  else if (e.key.toLowerCase() === "a") {
    e.preventDefault(); state.files.forEach(f => state.sel.add(f.id)); syncSel();
  }
});

/* ---------- filter wiring ---------- */
["#fq", "#fkind", "#fcat", "#frev", "#fsrc", "#fclu", "#ffaces", "#fgps",
 "#fhit", "#ferr", "#fskin", "#fcollapse", "#fsort"].forEach(s => {
  const el = $(s);
  el.addEventListener(s === "#fq" ? "input" : "change", debounce(reload, 250));
});
$("#fskin").addEventListener("input", () => $("#skinv").textContent = $("#fskin").value);
$("#ftile").addEventListener("input", () => {
  document.documentElement.style.setProperty("--tile", $("#ftile").value + "px");
  try { localStorage.setItem("gleapp.tile", $("#ftile").value); } catch (e) {}
});
$("#fpagesize").addEventListener("change", () => {
  state.pageSize = +$("#fpagesize").value || 200;
  try { localStorage.setItem("gleapp.pagesize", state.pageSize); } catch (e) {}
  state.page = 1; load();
});
$("#simBack").onclick = () => { state.vstack = null; load(); };
$("#btnMeta").onclick = () => toggleMeta();

$("#selbar").addEventListener("click", e => {
  const b = e.target.closest("[data-cat]");
  if (b) categorize([...state.sel], +b.dataset.cat);
});
$("#selRev").onclick = () => review([...state.sel], true);
$("#selTag").onclick = () => tagIds([...state.sel]);
$("#selClear").onclick = () => { state.sel.clear(); syncSel(); };
/* ---------- export / report dialog ---------- */
async function openReportDlg() {
  const s = await api("/api/stats").catch(() => ({}));
  const cats = Object.entries(s.by_category || {})
    .filter(([k]) => +k !== 0).reduce((a, [, v]) => a + v, 0);
  $("#scAll").textContent = s.total ? `(${s.total.toLocaleString()})` : "";
  $("#scCat").textContent = `(${cats.toLocaleString()})`;
  $("#scRev").textContent = `(${(s.reviewed || 0).toLocaleString()})`;
  $("#scSel").textContent = `(${state.sel.size})`;
  const selRadio = document.querySelector('input[name=rscope][value=selected]');
  selRadio.disabled = state.sel.size === 0;
  if (state.sel.size) selRadio.checked = true;
  $("#rfmtVic").style.display = $("#btnVic").style.display === "none" ? "none" : "block";
  $("#reportDlg").style.display = "block";
}
function closeReportDlg() { $("#reportDlg").style.display = "none"; }
$("#btnReport").onclick = openReportDlg;
$("#reportCancel").onclick = closeReportDlg;
$("#reportDlg").addEventListener("click", e => {
  if (e.target.id === "reportDlg") closeReportDlg();
});
$("#reportGo").onclick = async () => {
  const scope = document.querySelector("input[name=rscope]:checked").value;
  const fmt = [...document.querySelectorAll(".rfmt:checked")].map(c => c.value);
  if (!fmt.length) return toast("Pick at least one format");
  const body = { format: fmt, scope };
  if (scope === "selected") body.ids = [...state.sel];
  closeReportDlg();
  const r = await api("/api/report", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (r.error) return toast(r.message || "Export failed");
  toast(`Exported (${r.scope}) → ${r.dir}`);
};

async function exportMd5(ids) {
  if (!ids.length) return toast("Select some files first");
  const r = await api("/api/export/md5", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope: "selected", ids })
  });
  if (r.error) return toast(r.message || "MD5 export failed");
  toast(`${r.count} MD5 hash(es) → ${r.path}`);
}
$("#selMd5").onclick = () => exportMd5([...state.sel]);

/* ---------- face / skin screening ---------- */
function updateScreenInfo(scr) {
  if (!scr) return;
  const info = $("#screenInfo"), btn = $("#btnScreen");
  if (scr.backend === "none") {
    info.textContent = "Face detection unavailable in this build.";
    btn.style.display = "none";
  } else if (!scr.done) {
    info.textContent = `Not screened yet — "Has faces" and skin ratio need this. (${scr.backend})`;
    btn.style.display = "";
    btn.textContent = `Run screening (${scr.screenable.toLocaleString()} files)`;
  } else {
    info.textContent = `Screened: ${scr.with_faces.toLocaleString()} with faces, `
      + `${scr.with_skin.toLocaleString()} with skin tone. (${scr.backend})`;
    btn.style.display = "";
    btn.textContent = "Re-run screening";
  }
}
function pollJobInline(label, done) {
  const poll = async () => {
    const j = await api("/api/job");
    if (j.stage === "error") { toast(label + " error: " + j.error); return done && done(false); }
    if (j.stage === "done" || !j.running) return done && done(true, j);
    $("#screenInfo").textContent = j.total
      ? `${j.message || label} ${j.done}/${j.total}` : (j.message || label);
    setTimeout(poll, 900);
  };
  poll();
}
$("#btnRetryErr").onclick = async () => {
  const r = await api("/api/reprocess-errors", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start");
  $("#btnRetryErr").disabled = true;
  toast(`Retrying ${r.count} failed files…`);
  pollJobInline("Retrying failed files", (ok, j) => {
    $("#btnRetryErr").disabled = false;
    if (ok) {
      toast(`Recovered ${j.stats?.recovered ?? 0} of ${r.count} files`);
      location.reload();
    }
  });
};
$("#btnRedup").onclick = async () => {
  const r = await api("/api/redup", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start");
  $("#btnRedup").disabled = true;
  const poll = async () => {
    const j = await api("/api/job");
    if (j.stage === "error") { toast("Re-scan error: " + j.error); $("#btnRedup").disabled = false; return; }
    if (j.stage === "done" || !j.running) {
      $("#btnRedup").disabled = false;
      toast(`Re-scan done: ${j.stats?.visual_stacks ?? 0} visual stacks`);
      load();
      return;
    }
    $("#screenInfo").textContent = j.message || "working…";
    setTimeout(poll, 800);
  };
  poll();
};
$("#btnScreen").onclick = async () => {
  if (!confirm("Run face + skin-tone screening over every thumbnail?\n"
      + "Runs in the background; you can keep working.")) return;
  const r = await api("/api/screen", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start screening");
  $("#btnScreen").disabled = true;
  const poll = async () => {
    const j = await api("/api/job");
    if (j.stage === "error") {
      $("#screenInfo").textContent = "Screening error: " + (j.error || "");
      $("#btnScreen").disabled = false; return;
    }
    if (j.stage === "done" || !j.running) {
      $("#btnScreen").disabled = false;
      const c = await api("/api/context");
      updateScreenInfo(c.screening);
      toast("Screening complete");
      load();
      return;
    }
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    $("#screenInfo").textContent = `Screening… ${j.done}/${j.total} (${pct}%)`;
    setTimeout(poll, 1000);
  };
  poll();
};
$("#btnSnapshot").onclick = async () => {
  const label = prompt("Optional label for this snapshot:", "") ?? "";
  if (label === null) return;
  const r = await save("/api/snapshot", { label: label.trim() || null });
  if (!r.error) toast("Snapshot saved: " + r.name);
};
$("#btnClose").onclick = async () => {
  if (pendingNoteFlush) { try { await pendingNoteFlush(); } catch (e) {} }
  setSaveState("saving");
  await api("/api/case/close", { method: "POST" });
  location.reload();   // boot() sees no case -> shows the launcher
};
$("#btnVic").onclick = async () => {
  const only = confirm(
    "OK  = export ALL media (categorized + not)\n" +
    "Cancel = export only the media you have categorized");
  const r = await api("/api/report", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format: ["vic"], only_categorized: !only })
  });
  if (r.error) toast(r.message || "VIC export failed");
  else toast("Project VIC file written: " + (r.written[0] || r.dir));
};

/* ---------- launcher ---------- */
const Lr = { native: false, sources: [] };
function fmtAgo(ts) {
  const s = (Date.now() / 1000) - ts;
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
async function pick(kind) {
  if (Lr.native) {
    try {
      const r = await api("/api/pick", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind })
      });
      if (r && !r.error) return r.path || null;   // path or user-cancelled (null)
      if (r && r.error) toast("File dialog: " + r.error + " — type the path instead");
    } catch (e) {
      toast("File dialog unavailable — type the path instead");
    }
  }
  return prompt(kind === "folder"
    ? "Folder path:" : "Path to .json job file:") || null;
}
function renderSources() {
  $("#srcList").innerHTML = Lr.sources.map((s, i) =>
    `<div class="s"><span>${s.kind === "spec" ? "\u{1F4C4} " : "\u{1F4C1} "}${esc(s.path)}</span>
     <b data-rm="${i}" style="cursor:pointer;color:var(--danger)">×</b></div>`).join("");
  $("#srcList").querySelectorAll("[data-rm]").forEach(b =>
    b.onclick = () => { Lr.sources.splice(+b.dataset.rm, 1); renderSources(); });
}
function showLauncher(ctx) {
  Lr.native = !!ctx.native;
  $("#launcher").style.display = "block";
  $("#main").style.display = "none";
  const rc = ctx.recent || [];
  if (rc.length) {
    $("#recentCard").style.display = "block";
    $("#recentList").innerHTML = rc.map(r => {
      const n = (typeof r.files === "number")
        ? r.files.toLocaleString() + (r.files === 1 ? " file" : " files") : "";
      const bits = [esc(r.path), n, fmtAgo(r.opened)].filter(Boolean).join(" · ");
      return `<button data-path="${esc(r.path)}">${esc(r.name)}<small>${bits}</small></button>`;
    }).join("");
    $("#recentList").querySelectorAll("button").forEach(b =>
      b.onclick = () => openCase(b.dataset.path));
  }
}
async function openCase(path) {
  const r = await api("/api/case/open", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path })
  });
  if (r.error) return toast(r.message || "Could not open case");
  location.reload();
}
async function pollJob() {
  let j;
  try { j = await api("/api/job"); }
  catch (e) { setTimeout(pollJob, 800); return; }
  const pct = j.total ? Math.round(100 * j.done / j.total) : (j.running ? 5 : 0);
  $("#jobProg").style.display = "block";
  $("#jobProg").querySelector("i").style.width = pct + "%";
  $("#jobMsg").textContent =
    (j.stage === "process" && (!j.total || j.done < j.total))
      ? `Processing ${j.done}/${j.total} (${pct}%)`
      : (j.message || j.stage);
  if (j.stage === "error") {
    $("#jobMsg").textContent = "Error: " + (j.error || "processing failed");
    $("#createGo").disabled = false;
    return;
  }
  if (j.stage === "done") {
    $("#jobMsg").textContent = "Done — opening case…";
    setTimeout(() => location.reload(), 600);
    return;
  }
  setTimeout(pollJob, 500);   // idle / starting / ingest / process
}
$("#openBrowse").onclick = async () => { const p = await pick("folder"); if (p) $("#openPath").value = p; };
$("#openGo").onclick = () => $("#openPath").value && openCase($("#openPath").value.trim());
$("#newBrowse").onclick = async () => { const p = await pick("folder"); if (p) $("#newPath").value = p; };
function addSource(p) {
  p = (p || "").trim().replace(/^["']|["']$/g, "");
  if (!p) return;
  const kind = /\.json$/i.test(p) ? "spec" : "folder";
  if (!Lr.sources.some(s => s.path === p)) Lr.sources.push({ kind, path: p });
  renderSources();
}
$("#addFolder").onclick = async () => addSource(await pick("folder"));
$("#addSpec").onclick = async () => addSource(await pick("file"));
$("#srcTypeAdd").onclick = () => { addSource($("#srcTypePath").value); $("#srcTypePath").value = ""; };
$("#srcTypePath").addEventListener("keydown", e => {
  if (e.key === "Enter") { addSource($("#srcTypePath").value); $("#srcTypePath").value = ""; }
});
$("#optKf").addEventListener("input", () => $("#kfv").textContent = $("#optKf").value);
$("#createGo").onclick = async () => {
  const path = $("#newPath").value.trim();
  if (!path) { toast("Choose a case folder"); $("#newPath").focus(); return; }
  if (!Lr.sources.length) { toast("Add at least one evidence folder or JSON job"); return; }

  const btn = $("#createGo");
  btn.disabled = true;
  $("#jobProg").style.display = "block";
  $("#jobMsg").textContent = "Creating case…";
  const fail = (m) => { $("#jobMsg").textContent = m; toast(m); btn.disabled = false; };

  const cr = await api("/api/case/create", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path, name: $("#newName").value.trim(), examiner: $("#newExaminer").value.trim()
    })
  }).catch(() => ({ error: true, message: "request failed" }));
  if (cr.error) return fail(cr.message || "Could not create case");

  const specs = Lr.sources.filter(s => s.kind === "spec").map(s => s.path);
  const folders = Lr.sources.filter(s => s.kind === "folder")
    .map(s => ({ name: s.path.split(/[\\/]/).filter(Boolean).pop(), path: s.path }));
  $("#jobMsg").textContent = "Starting ingest…";
  const ing = await api("/api/case/ingest", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      spec: specs[0] || null, sources: folders,
      options: { screen: $("#optScreen").checked, keyframes: +$("#optKf").value }
    })
  }).catch(() => ({ error: true, message: "request failed" }));
  if (ing.error) return fail(ing.message || "Ingest failed");
  pollJob();
};

/* ---------- boot ---------- */
(async function boot() {
  const c = await api("/api/context");
  if (c.needs_case) { showLauncher(c); return; }
  $("#launcher").style.display = "none";
  $("#main").style.display = "";
  $("#caseName").textContent = "GLEAPP — " + (c.case || "case");
  $("#examiner").textContent = c.examiner;
  document.title = "GLEAPP — " + (c.case || "");
  if (c.vic) $("#btnVic").style.display = "";
  updateScreenInfo(c.screening);
  if (c.errors > 0) {
    $("#errCount").textContent = `(${c.errors.toLocaleString()})`;
    $("#btnRetryErr").style.display = "";
    $("#btnRetryErr").textContent = `Retry ${c.errors.toLocaleString()} failed files`;
  }
  state.cats = c.categories || [];
  await refreshCats();
  c.sources.forEach(s => $("#fsrc").insertAdjacentHTML("beforeend", `<option>${esc(s)}</option>`));
  c.clusters.forEach(cl => $("#fclu").insertAdjacentHTML("beforeend",
    `<option value="${cl.id}">#${cl.id} (${cl.n})</option>`));
  try {
    if (localStorage.getItem("gleapp.meta") === "1") toggleMeta(true);
    const ps = localStorage.getItem("gleapp.pagesize");
    if (ps) { state.pageSize = +ps; $("#fpagesize").value = ps; }
    const tl = localStorage.getItem("gleapp.tile");
    if (tl) { $("#ftile").value = tl;
      document.documentElement.style.setProperty("--tile", tl + "px"); }
  } catch (e) {}
  await load();

  // reflect background auto-snapshots in the header tooltip
  let seenBackup = 0;
  setInterval(async () => {
    try {
      const s = await api("/api/save-state");
      if (s.last_backup && s.last_backup !== seenBackup) {
        seenBackup = s.last_backup;
        const t = new Date(s.last_backup * 1000)
          .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        $("#saveState").title = "Last backup snapshot: " + t
          + `  (auto every ${s.auto_interval_min} min)`;
      }
    } catch (e) {}
  }, 90000);
})();
