"use strict";
/* GLEAPP review gallery client */

const $ = (s, r = document) => r.querySelector(s);
const api = (u, opt) => fetch(u, opt).then(r => r.json());
const cssEsc = (typeof CSS !== "undefined" && CSS.escape)
  ? s => CSS.escape(s) : s => String(s).replace(/[^\w-]/g, "\\$&");
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const state = {
  files: [], total: 0, page: 1, pageSize: 200,
  sel: new Set(), lastClick: null, focus: null,
  similarOf: null, vstack: null, stack: null, ctxIds: [],
  cats: [],                       // [{code,name,color,notable,position,active}]
  keyframeCache: new Map(),
  metaOpen: false,
  sources: [],
  view: "grid",                   // "grid" | "list"
  listCols: null,                 // Set of visible column keys (list view)
  sortCol: "name", sortDir: "asc",
  colFilters: {},                 // { colKey: [{col,op,val}, …] }
  colWidths: {},                  // { colId: pixels } — list-view column widths
  tz: "UTC",                      // display timezone for epoch timestamps (not EXIF)
};

function toast(msg, ms, onClick) {
  const t = $("#toast");
  t.textContent = msg; t.style.display = "block";
  t.classList.toggle("clickable", !!onClick);
  t.onclick = onClick || null;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.style.display = "none", ms || 1800);
}

/* Ask the desktop/OS to open a folder in its file manager (best-effort - a
   plain browser tab with no native window has nowhere to send this). */
async function openFolder(path) {
  const r = await api("/api/open-folder", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path })
  });
  if (r.error) toast(r.message || "Could not open that folder");
}

/* Report exports also land in a 🔔 dropdown that stays until dismissed, since
   the toast confirming one is gone in a few seconds. */
let reportNotifications = [];
function addReportNotification(dir, label) {
  reportNotifications.unshift({
    id: Date.now() + Math.random(), dir, label, ts: new Date(),
  });
  reportNotifications = reportNotifications.slice(0, 8);
  renderNotifyMenu();
  $("#notifyDot").hidden = false;
}
function renderNotifyMenu() {
  const m = $("#notifyMenu");
  if (!reportNotifications.length) {
    m.innerHTML = "<div class='empty'>No report exports yet</div>";
    return;
  }
  m.innerHTML = reportNotifications.map(n => `
    <button type="button" class="nitem" data-id="${n.id}">
      <div class="nlbl"><span>${esc(n.label)}</span><span class="nts">${
        n.ts.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      }</span></div>
      <div class="ndir">${esc(n.dir)}</div>
    </button>`).join("")
    + `<div class="nfoot"><button type="button" id="notifyClear">Clear</button></div>`;
}
/* Header dropdowns (the recent-exports bell and the grouped ☰ Menu) share this
   open/close/position plumbing - only one is open at a time, and either closes
   on an outside click. */
function toggleHdrMenu(btn, menu) {
  if (menu.style.display === "block") { menu.style.display = "none"; return; }
  document.querySelectorAll(".hdrmenu").forEach(m => m.style.display = "none");
  const b = btn.getBoundingClientRect();
  menu.style.display = "block";
  menu.style.top = (b.bottom + 4) + "px";
  menu.style.left = Math.max(4, b.right - menu.offsetWidth) + "px";
}
document.addEventListener("click", e => {
  if (e.target.closest(".hdrmenu") || e.target.closest("#btnNotify") || e.target.closest("#btnMenu")
      || e.target.closest("#btnRefMenuLauncher"))
    return;
  document.querySelectorAll(".hdrmenu").forEach(m => m.style.display = "none");
});

function toggleNotifyMenu() {
  if ($("#notifyMenu").style.display === "block") {
    $("#notifyMenu").style.display = "none";
    return;
  }
  renderNotifyMenu();
  toggleHdrMenu($("#btnNotify"), $("#notifyMenu"));
  $("#notifyDot").hidden = true;
}
$("#btnNotify").onclick = toggleNotifyMenu;
$("#notifyMenu").addEventListener("click", e => {
  if (e.target.id === "notifyClear") {
    reportNotifications = [];
    renderNotifyMenu();
    return;
  }
  const item = e.target.closest(".nitem");
  if (!item) return;
  const n = reportNotifications.find(x => x.id == item.dataset.id);
  if (n) openFolder(n.dir);
  $("#notifyMenu").style.display = "none";
});

const fmtDur = s => {
  if (!s && s !== 0) return "";
  s = Math.round(s); const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, "0")}`;
};
const fmtSize = b => !b ? "" : b > 1e6 ? (b / 1048576).toFixed(1) + " MB"
  : (b / 1024).toFixed(0) + " KB";
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
const diskName = f => (f.rel_path || f.path || "").split(/[\\/]/).pop();
/* preferred display name: the original file name, else the on-disk name (an MD5 for VIC imports) */
const dispName = f => f.orig_name || diskName(f) || ("file #" + f.id);

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
  if ($("#fsrc").value) p.set("source", $("#fsrc").value);
  if ($("#forigin").value) p.set("origin", $("#forigin").value);
  if ($("#finarch").checked) p.set("in_archive", "1");
  if ($("#fdup").value) p.set("hasdup", $("#fdup").value);
  if ($("#ffaces").checked) p.set("faces", "1");
  if ($("#fgps").checked) p.set("has_gps", "1");
  if ($("#fhit").checked) p.set("hashset", "1");
  if ($("#fhashset").value) p.set("hashset_name", $("#fhashset").value);
  if ($("#fhidegood").checked) p.set("hidegood", "1");
  if ($("#ferr").checked) p.set("error", "1");
  if (state.vstack) p.set("vstack", state.vstack);
  if (state.stack) p.set("stack", state.stack);
  if (+$("#fskin").value > 0) p.set("min_skin", $("#fskin").value);
  // the list view always shows every row, duplicates included - collapsing is a
  // grid-only convenience
  if ($("#fcollapse").checked && state.view !== "list") p.set("dupes", "collapse");
  // both views sort by the same state.sortCol / state.sortDir, so switching view
  // never re-sorts the results; the grid's Sort dropdown and the list's column
  // headers are two ways to set it
  p.set("sort", state.sortCol || "name");
  p.set("dir", state.sortDir || "asc");
  if (state.view === "list") {
    const cf = [];
    for (const v of Object.values(state.colFilters))
      if (v && v.clauses) cf.push(...v.clauses);
    if (cf.length) p.set("colfilters", JSON.stringify(cf));
  }
  p.set("limit", state.pageSize);
  p.set("offset", (state.page - 1) * state.pageSize);
  return p;
}

/* load the current page (call reload() to also reset to page 1).
   opts.keepScroll: keep the current scroll position (used by the live refresh
   while a processing job is running). */
async function load(opts = {}) {
  state.similarOf = null;
  if (!state.vstack && !state.stack) $("#simBanner").style.display = "none";
  const scroll = { mainT: $("#main").scrollTop, mainL: $("#main").scrollLeft,
                   gridT: $("#grid").scrollTop, gridL: $("#grid").scrollLeft };
  const d = await api("/api/files?" + filterParams());
  state.total = d.total;
  const pages = Math.max(1, Math.ceil(d.total / state.pageSize));
  if (state.page > pages) { state.page = pages; return load(opts); }
  state.files = d.files;
  renderFiles(d.files);
  renderPager();
  updateStat();
  refreshSections();
  if (opts.keepScroll) {
    $("#main").scrollTop = scroll.mainT; $("#main").scrollLeft = scroll.mainL;
    $("#grid").scrollTop = scroll.gridT; $("#grid").scrollLeft = scroll.gridL;
  } else {
    $("#main").scrollTop = 0;
    // list view scrolls horizontally - a new sort/filter shouldn't fling it sideways
    if (state.view === "list") $("#main").scrollLeft = scroll.mainL;
  }
}
function reload() { state.page = 1; state.vstack = null; state.stack = null; state.sel.clear(); load(); }

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
  renderFiles(d.files);
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
  // face-match results carry similarity with no "distance" (that's a pHash-only concept)
  const dist = (f.distance != null || f.similarity != null) ? `<span class="b">${f.similarity}%</span>` : "";
  const faces = f.faces ? `<span class="b">${f.faces}\u{1F464}</span>` : "";
  const hit = f.hashset_hit
    ? (f.hashset_kind === "known-good"
        ? `<span class="b good" title="${esc(f.hashset_hit)}">${
             esc(f.hashset_hit.split(/[\s_-]/)[0].toUpperCase().slice(0, 6))}</span>`
        : f.hashset_hit === "Local Hash Stash"
          ? `<span class="b stash" title="In your local hash stash — you previously categorized this file">STASH</span>`
          : `<span class="b hit" title="${esc(f.hashset_hit)}">HASH</span>`)
    : "";
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
  const nm = dispName(f);
  el.title = nm;
  el.innerHTML = `
    <div class="thumb">
      ${media}
      ${f.kind === "video" ? `<div class="scrub"><i></i></div>
        <span class="vid">▶ ${fmtDur(f.duration)}</span>` : ""}
      <div class="badges">${hit}${err}${dist}${faces}${gps}</div>
      ${stack}
      <span class="selcheck">✓</span>
    </div>
    <div class="fname">${esc(nm)}</div>
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
  const defs = state.view === "list" ? visibleDefs() : null;
  ids.forEach(id => {
    const t = document.querySelector(`.tile[data-id="${id}"], .lvrow[data-id="${id}"]`);
    const f = state.files.find(x => x.id === id);
    if (t && f) t.replaceWith(state.view === "list" ? lvRowEl(f, defs) : tileEl(f));
  });
  syncSel();
}
function refreshAllTiles() {
  if (state.view === "list") return renderList(state.files);
  document.querySelectorAll(".tile").forEach(t => {
    const f = state.files.find(x => x.id === +t.dataset.id);
    if (f) t.replaceWith(tileEl(f));
  });
}

/* pick grid vs. details-list rendering. The list view keeps its header/filter
   row across reloads (renderList reuses it), so it clears #grid itself only when
   the column set changes — don't wipe #grid here for the list. */
function renderFiles(files) {
  const g = $("#grid");
  g.classList.toggle("aslist", state.view === "list");
  if (state.view === "list") { renderList(files); }
  else { g.innerHTML = ""; render(files); }
}

/* =========================================================================
   Details list view — every column, sortable + filterable
   ===================================================================== */
/* an epoch (UTC) rendered in the chosen display timezone, with DST — never
   applied to Captured/EXIF, which is camera-local wall time shown as-is */
let _tzFmt = null, _tzFmtFor = null;
// The times a filesystem stored with no zone on them, exactly as stored. FAT and
// exFAT keep a wall-clock reading and no zone at all, so no instant can be built
// from one and the FS written / created / accessed columns stay empty for those
// volumes. Shown as plain text on purpose: a date here would be given a zone by
// whatever renders it, and that zone would be invented. exFAT's per-time UTC
// offset is shown as stored rather than applied, for the same reason.
function fmtRecorded(f) {
  if (!f || !f.recorded_times) return "";
  let got;
  try { got = JSON.parse(f.recorded_times); } catch (e) { return ""; }
  if (!got || typeof got !== "object" || Array.isArray(got)) return "";
  return Object.entries(got).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join("; ");
}

