/* svg-bench leaderboard — vanilla JS, no dependencies. Ratings are local-first
   (localStorage) with optional seed from the committed ratings.json. */
'use strict';

const LS_KEY = 'svgbench.ratings.v1';
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const state = {
  data: null,
  runs: [],
  scope: 'available', // 'available' = free models in the latest discovery snapshot
  filters: { task: 'all', status: 'all', provider: 'all', q: '', unrated: false },
  sort: { key: 'model_id', dir: 1 },
  previewTimer: null,
};

/* ───────── ratings store: localStorage first, seed fallback ───────── */
function lsRatings() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}'); } catch { return {}; }
}
function saveLsRatings(obj) { localStorage.setItem(LS_KEY, JSON.stringify(obj)); }
function seedRatings() { return (state.data && state.data.seed_ratings) || {}; }
function runRating(id) {
  const ls = lsRatings()[id];
  if (ls) return { stars: ls.stars || 0, note: ls.note || '', source: 'you' };
  const seed = seedRatings()[id];
  if (seed && seed.stars) return { stars: seed.stars, note: seed.note || '', source: 'seed' };
  return { stars: 0, note: '', source: 'none' };
}
function setStars(id, stars) {
  const ls = lsRatings();
  const prev = ls[id] || {};
  const next = prev.stars === stars ? { ...prev, stars: 0 } : { ...prev, stars };
  next.ts = Date.now();
  if (!next.stars && !(next.note || '').trim()) delete ls[id]; else ls[id] = next;
  saveLsRatings(ls);
}
function setNote(id, note) {
  const ls = lsRatings();
  const next = { ...(ls[id] || {}), note, ts: Date.now() };
  if (!next.stars && !note.trim()) delete ls[id]; else ls[id] = next;
  saveLsRatings(ls);
}
function exportRatings() {
  const payload = { schema: 'svgbench-ratings/1', exported_utc: new Date().toISOString(), ratings: lsRatings() };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'svgbench-ratings.json';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('Ratings exported — commit as svg-bench/ratings.json to seed every visitor');
}
function importRatings(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const parsed = JSON.parse(String(reader.result));
      const incoming = parsed.ratings && typeof parsed.ratings === 'object' ? parsed.ratings : parsed;
      if (!incoming || typeof incoming !== 'object') throw new Error('no ratings object in file');
      const ls = lsRatings();
      let n = 0;
      for (const [id, val] of Object.entries(incoming)) {
        if (val && typeof val === 'object' && val.stars != null) { ls[id] = { stars: +val.stars || 0, note: val.note || '', ts: val.ts || Date.now() }; n++; }
      }
      saveLsRatings(ls);
      toast(`Imported ${n} ratings`);
      render();
    } catch (e) { toast(`Import failed: ${e.message}`, true); }
  };
  reader.readAsText(file);
}
function resetRatings() {
  if (!confirm('Clear all ratings stored in this browser? Seed ratings (committed file) will remain.')) return;
  localStorage.removeItem(LS_KEY);
  toast('Local ratings cleared');
  render();
}

/* ───────── availability: free models from the latest discovery snapshot ───────── */
function availableSet() {
  const list = state.data && state.data.discovery && state.data.discovery.available;
  return Array.isArray(list) && list.length ? new Set(list) : null;
}
function isAvailable(model) {
  const set = availableSet();
  return set ? set.has(model) : true;
}
function scopedRuns() {
  if (state.scope === 'available') {
    const set = availableSet();
    return set ? state.runs.filter(r => set.has(r.model)) : state.runs;
  }
  if (state.scope === 'new') {
    return state.runs.filter(r => isNewModel(r.model));
  }
  return state.runs;
}

function modelFirstSeen(model) {
  const dates = state.runs.filter(r => r.model === model).map(r => r.date_utc).filter(Boolean).sort();
  return dates.length ? dates[0] : null;
}
function hasNewModels() {
  return state.runs.some(r => isNewModel(r.model));
}
function isNewModel(model) {
  const list = state.data && state.data.discovery && state.data.discovery.new;
  if (Array.isArray(list)) return list.indexOf(model) !== -1;
  // fallback: time-based within 7 days
  const first = modelFirstSeen(model);
  if (!first) return false;
  const gen = state.data && state.data.generated_utc;
  const now = gen ? Date.parse(gen) : Date.now();
  const firstMs = Date.parse(first);
  if (isNaN(firstMs) || isNaN(now)) return false;
  const week = 7 * 24 * 60 * 60 * 1000;
  return now - firstMs >= 0 && now - firstMs <= week;
}

/* ───────── matrix: one model per row, one task per artwork column ───────── */
function latestRunByTask(runs) {
  const byTask = new Map();
  for (const r of runs) {
    const prev = byTask.get(r.task);
    if (!prev || String(r.date_utc || '') >= String(prev.date_utc || '')) byTask.set(r.task, r);
  }
  return byTask;
}

function matrixRows() {
  const { task, status, provider, q, unrated } = state.filters;
  const allTasks = (state.data.summary?.tasks?.length)
    ? state.data.summary.tasks
    : [...new Set(state.runs.map(r => r.task))].sort();
  const tasks = task === 'all' ? allTasks : allTasks.filter(t => t === task);

  const byModel = new Map();
  for (const r of scopedRuns()) {
    if (provider !== 'all' && r.provider !== provider) continue;
    if (q && !r.model.toLowerCase().includes(q)) continue;
    if (!byModel.has(r.model)) {
      byModel.set(r.model, { model: r.model, model_id: r.model_id, provider: r.provider, runs: [] });
    }
    byModel.get(r.model).runs.push(r);
  }

  const rows = [];
  for (const m of byModel.values()) {
    const latest = latestRunByTask(m.runs);
    const cells = new Map();
    for (const t of tasks) {
      const r = latest.get(t);
      if (!r) continue;
      if (status !== 'all' && r.result_status !== status) continue;
      if (unrated && runRating(r.id).stars > 0) continue;
      cells.set(t, r);
    }
    if (!cells.size) continue;
    const present = [...cells.values()];
    const ok = present.filter(r => r.result_status === 'ok').length;
    const rated = present.map(r => runRating(r.id).stars).filter(s => s > 0);
    rows.push({
      ...m,
      cells,
      ok,
      total: present.length,
      avgRating: rated.length ? rated.reduce((a, b) => a + b, 0) / rated.length : 0,
      rated: rated.length,
      effort: [...new Set(present.map(r => r.effort).filter(Boolean))].join(', ') || '—',
      status: ok === present.length ? 'ok' : ok > 0 ? 'mixed' : (present[0].result_status || 'no_output'),
    });
  }

  const { key, dir } = state.sort;
  const val = m => {
    switch (key) {
      case 'provider': return m.provider || '';
      case 'rating': return m.avgRating;
      case 'valid': return m.total ? m.ok / m.total : 0;
      default: return m.model_id || '';
    }
  };
  rows.sort((a, b) => {
    const va = val(a), vb = val(b);
    const cmp = typeof va === 'string' ? va.localeCompare(vb) : va - vb;
    return cmp !== 0 ? cmp * dir : a.model_id.localeCompare(b.model_id);
  });
  return { tasks, rows };
}

/* ───────── stars ───────── */
function starsHtml(id) {
  const { stars, source } = runRating(id);
  const hint = source === 'seed' ? ' title="Seeded from ratings.json — click to make it yours"' : '';
  return `<span class="stars" data-id="${esc(id)}"${hint}>${[1, 2, 3, 4, 5].map(n =>
    `<button type="button" class="star${n <= stars ? ' on' : ''}" data-star="${n}" aria-label="Rate ${n} of 5">★</button>`
  ).join('')}</span><button type="button" class="note-btn${(runRating(id).note || '').trim() ? ' has' : ''}" aria-label="Toggle note">✎</button>`;
}

/* ───────── matrix rendering ───────── */
function taskClass(task) {
  return task.includes('horse') ? 'horse' : 'sloth';
}

function matrixHead(tasks) {
  return `<tr>
    <th class="matrix-model-head" data-sort="model_id">Model</th>
    ${tasks.map(t => {
      const meta = (state.data.tasks && state.data.tasks[t]) || {};
      const blurb = meta.blurb ? ` title="${esc(meta.blurb)}"` : '';
      return `<th class="matrix-task-head"${blurb}>
        <span class="task-chip task-${taskClass(t)}">${esc(t)}</span>
        ${meta.blurb ? `<span class="task-blurb">${esc(meta.blurb)}</span>` : ''}
      </th>`;
    }).join('')}
  </tr>`;
}