function fmtEpoch(e) {
  if (!e) return "";
  const d = new Date(e * 1000);
  if (isNaN(d)) return "";
  const tz = state.tz && state.tz !== "UTC" ? state.tz : "UTC";
  try {
    if (_tzFmtFor !== tz) {
      _tzFmt = new Intl.DateTimeFormat("en-CA", {
        timeZone: tz, hourCycle: "h23",
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        timeZoneName: "short",
      });
      _tzFmtFor = tz;
    }
    const p = {};
    _tzFmt.formatToParts(d).forEach(x => { p[x.type] = x.value; });
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second} ${p.timeZoneName || ""}`.trim();
  } catch (_) {
    return d.toISOString().slice(0, 19).replace("T", " ") + " UTC";
  }
}
const shortHash = h => !h ? "" : h.length > 12 ? h.slice(0, 12) + "…" : h;

// key:      files column (server-side sort/filter target); "" = not sortable
// label:    header text
// type:     text | num | enum | date  (drives the filter widget)
// get(f):   display string
// mono:     render in monospace
// options:  for enum filters — array or () => array of {value,label}
// The three ways a file reaches a case. The keys are the stored files.origin
// values (gleapp/db.py ORIGINS).
//
// Two wordings, because the two places this appears have very different room.
// The "How recovered" filter is a sidebar select that can explain itself. A
// list-view column defaults to 130px for an enum, and the long phrases measure
// 185px and 202px, so every row of a case of deleted recoveries would render as
// "Recovered from a ..." and have to be hovered to be read. The column is headed
// "How recovered", which supplies the context the short form drops.
const ORIGIN_LABEL = {
  walk: "Walked, still listed",
  deleted: "Recovered from a deleted record",
  carve: "Carved from unclaimed space",
};
const ORIGIN_SHORT = {
  walk: "Walked",
  deleted: "Deleted record",
  carve: "Carved",
};

const LIST_DEFS = [
  { key: "", label: "", type: "", get: () => "", thumb: true },
  { key: "id", label: "ID", type: "num", get: f => f.id },
  // filterKey: filter/sort on this virtual column server-side, so it matches the
  // fallback chain shown (a folder-ingest file has no orig_name/orig_path).
  { key: "orig_name", filterKey: "name", label: "Name", type: "text", alt: "name",
    get: f => dispName(f) },
  { key: "rel_path", label: "Stored name", type: "text", alt: "diskname",
    get: f => diskName(f) },
  // "File path" = the device path from the Project VIC JSON; for a folder
  // ingest (no VIC data) it's the source path. Not the local unpack folder.
  { key: "orig_path", filterKey: "file_path", label: "File path", type: "text",
    get: f => f.orig_path || (f.media_id ? "" : (f.path || f.rel_path)) || "" },
  { key: "path", label: "File path (working copy)", type: "text", get: f => f.path || "" },
  { key: "rel_path", label: "Relative path", type: "text", alt: "relpath",
    get: f => f.rel_path || "" },
  { key: "orig_name", label: "Original name", type: "text", get: f => f.orig_name || "" },
  { key: "source", label: "Source", type: "enum", get: f => f.source || "",
    options: () => state.sources.map(s => ({ value: s, label: s })) },
  // How the row was recovered. Only an acquisition has more than one answer, so
  // this is off by default and turned on from the column picker.
  { key: "origin", label: "How recovered", type: "enum", get: f => ORIGIN_SHORT[f.origin] || "",
    options: () => Object.entries(ORIGIN_SHORT).map(([value, label]) => ({ value, label })) },
  { key: "kind", label: "Type", type: "enum", get: f => f.kind || "",
    options: [{ value: "image", label: "image" }, { value: "video", label: "video" },
              { value: "other", label: "other" }] },
  { key: "ext", label: "Ext", type: "text", get: f => f.ext || "" },
  { key: "mime", label: "MIME", type: "text", get: f => f.mime || "" },
  { key: "size", label: "Size", type: "num", get: f => fmtSize(f.size), sortRaw: true,
    filterBareOp: "min", filterPh: "≥ KB · >1mb · 100kb-2mb" },
  { key: "width", label: "Width", type: "num", get: f => f.width || "" },
  { key: "height", label: "Height", type: "num", get: f => f.height || "" },
  { key: "duration", label: "Duration", type: "num", get: f => fmtDur(f.duration),
    sortRaw: true, filterBareOp: "min", filterPh: "≥ sec · >0:30 · 1:00-5:00" },
  { key: "created_dt", label: "Captured (EXIF)", type: "text", get: f => f.created_dt || "",
    filterPh: "2024-06" },
  { key: "ctime", label: "FS created", type: "epoch", get: f => fmtEpoch(f.ctime) },
  { key: "mtime", label: "FS written", type: "epoch", get: f => fmtEpoch(f.mtime) },
  { key: "atime", label: "FS accessed", type: "epoch", get: f => fmtEpoch(f.atime) },
  { key: "recorded_times", label: "Recorded (as stored)", type: "text",
    get: f => fmtRecorded(f), filterPh: "2024-06" },
  { key: "ingested_at", label: "Ingested", type: "epoch", get: f => fmtEpoch(f.ingested_at) },
  { key: "md5", label: "MD5", type: "text", mono: true, get: f => f.md5 || "" },
  { key: "sha1", label: "SHA-1", type: "text", mono: true, get: f => shortHash(f.sha1) },
  { key: "sha256", label: "SHA-256", type: "text", mono: true, get: f => shortHash(f.sha256) },
  { key: "phash", label: "pHash", type: "text", mono: true, get: f => f.phash || "" },
  { key: "camera", label: "Camera", type: "text", get: f => f.camera || "" },
  { key: "gps_lat", label: "GPS lat", type: "num", get: f => f.gps_lat ?? "",
    filterContains: true, filterPh: "45.4 · >40 · set" },
  { key: "gps_lon", label: "GPS lon", type: "num", get: f => f.gps_lon ?? "",
    filterContains: true, filterPh: "-1.9 · <10 · set" },
  { key: "faces", label: "Faces", type: "num", get: f => f.faces || 0, filterPh: ">0 · 1-3 · none" },
  { key: "skin_ratio", label: "Skin ratio", type: "num",
    get: f => f.skin_ratio == null ? "" : (f.skin_ratio * 100).toFixed(0) + "%", sortRaw: true,
    filterScale: 0.01, filterBareOp: "min", filterPh: "≥ % · >30 · 10-50" },
  { key: "category", label: "Category", type: "enum", cat: true,
    get: f => catName(f.category),
    options: () => state.cats.slice().sort((a, b) => a.position - b.position)
      .map(c => ({ value: c.code, label: c.name })) },
  { key: "triage", label: "Triage", type: "text", get: f => f.triage || "" },
  { key: "notes", label: "Notes", type: "text", get: f => f.notes || "" },
  { key: "tags", label: "Tags", type: "text", get: f => (f.tags || []).join(", ") },
  { key: "hashset_hit", label: "Hash set", type: "text", get: f => f.hashset_hit || "" },
  { key: "hashset_kind", label: "Hash kind", type: "enum", get: f => f.hashset_kind || "",
    options: [{ value: "known", label: "known" }, { value: "known-good", label: "known-good" },
              { value: "other", label: "other" }] },
  { key: "hashset_cat", label: "Hash cat", type: "num", get: f => f.hashset_cat ?? "" },
  { key: "stack_id", label: "Exact stack", type: "num", get: f => f.stack_id ?? "" },
  { key: "vstack_id", label: "Visual stack", type: "num", get: f => f.vstack_id ?? "" },
  { key: "cluster_id", label: "Cluster", type: "num", get: f => f.cluster_id ?? "" },
  { key: "media_id", label: "VIC MediaID", type: "num", get: f => f.media_id ?? "" },
  { key: "error", label: "Error", type: "text", get: f => f.error || "" },
];
const colId = d => d.alt || d.key || "thumb";
const DEFAULT_LIST_COLS = ["thumb", "name", "diskname", "orig_path", "kind", "ext", "size",
  "created_dt", "ctime", "mtime", "atime", "camera", "faces", "skin_ratio", "gps_lat", "gps_lon",
  "category", "md5", "hashset_hit", "tags", "notes", "error"];

function visibleDefs() {
  const on = state.listCols || new Set(DEFAULT_LIST_COLS);
  return LIST_DEFS.filter(d => on.has(colId(d)));
}
const LIST_PREFS_V = 5;   // bump when DEFAULT_LIST_COLS gains a column
function persistListPrefs() {
  try {
    localStorage.setItem("gleapp.list", JSON.stringify({
      v: LIST_PREFS_V,
      view: state.view,
      cols: [...(state.listCols || new Set(DEFAULT_LIST_COLS))],
      sortCol: state.sortCol, sortDir: state.sortDir,
      colFilters: state.colFilters,
      colWidths: state.colWidths,
    }));
  } catch (e) {}
}
function restoreListPrefs() {
  try {
    const s = JSON.parse(localStorage.getItem("gleapp.list") || "{}");
    if (s.view === "list") state.view = "list";
    state.listCols = new Set(s.cols && s.cols.length ? s.cols : DEFAULT_LIST_COLS);
    if (state.listCols.has("rel_path")) {          // migrate pre-"Name" prefs
      state.listCols.delete("rel_path");
      ["name", "diskname", "path"].forEach(k => state.listCols.add(k));
    }
    if ((s.v || 0) < LIST_PREFS_V) {               // one-time: refresh to new defaults
      state.listCols.delete("relpath");            // "Relative path" -> "File path"
      if ((s.v || 0) < 4) {                        // "File path" now = the VIC JSON path
        state.listCols.delete("path");
        state.listCols.add("orig_path");
      }
      DEFAULT_LIST_COLS.forEach(k => state.listCols.add(k));
      persistListPrefs();
    }
    if (["rel_path", "orig_name"].includes(s.sortCol)) state.sortCol = "name";
    else if (["path", "orig_path"].includes(s.sortCol)) state.sortCol = "file_path";
    else if (s.sortCol) { state.sortCol = s.sortCol; state.sortDir = s.sortDir || "asc"; }
    // v5: column-filter storage shape changed (date columns now hold {from,to})
    if (s.colFilters && (s.v || 0) >= 5) state.colFilters = s.colFilters;
    if (s.colWidths) state.colWidths = s.colWidths;
  } catch (e) { state.listCols = new Set(DEFAULT_LIST_COLS); }
  syncViewControls(state.view);
  updateClearFiltersBtn();
}

/* Controls whose relevance depends on grid vs. list. */
function syncViewControls(v) {
  const list = v === "list";
  $("#vGrid").classList.toggle("on", !list);
  $("#vList").classList.toggle("on", list);
  $("#btnCols").style.display = list ? "" : "none";
  $("#btnClearColFilters").style.display = list ? "" : "none";
  $("#sortWrap").style.display = list ? "none" : "";
  // the list view never collapses duplicates
  const fc = $("#fcollapse");
  fc.disabled = list;
  fc.closest("label").title = list
    ? "The list view always shows every row, duplicates included."
    : "one row per distinct image: exact copies + visual matches";
}

/* string -> number for a numeric column's filter box. Size understands
   b/kb/mb/gb (bare number = KB, since the column never shows raw bytes);
   a column with filterScale (e.g. Skin ratio, shown as %) is scaled. */
function numConv(def) {
  if (def.key === "size") return s => {
    const m = String(s).trim().match(/^(-?\d+(?:\.\d+)?)\s*(b|k|kb|m|mb|g|gb)?$/i);
    if (!m) return null;
    const u = (m[2] || "kb").toLowerCase().replace(/^([kmg])$/, "$1b");
    return Math.round(parseFloat(m[1]) * ({ b: 1, kb: 1024, mb: 1048576, gb: 1073741824 }[u]));
  };
  if (def.key === "duration") return s => {           // "1:30" or bare seconds
    s = String(s).trim();
    const m = s.match(/^(\d+):([0-5]?\d(?:\.\d+)?)$/);
    if (m) return parseInt(m[1], 10) * 60 + parseFloat(m[2]);
    const n = parseFloat(s);
    return isNaN(n) ? null : n;
  };
  const scale = def.filterScale || 1;
  return s => { const n = parseFloat(String(s).trim()); return isNaN(n) ? null : n * scale; };
}

/* epoch (seconds, UTC) for a wall-clock "YYYY-MM-DD" start/end of day in the
   chosen display timezone — so a date range matches what the column shows.
   FS/ingest times are stored UTC; the columns render them in state.tz. */
function zonedDayEpoch(dateStr, endOfDay) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(dateStr || "");
  if (!m) return null;
  const [Y, Mo, D] = [+m[1], +m[2], +m[3]];
  const hh = endOfDay ? 23 : 0, mm = endOfDay ? 59 : 0, ss = endOfDay ? 59 : 0;
  const tz = state.tz && state.tz !== "UTC" ? state.tz : "UTC";
  let guess = Date.UTC(Y, Mo - 1, D, hh, mm, ss);
  if (tz === "UTC") return Math.floor(guess / 1000);
  try {
    // correct the guess by the tz's offset at that instant (1 pass is exact
    // except inside a DST transition hour, which is fine for range filtering)
    const dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    const p = {};
    dtf.formatToParts(new Date(guess)).forEach(x => { p[x.type] = x.value; });
    const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
    return Math.floor((guess - (asUTC - guess)) / 1000);
  } catch (_) {
    return Math.floor(guess / 1000);
  }
}

/* [{col,op,val}] clauses for a date column, from the two calendar inputs */
function epochClauses(key, from, to) {
  const cl = [];
  if (from) { const v = zonedDayEpoch(from, false); if (v != null) cl.push({ col: key, op: "min", val: v }); }
  if (to)   { const v = zonedDayEpoch(to, true);    if (v != null) cl.push({ col: key, op: "max", val: v }); }
  return cl;
}

/* build the [{col,op,val}] clause list for one text/num column's raw string
   (enum uses op:eq directly; epoch uses epochClauses) */
function parseColFilter(def, raw) {
  raw = (raw || "").trim();
  if (!raw) return [];
  const key = def.filterKey || def.key;
  if (def.type === "enum") return [{ col: key, op: "eq", val: raw }];
  if (/^(set|any|\*)$/i.test(raw)) return [{ col: key, op: "set" }];
  if (/^(none|empty|null)$/i.test(raw)) return [{ col: key, op: "notset" }];
  if (def.type === "num") {
    const conv = numConv(def);
    // range: "a .. b" / "a to b" / "a-b"
    let m = raw.match(/^(.+?)\s*(?:\.\.|—|–|\bto\b|-)\s*(.+)$/i);
    if (m) {
      const a = conv(m[1]), b = conv(m[2]);
      if (a != null && b != null)
        return [{ col: key, op: "min", val: Math.min(a, b) },
                { col: key, op: "max", val: Math.max(a, b) }];
    }
    // explicit comparator
    m = raw.match(/^(>=|<=|>|<|=)\s*(.+)$/);
    if (m && conv(m[2]) != null) {
      const op = { ">": "gt", ">=": "min", "<": "lt", "<=": "max", "=": "eq" }[m[1]];
      return [{ col: key, op, val: conv(m[2]) }];
    }
    // a partial number in a decimal column (GPS) -> substring match
    if (def.filterContains) return [{ col: key, op: "contains", val: raw }];
    // bare number
    const n = conv(raw);
    if (n != null) return [{ col: key, op: def.filterBareOp || "eq", val: n }];
    return [];
  }
  // text
  return [{ col: key, op: "contains", val: raw }];
}

const COL_DEFAULT_W = { thumb: 46, num: 90, epoch: 155, enum: 130, text: 200 };
const colWidth = d => state.colWidths[colId(d)]
  || (d.thumb ? COL_DEFAULT_W.thumb : COL_DEFAULT_W[d.type] || 160);

function lvFilterCell(d) {
  if (d.thumb || !d.key) return "<th></th>";
  const cid = colId(d);
  const st = state.colFilters[cid] || {};
  if (d.type === "enum") {
    const opts = (typeof d.options === "function" ? d.options() : d.options) || [];
    const cur = st.raw || "";
    return `<th><select data-cid="${cid}"><option value="">–</option>` +
      opts.map(o => `<option value="${esc(o.value)}" ${String(o.value) === String(cur) ? "selected" : ""}>${esc(o.label)}</option>`).join("") +
      `</select></th>`;
  }
  if (d.type === "epoch") {
    const r = (st.raw && typeof st.raw === "object") ? st.raw : {};
    return `<th class="lvdate">` +
      `<input type="date" data-cid="${cid}" data-part="from" value="${esc(r.from || "")}" title="from">` +
      `<input type="date" data-cid="${cid}" data-part="to" value="${esc(r.to || "")}" title="to">` +
      `</th>`;
  }
  const ph = d.filterPh || (d.type === "num" ? ">100 · 5-9 · none" : "contains…");
  const cur = (typeof st.raw === "string") ? st.raw : "";
  return `<th><input data-cid="${cid}" class="${cur ? "on" : ""}" value="${esc(cur)}" placeholder="${esc(ph)}"></th>`;
}

// keep the header/filter row alive across data reloads (rebuilding it on every
// keystroke is what made typing jump / reverse characters) — only the <tbody>
// is replaced unless the visible column set actually changes.
function renderList(files) {
  const g = $("#grid");
  const defs = visibleDefs();
  const sig = defs.map(colId).join("|");
  let tbl = g.querySelector("table.lv");

  if (!tbl || g.dataset.lvsig !== sig) {
    g.innerHTML = "";
    tbl = document.createElement("table");
    tbl.className = "lv";
    let total = 0;
    const cols = defs.map(d => {
      const w = colWidth(d); total += w;
      return `<col data-cid="${colId(d)}" style="width:${w}px">`;
    }).join("");
    tbl.style.width = total + "px";
    const head = defs.map(d => {
      const sortable = d.key && !d.thumb;
      const sk = d.filterKey || d.key;
      return `<th class="${sortable ? "sortable" : ""}" data-k="${sk}" data-cid="${colId(d)}">` +
        `<span class="lbl">${esc(d.label)}<span class="ar" data-k="${sk}"></span></span>` +
        `<span class="rz" data-cid="${colId(d)}" title="Drag to resize · double-click to reset"></span></th>`;
    }).join("");
    const filt = defs.map(lvFilterCell).join("");
    tbl.innerHTML = `<colgroup>${cols}</colgroup><thead>` +
      `<tr class="lvhead">${head}</tr><tr class="lvfilt">${filt}</tr></thead><tbody></tbody>`;
    g.appendChild(tbl);
    g.dataset.lvsig = sig;
    wireListHeader();
  }
  updateSortArrows();

  const tb = document.createElement("tbody");
  files.forEach(f => tb.appendChild(lvRowEl(f, defs)));
  tbl.querySelector("tbody").replaceWith(tb);

  let cnt = g.querySelector(".lvcount");
  if (!files.length) {
    if (!cnt) { cnt = document.createElement("div"); cnt.className = "lvcount"; g.appendChild(cnt); }
    cnt.textContent = "No files match.";
  } else if (cnt) { cnt.remove(); }
}

function updateSortArrows() {
  $("#grid").querySelectorAll("tr.lvhead th .ar").forEach(ar => {
    const k = ar.dataset.k;
    ar.textContent = (k && state.sortCol === k)
      ? (state.sortDir === "desc" ? " ▼" : " ▲") : "";
  });
}

function lvRowEl(f, defs) {
  defs = defs || visibleDefs();
  const tr = document.createElement("tr");
  tr.className = "lvrow" + (state.sel.has(f.id) ? " sel" : "") + (state.focus === f.id ? " focus" : "");
  tr.dataset.id = f.id;
  tr.innerHTML = defs.map(d => {
    if (d.thumb) {
      return f.thumb
        ? `<td class="lvthumb"><img loading="lazy" src="/thumb/${f.thumb}" alt=""></td>`
        : `<td class="lvthumb"><div class="nt">${esc((f.ext || "?").replace(".", "").toUpperCase())}</div></td>`;
    }
    let v = d.get(f);
    v = v == null ? "" : String(v);
    let cls = d.mono ? "mono" : "";
    let style = "";
    if (d.cat) {
      cls += " catcell";
      style = ` style="border-left-color:${catColor(f.category)}"`;
    }
    return `<td class="${cls.trim()}"${style} title="${esc(v)}">${esc(v)}</td>`;
  }).join("");
  return tr;
}

let listHeaderWired = false;
function wireListHeader() {
  const g = $("#grid");
  g.querySelectorAll("tr.lvhead th.sortable").forEach(th => {
    th.onclick = e => {
      if (e.target.classList.contains("rz")) return;   // ignore the resize handle
      const k = th.dataset.k;
      if (state.sortCol === k) state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
      else { state.sortCol = k; state.sortDir = "asc"; }
      persistListPrefs();
      reload();
    };
  });

  // drag a column border to widen/narrow it; double-click resets to default
  const tbl = g.querySelector("table.lv");
  g.querySelectorAll("tr.lvhead th .rz").forEach(rz => {
    const cid = rz.dataset.cid;
    const col = g.querySelector(`col[data-cid="${cssEsc(cid)}"]`);
    rz.addEventListener("click", e => e.stopPropagation());
    rz.addEventListener("dblclick", e => {
      e.stopPropagation();
      delete state.colWidths[cid];
      const def = LIST_DEFS.find(d => colId(d) === cid);
      const old = col.getBoundingClientRect().width;
      const w = colWidth(def);
      col.style.width = w + "px";
      tbl.style.width = Math.round(tbl.getBoundingClientRect().width + (w - old)) + "px";
      persistListPrefs();
    });
    rz.addEventListener("mousedown", e => {
      e.preventDefault(); e.stopPropagation();
      const startX = e.clientX;
      const startW = col.getBoundingClientRect().width;
      const startTblW = tbl.getBoundingClientRect().width;
      rz.classList.add("drag");
      document.body.classList.add("colresize");
      const move = ev => {
        const w = Math.max(40, Math.round(startW + ev.clientX - startX));
        col.style.width = w + "px";
        // keep the fixed-layout table wide enough for the new column width
        tbl.style.width = Math.round(startTblW + (w - startW)) + "px";
      };
      const up = () => {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        rz.classList.remove("drag");
        document.body.classList.remove("colresize");
        const w = parseInt(col.style.width, 10);
        if (w) state.colWidths[cid] = w;
        persistListPrefs();
      };
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  });
  // the header row survives data reloads now, so a filter edit only swaps <tbody>
  const apply = debounce(() => {
    persistListPrefs();
    state.page = 1;
    load({ keepScroll: true });
  }, 300);
  // date-range (calendar) filters — read both from/to inputs on any edit
  g.querySelectorAll("tr.lvfilt input[type=date]").forEach(inp => {
    const cid = inp.dataset.cid;
    const def = LIST_DEFS.find(d => colId(d) === cid);
    if (!def) return;
    const update = () => {
      const parts = inp.closest("th").querySelectorAll("input[type=date]");
      const from = parts[0] ? parts[0].value : "";
      const to = parts[1] ? parts[1].value : "";
      const clauses = epochClauses(def.key, from, to);
      if (clauses.length) state.colFilters[cid] = { raw: { from, to }, clauses };
      else delete state.colFilters[cid];
      updateClearFiltersBtn();
      apply();
    };
    inp.addEventListener("change", update);
    inp.addEventListener("input", update);
  });
  // text / number / enum filters
  g.querySelectorAll("tr.lvfilt input:not([type=date]), tr.lvfilt select").forEach(inp => {
    const cid = inp.dataset.cid;
    const def = LIST_DEFS.find(d => colId(d) === cid);
    if (!def) return;
    const ev = inp.tagName === "SELECT" ? "change" : "input";
    inp.addEventListener(ev, () => {
      const raw = inp.value.trim();
      const clauses = parseColFilter(def, raw);
      if (raw && clauses.length) state.colFilters[cid] = { raw, clauses };
      else delete state.colFilters[cid];
      if (inp.tagName === "INPUT") inp.classList.toggle("on", !!raw);
      updateClearFiltersBtn();
      apply();
    });
  });
}

/* Columns ▾ menu */
function toggleColMenu() {
  const m = $("#colMenu");
  if (m.style.display === "block") { m.style.display = "none"; return; }
  const on = state.listCols || new Set(DEFAULT_LIST_COLS);
  m.innerHTML =
    `<div class="mrow"><button data-all="1">All</button>` +
    `<button data-all="0">Defaults</button>` +
    `<button data-rzw="1" title="Reset all column widths">Reset widths</button></div>` +
    LIST_DEFS.filter(d => !d.thumb).map(d => {
      const cid = colId(d);
      return `<label><input type="checkbox" data-cid="${cid}" ${on.has(cid) ? "checked" : ""}> ${esc(d.label)}</label>`;
    }).join("");
  const b = $("#btnCols").getBoundingClientRect();
  m.style.left = b.left + "px";
  m.style.top = (b.bottom + 4) + "px";
  m.style.display = "block";
  m.querySelectorAll("input[data-cid]").forEach(cb => cb.onchange = () => {
    const s = state.listCols || new Set(DEFAULT_LIST_COLS);
    cb.checked ? s.add(cb.dataset.cid) : s.delete(cb.dataset.cid);
    s.add("thumb");
    state.listCols = s;
    persistListPrefs();
    renderList(state.files);
  });
  m.querySelectorAll("button[data-all]").forEach(btn => btn.onclick = () => {
    state.listCols = new Set(btn.dataset.all === "1"
      ? LIST_DEFS.map(colId) : DEFAULT_LIST_COLS);
    persistListPrefs();
    toggleColMenu(); toggleColMenu();      // rebuild
    renderList(state.files);
  });
  m.querySelector("button[data-rzw]").onclick = () => {
    state.colWidths = {};
    persistListPrefs();
    renderList(state.files);
  };
}

// The grid's Sort dropdown and the list's clickable column headers are two ways
// to set one sort (state.sortCol / state.sortDir), so switching view never
// re-sorts. The dropdown offers the six sorts it can name; picking one sets the
// state, and any list sort is reflected back onto the dropdown (a list-only sort
// like Camera or MD5 shows as a transient "— column —" entry).
const GRID_SORT_TO_LIST = {
  path: ["name", "asc"], date: ["created_dt", "asc"], size: ["size", "asc"],
  skin: ["skin_ratio", "desc"], faces: ["faces", "desc"],
};
const LIST_SORT_TO_GRID = {
  name: "path", file_path: "path", rel_path: "path", orig_name: "path", path: "path",
  created_dt: "date", size: "size", skin_ratio: "skin", faces: "faces",
};

// Show state.sortCol/Dir on the grid's Sort dropdown.
function reflectGridSort() {
  const sel = $("#fsort");
  if (!sel) return;
  sel.querySelector("option.tmpsort")?.remove();
  const gv = LIST_SORT_TO_GRID[state.sortCol];
  if (gv) { sel.value = gv; return; }
  const def = LIST_DEFS.find(d => (d.filterKey || d.key) === state.sortCol);
  const o = document.createElement("option");
  o.className = "tmpsort";
  o.value = "__list__";
  o.textContent = (def ? def.label : state.sortCol)
    + (state.sortDir === "desc" ? " ↓" : " ↑");
  sel.appendChild(o);
  sel.value = "__list__";
}

$("#fsort").addEventListener("change", () => {
  const g = GRID_SORT_TO_LIST[$("#fsort").value];
  if (!g) return;                          // the transient "__list__" entry
  state.sortCol = g[0];
  state.sortDir = g[1];
  reflectGridSort();                       // clears the transient entry, if any
  persistListPrefs();
  reload();
});

function setView(v) {
  if (state.view === v) return;
  state.view = v;
  if (v === "grid") reflectGridSort();
  syncViewControls(v);
  $("#colMenu").style.display = "none";
  updateClearFiltersBtn();
  persistListPrefs();
  reload();
}

function updateClearFiltersBtn() {
  const n = Object.keys(state.colFilters || {}).length;
  const b = $("#btnClearColFilters");
  if (!b) return;
  b.disabled = !n;
  b.textContent = n ? `Clear column filters (${n})` : "Clear column filters";
}

function clearListFilters() {
  if (!Object.keys(state.colFilters || {}).length) return;
  state.colFilters = {};
  // wipe the filter-row widgets in place
  $("#grid").querySelectorAll("tr.lvfilt input, tr.lvfilt select").forEach(el => {
    el.value = "";
    el.classList.remove("on");
  });
  updateClearFiltersBtn();
  persistListPrefs();
  state.page = 1;
  load({ keepScroll: true });
  toast("Column filters cleared");
}

$("#vGrid").onclick = () => setView("grid");
$("#vList").onclick = () => setView("list");
$("#btnCols").onclick = toggleColMenu;
$("#btnClearColFilters").onclick = clearListFilters;
document.addEventListener("click", e => {
  if (!e.target.closest("#colMenu") && !e.target.closest("#btnCols"))
    $("#colMenu").style.display = "none";
});

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
  const ids = state.files.map(f => f.id);
  if (range && (state.lastClick != null || state.focus != null)) {
    // shift-click: (re)select the contiguous range from the anchor to here.
    // Replaces the selection unless ctrl is also held; the anchor stays put
    // so you can keep adjusting the range.
    const anchor = state.lastClick != null ? state.lastClick : state.focus;
    let a = ids.indexOf(anchor), b = ids.indexOf(id);
    if (a < 0) a = b;
    if (a > b) [a, b] = [b, a];
    if (!additive) state.sel.clear();
    for (let i = a; i <= b; i++) state.sel.add(ids[i]);
    setFocus(id);
    syncSel();
    return;
  }
  if (additive) {                       // ctrl/⌘-click: toggle this one
    state.sel.has(id) ? state.sel.delete(id) : state.sel.add(id);
  } else {                              // plain click: select only this one
    const only = state.sel.size === 1 && state.sel.has(id);
    state.sel.clear();
    if (!only) state.sel.add(id);
  }
  state.lastClick = id;
  setFocus(id);
  syncSel();
}
function setFocus(id) {
  state.focus = id;
  document.querySelectorAll(".tile.focus, .lvrow.focus").forEach(t => t.classList.remove("focus"));
  const t = document.querySelector(`.tile[data-id="${id}"], .lvrow[data-id="${id}"]`);
  if (t) t.classList.add("focus");
  if (state.metaOpen) showMeta(id);
}
function syncSel() {
  document.querySelectorAll(".tile, .lvrow").forEach(t =>
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
  toast(`${cat ? catName(cat) : "Uncategorized"} → ${ids.length} file(s)`);
  // working the Uncategorized backlog: move the cursor on to the next file
  // (the categorized tiles stay put until you hit Refresh)
  if (cat !== 0 && $("#fcat").value === "0") advancePast(ids);
  else if (state.metaOpen && ids.includes(state.focus)) showMeta(state.focus);
}

/* put focus on the first file after the ones just categorized */
function advancePast(justDone) {
  const order = state.files.map(f => f.id);
  const done = new Set(justDone);
  let last = -1;
  order.forEach((id, i) => { if (done.has(id)) last = i; });
  const next = last + 1;
  if (next >= order.length) {
    const pages = Math.max(1, Math.ceil(state.total / state.pageSize));
    if (!state.similarOf && state.page < pages) return gotoPage(state.page + 1);
    state.sel.clear(); state.focus = null; syncSel();
    return;
  }
  const id = order[next];
  state.sel.clear(); state.sel.add(id); setFocus(id); syncSel();
  document.querySelector(`.tile[data-id="${id}"], .lvrow[data-id="${id}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
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
  if (detailMap) setTimeout(() => detailMap.resize(), 60);   // it may have been sized while hidden
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
let _filmFaceLayerCleanups = [];
async function showMeta(id) {
  if (pendingNoteFlush) { try { await pendingNoteFlush(); } catch (e) {} pendingNoteFlush = null; }
  _filmFaceLayerCleanups.forEach(fn => fn());
  _filmFaceLayerCleanups = [];
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
  // device path for a VIC file; the on-disk path for a folder ingest;
  // never the local folder a VIC export was unpacked into
  const dispPath = f.orig_path || (f.media_id ? "" : (f.path || f.rel_path)) || "";
  // an Android extraction can hold one file under several storage views; the
  // kept spelling is the file path and the others are listed here
  let alsoAt = "";
  try {
    const ap = f.alt_paths ? JSON.parse(f.alt_paths) : null;
    if (ap && ap.length) alsoAt = ap.join("   ·   ");
  } catch (e) {}
  const rows = [
    ["Source", f.source], ["Type", f.kind],
    ["Original name", f.orig_name || ""],
    ["File path", dispPath],
    ["Also under", alsoAt],
    ["Stored at", f.orig_path && f.path && f.path !== f.orig_path ? f.path : ""],
    ["MIME", f.mime || ""],
    ["VIC MediaID", f.media_id ?? ""],
    ["VIC flags", flags],
    ["Size", fmtSize(f.size)],
    ["Dimensions", f.width ? `${f.width}×${f.height}` : ""],
    ["Duration", f.duration ? fmtDur(f.duration) : ""],
    ["Captured (EXIF, camera local)", f.created_dt || ""],
    ["FS created", fmtEpoch(f.ctime)],
    ["FS written", fmtEpoch(f.mtime)],
    ["FS accessed", fmtEpoch(f.atime)],
    ["Recorded (as stored, no zone)", fmtRecorded(f)],
    ["Camera", f.camera || ""],
    ["Faces", f.faces || 0], ["Skin ratio", f.skin_ratio ?? ""],
    ["Known hash", f.hashset_hit
      ? f.hashset_hit + (f.hashset_kind ? ` (${f.hashset_kind})` : "") : ""],
    ["Exact copies", f.stack && f.stack.length > 1 ? `${f.stack.length}` : "none"],
    ["Visually similar", f.vstack && f.vstack.length > 1 ? `${f.vstack.length} files` : "none"],
    ["Similar-group", f.cluster_id ? `#${f.cluster_id} (${f.cluster_size})` : "—"],
    ["Error", f.error || ""],
  ].filter(r => r[1] !== "" && r[1] != null);
  const hashes = [["MD5", f.md5], ["SHA1", f.sha1], ["SHA256", f.sha256], ["pHash", f.phash]]
    .filter(r => r[1]);
  // the coordinates are evidence: they stay on this machine. The map below draws
  // them on an imported offline basemap; Copy puts them on the clipboard.
  const gps = f.gps_lat != null
    ? `<tr><td>GPS</td><td>${f.gps_lat}, ${f.gps_lon}
       <button class="btn sm" id="mGpsCopy" title="Copy the coordinates">Copy</button></td></tr>` : "";

  const preview = f.thumb
    ? `<img src="/thumb/${f.thumb}" alt=""
         onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'noimg',textContent:'no preview'}))">
       <button class="btn sm full" id="mFull">View full size</button>`
    : `<div class="noimg">no preview<br><small>${esc(f.error || "not processed")}</small></div>`;
  const nm = (f.orig_name || f.rel_path || f.path || "").split(/[\\/]/).pop();
  m.innerHTML = `
    <div class="preview">${preview}</div>
    ${f.gps_lat != null ? `<div id="detMapWrap" class="detmapwrap" style="display:none">
      <div id="detMap" class="detmap"></div>
      <button class="btn sm mapexp" id="mMapFull" title="Open this map full size">⤢ Full size</button>
    </div>` : ""}
    <div class="body">
      <h2>${esc(nm || "file #" + f.id)}</h2>
      <div class="path">${esc(dispPath)}</div>
      ${f.error ? `<div class="path" style="color:var(--danger)">⚠ ${esc(f.error)}</div>` : ""}

      <div>Category:
        <span class="catnow" style="background:${catColor(f.category)}">
          ${esc(catName(f.category))}</span></div>
      <div class="row" id="mCats"></div>
      <div class="row">
        <button class="btn sm" id="mSim">Find similar</button>
        <button class="btn sm" id="mTag">Add tag</button>
        <button class="btn sm" id="mHex">Hex view</button>
      </div>
      <div>${(f.tags || []).map(t =>
        `<span class="pill">${esc(t)} <b data-t="${esc(t)}">×</b></span>`).join("")}</div>

      ${f.keyframes && f.keyframes.length ? `<div class="muted" style="margin-top:8px">Key frames</div>
        <div class="film">${f.keyframes.map(k =>
          `<img src="${k.thumb}" title="${fmtDur(k.ts)}" data-ts="${k.ts}" data-kf="${k.id}">`).join("")}</div>` : ""}

      <table>
        ${rows.map(r => `<tr><td>${r[0]}</td><td>${esc(r[1])}</td></tr>`).join("")}
        ${gps}
        ${hashes.map(r => `<tr><td>${r[0]}</td><td class="mono">${esc(r[1])}</td></tr>`).join("")}
      </table>

      <div class="muted" style="margin-top:8px">Notes <span id="mNoteState" class="muted" style="font-size:11px"></span></div>
      <textarea id="mNotes" placeholder="autosaves as you type">${esc(f.notes || "")}</textarea>

      ${f.stack && f.stack.length > 1 ? `<div class="muted">Exact copies (${f.stack.length})</div>
        <div class="film">${f.stack.filter(s => s.thumb).map(s =>
          `<img src="/thumb/${s.thumb}" title="${esc(s.rel_path)}" data-open="${s.id}">`).join("")}</div>
        <div class="row"><button class="btn sm" id="mStack">Show all copies</button></div>` : ""}
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
  renderDetailMap(f);
  if ($("#mGpsCopy")) $("#mGpsCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(`${f.gps_lat}, ${f.gps_lon}`); toast("Coordinates copied"); }
    catch (e) { toast("Clipboard unavailable; select the text instead"); }
  };
  $("#mSim").onclick = () => showSimilar(id);
  $("#mTag").onclick = () => tagIds([id]);
  $("#mHex").onclick = () => openHex(id);
  if ($("#mFull")) $("#mFull").onclick = () => openViewer(id);
  if ($("#mMapFull")) $("#mMapFull").onclick = () => openSingleMapView(f);
  if ($("#mVstack")) $("#mVstack").onclick = () => {
    state.vstack = f.vstack_id; state.page = 1; load();
    $("#simBanner").style.display = "flex";
    $("#simId").textContent = `visual-match group (${f.vstack.length})`;
  };
  if ($("#mStack")) $("#mStack").onclick = () => {
    state.stack = f.stack_id; state.page = 1; load();
    $("#simBanner").style.display = "flex";
    $("#simId").textContent = `exact-duplicate group (${f.stack.length})`;
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
  const film = m.querySelector(".film");
  if (film && film.querySelector("img[data-kf]")) loadKeyframeFaceBoxes(id, film);
  if (f.faces) loadPreviewFaceBoxes(id, m.querySelector(".preview"), isVid ? _posterKeyframeId(f) : null);
}

/* a video's preview thumbnail IS one specific key frame - pipeline.py picks
   the middle extracted frame as both (see "mid = len(frames) // 2"), and
   that frame was written to the keyframes table like any other, faces and
   all. Match it by its thumb filename to find which keyframe id it is, so
   the same box the filmstrip shows can be drawn on the bigger preview too. */
function _posterKeyframeId(f) {
  if (!f.thumb || !Array.isArray(f.keyframes)) return null;
  const kf = f.keyframes.find(k => k.thumb === "/thumb/" + f.thumb);
  return kf ? kf.id : null;
}

/* the details pane's own preview thumbnail - same idea as the full-size
   viewer's loadFaceBoxes, just a smaller image and no modal to close first.
   keyframeId is null for an image's own faces; for a video it's the poster
   frame's keyframe id from _posterKeyframeId (or null if it couldn't be
   matched, which just means no box - the same as a file with no faces). */
function loadPreviewFaceBoxes(id, container, keyframeId) {
  const img = container && container.querySelector("img");
  if (!img) return;
  api(`/api/faces/${id}`).then(raw => {
    const faces = Array.isArray(raw) ? raw.filter(fc => fc.keyframe_id === keyframeId) : [];
    if (!faces.length || !document.body.contains(img)) return;
    const cleanup = attachFaceLayer(img, faces, container, faceId => showFaceMatches(faceId));
    if (cleanup) _filmFaceLayerCleanups.push(cleanup);
  }).catch(() => {});
}

/* ---------- full-size viewer ---------- */
/* view-only brightness/shadow lift — a display filter on the on-screen image;
   it never touches the file, thumbnail, hashes or anything stored. */
const _enh = { on: false, amt: 55 };
try { Object.assign(_enh, JSON.parse(localStorage.getItem("gleapp.viewenh") || "{}")); } catch (e) {}

function _applyEnh() {
  const t = Math.max(0, Math.min(100, _enh.amt)) / 100;
  const gamma = (1 - 0.72 * t).toFixed(3);   // <1 lifts shadows/midtones
  const slope = (1 + 0.55 * t).toFixed(3);   // a little extra gain on top
  ["vEnhG_R", "vEnhG_G", "vEnhG_B"].forEach(k => $("#" + k).setAttribute("exponent", gamma));
  ["vEnhL_R", "vEnhL_G", "vEnhL_B"].forEach(k => $("#" + k).setAttribute("slope", slope));
  $("#vEnhOn").checked = _enh.on;
  $("#vEnhAmt").value = _enh.amt;
  $("#vEnhVal").textContent = _enh.amt;
  const media = $("#vWrap").querySelector("img, video");
  if (media) media.classList.toggle("enh", _enh.on);
  try { localStorage.setItem("gleapp.viewenh", JSON.stringify(_enh)); } catch (e) {}
}

/* click a detected face's box to find other files with a matching face -
   opt-in: only drawn/clickable when screening was on and SFace embedded it.
   The overlay is a separate layer positioned in JS from the <img>'s actual
   rendered box (getBoundingClientRect) rather than nested CSS percentages -
   percentage max-height doesn't resolve through an auto-sized wrapper, which
   left the image itself unconstrained (way oversized) when tried that way.
   Shared by the full-size viewer (one image) and the details pane's video
   key-frame filmstrip (several small thumbnails, one container). Returns a
   cleanup function the caller should run when the image/thumbnail goes away. */
function attachFaceLayer(img, faces, container, onClick) {
  if (!faces || !faces.length) return null;
  const layer = document.createElement("div");
  layer.className = "faceLayer";
  faces.forEach(fc => {
    const [x, y, bw, bh] = fc.bbox;
    const box = document.createElement(fc.has_embedding ? "button" : "div");
    box.className = "faceBox" + (fc.has_embedding ? " clickable" : "");
    box.style.left = (x * 100) + "%";
    box.style.top = (y * 100) + "%";
    box.style.width = (bw * 100) + "%";
    box.style.height = (bh * 100) + "%";
    if (fc.has_embedding) {
      box.title = "Find matching faces";
      box.onclick = e => { e.stopPropagation(); onClick(fc.id); };
    }
    layer.appendChild(box);
  });
  container.appendChild(layer);

  const sync = () => {
    if (!document.body.contains(img)) { cleanup(); return; }
    const cRect = container.getBoundingClientRect();
    const iRect = img.getBoundingClientRect();
    layer.style.left = (iRect.left - cRect.left) + "px";
    layer.style.top = (iRect.top - cRect.top) + "px";
    layer.style.width = iRect.width + "px";
    layer.style.height = iRect.height + "px";
  };
  const cleanup = () => {
    window.removeEventListener("resize", sync);
    container.removeEventListener("scroll", sync);
  };
  if (img.complete && img.naturalWidth) sync();
  else img.addEventListener("load", sync, { once: true });
  window.addEventListener("resize", sync);
  container.addEventListener("scroll", sync);
  return cleanup;
}

let _viewerFaceLayerCleanup = null;   // the viewer's active listener, if any - one at a time

function openViewer(id, ts) {
  const f = state.files.find(x => x.id === id) || {};
  const w = $("#vWrap");
  $("#vBar").classList.add("show");   // "Lighten dark areas" works on video too
  if (_viewerFaceLayerCleanup) { _viewerFaceLayerCleanup(); _viewerFaceLayerCleanup = null; }
  if (f.kind === "video") {
    w.innerHTML = `<video src="/media/${id}" controls autoplay></video>`;
    if (ts) w.querySelector("video").currentTime = ts;
  } else {
    // /view transcodes HEIC/TIFF/RAW that the browser can't render
    w.innerHTML = `<img src="/view/${id}" alt=""
      onerror="this.replaceWith(Object.assign(document.createElement('div'),
        {className:'noimg',style:'padding:40px',
         innerHTML:'This file can\\'t be displayed<br><small>GPU texture / proprietary format — try the original file</small>'}))">`;
    loadFaceBoxes(id);
  }
  $("#viewer").style.display = "block";
  _applyEnh();
}
function closeViewer() {
  $("#viewer").style.display = "none"; $("#vWrap").innerHTML = "";
  if (_viewerFaceLayerCleanup) { _viewerFaceLayerCleanup(); _viewerFaceLayerCleanup = null; }
}
async function loadFaceBoxes(id) {
  const img = $("#vWrap img");
  if (!img) return;
  // only an image's own faces (keyframe_id null) belong on the full-size photo
  const raw = await api(`/api/faces/${id}`).catch(() => []);
  const faces = Array.isArray(raw) ? raw.filter(fc => fc.keyframe_id == null) : [];
  if (!document.body.contains(img)) return;   // viewer closed/reopened meanwhile
  _viewerFaceLayerCleanup = attachFaceLayer(img, faces, $("#vWrap"),
    faceId => { closeViewer(); showFaceMatches(faceId); });
}

/* the details pane's video key-frame filmstrip: same idea, one small
   thumbnail at a time, each keyed by its own keyframe id */
function loadKeyframeFaceBoxes(fileId, film) {
  api(`/api/faces/${fileId}`).then(faces => {
    if (!Array.isArray(faces) || !document.body.contains(film)) return;
    const byKf = {};
    faces.forEach(fc => {
      if (fc.keyframe_id == null) return;
      (byKf[fc.keyframe_id] ||= []).push(fc);
    });
    film.querySelectorAll("img[data-kf]").forEach(img => {
      const kfFaces = byKf[img.dataset.kf];
      if (!kfFaces) return;
      const cleanup = attachFaceLayer(img, kfFaces, film, faceId => showFaceMatches(faceId));
      if (cleanup) _filmFaceLayerCleanups.push(cleanup);
    });
  }).catch(() => {});
}
async function showFaceMatches(faceId) {
  const d = await api(`/api/face-match/${faceId}`);
  state.similarOf = faceId;   // reuses the same "special result set" plumbing as find-similar
  state.files = d.files;
  renderFiles(d.files);
  renderPager();
  $("#simBanner").style.display = "flex";
  $("#simId").textContent = `files with a matching face (#${faceId})`;
  updateStat();
}
$("#vClose").onclick = closeViewer;
$("#viewer").addEventListener("click", e => { if (e.target.id === "viewer") closeViewer(); });
$("#vEnhOn").onchange = () => { _enh.on = $("#vEnhOn").checked; _applyEnh(); };
$("#vEnhAmt").oninput = () => { _enh.amt = +$("#vEnhAmt").value; _enh.on = true; _applyEnh(); };

/* ---------- hex viewer ---------- */
const HEX_PAGE = 2048;
async function openHex(id, offset = 0) {
  const d = await api(`/api/file/${id}/hex?offset=${Math.max(0, offset)}&length=${HEX_PAGE}`)
    .catch(() => ({ error: true }));
  if (!d || d.error || d.id === null) return toast(d && d.message || "Can't read this file");
  d.fileId = id;
  state.hex = d;
  renderHex(d);
  $("#hexDlg").style.display = "block";
}
function renderHex(d) {
  $("#hexTitle").textContent = "Hex — " + d.name;
  const last = Math.max(0, d.size - HEX_PAGE);
  $("#hexNav").textContent =
    `${d.offset.toLocaleString()}–${Math.min(d.size, d.offset + d.bytes.length / 2).toLocaleString()}`
    + ` of ${d.size.toLocaleString()} bytes`;
  $("#hexPrev").disabled = d.offset <= 0;
  $("#hexNext").disabled = d.offset >= last;
  const b = d.bytes, rows = [];
  for (let i = 0; i < b.length; i += 32) {
    const off = (d.offset + i / 2).toString(16).padStart(8, "0");
    const pairs = (b.slice(i, i + 32).match(/../g) || []);
    const hex = pairs.map((p, j) => (j === 8 ? " " : "") + p).join(" ").padEnd(48);
    const asc = pairs.map(p => {
      const c = parseInt(p, 16); return c >= 32 && c < 127 ? String.fromCharCode(c) : ".";
    }).join("");
    rows.push(`${off}  ${hex}  |${asc}|`);
  }
  $("#hexBody").textContent = rows.join("\n") || "(empty file)";
  $("#hexBody").scrollTop = 0;
}
function hexStep(delta) {
  if (!state.hex) return;
  const last = Math.max(0, state.hex.size - HEX_PAGE);
  openHex(state.hex.fileId, Math.min(last, Math.max(0, state.hex.offset + delta * HEX_PAGE)));
}
$("#hexPrev").onclick = () => hexStep(-1);
$("#hexNext").onclick = () => hexStep(1);
$("#hexClose").onclick = () => $("#hexDlg").style.display = "none";
$("#hexDlg").addEventListener("click", e => {
  if (e.target.id === "hexDlg") $("#hexDlg").style.display = "none";
});
$("#hexJump").addEventListener("keydown", e => {
  if (e.key !== "Enter" || !state.hex) return;
  const v = $("#hexJump").value.trim().replace(/^0x/i, "");
  let o = /^[0-9]+$/.test($("#hexJump").value.trim()) ? parseInt(v, 10) : parseInt(v, 16);
  if (!isNaN(o)) openHex(state.hex.fileId, o);
});

/* ---------- context menu ---------- */
function openCtx(x, y, ids) {
  state.ctxIds = ids;
  const many = ids.length > 1 ? ` (${ids.length})` : "";
  // One row can be standing in for a whole group that "Collapse duplicates"
  // hid - either an exact-copy stack or a visual-similarity group, whichever
  // /api/files' own collapse picks a representative for: COALESCE(vstack_id,
  // stack_id, id). Mirror that here so one menu entry reaches everything the
  // grid is currently not showing for this tile, whichever kind it is.
  const f0 = ids.length === 1 ? state.files.find(x => x.id === ids[0]) : null;
  const groupField = f0 && f0.vstack_id ? "vstack" : "stack";
  const groupId = f0 ? (f0.vstack_id || f0.stack_id) : null;
  const groupN = f0 ? (f0.vstack_id ? f0.vstack_count : f0.stack_count) || 1 : 0;
  const catBtns = activeCats().map((c, i) =>
    `<button data-a="c${c.code}"><span class="dot" style="background:${c.color}"></span>
      ${esc(c.name || "Category " + c.code)}${many} <span class="muted">${i + 1}</span></button>`).join("");
  $("#ctx").innerHTML = `
    <button data-a="similar">\u{1F50D} Find similar images</button>
    ${groupId && groupN > 1 ? `<button data-a="group">\u{1F4CB} Show all in group (${groupN})</button>` : ""}
    <button data-a="meta">ℹ Show details</button>
    <button data-a="full">⤢ View full size</button>
    <button data-a="hex">\u{1F524} Hex view</button>
    <div class="sep"></div>
    ${catBtns}
    <button data-a="c0"><span class="dot" style="background:#3a3f4b"></span>Clear category${many} <span class="muted">0</span></button>
    <div class="sep"></div>
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
  if (a === "group") {
    const f0 = state.files.find(x => x.id === ids[0]);
    if (!f0) return;
    if (f0.vstack_id) {
      state.vstack = f0.vstack_id; state.stack = null; state.page = 1; load();
      $("#simId").textContent = `visual-match group (${f0.vstack_count})`;
    } else if (f0.stack_id) {
      state.stack = f0.stack_id; state.vstack = null; state.page = 1; load();
      $("#simId").textContent = `exact-duplicate group (${f0.stack_count})`;
    } else {
      return;
    }
    $("#simBanner").style.display = "flex";
    return;
  }
  if (a === "meta") { toggleMeta(true); return setFocus(ids[0]); }
  if (a === "full") return openViewer(ids[0]);
  if (a === "hex") return openHex(ids[0]);
  if (a === "mediafile") return window.open("/media/" + ids[0], "_blank");
  if (a === "md5") return exportMd5(ids);
  if (a[0] === "c") return categorize(ids, +a.slice(1));
  if (a === "tag") return tagIds(ids);
});

/* ---------- category editor ---------- */
function renderCatEd() {
  const rows = state.cats.filter(c => c.code !== 0).sort((a, b) => a.position - b.position);
  const custom = rows.filter(c => !c.locked);
  $("#catRows").innerHTML = rows.map(c => c.locked ? `
    <div class="cat locked" data-code="${c.code}">
      <span class="grip" title="Project VIC preset — locked">🔒</span>
      <span class="dot" style="background:${c.color}"></span>
      <span class="lockname">${esc(c.name)}</span>
      <span class="muted vcode">VIC ${c.code}</span>
    </div>` : `
    <div class="cat" draggable="true" data-code="${c.code}">
      <span class="grip">☰</span>
      <input type="color" value="${esc(c.color || "#8b93a3")}" title="Category color">
      <input type="text" value="${esc(c.name)}" placeholder="Category ${c.code} (unnamed)">
      <button class="btn sm" data-del="${c.code}">Delete</button>
    </div>`).join("");

  $("#catRows").querySelectorAll(".cat:not(.locked) input[type=text]").forEach(inp => {
    inp.addEventListener("change", async () => {
      const code = +inp.closest(".cat").dataset.code;
      await save("/api/categories/" + code, { name: inp.value.trim() }, "PATCH");
      await refreshCats(); renderCatEd(); refreshAllTiles();
      if (state.metaOpen && state.focus != null) showMeta(state.focus);
    });
  });
  $("#catRows").querySelectorAll(".cat:not(.locked) input[type=color]").forEach(inp => {
    inp.addEventListener("change", async () => {
      const code = +inp.closest(".cat").dataset.code;
      await save("/api/categories/" + code, { color: inp.value }, "PATCH");
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

  // drag reorder (the examiner's own categories only; presets are fixed)
  let dragCode = null;
  $("#catRows").querySelectorAll(".cat:not(.locked)").forEach(row => {
    row.addEventListener("dragstart", () => { dragCode = +row.dataset.code; row.classList.add("drag"); });
    row.addEventListener("dragend", () => row.classList.remove("drag"));
    row.addEventListener("dragover", e => e.preventDefault());
    row.addEventListener("drop", async e => {
      e.preventDefault();
      const target = +row.dataset.code;
      if (dragCode == null || dragCode === target) return;
      const order = custom.map(c => c.code).filter(c => c !== dragCode);
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

/* ---------- grid / list events ---------- */
$("#grid").addEventListener("click", e => {
  const t = e.target.closest(".tile, .lvrow"); if (!t) return;
  toggleSel(+t.dataset.id, e.ctrlKey || e.metaKey, e.shiftKey);
});
$("#grid").addEventListener("dblclick", e => {
  const t = e.target.closest(".tile, .lvrow"); if (t) openViewer(+t.dataset.id);
});
$("#grid").addEventListener("contextmenu", e => {
  const t = e.target.closest(".tile, .lvrow"); if (!t) return;
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
  document.querySelector(`.tile[data-id="${id}"], .lvrow[data-id="${id}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" });
}
document.addEventListener("keydown", e => {
  if (/input|textarea|select/i.test(e.target.tagName)) return;
  if (e.key === "Escape") {
    closeCtx(); closeViewer();
    $("#catEd").style.display = "none"; $("#reportDlg").style.display = "none";
    $("#helpDlg").style.display = "none"; $("#hexDlg").style.display = "none";
    $("#snapDlg").style.display = "none"; $("#stashDlg").style.display = "none";
    $("#stashWipeDlg").style.display = "none"; $("#hashImportDlg").style.display = "none";
    $("#refDlg").style.display = "none"; $("#histDlg").style.display = "none";
    $("#mainMenu").style.display = "none"; $("#notifyMenu").style.display = "none";
    return;
  }
  if (e.key === "?" && !$("#helpDlg").style.display.includes("block")) {
    e.preventDefault(); openHelp();
  }
  if (e.key === "PageDown") { e.preventDefault(); return gotoPage(state.page + 1); }
  if (e.key === "PageUp") { e.preventDefault(); return gotoPage(state.page - 1); }
  if (e.key === "Home" && e.ctrlKey) { e.preventDefault(); return gotoPage(1); }
  if (e.key === "End" && e.ctrlKey) {
    e.preventDefault();
    return gotoPage(Math.ceil(state.total / state.pageSize));
  }
  if (state.view === "list") {
    // list is a vertical stack: ↑/↓ move between rows, ←/→ scroll the columns
    if (e.key === "ArrowDown") { e.preventDefault(); return moveFocus(1); }
    if (e.key === "ArrowUp") { e.preventDefault(); return moveFocus(-1); }
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      $("#main").scrollBy({ left: e.key === "ArrowRight" ? 180 : -180, behavior: "smooth" });
      return;
    }
  } else {
    if (e.key === "ArrowRight") { e.preventDefault(); return moveFocus(1); }
    if (e.key === "ArrowLeft") { e.preventDefault(); return moveFocus(-1); }
  }
  const ids = selIds();
  if (e.key >= "1" && e.key <= "9") {
    const c = activeCats()[+e.key - 1];
    if (c && ids.length) categorize(ids, c.code);
  } else if (e.key === "0" && ids.length) categorize(ids, 0);
  else if (e.key.toLowerCase() === "f" && ids.length) showSimilar(ids[0]);
  else if (e.key.toLowerCase() === "h" && ids.length) openHex(ids[0]);
  else if (e.key.toLowerCase() === "i") toggleMeta();
  else if (e.key.toLowerCase() === "a") {
    e.preventDefault(); state.files.forEach(f => state.sel.add(f.id)); syncSel();
  }
});

/* ---------- filter wiring ---------- */
// #fsort has its own handler (it maps to state.sortCol/Dir), so it's not here
["#fq", "#fkind", "#fcat", "#fsrc", "#forigin", "#finarch", "#fdup", "#ffaces", "#fgps",
 "#fhit", "#fhashset", "#fhidegood", "#ferr", "#fskin", "#fcollapse"].forEach(s => {
  const el = $(s);
  el.addEventListener(s === "#fq" ? "input" : "change", debounce(reload, 250));
});

/* ---------- collapsible feature sections ---------- */
const SEC_ACTIVE = {
  hash:   () => !!$("#fhashset").value || $("#fhit").checked || $("#fhidegood").checked,
  screen: () => $("#ffaces").checked || +$("#fskin").value > 0,
  dup:    () => !!$("#fdup").value,
  err:    () => $("#ferr").checked,
  loc:    () => $("#fgps").checked,
  carve:  () => !!$("#forigin").value,
  arch:   () => $("#finarch").checked,
};
// Each active filter drives a removable chip under "N filters" - "Clear"
// still resets everything, but one filter can now come off on its own.
// active() decides whether it's on, label() is the chip's text, clear()
// resets just that one control (the field-change handlers below re-run the
// query the same way they do for a normal edit).
const FILTER_DEFS = [
  { active: () => $("#fq").value.trim(),
    label: () => `Search: "${esc($("#fq").value.trim())}"`,
    clear: () => { $("#fq").value = ""; } },
  { active: () => $("#fcat").value !== "any",
    label: () => `Category: ${esc($("#fcat").selectedOptions[0].textContent)}`,
    clear: () => { $("#fcat").value = "any"; } },
  { active: () => !!$("#fkind").value,
    label: () => `Type: ${esc($("#fkind").selectedOptions[0].textContent)}`,
    clear: () => { $("#fkind").value = ""; } },
  { active: () => !!$("#fsrc").value,
    label: () => `Source: ${esc($("#fsrc").selectedOptions[0].textContent)}`,
    clear: () => { $("#fsrc").value = ""; } },
  { active: () => !!$("#forigin").value,
    label: () => `How recovered: ${esc($("#forigin").selectedOptions[0].textContent)}`,
    clear: () => { $("#forigin").value = ""; } },
  { active: () => !!$("#fhashset").value,
    label: () => `Known hashes: ${esc($("#fhashset").selectedOptions[0].textContent)}`,
    clear: () => { $("#fhashset").value = ""; } },
  { active: () => !!$("#fdup").value,
    label: () => `Duplicates: ${esc($("#fdup").selectedOptions[0].textContent)}`,
    clear: () => { $("#fdup").value = ""; } },
  { active: () => $("#fhit").checked,
    label: () => "Any known-hash hit",
    clear: () => { $("#fhit").checked = false; } },
  { active: () => $("#fhidegood").checked,
    label: () => "Hide known-NSRL",
    clear: () => { $("#fhidegood").checked = false; } },
  { active: () => $("#ffaces").checked,
    label: () => "Has faces",
    clear: () => { $("#ffaces").checked = false; } },
  { active: () => +$("#fskin").value > 0,
    label: () => `Skin-tone ratio: ${esc($("#fskin").selectedOptions[0].textContent)}`,
    clear: () => { $("#fskin").value = "0"; } },
  { active: () => $("#ferr").checked,
    label: () => "Processing error / no preview",
    clear: () => { $("#ferr").checked = false; } },
  { active: () => $("#fgps").checked,
    label: () => "Has GPS",
    clear: () => { $("#fgps").checked = false; } },
  { active: () => $("#finarch").checked,
    label: () => "Extracted from an archive",
    clear: () => { $("#finarch").checked = false; } },
  { active: () => !!state.vstack,
    label: () => "Viewing a visual-match group",
    clear: () => { state.vstack = null; } },
  { active: () => !!state.stack,
    label: () => "Viewing an exact-duplicate group",
    clear: () => { state.stack = null; } },
];
function countActiveFilters() {
  return FILTER_DEFS.filter(f => f.active()).length;
}
function renderFilterChips() {
  const box = $("#fchips");
  if (!box) return;
  box.innerHTML = FILTER_DEFS.map((f, i) => !f.active() ? "" : (
    `<span class="fchip"><span>${f.label()}</span>` +
    `<button type="button" data-i="${i}" title="Remove this filter">&times;</button></span>`
  )).join("");
}
$("#fchips").addEventListener("click", e => {
  const btn = e.target.closest("button[data-i]");
  if (!btn) return;
  FILTER_DEFS[+btn.dataset.i].clear();
  refreshSections();
  reload();
});
function refreshSections() {
  document.querySelectorAll(".fsec").forEach(sec => {
    const fn = SEC_ACTIVE[sec.dataset.sec];
    sec.classList.toggle("active", !!(fn && fn()));
  });
  const n = countActiveFilters();
  const bar = $("#fbar");
  if (bar) {
    bar.hidden = !n;
    $("#fbarN").innerHTML = `<b>${n}</b> filter${n === 1 ? "" : "s"}`;
  }
  renderFilterChips();
}
document.querySelectorAll(".fsec").forEach(sec => {
  const key = "gleapp.fsec." + sec.dataset.sec;
  try {
    const v = localStorage.getItem(key);
    if (v === "1") sec.open = true;
    else if (v === "0") sec.open = false;
  } catch (e) {}
  sec.addEventListener("toggle", () => {
    try { localStorage.setItem(key, sec.open ? "1" : "0"); } catch (e) {}
  });
});
$("#ftile").addEventListener("input", () => {
  document.documentElement.style.setProperty("--tile", $("#ftile").value + "px");
  try { localStorage.setItem("gleapp.tile", $("#ftile").value); } catch (e) {}
});