function matrixCell(r, task) {
  if (!r) return `<td class="matrix-cell is-empty"><span class="cell-none">—</span></td>`;
  const status = r.result_status || 'unknown';
  const art = r.svg_rel
    ? `<img class="thumb matrix-thumb" loading="lazy" src="${esc(r.svg_rel)}" alt="${esc(r.model_id)} — ${esc(task)}" data-id="${esc(r.id)}">`
    : `<div class="no-art matrix-no-art"><span>✕</span><span>${esc(status.replace('_', ' '))}</span></div>`;
  return `<td class="matrix-cell" data-id="${esc(r.id)}" title="${esc(status)} — ${esc(r.model_id)} on ${esc(task)}">
    <div class="cell-card">
      ${art}
      <div class="cell-meta">
        <span class="cell-date">${esc((r.date_utc || '').slice(0, 10))}</span>
        ${starsHtml(r.id)}
        ${r.effort_fallback ? `<span class="fallback-note" title="Provider rejected #high; retried with default">↺</span>` : ''}
        <button type="button" class="rerun-row" data-id="${esc(r.id)}" title="Re-run ${esc(r.model_id)} on ${esc(task)} (forced)" aria-label="Re-run this model on this task">↻</button>
      </div>
      ${status !== 'ok' ? `<span class="cell-status pill st-${esc(status)}">${esc(status.replace('_', ' '))}</span>` : ''}
      <div class="cell-note" hidden><textarea placeholder="Notes for this run… (saved locally as you type)">${esc(runRating(r.id).note)}</textarea></div>
    </div>
  </td>`;
}

function matrixRow(m, tasks) {
  const dot = m.status === 'mixed' ? 'mixed' : m.status;
  const avail = isAvailable(m.model);
  return `<tr data-model="${esc(m.model)}">
    <th scope="row" class="matrix-model">
      <div class="model-name"><span class="status-dot st-${esc(dot)}"></span>${esc(m.model_id)} ${isNewModel(m.model) ? '<span class="badge new" title="First seen within the last 7 days">NEW</span>' : ''}</div>
      <div class="model-id" title="${esc(m.model)}">${esc(m.model)}</div>
      <div class="model-sub">
        <span class="tag" title="Provider / agent that served the model"><span class="tag-key">agent</span>${esc(m.provider)}</span>
        <span class="tag" title="Effort variant requested (#high or default)"><span class="tag-key">effort</span>${esc(m.effort)}</span>
        <span class="tag" title="Visible runs that produced a valid SVG"><span class="tag-key">valid</span>${m.ok}/${m.total}</span>
        ${avail ? '' : '<span class="badge historic" title="Not in the current free-model discovery snapshot — paid, delisted or region-blocked">not available</span>'}
      </div>
    </th>
    ${tasks.map(t => matrixCell(m.cells.get(t), t)).join('')}
  </tr>`;
}

function renderMatrix() {
  const { tasks, rows } = matrixRows();
  $('#matrix-head').innerHTML = matrixHead(tasks);
  $('#runs-matrix tbody').innerHTML = rows.map(m => matrixRow(m, tasks)).join('');
  const scope = state.scope === 'available' ? 'free &amp; available'
    : state.scope === 'new' ? 'new' : 'ever tested';
  $('#shown-count').innerHTML = `${rows.length} <b>${scope}</b> model${rows.length === 1 ? '' : 's'} × ${tasks.length} task${tasks.length === 1 ? '' : 's'}`;
  $('#empty').hidden = rows.length !== 0;
  $$('#matrix-head th[data-sort]').forEach(th => {
    th.classList.toggle('sorted-asc', state.sort.key === th.dataset.sort && state.sort.dir === 1);
    th.classList.toggle('sorted-desc', state.sort.key === th.dataset.sort && state.sort.dir === -1);
  });
}