$("#fpagesize").addEventListener("change", () => {
  state.pageSize = +$("#fpagesize").value || 200;
  try { localStorage.setItem("gleapp.pagesize", state.pageSize); } catch (e) {}
  state.page = 1; load();
});
$("#simBack").onclick = () => { state.vstack = null; state.stack = null; load(); };
$("#btnMeta").onclick = () => toggleMeta();
/* ---------- help / manual ---------- */
let helpLoaded = false;
async function openHelp() {
  const dlg = $("#helpDlg");
  dlg.style.display = "block";
  if (!helpLoaded) {
    try {
      $("#helpDoc").innerHTML = await fetch("/static/help.html").then(r => r.text());
      helpLoaded = true;
    } catch (e) {
      $("#helpDoc").innerHTML = "<p>Could not load the manual.</p>";
    }
  }
  $("#helpDoc").scrollTop = 0;
}
/* "☰ Menu" groups the case-tools, reference-data and help buttons that used to
   sprawl across the whole header into one dropdown (Case / Reference / Help),
   sharing toggleHdrMenu's open/close/position plumbing with the bell. Each
   button inside keeps its own onclick, wired where the rest of that feature
   lives; this listener only closes the menu once one of them fires, and
   routes the two Help entries (which have no case of their own to act on).
   The launcher's own Help button has no case either, so it opens the manual
   directly, unaffected by any of this. */