/* ───────── preview panel ───────── */
function showPreview(r) {
  clearTimeout(state.previewTimer);
  const panel = $('#preview');
  panel.hidden = false;
  const pill = $('#preview-status');
  pill.className = `pill st-${r.result_status || 'unknown'}`;
  pill.textContent = (r.result_status || 'unknown').replace('_', ' ');
  $('#preview-name').textContent = r.model_id;
  $('#preview-name').title = r.model;

  const img = $('#preview-img');
  const empty = $('#preview-empty');
  if (r.svg_rel) {
    img.style.display = '';
    empty.style.display = 'none';
    img.src = r.svg_rel;
    img.alt = `${r.model_id} — ${r.task}`;
  } else {
    img.style.display = 'none';
    empty.style.display = '';
    empty.innerHTML = `<div class="none-status">${esc((r.result_status || 'no output').replace('_', ' '))}</div>
      <div class="none-msg">${esc(r.error_message || 'The model produced no usable SVG file.')}</div>`;
  }
  $('#preview-chips').innerHTML = '';
  $('#preview-chips').hidden = true;
  const openHtml = r.svg_rel ? `<a id="preview-open" class="btn small" href="${esc(r.svg_rel)}" target="_blank" rel="noopener">Open raw SVG ↗</a>` : '';
  $('#preview-rating').innerHTML = `<span class="pchip">${esc(r.task)}</span>` + starsHtml(r.id) + `<span class="cell-date">${esc((r.date_utc || '').slice(0, 10))}</span>` + openHtml;
  const noteBox = $('#preview-note');
  const noteTa = noteBox.querySelector('textarea');
  noteTa.value = runRating(r.id).note || '';
  noteBox.hidden = true;
}

/* ───────── data loading with 10s polling ───────── */
async function fetchData() {
  try {
    const res = await fetch(`data.json?ts=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (e) {
    if (window.__SVG_BENCH__) return window.__SVG_BENCH__;
    throw e;
  }
}
function markUpdated(iso) {
  const el = $('#foot-updated');
  if (el && iso) el.textContent = 'updated ' + iso.slice(11, 19) + 'Z';
}
async function poll() {
  try {
    const res = await fetch(`data.json?ts=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) return;
    const d = await res.json();
    const sig = JSON.stringify(d);
    if (sig === state.lastSig) return;
    state.lastSig = sig;
    const typing = $$('.cell-note textarea').some(t => t === document.activeElement);
    state.data = d;
    state.runs = d.runs || [];
    markUpdated(d.generated_utc);
    if (typing) { renderChips(); toast('New benchmark results recorded — press F5 to refresh', false, 5000); }
    else { render(); toast('Leaderboard updated'); }
  } catch { /* file:// or transient — stay quiet; embedded data is still shown */ }
}

/* ───────── chips & tabs ───────── */
function renderChips() {
  const s = state.data.summary;
  const d = (state.data.generated_utc || '').slice(0, 10);
  const avail = availableSet()?.size;
  $('#chips').innerHTML = `
    <span class="chip"><b>${s.ok}</b>/${s.runs} valid</span>
    ${avail != null ? `<span class="chip"><b>${avail}</b> free now</span>` : ''}
    <span class="chip"><b>${s.models}</b> tested</span>
    <span class="chip">tasks: <b>${esc(s.tasks.join(' · '))}</b></span>`;
  $('#foot-generated').textContent = `generated ${d} · opencode v${esc(state.runs[0]?.opencode_version || '?')}`;
}

function syncNewTab() {
  const btn = document.querySelector('.tab[data-scope="new"]');
  if (!btn) return;
  btn.hidden = !hasNewModels();
  if (btn.hidden && state.scope === 'new') setScope('available');
}
function setScope(scope) {
  state.scope = scope;
  $$('.tab').forEach(t => {
    const on = t.dataset.scope === scope;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  renderMatrix();
  const visible = scopedRuns();
  const first = visible.find(r => r.result_status === 'ok' && r.svg_rel) || visible[0];
  if (first) showPreview(first);
  else $('#preview').hidden = true;
}

/* ───────── toast ───────── */
let toastTimer = null;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.style.borderColor = isError ? 'var(--red)' : 'var(--orange-2)';
  el.style.color = isError ? 'var(--red)' : 'var(--amber)';
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 3400);
}

/* ───────── benchmark re-runs via the local server ───────── */
const runbar = { running: false };
const isBusy = () => document.body.classList.contains('bench-running');

async function apiPost(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error page */ }
  return { ok: res.ok, status: res.status, data };
}
async function apiGet(path) {
  try {
    const res = await fetch(path, { cache: 'no-store' });
    return res.ok ? await res.json() : null;
  } catch { return null; }
}
const pairKey = ([m, t]) => `${m} × ${t}`;
function uniqPairs(pairs) {
  const seen = new Set();
  return pairs.filter(([m, t]) => {
    const k = `${m}\u0000${t}`;
    if (!m || !t || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
const failedPairs = () => uniqPairs(scopedRuns().filter(r => r.result_status !== 'ok').map(r => [r.model, r.task]));
const allPairs = () => uniqPairs(scopedRuns().map(r => [r.model, r.task]));

async function launchRerun(pairs, label) {
  pairs = pairs.filter(([m, t]) => m && t);
  if (!pairs.length) { toast('Nothing to re-run'); return; }
  if (isBusy()) { toast('A benchmark run is already active — wait for it to finish', true); return; }
  const preview = pairs.slice(0, 8).map(pairKey).join('\n');
  const more = pairs.length > 8 ? `\n… and ${pairs.length - 8} more` : '';
  const msg = `${label}\n\n${pairs.length} pair(s), forced (--force):\n\n${preview}${more}\n\nThis launches the full agentic harness — it can take a long time and overwrites today's artifacts. Continue?`;
  if (!confirm(msg)) return;
  const { ok, status, data } = await apiPost('/api/run', { pairs });
  if (!ok) { toast(`Re-run refused (${status}): ${data?.error || 'unknown error'}`, true); return; }
  toast(`Re-run started — ${pairs.length} pair(s)`);
  document.body.classList.add('bench-running');
  $('#runbar').hidden = false;
  pollRunState();
}

async function pollRunState() {
  const el = $('#runbar');
  const st = await apiGet('/api/run');
  if (!st) return; // file:// or server offline — stay silent
  if (st.state === 'running') {
    runbar.running = true;
    document.body.classList.add('bench-running');
    const done = st.done ?? 0;
    const total = st.total ?? st.pairs?.length ?? '?';
    const elapsed = st.elapsed_seconds ?? Math.floor((Date.now() - Date.parse(st.started_utc)) / 1000);
    $('#runbar-text').textContent = `benchmark running · ${done}/${total} pairs done · ${Math.floor(elapsed / 60)}m${String(Math.max(0, elapsed) % 60).padStart(2, '0')}s`;
    el.hidden = false;
  } else {
    const wasRunning = runbar.running;
    runbar.running = false;
    document.body.classList.remove('bench-running');
    el.hidden = true;
    if (wasRunning && st.state === 'finished') toast('Benchmark re-run finished — results updated');
  }
}

function initRerunUI() {
  $('#btn-rerun-failed').addEventListener('click', () => launchRerun(failedPairs(), 'Re-run failed pairs'));
  $('#btn-rerun-all').addEventListener('click', () => launchRerun(allPairs(), 'Re-run EVERYTHING'));
  // per-cell ↻ buttons (delegated alongside stars/notes)
  $('#runs-matrix').addEventListener('click', e => {
    const btn = e.target.closest('.rerun-row');
    if (!btn) return;
    e.stopPropagation();
    const run = state.runs.find(r => r.id === btn.dataset.id);
    if (run) launchRerun([[run.model, run.task]], `Re-run ${run.model_id}`);
  }, true); // capture phase so it wins over the row's click-to-preview
  pollRunState();
  setInterval(pollRunState, 3000);
}

/* ───────── events ───────── */
function bindEvents() {
  $('#f-task').addEventListener('change', e => { state.filters.task = e.target.value; renderMatrix(); });
  $('#f-status').addEventListener('change', e => { state.filters.status = e.target.value; renderMatrix(); });
  $('#f-provider').addEventListener('change', e => { state.filters.provider = e.target.value; renderMatrix(); });
  $('#f-search').addEventListener('input', e => { state.filters.q = e.target.value.trim().toLowerCase(); renderMatrix(); });
  $('#f-unrated').addEventListener('change', e => { state.filters.unrated = e.target.checked; renderMatrix(); });

  // sortable model header (re-rendered each time, so delegate)
  $('#runs-matrix').addEventListener('click', e => {
    const th = e.target.closest('#matrix-head th[data-sort]');
    if (!th) return;
    const key = th.dataset.sort;
    state.sort = state.sort.key === key ? { key, dir: -state.sort.dir } : { key, dir: 1 };
    renderMatrix();
  });

  // stars + notes + artwork preview (delegated)
  $('#runs-matrix').addEventListener('click', e => {
    const starBtn = e.target.closest('.star');
    if (starBtn) {
      const wrap = starBtn.closest('.stars');
      setStars(wrap.dataset.id, +starBtn.dataset.star);
      renderMatrix();
      return;
    }
    const noteBtn = e.target.closest('.note-btn');
    if (noteBtn) {
      const cell = noteBtn.closest('.matrix-cell');
      const note = cell && cell.querySelector('.cell-note');
      if (note) {
        note.hidden = !note.hidden;
        if (!note.hidden) note.querySelector('textarea').focus();
      }
      return;
    }
    const thumb = e.target.closest('.matrix-thumb');
    if (thumb) { window.open(thumb.src, '_blank', 'noopener'); return; }
    const cell = e.target.closest('td.matrix-cell[data-id]');
    if (cell) showPreview(state.runs.find(r => r.id === cell.dataset.id));
  });
  $('#runs-matrix').addEventListener('input', e => {
    const ta = e.target.closest('.cell-note textarea');
    if (!ta) return;
    const id = ta.closest('td.matrix-cell').dataset.id;
    clearTimeout(ta._saveTimer);
    ta._saveTimer = setTimeout(() => setNote(id, ta.value), 350);
  });
  const cellEnter = e => {
    const img = e.target.closest?.('.matrix-thumb, .matrix-no-art');
    if (!img) return;
    const cell = img.closest('td.matrix-cell[data-id]');
    if (cell) showPreview(state.runs.find(r => r.id === cell.dataset.id));
  };
  $('#runs-matrix').addEventListener('mouseover', cellEnter);
  $('#runs-matrix').addEventListener('focusin', cellEnter);

  // preview rating + note (delegated)
  $('#preview').addEventListener('click', e => {
    const starBtn = e.target.closest('.star');
    if (starBtn) {
      const wrap = starBtn.closest('.stars');
      const id = wrap.dataset.id;
      setStars(id, +starBtn.dataset.star);
      $('#preview-rating').innerHTML = starsHtml(id);
      renderMatrix();
      return;
    }
    const noteBtn = e.target.closest('.note-btn');
    if (noteBtn) {
      const note = $('#preview-note');
      note.hidden = !note.hidden;
      if (!note.hidden) note.querySelector('textarea').focus();
      return;
    }
  });
  $('#preview').addEventListener('input', e => {
    const ta = e.target.closest('.preview-note textarea');
    if (!ta) return;
    const id = $('#preview-rating .stars')?.dataset.id;
    if (!id) return;
    clearTimeout(ta._saveTimer);
    ta._saveTimer = setTimeout(() => {
      setNote(id, ta.value);
      $('#preview-rating').innerHTML = starsHtml(id);
    }, 350);
  });

  $$('.tab').forEach(t => t.addEventListener('click', () => setScope(t.dataset.scope)));
  $('#btn-export').addEventListener('click', exportRatings);
  $('#btn-import').addEventListener('click', () => $('#import-file').click());
  $('#import-file').addEventListener('change', e => { const f = e.target.files[0]; if (f) importRatings(f); e.target.value = ''; });
  $('#btn-reset').addEventListener('click', resetRatings);
}

/* ───────── boot ───────── */
function render() {
  renderChips();
  syncNewTab();
  renderMatrix();
}

async function boot() {
  let data;
  try {
    data = await fetchData();
  } catch (e) {
    document.body.innerHTML = `<div class="prose" style="padding:40px"><h2>Couldn't load the benchmark data</h2>
      <p>${esc(e.message)}. Regenerate it and serve the page with one command:</p>
      <pre>cd svg-bench &amp;&amp; ./run.sh result</pre>
      <p>then open <code>http://127.0.0.1:8000/site/</code>.</p></div>`;
    return;
  }
  state.data = data;
  state.runs = data.runs || [];
  state.lastSig = JSON.stringify(data);

  const tasks = [...new Set(state.runs.map(r => r.task))].sort();
  $('#f-task').innerHTML = '<option value="all">All tasks</option>' + tasks.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  const providers = [...new Set(state.runs.map(r => r.provider))].sort();
  $('#f-provider').innerHTML = '<option value="all">All providers</option>' + providers.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');

  renderChips();
  bindEvents();
  initRerunUI();
  render();
  markUpdated(data.generated_utc);
  const visible = scopedRuns();
  const first = visible.find(r => r.result_status === 'ok' && r.svg_rel) || visible[0];
  if (first) showPreview(first);
  setInterval(poll, 10_000);
}

boot();