$("#btnMenu").onclick = () => toggleHdrMenu($("#btnMenu"), $("#mainMenu"));
$("#mainMenu").addEventListener("click", e => {
  const which = e.target.dataset.help;
  if (which === "manual") openHelp();
  else if (which === "history") openHistDlg();
  if (e.target.tagName === "BUTTON") $("#mainMenu").style.display = "none";
});
$("#btnHelpLauncher").onclick = openHelp;      // same manual, from the launcher
$("#helpClose").onclick = () => $("#helpDlg").style.display = "none";
$("#helpDlg").addEventListener("click", e => {
  if (e.target.id === "helpDlg") $("#helpDlg").style.display = "none";
});

/* ---------- live processing bar + auto-refresh ---------- */
let liveTimer = null, liveTick = 0;
let lastReportScope = "";   // scope label for the LAVA job currently in flight, if any
function stopLive() {
  if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
}
function showProc(txt, pct, cls) {
  const bar = $("#procBar");
  bar.style.display = "flex";
  bar.className = cls || "";
  $("#procTxt").textContent = txt;
  $("#procFill").style.width = (pct == null ? 8 : pct) + "%";
  $("#procPct").textContent = pct == null ? "" : pct + "%";
}
$("#procDismiss").onclick = () => { $("#procBar").style.display = "none"; };

/* Poll the running job: keep the bottom bar current and, every few ticks,
   pull newly-thumbnailed files into the grid without disturbing the view. */
async function liveJob() {
  stopLive();
  let j;
  try { j = await api("/api/job"); }
  catch (e) { liveTimer = setTimeout(liveJob, 1500); return; }

  if (j.stage === "error") {
    showProc("Processing failed: " + (j.error || "unknown error"), 100, "err");
    $("#procDismiss").style.display = "";
    await load({ keepScroll: true }).catch(() => {});
    return;
  }
  if (!j.running || j.stage === "done") {
    // an export writes files rather than changing the case, so it says so
    const exported = j.stats && j.stats.report_dir;
    showProc(exported ? "Export complete" : "Processing complete", 100);
    $("#procPct").textContent = "";
    await refreshContext().catch(() => {});
    await load({ keepScroll: true }).catch(() => {});
    setTimeout(() => { $("#procBar").style.display = "none"; }, 4000);
    toast(exported
      ? `Export complete → ${j.stats.report_dir} — click to open`
      : "Processing complete" + (j.stats && j.stats.processed
        ? ` — ${j.stats.processed.toLocaleString()} files` : ""),
      exported ? 5000 : 1800,
      exported ? () => openFolder(j.stats.report_dir) : null);
    if (exported) {
      addReportNotification(j.stats.report_dir,
        `LAVA export (${lastReportScope || "all files"})`);
      lastReportScope = "";
    }
    return;
  }

  const pct = j.total ? Math.round(100 * j.done / j.total) : null;
  const label = j.stage === "ingest"
    ? (j.message || "Registering files…")
    : `${j.message || "Processing"} ${j.total ? `— ${j.done.toLocaleString()}/${j.total.toLocaleString()}` : ""}`;
  showProc(label, pct);

  liveTick++;
  // refresh the grid every ~5s (every 5th poll) so thumbnails appear as they land
  if (liveTick % 5 === 0) {
    await load({ keepScroll: true }).catch(() => {});
    if (liveTick % 15 === 0) await refreshContext().catch(() => {});
  }
  liveTimer = setTimeout(liveJob, 1000);
}

/* re-pull the bits of /api/context that change while a job runs */
async function refreshContext() {
  const c = await api("/api/context");
  if (c.needs_case) return;
  state.basemap = c.basemap || null;
  showSourceStatus(c.archive_sources);
  state.cats = c.categories || [];
  try { await refreshCats(); } catch (e) {}
  updateScreenInfo(c.screening);
  updateArchInfo(c.archives);
  updateKnownHash(c.known_hash);
  const src = $("#fsrc"), have = new Set([...src.options].map(o => o.value));
  (c.sources || []).forEach(s => {
    if (!have.has(s)) src.insertAdjacentHTML("beforeend", `<option>${esc(s)}</option>`);
  });
  if (c.errors > 0) {
    $("#errCount").textContent = `(${c.errors.toLocaleString()})`;
    $("#btnRetryErr").style.display = "";
    $("#btnRetryErr").textContent = `Retry ${c.errors.toLocaleString()} failed files`;
  }
}

$("#btnClearFilters").onclick = () => {
  $("#fq").value = "";
  $("#fkind").value = "";
  $("#fcat").value = "any";
  $("#fsrc").value = "";
  $("#forigin").value = "";
  $("#finarch").checked = false;
  $("#fdup").value = "";
  $("#fhashset").value = "";
  ["#ffaces", "#fgps", "#fhit", "#fhidegood", "#ferr"].forEach(s => $(s).checked = false);
  $("#fcollapse").checked = true;
  $("#fskin").value = "0";
  state.sortCol = "name"; state.sortDir = "asc";
  reflectGridSort();
  state.vstack = null; state.stack = null; state.similarOf = null;
  $("#simBanner").style.display = "none";
  // also drop the list-view per-column filters
  state.colFilters = {};
  $("#grid").querySelectorAll("tr.lvfilt input, tr.lvfilt select").forEach(el => {
    el.value = ""; el.classList.remove("on");
  });
  updateClearFiltersBtn();
  refreshSections();
  persistListPrefs();
  reload();
  toast("Filters cleared");
};
$("#btnRefresh").onclick = () => {
  const n = state.total;
  state.sel.clear();
  load().then(() => {
    const gone = n - state.total;
    toast(gone > 0 ? `Refreshed — ${gone} file(s) dropped out of view` : "View refreshed");
  });
};

$("#selbar").addEventListener("click", e => {
  const b = e.target.closest("[data-cat]");
  if (b) categorize([...state.sel], +b.dataset.cat);
});
$("#selTag").onclick = () => tagIds([...state.sel]);
$("#selClear").onclick = () => { state.sel.clear(); syncSel(); };
/* ---------- export / report dialog ---------- */
let rptLogo = null;   // data: URI of the chosen agency logo, or null

async function openReportDlg() {
  const s = await api("/api/stats").catch(() => ({}));
  const cats = Object.entries(s.by_category || {})
    .filter(([k]) => +k !== 0).reduce((a, [, v]) => a + v, 0);
  $("#scAll").textContent = s.total ? `(${s.total.toLocaleString()})` : "";
  $("#scCat").textContent = `(${cats.toLocaleString()})`;
  $("#scUncat").textContent = s.total
    ? `(${((s.total || 0) - cats).toLocaleString()})` : "";
  $("#scSel").textContent = `(${state.sel.size})`;
  const selRadio = document.querySelector('input[name=rscope][value=selected]');
  selRadio.disabled = state.sel.size === 0;
  if (state.sel.size) selRadio.checked = true;
  $("#rfmtVic").style.display = $("#btnVic").style.display === "none" ? "none" : "block";

  const p = await api("/api/report/prefs").catch(() => ({}));
  const h = p.header || {};
  $("#rhAgency").value = h.agency || "";
  $("#rhCase").value = h.case_number || "";
  $("#rhItem").value = h.item_number || "";
  $("#rhExaminer").value = h.examiner || "";
  $("#rhNotes").value = h.notes || "";
  setRptLogo(h.logo || null);
  $("#rhFull").checked = p.full_images !== false;
  $("#rhVideo").checked = p.full_videos !== false;
  $("#rhMaps").checked = p.maps !== false;
  $("#rhMapsRow").style.display = state.basemap ? "" : "none";
  const chosen = new Set(p.fields || ["name", "created_dt", "md5"]);
  $("#rptFields").innerHTML = (p.field_options || []).map(o =>
    `<label><input type="checkbox" class="rfld" value="${esc(o.key)}"${
      chosen.has(o.key) ? " checked" : ""}> ${esc(o.label)}</label>`).join("");

  const byCat = s.by_category || {};
  $("#rscopeCats").innerHTML = [{ code: 0, name: "Uncategorized" }]
    .concat(state.cats.filter(c => c.code !== 0).sort((a, b) => a.position - b.position))
    .map(c => `<label><input type="checkbox" class="rscat" value="${c.code}">
      ${esc(c.name || "Category " + c.code)}
      <span class="muted">(${(byCat[c.code] || 0).toLocaleString()})</span></label>`).join("");
  syncRptScope();
  syncRptHtmlOpts();
  $("#reportDlg").style.display = "block";
}
function syncRptScope() {
  const on = document.querySelector('input[name=rscope][value=categories]').checked;
  $("#rscopeCats").style.display = on ? "" : "none";
}
function setRptLogo(uri) {
  rptLogo = (typeof uri === "string" && uri.startsWith("data:image/")) ? uri : null;
  const prev = $("#rhLogoPrev"), clr = $("#rhLogoClear");
  prev.style.display = clr.style.display = rptLogo ? "" : "none";
  if (rptLogo) prev.src = rptLogo;
}
function syncRptHtmlOpts() {
  const on = $("#reportDlg").querySelector('.rfmt[value=html]').checked;
  $("#rptHtmlOpts").style.display = on ? "" : "none";
}
$("#rhLogoFile").addEventListener("change", e => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 3_000_000) return toast("Logo too large — pick an image under 3 MB");
  const rd = new FileReader();
  rd.onload = () => setRptLogo(rd.result);
  rd.readAsDataURL(f);
});
$("#rhLogoClear").onclick = () => { $("#rhLogoFile").value = ""; setRptLogo(null); };
$("#reportDlg").addEventListener("change", e => {
  if (e.target.classList.contains("rfmt")) syncRptHtmlOpts();
  if (e.target.name === "rscope") syncRptScope();
});

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
  if (scope === "categories") {
    body.categories = [...document.querySelectorAll(".rscat:checked")].map(c => +c.value);
    if (!body.categories.length) return toast("Pick at least one category");
  }
  body.report_header = {
    agency: $("#rhAgency").value.trim(),
    case_number: $("#rhCase").value.trim(),
    item_number: $("#rhItem").value.trim(),
    examiner: $("#rhExaminer").value.trim(),
    notes: $("#rhNotes").value.trim(),
    logo: rptLogo || "",
  };
  body.fields = [...document.querySelectorAll(".rfld:checked")].map(c => c.value);
  body.full_images = $("#rhFull").checked;
  body.full_videos = $("#rhVideo").checked;
  body.maps = $("#rhMaps").checked;
  closeReportDlg();
  const r = await api("/api/report", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (r.error) return toast(r.message || "Export failed");
  if (r.job) {
    // a LAVA project stages the media and draws a map per geolocated file, so
    // it runs as a job and the bottom bar follows it to the end
    lastReportScope = r.scope || "";
    toast(`Building the report (${r.scope}) — the bar at the bottom follows it`);
    liveTick = 0; liveJob();
    return;
  }
  toast(`Exported (${r.scope}) → ${r.dir} — click to open`, 5000, () => openFolder(r.dir));
  addReportNotification(r.dir, `Export (${r.scope})`);
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

/* ---------- display timezone ---------- */
const OTHER_TZ = "__other__";
function setupTz(c) {
  state.tz = c.timezone || "UTC";
  const sel = $("#ftz");
  const opts = (c.timezone_options && c.timezone_options.length)
    ? c.timezone_options.slice()
    : [{ value: "UTC", label: "UTC" }];
  let device = "";
  try { device = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) {}
  if (device && device !== "UTC" && !opts.some(o => o.value === device))
    opts.push({ value: device, label: device + " (this device)" });
  if (state.tz !== "UTC" && !opts.some(o => o.value === state.tz))
    opts.push({ value: state.tz, label: state.tz });
  sel.innerHTML = opts.map(o =>
    `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("")
    + `<option value="${OTHER_TZ}">Other (type IANA name)…</option>`;
  sel.value = state.tz;
}
async function applyTz(tz) {
  const r = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ timezone: tz })
  });
  if (r.error) { toast(r.message || "Unknown timezone"); $("#ftz").value = state.tz; return; }
  state.tz = r.timezone;
  _tzFmtFor = null;                       // force the Intl formatter to rebuild
  if (![...$("#ftz").options].some(o => o.value === state.tz)) {
    $("#ftz").insertAdjacentHTML("beforeend",
      `<option value="${esc(state.tz)}">${esc(state.tz)}</option>`);
  }
  $("#ftz").value = state.tz;
  toast("Times shown in " + (r.label || state.tz));
  await load({ keepScroll: true });
  if (state.metaOpen && state.focus != null) showMeta(state.focus);
}
$("#ftz").addEventListener("change", () => {
  const v = $("#ftz").value;
  if (v === OTHER_TZ) {
    const name = prompt("IANA timezone name (e.g. Europe/Berlin, America/Bogota):", state.tz);
    $("#ftz").value = state.tz;
    if (name) applyTz(name.trim());
    return;
  }
  applyTz(v);
});

/* ---------- known-hash lists ---------- */
function updateKnownHash(kh) {
  if (!kh) return;
  $("#goodCount").textContent = kh.known_good
    ? `(${kh.known_good.toLocaleString()})` : "";
  renderCaseSets(kh.case_sets);
  renderRefStore(kh.global_sets, kh.global_entries || 0);
  const st = kh.stash;
  $("#stashInfo").textContent = st && st.total
    ? `🔒 Hash stash: ${st.total.toLocaleString()} MD5(s) — click to manage`
    : "🔒 Hash stash: empty — click to add your category 1–3 hashes";
  $("#fusestash").checked = kh.use_stash !== false;
}
$("#fusestash").addEventListener("change", async () => {
  const on = $("#fusestash").checked;
  const r = await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ use_stash: on })
  });
  if (r.error) { $("#fusestash").checked = !on; return toast(r.message || "Could not change this"); }
  if (on) {
    toast("Hash stash matching on — click Re-check to check files already in this case");
  } else {
    toast(r.cleared
      ? `Hash stash matching off — ${r.cleared.toLocaleString()} stash hit(s) cleared`
      : "Hash stash matching off for this case");
    try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
    load();
  }
});

/* ---------- nested archives (.zip / .tar / .gz inside a source) ---------- */
function updateArchInfo(a) {
  const sec = document.querySelector('.fsec[data-sec="arch"]');
  const el = $("#archInfo");
  if (!el) return;
  if (sec) sec.hidden = !(a && a.total);
  if (!a || !a.total) { el.innerHTML = ""; return; }
  const pending = a.total - a.expanded;
  const label = pending > 0 ? "Expand archives" : "Re-check archives";
  el.innerHTML = `${a.total.toLocaleString()} archive${a.total === 1 ? "" : "s"}`
    + (a.expanded ? ` · ${a.expanded.toLocaleString()} expanded` : "")
    + ` <button class="btn sm" id="btnExpand">${label}</button>`
    + `<div id="expandInfo" class="fnote"></div>`;
  $("#btnExpand").onclick = async () => {
    const r = await api("/api/expand-archives", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: pending === 0 }),
    });
    if (r.error) return toast(r.message || "Could not start");
    $("#btnExpand").disabled = true;
    toast(`Opening ${r.count} archive(s)…`);
    trackJob("#expandInfo", "#taskProg", "Opening archives", (ok, j) => {
      if (ok) {
        const added = j.stats?.expanded ?? 0;
        $("#expandInfo").textContent = added
          ? `${added} file(s) recovered — reloading…` : "Nothing new";
        if (added) setTimeout(() => location.reload(), 900);
        else $("#btnExpand").disabled = false;
      } else $("#btnExpand").disabled = false;
    });
  };
}

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
/* Poll the shared background job and mirror its progress into a
   section-local status line + mini bar, so each action reports where it
   lives: "Retry failed files" under Other, "Re-scan for duplicates" under
   Display, screening under Screening. */
function trackJob(infoSel, barSel, label, done) {
  const info = $(infoSel);
  const bar = barSel ? $(barSel) : null;
  if (bar) { bar.style.display = "block"; bar.querySelector("i").style.width = "0"; }
  const finish = (ok, j) => {
    if (bar) bar.style.display = "none";
    if (done) done(ok, j || {});
  };
  const poll = async () => {
    let j;
    try { j = await api("/api/job"); }
    catch (e) { return setTimeout(poll, 900); }
    if (j.stage === "error") {
      info.textContent = `${label} failed: ${j.error || "unknown error"}`;
      return finish(false, j);
    }
    if (j.stage === "done" || !j.running) return finish(true, j);
    const pct = j.total ? Math.round(100 * j.done / j.total) : 0;
    info.textContent = j.total
      ? `${j.message || label} — ${j.done.toLocaleString()}/${j.total.toLocaleString()} (${pct}%)`
      : `${j.message || label}…`;
    if (bar) bar.querySelector("i").style.width = (j.total ? pct : 12) + "%";
    setTimeout(poll, 900);
  };
  poll();
}
$("#btnRetryErr").onclick = async () => {
  const r = await api("/api/reprocess-errors", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start");
  $("#btnRetryErr").disabled = true;
  toast(`Retrying ${r.count} failed files…`);
  trackJob("#retryInfo", "#taskProg", "Retrying failed files", (ok, j) => {
    $("#btnRetryErr").disabled = false;
    if (ok) {
      const fixed = j.stats?.recovered ?? 0;
      $("#retryInfo").textContent = `Recovered ${fixed} of ${r.count} — reloading…`;
      toast(`Recovered ${fixed} of ${r.count} files`);
      location.reload();
    }
  });
};
$("#btnRehash").onclick = async () => {
  const r = await api("/api/rehash", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start");
  $("#btnRehash").disabled = true;
  trackJob("#rehashInfo", "#taskProg", "Re-checking known hashes", async (ok, j) => {
    $("#btnRehash").disabled = false;
    if (!ok) return;
    const hits = j.stats?.hashset_hits ?? 0;
    $("#rehashInfo").textContent = `${hits.toLocaleString()} known-hash hit(s)`;
    toast(`Known-hash re-check done: ${hits.toLocaleString()} hit(s)`);
    try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
    load();
  });
};

/* ---------- import a hash set (CyberTip MD5s, CAID, CSV, VIC JSON) ---------- */
/* PhotoDNA entries sit in a set's total but can never flag a file: comparing
   them needs a licensed PhotoDNA implementation, which GLEAPP does not ship.
   Say so next to the count rather than letting the total read as coverage. */
const PDNA_WHY = "PhotoDNA values are stored but never matched: comparing PhotoDNA "
  + "needs a licensed PhotoDNA implementation, which GLEAPP does not ship.";
function pdnaBadge(n) {
  n = +n || 0;
  return n ? ` <span class="muted" title="${esc(PDNA_WHY)}">· ${n.toLocaleString()} PhotoDNA, not matched</span>` : "";
}

function renderCaseSets(sets) {
  sets = sets || [];
  const el = $("#caseSets");
  if (el) {
    el.innerHTML = sets.map(s =>
      `<div class="cs" data-id="${s.id}">` +
      `<span title="${esc(s.source || "")}">${s.kind === "known" ? "<b>⚑</b> " : "· "}` +
      `${esc(s.name)}</span>` +
      `<span class="muted">${(s.count || 0).toLocaleString()} · ${(s.hits || 0).toLocaleString()} hit${s.hits === 1 ? "" : "s"}</span>` +
      pdnaBadge(s.photodna) +
      `<span class="x" title="Remove this set and clear its flags">✕</span></div>`).join("")
      || `<span class="muted">No hash set imported yet.</span>`;
    el.querySelectorAll(".cs .x").forEach(x => x.onclick = async () => {
      const row = x.closest(".cs");
      const id = +row.dataset.id;
      if (!confirm("Remove this hash set and clear the flags it added?")) return;
      row.style.opacity = ".4";
      const r = await api("/api/hashset/remove", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      if (r.error) { row.style.opacity = ""; return toast(r.message || "Could not remove"); }
      row.remove();
      const refresh = async () => {
        try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
        if ($("#fhashset").value && ![...$("#fhashset").options].some(o => o.value === $("#fhashset").value))
          $("#fhashset").value = "";
        load();
      };
      if (r.rematched) trackJob("#rehashInfo", "#taskProg", "Updating flags", refresh);
      else { toast("Hash set removed"); refresh(); }
    });
  }
  // the "Show" filter dropdown: one row per imported set, plus "any"
  const sel = $("#fhashset");
  if (sel) {
    const cur = sel.value;
    sel.innerHTML = `<option value="">— all files —</option>` +
      `<option value="*good">Only: NSRL / known-good hits</option>` +
      `<option value="Local Hash Stash">Only: Hash stash hits</option>` +
      (sets.length ? `<option value="*">Any imported hash set</option>` : "") +
      sets.map(s => `<option value="${esc(s.name)}">Only: ${esc(s.name)}</option>`).join("");
    sel.value = [...sel.options].some(o => o.value === cur) ? cur : "";
  }
}

async function browseHashFile() {
  const p = await pick("hashlist",
    "Path to the hash list (a CyberTip MD5 list, CSV, VIC JSON…):");
  if (!p) return;                       // user cancelled the dialog
  $("#hiPath").value = p;
  if (!$("#hiName").value.trim())
    $("#hiName").value = p.split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
}
$("#btnImportHash").onclick = () => {
  $("#hiPath").value = "";
  $("#hiName").value = "";
  $("#hiPath").readOnly = !!Lr.native;   // desktop: pick only; browser: allow paste
  $("#hiPath").placeholder = Lr.native ? "click “Choose file…”" : "paste the path, or Choose file…";
  document.querySelector('input[name=hiKind][value=known]').checked = true;
  $("#hiGo").disabled = false;
  $("#hashImportDlg").style.display = "block";
  browseHashFile();                       // open the OS file dialog right away
};
$("#hiBrowse").onclick = browseHashFile;
$("#hiCancel").onclick = () => $("#hashImportDlg").style.display = "none";
$("#hashImportDlg").addEventListener("click", e => {
  if (e.target.id === "hashImportDlg") $("#hashImportDlg").style.display = "none";
});
$("#hiGo").onclick = async () => {
  const path = $("#hiPath").value.trim();
  if (!path) return toast("Choose a hash-list file first");
  const kind = document.querySelector('input[name=hiKind]:checked').value;
  const name = $("#hiName").value.trim();
  $("#hiGo").disabled = true;
  const r = await api("/api/hashset/import", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, name, kind }),
  });
  $("#hiGo").disabled = false;
  if (r.error) return toast(r.message || "Import failed");
  $("#hashImportDlg").style.display = "none";
  toast(`Imported ${r.entries.toLocaleString()} hashes as “${r.name}” — flagging files…`
    + (r.photodna_note ? `. ${r.photodna_note}` : ""),
    r.photodna_note ? 9000 : 0);
  // a toast goes away, so the same fact also sits on the set's own row
  // (pdnaBadge), in the case audit log, and in the LAVA export's Known Hash Sets
  trackJob("#rehashInfo", "#taskProg", "Flagging files", async (ok, j) => {
    if (!ok) return;
    const hits = j.stats?.hashset_hits ?? 0;
    toast(`“${r.name}”: ${hits.toLocaleString()} file(s) flagged`);
    try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
    // jump straight to the flagged files
    if (r.kind === "known" && [...$("#fhashset").options].some(o => o.value === r.name)) {
      $("#fhashset").value = r.name;
    }
    reload();
  });
};

/* ---------- global reference store (NSRL RDS etc.) ---------- */
function renderRefStore(sets, total) {
  sets = sets || [];
  const info = $("#refInfo");
  if (info) info.textContent = sets.length
    ? `Reference data: ${total.toLocaleString()} hashes · ${sets.length} set${sets.length === 1 ? "" : "s"} ▸`
    : "Reference data: none — add NSRL ▸";

  const list = $("#refList");
  if (!list) return;
  list.innerHTML = sets.length
    ? sets.map(s =>
        `<div class="cs" data-id="${s.id}">` +
        `<span style="flex:1" title="${esc(s.source || "")}">${esc(s.name)}</span>` +
        `<span class="muted">${(s.count || 0).toLocaleString()}</span>` +
        pdnaBadge(s.photodna) +
        `<span class="x" title="Remove from the shared store">✕</span></div>`).join("")
    : `<span class="muted">Nothing imported yet.</span>`;
  list.querySelectorAll(".cs .x").forEach(x => x.onclick = async () => {
    const row = x.closest(".cs"), id = +row.dataset.id;
    if (!confirm("Remove this reference set from the shared store?\n\nEvery case stops matching against it.")) return;
    row.style.opacity = ".4";
    const r = await api("/api/hashset/global/remove", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    if (r.error) { row.style.opacity = ""; return toast(r.message || "Could not remove"); }
    toast("Reference set removed");
    const after = async () => {
      try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
      // this dialog is also reachable pre-case, from the launcher — nothing to reload then
      if ($("#launcher").style.display !== "block") load();
    };
    if (r.rematched) trackJob("#rehashInfo", "#taskProg", "Updating flags", after);
    else after();
  });
}

function refMode() {
  return document.querySelector('input[name=refMode]:checked').value;
}
function syncRefDlg() {
  const delta = refMode() === "delta";
  $("#refBaseRow").style.display = delta ? "" : "none";
  $("#refFileLbl").textContent = delta ? "Delta script (_delta.sql)" : "Database file";
}
async function browseRef(target) {
  const p = await pick("hashdb", "Path to the reference database / delta script:");
  if (!p) return;
  $(target).value = p;
  if (target === "#refPath" && !$("#refName").value.trim())
    $("#refName").value = p.split(/[\\/]/).pop()
      .replace(/\.(db|sqlite3?|sql)$/i, "").replace(/_delta$/i, "");
}
function openRefDlg() {
  $("#refPath").value = ""; $("#refBase").value = ""; $("#refName").value = "";
  $("#refPath").readOnly = $("#refBase").readOnly = !!Lr.native;
  document.querySelector('input[name=refMode][value=full]').checked = true;
  $("#refKind").value = "known-good";
  $("#refAlgos").value = "md5";
  $("#refGo").disabled = false;
  syncRefDlg();
  $("#refDlg").style.display = "block";
}
$("#refInfo").onclick = openRefDlg;
$("#btnRefStore").onclick = openRefDlg;
$("#btnRefMenuLauncher").onclick = () => toggleHdrMenu($("#btnRefMenuLauncher"), $("#refMenuLauncher"));
$("#refMenuLauncher").addEventListener("click", e => {
  if (e.target.tagName === "BUTTON") $("#refMenuLauncher").style.display = "none";
});
$("#btnMapsLauncher").onclick = openMapsDlg;
$("#btnStashLauncher").onclick = openStashDlg;
$("#btnRefStoreLauncher").onclick = openRefDlg;
document.querySelectorAll('input[name=refMode]').forEach(r => r.onchange = syncRefDlg);
$("#refBrowse").onclick = () => browseRef("#refPath");
$("#refBaseBrowse").onclick = () => browseRef("#refBase");
$("#refCancel").onclick = () => $("#refDlg").style.display = "none";
$("#refDlg").addEventListener("click", e => {
  if (e.target.id === "refDlg") $("#refDlg").style.display = "none";
});
$("#refGo").onclick = async () => {
  const path = $("#refPath").value.trim();
  if (!path) return toast("Choose the reference file first");
  const delta = refMode() === "delta";
  const base = $("#refBase").value.trim();
  if (delta && !base) return toast("A quarterly delta needs the previous full .db");
  const algos = $("#refAlgos").value.split(",");
  const kind = $("#refKind").value;
  const name = $("#refName").value.trim();
  $("#refGo").disabled = true;
  const r = await api("/api/hashset/global/import", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, base: delta ? base : "", name, kind, algos }),
  });
  $("#refGo").disabled = false;
  if (r.error) return toast(r.message || "Import failed");
  $("#refDlg").style.display = "none";
  toast(`Importing ${r.name} — this runs in the background`);
  trackJob("#rehashInfo", "#taskProg", "Importing reference data", async (ok, j) => {
    if (!ok) return;
    toast(j.message || "Reference data imported");
    try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
    // this dialog is also reachable pre-case, from the launcher — nothing to reload then
    if ($("#launcher").style.display !== "block") load();
  });
};

$("#btnRedup").onclick = async () => {
  const r = await api("/api/redup", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start");
  $("#btnRedup").disabled = true;
  trackJob("#redupInfo", "#taskProg", "Re-scanning for duplicates", (ok, j) => {
    $("#btnRedup").disabled = false;
    if (ok) {
      const vs = j.stats?.visual_stacks ?? 0, cl = j.stats?.clusters ?? 0;
      $("#redupInfo").textContent = `${vs} visual stacks · ${cl} near-dup clusters`;
      toast(`Re-scan done: ${vs} visual stacks`);
      load();
    }
  });
};
$("#btnScreen").onclick = async () => {
  if (!confirm("Run face + skin-tone screening over every thumbnail?\n"
      + "Runs in the background; you can keep working.")) return;
  const r = await api("/api/screen", { method: "POST" });
  if (r.error) return toast(r.message || "Could not start screening");
  $("#btnScreen").disabled = true;
  trackJob("#screenInfo", "#taskProg", "Screening", async (ok) => {
    $("#btnScreen").disabled = false;
    if (!ok) return;                     // leave the failure text in place
    try {
      const c = await api("/api/context");
      updateScreenInfo(c.screening);
    } catch (e) {}
    toast("Screening complete");
    load();
  });
};
/* ---------- snapshots ---------- */
const _snapBytes = n => {
  if (n < 1024) return n + " B";
  const u = ["KB", "MB", "GB", "TB"]; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(1) + " " + u[i];
};
async function refreshSnapList() {
  const box = $("#snapList");
  box.textContent = "Loading…";
  const rows = await api("/api/snapshots").catch(() => []);
  if (!rows.length) { box.textContent = "No snapshots yet."; return; }
  box.innerHTML = rows.map(s => {
    const when = fmtEpoch(s.created);
    const tag = s.auto ? `<span class="tagauto">auto</span>`
      : s.label ? `<span class="tagauto" style="background:#2980b9;border-color:#2980b9;color:#fff">${esc(s.label)}</span>`
      : `<span class="tagauto">manual</span>`;
    return `<div class="snaprow">
      <div><b>${when}</b> ${tag}<br><small>${esc(s.name)} · ${_snapBytes(s.size)}</small></div>
      <button class="btn" data-snap="${esc(s.name)}">Restore</button>
    </div>`;
  }).join("");
  box.querySelectorAll("button[data-snap]").forEach(b => {
    b.onclick = () => restoreSnap(b.dataset.snap);
  });
}
async function restoreSnap(name) {
  if (!confirm(
    "Restore this snapshot?\n\n" + name + "\n\n" +
    "The current state is saved as a \"pre-restore\" snapshot first, so this is undoable. " +
    "The case will reload.")) return;
  const r = await save("/api/snapshot/restore", { name });
  if (r.error) return toast("Restore failed: " + (r.message || "unknown error"));
  toast("Snapshot restored — reloading");
  setTimeout(() => location.reload(), 600);
}
function openSnapDlg() {
  $("#snapLabel").value = "";
  $("#snapDlg").style.display = "block";
  refreshSnapList();
}
$("#snapSave").onclick = async () => {
  const label = $("#snapLabel").value.trim();
  const r = await save("/api/snapshot", { label: label || null });
  if (r.error) return toast("Snapshot failed");
  $("#snapLabel").value = "";
  toast("Snapshot saved: " + r.name);
  refreshSnapList();
};
$("#snapClose").onclick = () => $("#snapDlg").style.display = "none";
$("#snapDlg").addEventListener("click", e => {
  if (e.target.id === "snapDlg") $("#snapDlg").style.display = "none";
});
$("#btnSnapshot").onclick = openSnapDlg;

/* ---------- processing history (audit log) ---------- */
// Actions written by a background run, vs. an examiner edit. Anything not listed
// here is treated as an edit for the "Show" filter.
const HIST_RUN_ACTIONS = new Set([
  "ingest", "ingest-archive", "process", "screen_pass", "rematch_hashes",
  "hashset_import", "hashset_remove", "import_vic", "export_vic",
  "carve-source", "stage-source", "unstage-source", "relink-source",
  "snapshot", "restore_snapshot",
]);
// A Python dict repr (single quotes, None/True/False) -> object, best effort.
function _parsePyRepr(s) {
  if (typeof s !== "string" || s[0] !== "{") return null;
  try {
    return JSON.parse(s
      .replace(/'/g, '"').replace(/\bNone\b/g, "null")
      .replace(/\bTrue\b/g, "true").replace(/\bFalse\b/g, "false"));
  } catch (_e) { return null; }
}
function _histDetail(e) {
  const d = _parsePyRepr(e.detail);
  if (e.action === "process" && d) {
    const bits = [
      `${d.discovered ?? "?"} files`,
      `${d.processed ?? 0} processed`,
      d.skipped ? `${d.skipped} skipped` : null,
      `${d.errors ?? 0} errored`,
      d.hashset_hits ? `${d.hashset_hits} known-hash hits` : null,
      d.redundant_duplicates ? `${d.redundant_duplicates} exact dups` : null,
      d.visual_stacks ? `${d.visual_stacks} visual stacks` : null,
      d.clusters ? `${d.clusters} clusters` : null,
    ].filter(Boolean);
    let html = `<div class="hdetail">${esc(bits.join(" · "))}</div>`;
    const stages = d.stages || {};
    const keys = Object.keys(stages);
    if (keys.length) {
      html += `<div class="hstages">` + keys.map(k => {
        const v = String(stages[k]);
        const bad = v.startsWith("failed");
        return `<span class="hpill ${bad ? "fail" : "ok"}" title="${esc(v)}">`
          + `${esc(k)}${bad ? " ✗" : " ✓"}</span>`;
      }).join("") + `</div>`;
    }
    return html;
  }
  return e.detail ? `<div class="hdetail">${esc(e.detail)}</div>` : "";
}
function _histMatch(e, f) {
  if (f === "runs") return HIST_RUN_ACTIONS.has(e.action);
  if (f === "edits") return !HIST_RUN_ACTIONS.has(e.action);
  if (f === "failed") return /failed/i.test(e.detail || "");
  return true;
}
async function refreshHistList() {
  const box = $("#histList");
  box.textContent = "Loading…";
  const rows = await api("/api/audit").catch(() => []);
  const f = $("#histFilter").value;
  const shown = rows.filter(e => _histMatch(e, f));
  if (!shown.length) { box.textContent = rows.length ? "Nothing matches this filter." : "No history recorded yet."; return; }
  box.innerHTML = shown.map(e => `<div class="histrow">
    <div class="htop">
      <time>${esc(fmtEpoch(e.ts))}</time>
      <span class="hact">${esc(e.action)}</span>
      <span class="hactor">${esc(e.actor || "")}</span>
    </div>
    ${_histDetail(e)}
  </div>`).join("");
}
function openHistDlg() {
  $("#histDlg").style.display = "block";
  refreshHistList();
}
$("#histFilter").onchange = refreshHistList;
$("#histRefresh").onclick = refreshHistList;
$("#histClose").onclick = () => $("#histDlg").style.display = "none";
$("#histDlg").addEventListener("click", e => {
  if (e.target.id === "histDlg") $("#histDlg").style.display = "none";
});
// opened from the "☰ Menu" dropdown's Help section

/* ---------- local hash stash ---------- */
const STASH_CAT_NAMES = { 1: "CAM", 2: "Child Exploitative", 3: "CGI / Animation" };
function stashRows(byCat, catList) {
  const cats = (catList || [1, 2, 3]).slice().sort();
  let total = 0, html = "";
  for (const c of cats) {
    const n = byCat[c] || 0; total += n;
    html += `<div><span class="dot" style="background:${catColor(c)}"></span>`
      + `${esc(catName(c) || STASH_CAT_NAMES[c] || "Category " + c)}</div>`
      + `<div class="n">${n.toLocaleString()}</div>`;
  }
  html += `<div class="tot">Total</div><div class="n tot">${total.toLocaleString()}</div>`;
  return html;
}
let stashLastTotal = 0;
async function refreshStashDlg() {
  $("#stashCase").innerHTML = "<div class='muted'>Loading…</div><div></div>";
  const d = await api("/api/stash").catch(() => null);
  if (!d) { $("#stashCase").innerHTML = "<div>Couldn't read the stash.</div><div></div>"; return; }
  stashLastTotal = d.stash.total || 0;
  $("#stashCase").innerHTML = stashRows(d.case.by_category, d.case.categories);
  $("#stashTotal").innerHTML = stashRows(d.stash.by_category, d.case.categories);
  $("#stashAdd").disabled = !d.case.eligible;
  $("#stashAdd").textContent = d.case.eligible
    ? `Add this case's ${d.case.eligible.toLocaleString()} hash(es) to the stash`
    : ($("#launcher").style.display === "block"
       ? "Open a case to add its category 1–3 hashes"
       : "No category 1–3 files in this case yet");
  $("#stashClear").disabled = !d.stash.total;
  $("#stashUpdated").textContent = d.stash.updated
    ? "Stash last updated " + fmtEpoch(d.stash.updated)
    : "The stash is empty.";
  $("#stashPath").textContent = d.stash.path || "";
  $("#stashShared").textContent = d.stash.shared ? " — shared location" : "";
  $("#stashResetPath").style.display = d.stash.shared ? "" : "none";
}
async function stashRefreshAndSidebar() {
  await refreshStashDlg();
  try { updateKnownHash((await api("/api/context")).known_hash); } catch (e) {}
}
function openStashDlg() { $("#stashDlg").style.display = "block"; refreshStashDlg(); }
$("#btnStash").onclick = openStashDlg;
$("#stashInfo").onclick = openStashDlg;
$("#stashClose").onclick = () => $("#stashDlg").style.display = "none";
$("#stashDlg").addEventListener("click", e => {
  if (e.target.id === "stashDlg") $("#stashDlg").style.display = "none";
});
$("#stashAdd").onclick = async () => {
  $("#stashAdd").disabled = true;
  const r = await api("/api/stash/add", { method: "POST" });
  if (r.error) { toast(r.message || "Could not update the stash"); return refreshStashDlg(); }
  toast(`Stash: +${r.added.toLocaleString()} new (${r.submitted.toLocaleString()} submitted, `
    + `${r.total.toLocaleString()} total)`);
  stashRefreshAndSidebar();
};
$("#stashClear").onclick = () => {
  $("#wipeCount").textContent = stashLastTotal.toLocaleString();
  $("#wipeConfirm").checked = false;
  $("#wipeGo").disabled = true;
  $("#stashWipeDlg").style.display = "block";
};
$("#wipeConfirm").onchange = () => { $("#wipeGo").disabled = !$("#wipeConfirm").checked; };
$("#wipeCancel").onclick = () => { $("#stashWipeDlg").style.display = "none"; };
$("#stashWipeDlg").addEventListener("click", e => {
  if (e.target.id === "stashWipeDlg") $("#stashWipeDlg").style.display = "none";
});
$("#wipeGo").onclick = async () => {
  if (!$("#wipeConfirm").checked) return;
  $("#wipeGo").disabled = true;
  const r = await api("/api/stash/clear", { method: "POST" });
  $("#stashWipeDlg").style.display = "none";
  if (r.error) return toast(r.message || "Clear failed");
  toast(`Stash cleared — ${(r.removed || 0).toLocaleString()} entries removed`);
  stashRefreshAndSidebar();
};
async function stashExport(format) {
  const r = await api("/api/stash/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ format })
  });
  if (r.error) return toast(r.message || "Export failed");
  toast(`Stash written to ${r.written} — click to open the folder`, 6000, () => openFolder(r.dir));
}
$("#stashExportDb").onclick = () => stashExport("db");
$("#stashExportCsv").onclick = () => stashExport("csv");
$("#stashMerge").onclick = async () => {
  const p = await pick("stashfile", "Path to a colleague's stash file (.gleapp or .csv):");
  if (!p) return;
  const r = await api("/api/stash/merge", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: p })
  });
  if (r.error) return toast(r.message || "Merge failed");
  toast(`Merged — +${r.added.toLocaleString()} new, ${r.total.toLocaleString()} total`);
  stashRefreshAndSidebar();
};
async function stashSetPath(p) {
  const r = await api("/api/stash/path", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: p || "" })
  });
  if (r.error) return toast(r.message || "Could not switch stash file");
  toast(p ? "Now using the shared stash file" : "Back to your own stash file");
  stashRefreshAndSidebar();
}
$("#stashSetPath").onclick = async () => {
  const p = await pick("stashfile", "Path to the shared stash file (e.g. on a network drive):");
  if (p) stashSetPath(p);
};
$("#stashResetPath").onclick = () => {
  if (confirm("Switch back to your own per-user stash file?\n\n"
    + "The shared file is left untouched; your local stash is used again "
    + "(it may be empty or out of date — Merge the shared file in if you want).")) {
    stashSetPath("");
  }
};

$("#btnClose").onclick = async () => {
  if (liveTimer) return toast("Processing is still running — let it finish first");
  if (pendingNoteFlush) { try { await pendingNoteFlush(); } catch (e) {} }
  setSaveState("saving");
  const r = await api("/api/case/close", { method: "POST" });
  if (r && r.error) { setSaveState("saved"); return toast(r.message || "Could not close the case"); }
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
const Lr = { native: false, sources: [], logo: null };
function fmtAgo(ts) {
  const s = (Date.now() / 1000) - ts;
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}
async function pick(kind, label) {
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
  return prompt(label || ({ folder: "Folder path:",
    archive: "Path to the extraction archive or acquisition (zip, tar, tar.gz/bz2/xz, E01):",
    basemap: "Path to a basemap file (.pmtiles or .mbtiles):" }[kind]
    || "Path to .json job file:")) || null;
}
function renderSources() {
  const icon = { spec: "\u{1F4C4} ", archive: "\u{1F4E6} " };   // 📄  📦  📁
  $("#srcList").innerHTML = Lr.sources.map((s, i) =>
    `<div class="s"><span>${icon[s.kind] || "\u{1F4C1} "}${esc(s.path)}</span>
     <b data-rm="${i}" style="cursor:pointer;color:var(--danger)">×</b></div>`).join("");
  $("#srcList").querySelectorAll("[data-rm]").forEach(b =>
    b.onclick = () => { Lr.sources.splice(+b.dataset.rm, 1); renderSources(); });
}
function showLauncher(ctx) {
  Lr.native = !!ctx.native;
  $("#launcher").style.display = "block";
  $("#main").style.display = "none";
  if (ctx.known_hash) updateKnownHash(ctx.known_hash);
  // named from the server's own list, so the screen cannot drift from the walk
  const fs = ctx.walked_filesystems || [];
  $("#fsList").innerHTML = fs.length
    ? fs.map(f => `<code>${esc(f)}</code>`).join(", ")
    : "the filesystems the reader supports";
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
  Lr.logo = ctx.agency_logo || null;
  setSettingsLogoPreview(Lr.logo);
}

/* ---------- agency logo: app-wide default, set from ☰ Settings on the
   launcher. It lives in appconfig (not any case) and goes on every case's
   report header unless that case sets its own from its own Export dialog. */
function setSettingsLogoPreview(uri) {
  const prev = $("#logoPrev"), clr = $("#logoClear");
  prev.style.display = clr.style.display = uri ? "" : "none";
  if (uri) prev.src = uri;
}
function openLogoDlg() {
  $("#logoFile").value = "";
  setSettingsLogoPreview(Lr.logo);
  $("#logoDlg").style.display = "block";
}
$("#btnLogoLauncher").onclick = openLogoDlg;
$("#logoFile").addEventListener("change", e => {
  const f = e.target.files[0];
  if (!f) return;
  if (f.size > 3_000_000) return toast("Logo too large — pick an image under 3 MB");
  const rd = new FileReader();
  rd.onload = async () => {
    Lr.logo = rd.result;
    setSettingsLogoPreview(Lr.logo);
    const r = await api("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agency_logo: Lr.logo })
    }).catch(() => ({ error: true }));
    if (r.error) return toast(r.message || "Could not save the logo");
    toast("Agency logo saved — used on every case's report unless overridden");
  };
  rd.readAsDataURL(f);
});
$("#logoClear").onclick = async () => {
  Lr.logo = null;
  $("#logoFile").value = "";
  setSettingsLogoPreview(null);
  await api("/api/settings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agency_logo: null })
  });
  toast("Agency logo cleared");
};
$("#logoClose").onclick = () => $("#logoDlg").style.display = "none";
$("#logoDlg").addEventListener("click", e => {
  if (e.target.id === "logoDlg") $("#logoDlg").style.display = "none";
});

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
  $("#jobMsg").textContent = j.message || j.stage;
  if (j.stage === "error") {
    $("#jobMsg").textContent = "Error: " + (j.error || "processing failed");
    $("#createGo").disabled = false;
    return;
  }
  // Open the gallery as soon as files exist — the user reviews already-processed
  // files while the rest process, with a live progress bar at the bottom.
  if (j.stage === "process" || j.stage === "done" || j.stage === "carve" ||
      (j.stage === "ingest" && j.done > 0)) {
    $("#jobMsg").textContent = "Opening case…";
    setTimeout(() => location.reload(), 400);
    return;
  }
  setTimeout(pollJob, 500);   // idle / starting / early ingest
}
$("#openBrowse").onclick = async () => { const p = await pick("folder"); if (p) $("#openPath").value = p; };
$("#openGo").onclick = () => $("#openPath").value && openCase($("#openPath").value.trim());
$("#newBrowse").onclick = async () => { const p = await pick("folder"); if (p) $("#newPath").value = p; };
const ARCHIVE_RE = /\.(zip|tar|tgz|tbz2?|txz|tar\.(gz|bz2|xz))$/i;
function addSource(p) {
  p = (p || "").trim().replace(/^["']|["']$/g, "");
  if (!p) return;
  const kind = /\.json$/i.test(p) ? "spec" : ARCHIVE_RE.test(p) ? "archive" : "folder";
  if (!Lr.sources.some(s => s.path === p)) Lr.sources.push({ kind, path: p });
  renderSources();
}
$("#addFolder").onclick = async () => addSource(await pick("folder"));
$("#addArchive").onclick = async () => addSource(await pick("archive"));
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
      path, name: $("#newName").value.trim(), examiner: $("#newExaminer").value.trim(),
      use_stash: $("#optStash").checked
    })
  }).catch(() => ({ error: true, message: "request failed" }));
  if (cr.error) return fail(cr.message || "Could not create case");

  const specs = Lr.sources.filter(s => s.kind === "spec").map(s => s.path);
  // folders and archives both go through parse_source_spec server-side, which
  // detects an archive from its bytes and ingests it as one.
  const folders = Lr.sources.filter(s => s.kind === "folder" || s.kind === "archive")
    .map(s => ({ name: s.path.split(/[\\/]/).filter(Boolean).pop(), path: s.path }));
  $("#jobMsg").textContent = "Starting ingest…";
  const ing = await api("/api/case/ingest", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      spec: specs[0] || null, sources: folders,
      options: { screen: $("#optScreen").checked, keyframes: +$("#optKf").value,
                 stage: $("#optStage").checked, carve: $("#optCarve").checked }
    })
  }).catch(() => ({ error: true, message: "request failed" }));
  if (ing.error) return fail(ing.message || "Ingest failed");
  pollJob();
};

/* ---------- extraction zips read on demand ---------- */
// A case built from an extraction zip or an E01 acquisition without copying the media
// out depends on that file staying where the case recorded it. When it is missing or has changed,
// say so at the top of the gallery and offer to relink it; the server accepts a
// new location only if it holds every registered file with the same size and CRC.
// The sidebar's Source section lists every extraction zip the case was built from,
// with its mode, and offers the conversion each way: "Copy into case" runs the stage
// job (the live bottom bar follows it), "Drop copies" deletes the copies and goes back
// to reading the zip, which the server refuses unless the zip still holds every file.
function renderSourcePanel(list) {
  const el = $("#srcInfo");
  if (!el) return;
  list = list || [];
  if (!list.length) { el.innerHTML = ""; return; }
  el.innerHTML = list.map(s => {
    const ok = s.status === "ok";
    const state = ok ? "" : ` <span style="color:#c98a2b" title="${esc(s.path)}">(${esc(s.status)})</span>`;
    const ewf = s.format === "ewf";
    const mode = s.mode === "staged" ? "copied into the case"
      : (ewf ? "read from the acquisition" : "read from the archive");
    // How the rows were recovered, counted from what the ingest recorded rather
    // than guessed from the format: an acquisition whose filesystems can be read
    // is walked, and carving one is a separate thing to ask for, so a source can
    // hold both kinds of row. Only an acquisition can hold either.
    const walked = s.walked || 0, cut = s.carved || 0, rec = s.recovered || 0;
    const how = [];
    if (walked) how.push(`<span class="muted" title="Read out of a filesystem in`
      + ` the acquisition, so each file keeps the name, path and any timestamps the`
      + ` filesystem recorded.">· ${walked.toLocaleString()} walked</span>`);
    if (cut) how.push(`<span class="muted" title="Recovered by signature from bytes`
      + ` no file claims. A carved file has no name, path or timestamp of its own;`
      + ` the name is the offset it was found at, and the date columns are empty.">`
      + `· ${cut.toLocaleString()} carved</span>`);
    if (rec) how.push(`<span class="muted" title="Recovered from a deleted record that`
      + ` still named the file (NTFS MFT, or a FAT32 or exFAT directory entry), so it`
      + ` keeps its name. NTFS keeps the dates as recorded; FAT and exFAT store a`
      + ` zone-less wall clock, kept as text. Reaches a resident NTFS file, which`
      + ` carving cannot.">· ${rec.toLocaleString()} recovered</span>`);
    const origin = how.length ? " " + how.join(" ") : "";
    // Which volumes the acquisition held and what each was read as, and above
    // all the ones that could not be read: an examiner has to be able to see
    // that part of the disk was not examined, rather than infer it from a count.
    let vols = "";
    try {
      const list = s.volumes ? JSON.parse(s.volumes) : [];
      if (list.length) {
        vols = `<div class="muted" style="font-size:11px;margin-top:2px">read as `
          + list.map(v => `<b>${esc(v.kind)}</b>${v.label ? " " + esc(v.label) : ""}`
                          + ` (${_snapBytes(v.size || 0)})`).join(", ") + `</div>`;
      }
      const bad = s.volumes_not_read ? JSON.parse(s.volumes_not_read) : [];
      if (bad.length) {
        vols += `<div style="font-size:11px;margin-top:2px;color:var(--danger)"`
          + ` title="These volumes were found in the acquisition and could not be`
          + ` opened, so nothing in them was registered.">not read: `
          + bad.map(esc).join("; ") + `</div>`;
      }
    } catch (e) { vols = ""; }
    // a compressed tar cannot be read on demand, so its copies cannot be dropped
    const fixed = s.format === "tar-compressed";
    const btn = s.mode === "staged"
      ? `<button class="btn sm" data-unstage="${esc(s.name)}"${ok && !fixed ? "" : " disabled"}
           title="${fixed ? "A compressed tar cannot be read on demand, so the copies stay."
             : "Delete the copies and read from the archive on demand again. Refused unless the archive still holds every registered file."}">Drop copies</button>`
      : `<button class="btn sm" data-stage="${esc(s.name)}"${ok ? "" : " disabled"}
           title="Copy every registered file out of the archive into the case, so the case no longer needs it.">Copy into case</button>`;
    return `<div style="margin:3px 0"><b title="${esc(s.path)}">${esc(s.name)}</b>
      <span class="muted">· ${(s.files || 0).toLocaleString()} files · ${mode}</span>${origin}${state}
      ${vols}<div style="margin-top:2px">${btn}</div></div>`;
  }).join("");
  const post = (url, body) => api(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).catch(() => ({ error: true, message: "request failed" }));
  el.querySelectorAll("[data-stage]").forEach(b => b.onclick = async () => {
    const name = b.dataset.stage;
    const r = await post("/api/source/stage", { name });
    if (r.error) { toast(r.message || "Could not start the copy"); return; }
    toast(`Copying ${name} into the case…`);
    liveTick = 0; liveJob();                       // the bottom bar follows the job
  });
  el.querySelectorAll("[data-unstage]").forEach(b => b.onclick = async () => {
    const name = b.dataset.unstage;
    if (!confirm(`Delete the copies of ${name} from the case and read from the archive on demand?\n\n`
      + "The archive must still hold every registered file, or this is refused.")) return;
    const r = await post("/api/source/unstage", { name });
    if (r.error) { toast(r.message || "Refused"); return; }
    toast(`${(r.removed || 0).toLocaleString()} copies removed; ${name} is read from the archive again`);
    try { showSourceStatus((await api("/api/context")).archive_sources); } catch (e) {}
  });
}

/* ---------- Carving section (E01 acquisitions only) ---------- */
function renderCarveSection(list) {
  const sec = document.querySelector('.fsec[data-sec="carve"]');
  const ewf = (list || []).filter(s => s.format === "ewf");
  if (sec) sec.hidden = !ewf.length;
  const el = $("#carveList");
  if (!el) return;
  el.innerHTML = ewf.map(s => {
    const ok = s.status === "ok";
    // /api/context already splits this source's rows by origin; report all three
    // it can carry, so a deleted-record recovery is visible here and not only in
    // the filter.
    const walked = s.walked || 0, cut = s.carved || 0, back = s.recovered || 0;
    return `<div style="margin:5px 0">
      <b title="${esc(s.path)}">${esc(s.name)}</b>
      <span class="muted">· ${walked.toLocaleString()} walked${back ? ` · ${back.toLocaleString()} recovered from deleted records` : ""}${cut ? ` · ${cut.toLocaleString()} carved` : ""}</span>
      <div style="margin-top:2px"><button class="btn sm" data-carve="${esc(s.name)}"${ok ? "" : " disabled"}
        title="Scan the space no volume claims for deleted images and video. A carved file has no name, path or date of its own. Runs the whole free area${cut ? "; offsets already carved are skipped" : ""}.">${cut ? "Carve again" : "Carve for deleted media"}</button></div>
    </div>`;
  }).join("");
  el.querySelectorAll("[data-carve]").forEach(b => b.onclick = async () => {
    const name = b.dataset.carve;
    if (!confirm(`Carve ${name} for deleted media?\n\n`
      + "This scans the whole of the space no volume claims for image and video "
      + "signatures, then processes what it finds. It can take a while on a large "
      + "acquisition. Carved files have no name, path or date of their own.")) return;
    const r = await api("/api/source/carve", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).catch(() => ({ error: true, message: "request failed" }));
    if (r.error) { toast(r.message || "Could not start carving"); return; }
    toast(`Carving ${name} — the bar at the bottom follows it`);
    liveTick = 0; liveJob();
  });
}

function showSourceStatus(list) {
  renderSourcePanel(list);
  renderCarveSection(list);
  const bad = (list || []).filter(s => s.status !== "ok");
  let el = $("#srcBanner");
  if (!bad.length) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "srcBanner";
    el.style.cssText = "margin:6px 12px;padding:8px 12px;border:1px solid #c98a2b;"
      + "border-radius:6px;background:rgba(201,138,43,.12);font-size:13px";
    $("#main").prepend(el);
  }
  el.innerHTML = bad.map(s => {
    const what = s.status === "missing"
      ? "was not found at" : "has a different size or date than the case recorded, at";
    const note = s.mode === "reference"
      ? "Full-size viewing and export need it; thumbnails, hashes and categories still work."
      : "The case holds its own copies, so nothing is lost.";
    return `<div style="margin:2px 0"><b>${esc(s.name)}</b> ${what} <code>${esc(s.path)}</code>. ${note}
      <button data-relink="${esc(s.name)}" style="margin-left:8px">Relink…</button></div>`;
  }).join("");
  el.querySelectorAll("[data-relink]").forEach(b => b.onclick = async () => {
    const name = b.dataset.relink;
    let p = Lr.native ? await pick("archive") : null;
    if (!p) p = prompt(`Where is ${name} now? Full path to the archive:`);
    if (!p) return;
    const r = await api("/api/source/relink", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path: p })
    }).catch(() => ({ error: true, message: "request failed" }));
    if (r.error) { toast(r.message || "Relink refused"); return; }
    toast(`${name} relinked`);
    try { showSourceStatus((await api("/api/context")).archive_sources); } catch (e) {}
  });
}

/* ---------- offline maps ---------- */
// GLEAPP ships no map data and the page requests none from anywhere: the examiner
// imports a basemap file (Maps), the server hands the gallery a style whose every URL
// is local, MapLibre draws it, and the case records which file its maps were drawn on.
let mapProto = false, detailMap = null, viewMap = null, mapUsedFor = "";
function mapsAvailable() { return !!(window.maplibregl && window.pmtiles); }
function mapsInit() {
  if (mapProto || !mapsAvailable()) return;
  maplibregl.addProtocol("pmtiles", new pmtiles.Protocol().tile);
  mapProto = true;
}
async function mapStyle() {
  const s = await api("/api/basemaps/style?flavor=dark").catch(() => null);
  if (!s || s.error) return null;
  // MapLibre validates the sprite and glyph URLs as absolute; the server hands out
  // local paths on purpose, so the page's own origin is prefixed here and nowhere else.
  for (const k of ["sprite", "glyphs"]) {
    if (typeof s[k] === "string" && s[k].startsWith("/")) s[k] = location.origin + s[k];
  }
  return s;
}
async function noteBasemapUsed(style) {
  const key = (style && style.name) || "";
  if (!key || mapUsedFor === key) return;
  mapUsedFor = key;
  try { await save("/api/basemaps/used", {}); } catch (e) {}
}
function makeMap(container, style, opts = {}) {
  mapsInit();
  const m = new maplibregl.Map({ container, style, attributionControl: { compact: false }, ...opts });
  m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  return m;
}
async function renderDetailMap(f) {
  const wrap = $("#detMapWrap"), box = $("#detMap");
  if (detailMap) { detailMap.remove(); detailMap = null; }
  if (!box || !wrap) return;
  if (f.gps_lat == null || !state.basemap || !mapsAvailable()) { wrap.style.display = "none"; return; }
  const style = await mapStyle();
  if (!style) { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  detailMap = makeMap(box, style, { center: [f.gps_lon, f.gps_lat], zoom: 14 });
  new maplibregl.Marker({ color: "#e74c3c" }).setLngLat([f.gps_lon, f.gps_lat]).addTo(detailMap);
  detailMap.on("load", () => noteBasemapUsed(style));
}
// The details-pane map (above) is a fixed 220px preview, same idea as the image
// preview's "View full size": this reopens it centered on the same point in the
// existing full-screen map dialog, just without the multi-file marker layer.
async function openSingleMapView(f) {
  if (f.gps_lat == null) return;
  if (!state.basemap) { toast("Import a basemap first"); openMapsDlg(); return; }
  const style = await mapStyle();
  if (!style) { toast("The active basemap could not be loaded"); return; }
  const nm = (f.orig_name || f.rel_path || f.path || "").split(/[\\/]/).pop() || `file #${f.id}`;
  $("#mapViewInfo").textContent = nm;
  $("#mapView").style.display = "block";
  if (viewMap) { viewMap.remove(); viewMap = null; }
  viewMap = makeMap("mapViewMap", style, { center: [f.gps_lon, f.gps_lat], zoom: 15 });
  viewMap.on("load", () => {
    noteBasemapUsed(style);
    new maplibregl.Marker({ color: "#e74c3c" }).setLngLat([f.gps_lon, f.gps_lat]).addTo(viewMap);
  });
}
async function openMapView() {
  if (!state.basemap) { toast("Import a basemap first"); openMapsDlg(); return; }
  const style = await mapStyle();
  if (!style) { toast("The active basemap could not be loaded"); return; }
  const p = new URLSearchParams(filterParams());
  p.set("has_gps", "1"); p.set("limit", "5000"); p.set("offset", "0");
  const d = await api("/api/files?" + p.toString());
  const feats = (d.files || []).filter(x => x.gps_lat != null).map(x => ({
    type: "Feature", geometry: { type: "Point", coordinates: [x.gps_lon, x.gps_lat] },
    properties: { id: x.id, thumb: x.thumb || "", name: x.orig_name || x.rel_path || "" } }));
  // The points open a thumbnail and a Details button, and the only cue on the map
  // itself was the cursor changing over one, so the header says so.
  $("#mapViewInfo").textContent = `${feats.length.toLocaleString()} geolocated file(s) in the current filter`
    + (d.total > feats.length ? ` (showing the first ${feats.length.toLocaleString()} of ${d.total.toLocaleString()})` : "")
    + (feats.length ? " · click a point for its thumbnail and details" : "");
  $("#mapsDlg").style.display = "none";
  $("#mapView").style.display = "block";
  if (viewMap) { viewMap.remove(); viewMap = null; }
  viewMap = makeMap("mapViewMap", style, { center: [0, 20], zoom: 1 });
  viewMap.on("load", () => {
    noteBasemapUsed(style);
    viewMap.addSource("files", { type: "geojson", data: { type: "FeatureCollection", features: feats } });
    viewMap.addLayer({ id: "files-halo", type: "circle", source: "files",
      paint: { "circle-radius": 9, "circle-color": "#ffffff", "circle-opacity": 0.55 } });
    viewMap.addLayer({ id: "files", type: "circle", source: "files",
      paint: { "circle-radius": 6, "circle-color": "#e74c3c", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1 } });
    if (feats.length) {
      const b = new maplibregl.LngLatBounds(feats[0].geometry.coordinates, feats[0].geometry.coordinates);
      feats.forEach(x => b.extend(x.geometry.coordinates));
      viewMap.fitBounds(b, { padding: 60, maxZoom: 15, duration: 0 });
    }
    viewMap.on("click", "files", e => {
      const pr = e.features[0].properties;
      const html = `${pr.thumb ? `<img src="/thumb/${esc(pr.thumb)}" data-open="${pr.id}">` : ""}`
        + `<div>${esc(pr.name)}</div><button class="btn sm" data-open="${pr.id}">Details</button>`;
      const pop = new maplibregl.Popup({ maxWidth: "220px" })
        .setLngLat(e.features[0].geometry.coordinates).setHTML(html).addTo(viewMap);
      pop.getElement().querySelectorAll("[data-open]").forEach(el => el.onclick = () => {
        closeMapView(); toggleMeta(true); showMeta(+el.dataset.open);
      });
    });
    viewMap.on("mouseenter", "files", () => { viewMap.getCanvas().style.cursor = "pointer"; });
    viewMap.on("mouseleave", "files", () => { viewMap.getCanvas().style.cursor = ""; });
  });
}
function closeMapView() {
  $("#mapView").style.display = "none";
  if (viewMap) { viewMap.remove(); viewMap = null; }
}
async function refreshMapsList() {
  const box = $("#mapsList");
  box.textContent = "Loading…";
  const d = await api("/api/basemaps").catch(() => ({ active: null, basemaps: [] }));
  state.basemap = d.active || null;
  // "Show on map" needs an open case's geolocated files — Maps is otherwise
  // reachable pre-case (from the launcher) to import/manage basemaps only
  $("#mapsShow").disabled = !d.active || $("#launcher").style.display === "block";
  if (!d.basemaps.length) {
    box.innerHTML = `<div class="muted" style="font-size:12px">No basemap imported yet.</div>`;
    return;
  }
  box.innerHTML = d.basemaps.map(b => `<div class="bmrow">
    <input type="radio" name="bmActive" value="${esc(b.name)}" ${b.active ? "checked" : ""} title="Use this basemap">
    <div class="nm"><b>${esc(b.name)}</b> <span class="muted">· ${esc(b.format)} · zoom ${b.min_zoom} to ${b.max_zoom} · ${_snapBytes(b.size)}${b.present ? "" : " · <span style='color:var(--danger)'>file missing</span>"}</span><br><code>sha256 ${esc(b.sha256)}</code>${b.attribution ? `<br><span class="muted">${esc(b.attribution)}</span>` : ""}</div>
    <button class="btn sm" data-rm="${esc(b.name)}">Remove</button></div>`).join("");
  box.querySelectorAll("input[name=bmActive]").forEach(r => r.onchange = async () => {
    await save("/api/basemaps/active", { name: r.value });
    refreshMapsList();
  });
  box.querySelectorAll("[data-rm]").forEach(b => b.onclick = async () => {
    if (!confirm(`Remove basemap ${b.dataset.rm}?\n\nThe imported copy is deleted; your original file is untouched.`)) return;
    const r = await save("/api/basemaps/remove", { name: b.dataset.rm });
    if (r.error) return toast(r.message || "Could not remove");
    refreshMapsList();
  });
}
async function importBasemap() {
  const p = await pick("basemap");
  if (!p) return;
  const r = await save("/api/basemaps/import", { path: p });
  if (r.error) return toast(r.message || "Import refused");
  toast("Importing basemap…");
  liveTick = 0; liveJob();                                    // the bottom bar follows the copy
  const wait = setInterval(async () => {
    const j = await api("/api/job").catch(() => null);
    if (j && !j.running) { clearInterval(wait); refreshMapsList(); }
  }, 800);
}
function openMapsDlg() { $("#mapsDlg").style.display = "block"; refreshMapsList(); }
$("#btnMaps").onclick = openMapsDlg;
$("#mapsClose").onclick = () => $("#mapsDlg").style.display = "none";
$("#mapsDlg").addEventListener("click", e => { if (e.target.id === "mapsDlg") $("#mapsDlg").style.display = "none"; });
$("#mapsImport").onclick = importBasemap;
$("#mapsShow").onclick = openMapView;
$("#mapViewClose").onclick = closeMapView;

/* ---------- boot ---------- */
(async function boot() {
  let c;
  try { c = await api("/api/context"); }
  catch (e) { c = {}; }
  Lr.native = !!c.native;          // so pick() uses the OS file dialog even when
                                   // GLEAPP boots straight into an existing case
  if (c.needs_case) { showLauncher(c); return; }
  $("#launcher").style.display = "none";
  $("#main").style.display = "";
  $("#caseName").textContent = "GLEAPP — " + (c.case || "case");
  document.title = "GLEAPP — " + (c.case || "");
  if (c.vic) $("#btnVic").style.display = "";
  updateScreenInfo(c.screening);
  updateArchInfo(c.archives);
  updateKnownHash(c.known_hash);
  if (c.errors > 0) {
    $("#errCount").textContent = `(${c.errors.toLocaleString()})`;
    $("#btnRetryErr").style.display = "";
    $("#btnRetryErr").textContent = `Retry ${c.errors.toLocaleString()} failed files`;
  }
  state.cats = c.categories || [];
  state.sources = c.sources || [];
  state.basemap = c.basemap || null;
  showSourceStatus(c.archive_sources);
  setupTz(c);
  try { await refreshCats(); } catch (e) {}
  (c.sources || []).forEach(s => $("#fsrc").insertAdjacentHTML("beforeend", `<option>${esc(s)}</option>`));
  try {
    if (localStorage.getItem("gleapp.meta") === "1") toggleMeta(true);
    const ps = localStorage.getItem("gleapp.pagesize");
    if (ps) { state.pageSize = +ps; $("#fpagesize").value = ps; }
    const tl = localStorage.getItem("gleapp.tile");
    if (tl) { $("#ftile").value = tl;
      document.documentElement.style.setProperty("--tile", tl + "px"); }
  } catch (e) {}
  restoreListPrefs();
  reflectGridSort();          // mirror the restored sort onto the grid dropdown
  await load();

  // a processing job is still running (we entered the gallery early) — show the
  // live bottom bar and refresh the grid as thumbnails land
  if (c.job && c.job.running) { liveTick = 0; liveJob(); }

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
