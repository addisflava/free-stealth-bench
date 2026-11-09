/* svg-bench leaderboard — vanilla JS, no build step, GitHub Pages friendly.
   Private ratings are local-first (localStorage) with an optional committed seed.
   Public reviews are read from and written to a Supabase table configured in
   config.js; if no backend is configured the page still works offline. */
'use strict';

const LS_KEY = 'svgbench.ratings.v1';
const LS_REVIEWER = 'svgbench.reviewer.v1';
const LS_ADMIN = 'svgbench.admin.v1';
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ───────── automated judge data (embedded in data.json by leaderboard.py) ───────── */
const DIM_LABELS = {
  output_quality: 'Quality', instruction_following: 'Instructions',
  creativity: 'Creativity', animation_fidelity: 'Motion',
  richness_detail: 'Detail', composition_aesthetics: 'Aesthetics',
  technical_validity: 'Technical',
};
const judgeData = () => (state.data && state.data.judge) || null;
function judgeFor(runId) {
  const j = judgeData();
  return j && j.by_run ? j.by_run[runId] || null : null;
}
function rankFor(model) {
  const j = judgeData();
  return j && Array.isArray(j.ranking) ? j.ranking.find(r => r.model === model) || null : null;
}
const dimLabel = k => DIM_LABELS[k] || k;
const shortModel = m => String(m || '').split('/').pop() || m || '—';

const state = {
  data: null,
  runs: [],
  scope: 'available', // 'available' = free models in the latest discovery snapshot
  view: 'runs',       // 'runs' = artwork matrix, 'ranking' = automated judge ranking
  filters: { task: 'all', status: 'all', provider: 'all', q: '', unrated: false },
  sort: { key: 'judge', dir: -1 }, // default: automated judge score, highest first
  previewTimer: null,
  previewRun: null,
  previewRunId: null,
  seedReviews: [],
  reviews: [],
  reviewsByRun: new Map(),
  reviewsSig: '',
  reviewsFetchedAt: 0,
  localServer: false,
  admin: false,
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
  const payload = {
    schema: 'svgbench-ratings/1',
    exported_utc: new Date().toISOString(),
    ratings: lsRatings(),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'svgbench-ratings.json';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('Ratings exported — keep them private or commit as svg-bench/ratings.json to seed everyone');
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
        if (val && typeof val === 'object' && val.stars != null) {
          ls[id] = { stars: +val.stars || 0, note: val.note || '', ts: val.ts || Date.now() };
          n++;
        }
      }
      saveLsRatings(ls);
      toast(`Imported ${n} ratings`);
      render();
    } catch (e) { toast(`Import failed: ${e.message}`, true); }
  };
  reader.readAsText(file);
}
function resetRatings() {
  if (!confirm('Clear all private ratings stored in this browser? Public reviews stay online.')) return;
  localStorage.removeItem(LS_KEY);
  toast('Local ratings cleared');
  render();
}

/* ───────── public review backend (Supabase REST) ───────── */
function benchConfig() { return (window.SVG_BENCH_CONFIG || {}); }
function supabaseCfg() {
  const s = benchConfig().supabase || {};
  if (!s.url || !s.anonKey) return null;
  return {
    url: String(s.url).replace(/\/+$/, ''),
    anonKey: String(s.anonKey),
    table: String(s.table || 'svgbench_reviews'),
  };
}
function normalizeReview(r) {
  if (!r || typeof r !== 'object') return null;
  const stars = Math.max(1, Math.min(5, Math.round(+r.stars || 0)));
  if (!r.run_id || !stars) return null;
  return {
    id: String(r.id ?? `${r.run_id}:${r.created_at || ''}:${r.reviewer || ''}`),
    run_id: String(r.run_id),
    model: r.model ? String(r.model) : '',
    task: r.task ? String(r.task) : '',
    stars,
    note: String(r.note || '').slice(0, 2000),
    reviewer: String(r.reviewer || 'anonymous').slice(0, 60),
    created_at: r.created_at || null,
    source: 'public',
  };
}
function indexReviews() {
  const byRun = new Map();
  for (const r of state.reviews) {
    if (!byRun.has(r.run_id)) byRun.set(r.run_id, []);
    byRun.get(r.run_id).push(r);
  }
  for (const list of byRun.values()) {
    list.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
  }
  state.reviewsByRun = byRun;
}
function mergeReviews(...groups) {
  const byId = new Map();
  for (const group of groups) for (const r of group) byId.set(r.id, r);
  return [...byId.values()].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
}
function communityReviews(runId) { return state.reviewsByRun.get(runId) || []; }
function reviewStats(runIds) {
  const reviews = runIds.flatMap(id => communityReviews(id));
  if (reviews.length) {
    return {
      avg: reviews.reduce((a, r) => a + r.stars, 0) / reviews.length,
      count: reviews.length,
      source: 'public',
    };
  }
  const local = runIds.map(runRating).filter(r => r.stars > 0);
  if (local.length) {
    return {
      avg: local.reduce((a, r) => a + r.stars, 0) / local.length,
      count: local.length,
      source: 'local',
    };
  }
  return { avg: 0, count: 0, source: 'none' };
}
function reviewStatsLabel(stats) {
  if (!stats.count) return 'no reviews yet';
  return stats.source === 'public'
    ? `${stats.count} public review${stats.count === 1 ? '' : 's'}`
    : `${stats.count} private rating${stats.count === 1 ? '' : 's'}`;
}
async function fetchPublicReviews() {
  const cfg = supabaseCfg();
  if (!cfg) return [];
  const params = new URLSearchParams({
    select: 'id,run_id,model,task,stars,note,reviewer,created_at',
    revoked: 'eq.false',
    order: 'created_at.desc',
    limit: '1000',
  });
  const res = await fetch(`${cfg.url}/rest/v1/${encodeURIComponent(cfg.table)}?${params}`, {
    headers: { apikey: cfg.anonKey, Authorization: `Bearer ${cfg.anonKey}` },
    cache: 'no-store',
  });
  if (!res.ok) throw new Error(`Supabase HTTP ${res.status}`);
  const rows = await res.json();
  return (Array.isArray(rows) ? rows : []).map(normalizeReview).filter(Boolean);
}
async function refreshPublicReviews({ quiet = true } = {}) {
  state.reviewsFetchedAt = Date.now();
  let remote = [];
  try {
    remote = await fetchPublicReviews();
  } catch (e) {
    if (!quiet) toast(`Could not load public reviews: ${e.message}`, true);
  }
  const merged = mergeReviews(state.seedReviews, remote);
  const sig = JSON.stringify(merged);
  if (sig === state.reviewsSig) return;
  state.reviews = merged;
  state.reviewsSig = sig;
  indexReviews();
  if (!quiet) toast('Public reviews refreshed');
  const typing = $$('.cell-note textarea, #review-form textarea, .preview-note textarea').some(t => t === document.activeElement);
  if (!typing) {
    if (state.previewRun) renderReviews(state.previewRun);
    renderMatrix();
    renderChips();
  }
}
async function publishReview(run, reviewer, stars, note) {
  const cfg = supabaseCfg();
  if (!cfg) throw new Error('public reviews are not configured for this deployment');
  const res = await fetch(`${cfg.url}/rest/v1/${encodeURIComponent(cfg.table)}`, {
    method: 'POST',
    headers: {
      apikey: cfg.anonKey,
      Authorization: `Bearer ${cfg.anonKey}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: JSON.stringify({
      run_id: run.id,
      model: run.model,
      task: run.task,
      stars,
      note,
      reviewer: reviewer || 'anonymous',
    }),
  });
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.text()).slice(0, 240); } catch { /* ignore */ }
    throw new Error(`HTTP ${res.status}${detail ? ` — ${detail}` : ''}`);
  }
  const rows = await res.json().catch(() => []);
  return (Array.isArray(rows) ? rows : []).map(normalizeReview).filter(Boolean)[0] || null;
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
  if (state.scope === 'new') return state.runs.filter(r => isNewModel(r.model));
  return state.runs;
}

function modelFirstSeen(model) {
  const dates = state.runs.filter(r => r.model === model).map(r => r.date_utc).filter(Boolean).sort();
  return dates.length ? dates[0] : null;
}
function hasNewModels() { return state.runs.some(r => isNewModel(r.model)); }
function isNewModel(model) {
  const list = state.data && state.data.discovery && state.data.discovery.new;
  if (Array.isArray(list)) return list.indexOf(model) !== -1;
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
      if (unrated && reviewStats([r.id]).count > 0) continue;
      cells.set(t, r);
    }
    if (!cells.size) continue;
    const present = [...cells.values()];
    const ok = present.filter(r => r.result_status === 'ok').length;
    const stats = reviewStats(present.map(r => r.id));
    const taskResults = tasks.map(t => ({ task: t, run: latest.get(t) || null })).filter(x => x.run);
    const rank = rankFor(m.model);
    rows.push({
      ...m,
      latest,
      cells,
      taskResults,
      judgeScore: rank ? rank.mean_composite : null,
      judgeRank: rank ? rank.rank : null,
      judgeDnf: rank ? rank.dnf : 0,
      ok,
      total: present.length,
      avgRating: stats.avg,
      rated: stats.count,
      reviewSource: stats.source,
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
      case 'judge': return m.judgeScore ?? -1;
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
  const hint = source === 'seed' ? ' title="Seeded from ratings.json — click to make it yours"' : ' title="Your private rating (saved in this browser)"';
  return `<span class="stars" data-id="${esc(id)}"${hint}>${[1, 2, 3, 4, 5].map(n =>
    `<button type="button" class="star${n <= stars ? ' on' : ''}" data-star="${n}" aria-label="Rate ${n} of 5">★</button>`
  ).join('')}</span><button type="button" class="note-btn${(runRating(id).note || '').trim() ? ' has' : ''}" aria-label="Toggle private note">✎</button>`;
}

/* ───────── matrix rendering ───────── */
function taskClass(task) { return task.includes('horse') ? 'horse' : 'sloth'; }
function taskIcon(status) {
  return ({ ok: '✓', invalid_svg: '!', timeout: '⏱', no_output: '✕' })[status] || '?';
}

function matrixHead(tasks) {
  return `<tr>
    <th class="matrix-model-head" data-sort="model_id">Model &amp; benchmark result</th>
    <th class="matrix-score-head num" data-sort="judge" title="Automated blind LLM judge rank (0–100). Click to sort, highest first.">Rank</th>
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

function cellReviewBadge(r) {
  const s = reviewStats([r.id]);
  if (!s.count) return '';
  return `<span class="cell-reviews" title="${esc(reviewStatsLabel(s))}">★ ${s.avg.toFixed(1)}<span>(${s.count})</span></span>`;
}

function matrixCell(r, task) {
  if (!r) return `<td class="matrix-cell is-empty"><span class="cell-none">—</span></td>`;
  const status = r.result_status || 'unknown';
  const art = r.svg_rel
    ? `<img class="thumb matrix-thumb" loading="lazy" src="${esc(r.svg_rel)}" alt="${esc(r.model_id)} — ${esc(task)}" data-id="${esc(r.id)}">`
    : `<div class="no-art matrix-no-art"><span>✕</span><span>${esc(status.replace('_', ' '))}</span></div>`;
  const adminRerun = state.admin
    ? `<button type="button" class="rerun-row" data-id="${esc(r.id)}" title="Admin: re-run ${esc(r.model_id)} on ${esc(task)} (forced)" aria-label="Re-run this model on this task">↻</button>`
    : '';
  const jr = judgeFor(r.id);
  const judgeChip = jr
    ? `<span class="cell-judge" title="Automated judge score ${Number(jr.composite || 0).toFixed(1)} / 100${jr.dnf ? ' — did not finish' : ''}">◆ ${Number(jr.composite || 0).toFixed(1)}</span>`
    : '';
  return `<td class="matrix-cell" data-id="${esc(r.id)}" title="${esc(status)} — ${esc(r.model_id)} on ${esc(task)}">
    <div class="cell-card">
      ${art}
      <div class="cell-meta">
        <span class="cell-date">${esc((r.date_utc || '').slice(0, 10))}</span>
        ${judgeChip}
        ${cellReviewBadge(r)}
        ${starsHtml(r.id)}
        ${r.effort_fallback ? `<span class="fallback-note" title="Provider rejected #high; retried with default">↺</span>` : ''}
        <button type="button" class="run-output cell-run" data-id="${esc(r.id)}" title="Run this SVG output (restarts the animation)" aria-label="Run this output"${r.svg_rel ? '' : ' hidden'}>▶</button>
        ${adminRerun}
      </div>
      ${status !== 'ok' ? `<span class="cell-status pill st-${esc(status)}">${esc(status.replace('_', ' '))}</span>` : ''}
      <div class="cell-note" hidden><textarea placeholder="Your private notes for this run… (saved in this browser)">${esc(runRating(r.id).note)}</textarea></div>
    </div>
  </td>`;
}

function nameCell(m, tasks) {
  const dot = m.status === 'mixed' ? 'mixed' : m.status;
  const avail = isAvailable(m.model);
  const chips = m.taskResults.map(({ task, run }) => {
    const st = run.result_status || 'unknown';
    const s = reviewStats([run.id]);
    const reviews = s.count ? ` · ★${s.avg.toFixed(1)}` : '';
    return `<span class="task-result st-${esc(st)}" title="${esc(task)}: ${esc(st)}${reviews}">${esc(task)} ${taskIcon(st)}${reviews}</span>`;
  }).join('');
  return `<th scope="row" class="matrix-model">
    <div class="model-name">
      <span class="status-dot st-${esc(dot)}"></span>${esc(m.model_id)}
      ${isNewModel(m.model) ? '<span class="badge new" title="First seen within the last 7 days">NEW</span>' : ''}
      <span class="effort-chip" title="Effort variant used"><span class="tag-key">effort</span>${esc(m.effort)}</span>
    </div>
    <div class="model-id" title="${esc(m.model)}">${esc(m.model)}</div>
    <div class="task-results">${chips}</div>
    <div class="model-sub">
      <span class="tag" title="Provider / agent that served the model"><span class="tag-key">agent</span>${esc(m.provider)}</span>
      ${avail ? '' : '<span class="badge historic" title="Not in the current free-model discovery snapshot — paid, delisted or region-blocked">not available</span>'}
    </div>
  </th>`;
}

function matrixScoreCell(m) {
  const rank = rankFor(m.model);
  const avg = m.avgRating ? m.avgRating.toFixed(1) : '—';
  const filled = m.rated ? Math.round(m.avgRating) : 0;
  const reviewBadge = m.rated
    ? `<div class="rating-summary" title="${esc(reviewStatsLabel({ count: m.rated, source: m.reviewSource }))}">
         <span class="rating-stars">${[1, 2, 3, 4, 5].map(n => `<span class="${n <= filled ? 'on' : ''}">★</span>`).join('')}</span>
         <b class="rating-avg">${avg}</b>
         <span class="rating-meta">${m.rated} review${m.rated === 1 ? '' : 's'}</span>
       </div>`
    : `<div class="rating-summary is-empty" title="No reviews yet">
         <span class="rating-stars">☆☆☆☆☆</span>
         <b class="rating-avg">—</b>
         <span class="rating-meta">no reviews</span>
       </div>`;
  const judgeBadge = rank
    ? `<span class="badge rank" title="Automated judge rank #${rank.rank} · mean score ${Number(rank.mean_composite).toFixed(1)} / 100">#${rank.rank} · ${Number(rank.mean_composite).toFixed(1)}</span>`
    : `<span class="badge rank is-empty" title="No automated judge score yet">score —</span>`;
  if (m.judgeScore == null) return `<td class="matrix-score num is-empty" title="Not judged yet">—</td>`;
  return `<td class="matrix-score num" title="Rank #${rank.rank} · mean score ${Number(m.judgeScore).toFixed(1)} / 100">
    <div class="score-stack">
      ${reviewBadge}
      ${judgeBadge}
      ${m.judgeDnf ? `<span class="matrix-score-dnf" title="${m.judgeDnf} did not finish">${m.judgeDnf} DNF</span>` : ''}
    </div>
  </td>`;
}

function matrixRow(m, tasks) {
  return `<tr data-model="${esc(m.model)}">${nameCell(m, tasks)}${matrixScoreCell(m)}${tasks.map(t => matrixCell(m.cells.get(t), t)).join('')}</tr>`;
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
function starsReadonly(stars) {
  return `<span class="review-stars" aria-label="${stars} out of 5">${[1, 2, 3, 4, 5].map(n => `<span class="${n <= stars ? 'on' : ''}">★</span>`).join('')}</span>`;
}

function reviewListHtml(run) {
  const list = communityReviews(run.id);
  if (!list.length) return `<p class="reviews-empty">No public reviews for this run yet. Be the first.</p>`;
  return list.map(r => `<article class="review">
    <div class="review-top">
      ${starsReadonly(r.stars)}
      <b class="review-author">${esc(r.reviewer)}</b>
      <time class="review-date">${esc((r.created_at || '').slice(0, 10))}</time>
    </div>
    ${r.note ? `<p class="review-note">${esc(r.note)}</p>` : ''}
  </article>`).join('');
}

function reviewFormHtml(run) {
  const cfg = supabaseCfg();
  if (!cfg) {
    return `<p class="reviews-empty config-hint">Public reviews are not configured for this deployment.
      Add a Supabase URL and anon key to <code>site/config.js</code> (or build with
      <code>SUPABASE_URL</code> / <code>SUPABASE_ANON_KEY</code>). See <code>svg-bench/supabase.sql</code>.</p>`;
  }
  const local = runRating(run.id);
  const selected = local.stars || 0;
  const reviewer = localStorage.getItem(LS_REVIEWER) || '';
  return `<form id="review-form" class="review-form" data-id="${esc(run.id)}" data-stars="${selected}">
    <div class="review-form-row">
      <label class="field compact"><span>Your name</span>
        <input name="reviewer" maxlength="60" placeholder="optional" value="${esc(reviewer)}" autocomplete="nickname">
      </label>
      <div class="field compact"><span>Stars</span>
        <span class="rev-picker" role="group" aria-label="Review stars">
          ${[1, 2, 3, 4, 5].map(n => `<button type="button" class="rev-star${n <= selected ? ' on' : ''}" data-star="${n}" aria-label="${n} of 5">★</button>`).join('')}
        </span>
      </div>
    </div>
    <label class="field"><span>Review</span>
      <textarea name="note" maxlength="2000" placeholder="What did you think of this output?">${esc(local.note)}</textarea>
    </label>
    <div class="review-form-actions">
      <button class="btn small" type="submit">Publish review</button>
      <span class="hint">Public — every visitor will see it.</span>
    </div>
  </form>`;
}

function renderReviews(run) {
  if (!run) return;
  $('#reviews-list').innerHTML = reviewListHtml(run);
  $('#reviews-count').textContent = String(communityReviews(run.id).length);
  $('#review-form-wrap').innerHTML = reviewFormHtml(run);
}

/* ───────── automated judge breakdown in the preview panel ───────── */
function renderJudgeBreakdown(run) {
  const el = $('#preview-judge');
  if (!el) return;
  const j = judgeData();
  const jr = judgeFor(run.id);
  if (!j || !jr) {
    el.innerHTML = `<div class="judge-head"><h2>Automated judge</h2></div>
      <p class="judge-empty">Not judged yet — run <code>./run.sh judge</code> to score every output.</p>`;
    return;
  }
  const rank = rankFor(run.model);
  const dims = (j.dimensions && j.dimensions.length) ? j.dimensions : Object.keys(j.weights || {});
  const weights = j.weights || {};
  const scores = jr.scores || {};
  const reasons = jr.reasons || {};
  const rows = dims.map(k => {
    const s = Number(scores[k] || 0);
    const reason = reasons[k];
    return `<div class="judge-dim">
      <div class="judge-dim-top">
        <span class="judge-dim-name" title="${esc(k)}">${esc(dimLabel(k))}</span>
        <span class="judge-dim-weight" title="Weight in the composite">w ${Number(weights[k] || 0).toFixed(2)}</span>
        <span class="score-bar"><span style="width:${Math.round(s / 5 * 100)}%"></span></span>
        <b class="judge-dim-score">${s.toFixed(0)}<small>/5</small></b>
      </div>
      ${reason ? `<p class="judge-dim-reason">${esc(reason)}</p>` : ''}
    </div>`;
  }).join('');
  const checks = Object.entries(jr.technical_checks || {}).map(([k, v]) =>
    `<span class="judge-check ${v ? 'ok' : 'bad'}" title="Deterministic technical check">${v ? '✓' : '✕'} ${esc(k.replace(/_/g, ' '))}</span>`).join('');
  el.innerHTML = `
    <div class="judge-head">
      <h2>Automated judge</h2>
      ${rank ? `<span class="badge rank" title="Model rank by mean composite">#${rank.rank}</span>` : ''}
      <b class="judge-composite" title="Weighted score across all seven dimensions (0–100)">${Number(jr.composite || 0).toFixed(1)}<small>/100</small></b>
    </div>
    <div class="judge-meta">
      <span class="pchip" title="How the judge saw this submission">${esc(jr.input_mode || '?')} input</span>
      <span class="pchip" title="Judge model">${esc(shortModel(jr.judge_model))}</span>
      ${jr.dnf ? '<span class="pchip warn">DNF · score 0</span>' : ''}
      ${jr.parse_ok === false ? '<span class="pchip warn" title="The judge answer could not be parsed">parse failed</span>' : ''}
      ${jr.frames ? `<span class="pchip">${jr.frames} rendered frames</span>` : ''}
    </div>
    ${checks ? `<div class="judge-checks">${checks}</div>` : ''}
    <div class="judge-dims">${rows}</div>
    <p class="judge-foot">Blind 1–5 rubric · ${esc(j.rubric_version || '')}${jr.parse_note ? ' · ' + esc(jr.parse_note) : ''}</p>`;
}

function showPreview(r) {
  if (!r) return;
  clearTimeout(state.previewTimer);
  state.previewRun = r;
  state.previewRunId = r.id;
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
  $('#preview-chips').innerHTML = `
    <span class="pchip">${esc(r.task)}</span>
    <span class="pchip">${esc(r.effort || 'default')} effort</span>
    <span class="pchip">${r.animation_elements ?? 0} animations</span>
    <span class="pchip">${r.size_bytes ? `${Math.round(r.size_bytes / 1024)} KB` : 'no file'}</span>`;
  $('#preview-open').style.display = r.svg_rel ? '' : 'none';
  $('#preview-open').href = r.svg_rel || '#';
  $('#preview-rating').innerHTML = `<span class="pchip">${esc(r.task)}</span>` + starsHtml(r.id) +
    `<span class="cell-date">${esc((r.date_utc || '').slice(0, 10))}</span>`;
  const noteBox = $('#preview-note');
  const noteTa = noteBox.querySelector('textarea');
  noteTa.value = runRating(r.id).note || '';
  noteBox.hidden = true;
  renderJudgeBreakdown(r);
  renderReviews(r);
}

function runOutput(r) {
  if (!r || !r.svg_rel) { toast('No SVG output for this run', true); return; }
  showPreview(r);
  const img = $('#preview-img');
  img.src = `${r.svg_rel}?run=${Date.now()}`;
  toast(`Running ${r.model_id} · ${r.task}`);
}

/* ───────── data loading with polling ───────── */
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
    if (sig !== state.lastSig) {
      state.lastSig = sig;
      const typing = $$('.cell-note textarea, #review-form textarea').some(t => t === document.activeElement);
      state.data = d;
      state.runs = d.runs || [];
      state.seedReviews = Array.isArray(d.reviews) ? d.reviews : [];
      if (!supabaseCfg()) {
        state.reviews = state.seedReviews;
        indexReviews();
      }
      markUpdated(d.generated_utc);
      if (typing) { renderChips(); toast('New benchmark results recorded — press F5 to refresh', false, 5000); }
      else { render(); if (state.previewRunId) { const r = state.runs.find(x => x.id === state.previewRunId); if (r) showPreview(r); } toast('Leaderboard updated'); }
    }
    if (Date.now() - state.reviewsFetchedAt > 30_000) await refreshPublicReviews({ quiet: true });
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
    <span class="chip"><b>${s.judged || 0}</b> judged</span>
    <span class="chip"><b>${state.reviews.length}</b> public reviews</span>
    <span class="chip">tasks: <b>${esc(s.tasks.join(' · '))}</b></span>`;
  $('#foot-generated').textContent = `generated ${d} · opencode ${esc(state.runs[0]?.opencode_version || '?')}`;
}

function syncNewTab() {
  const btn = document.querySelector('.tab[data-scope="new"]');
  if (!btn) return;
  btn.hidden = !hasNewModels();
  if (btn.hidden && state.scope === 'new') setScope('available');
}
function setScope(scope) {
  state.view = 'runs';
  state.scope = scope;
  $$('.view').forEach(s => s.classList.toggle('active', s.id === 'view-runs'));
  $$('.tab[data-scope]').forEach(t => {
    const on = t.dataset.scope === scope;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  $$('.tab[data-view]').forEach(t => {
    t.classList.remove('active');
    t.setAttribute('aria-selected', 'false');
  });
  renderMatrix();
  const visible = scopedRuns();
  const first = visible.find(r => r.result_status === 'ok' && r.svg_rel) || visible[0];
  if (first) showPreview(first);
  else $('#preview').hidden = true;
}

function setView(view) {
  const v = view === 'ranking' ? 'ranking' : 'runs';
  state.view = v;
  const isRank = v === 'ranking';
  $$('.view').forEach(s => s.classList.toggle('active', s.id === `view-${v}`));
  $$('.tab[data-view]').forEach(t => {
    const on = isRank && t.dataset.view === 'ranking';
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  $$('.tab[data-scope]').forEach(t => {
    const on = !isRank && t.dataset.scope === state.scope;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  if (isRank) renderRanking();
  else renderMatrix();
}

function renderRanking() {
  const el = $('#ranking-body');
  if (!el) return;
  const j = judgeData();
  if (!j || !Array.isArray(j.ranking) || !j.ranking.length) {
    el.innerHTML = `<div class="ranking-empty">
      <h2>No automated judge scores yet</h2>
      <p>Run <code>./run.sh judge</code> (or <code>python3 judge.py</code>) to score every
      output blind with a free model, then refresh <code>data.json</code> with
      <code>python3 leaderboard.py</code>.</p></div>`;
    return;
  }
  const meta = j.meta || {};
  const probe = meta.probe || {};
  const dims = (j.dimensions && j.dimensions.length) ? j.dimensions : Object.keys(j.weights || {});
  const weights = j.weights || {};
  const head = `<tr><th class="num">#</th><th>Model</th><th class="num" title="Weighted mean score across the model's tasks (0–100)">Score</th>
    ${dims.map(k => `<th class="num" title="${esc(k)} · weight ${Number(weights[k] || 0).toFixed(2)}">${esc(dimLabel(k))}</th>`).join('')}
    <th class="num" title="Tasks scored">Runs</th><th class="num" title="Did not finish">DNF</th></tr>`;
  const body = j.ranking.map(r => `<tr>
    <td class="num rank-cell">${r.rank}</td>
    <td class="rank-model"><b>${esc(shortModel(r.model))}</b><span>${esc(r.model)}</span></td>
    <td class="num rank-score"><b>${Number(r.mean_composite || 0).toFixed(1)}</b></td>
    ${dims.map(k => `<td class="num">${Number((r.dimensions || {})[k] || 0).toFixed(1)}</td>`).join('')}
    <td class="num">${r.n_runs}</td>
    <td class="num">${r.dnf || '—'}</td>
  </tr>`).join('');
  el.innerHTML = `
    <div class="ranking-meta">
      <div><h2>Automated judge ranking</h2>
        <p>Blind 1–5 rubric · ${esc(j.rubric_version || '')} · judge
        <b>${esc(shortModel(j.judge_model))}</b> · ${esc(probe.mode || 'unknown')} input ·
        ${meta.n_judged || 0} runs scored · updated ${esc((j.updated_utc || '').slice(0, 16).replace('T', ' '))}</p></div>
      <span class="badge rank" title="Number of ranked models">${j.ranking.length} models</span>
    </div>
    <div class="ranking-weights">Weights: ${dims.map(k => `<span class="pchip">${esc(dimLabel(k))} ${Number(weights[k] || 0).toFixed(2)}</span>`).join('')}
      <span class="pchip" title="Failed runs score 0">DNF = 0</span></div>
    <div class="ranking-table-wrap"><table class="ranking-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>
    ${probe.mode === 'text' ? '<p class="ranking-note">No free model accepted rendered frames — scores come from SVG source only and are less reliable on visual dimensions.</p>' : ''}
    ${meta.blind_note ? `<p class="ranking-note">${esc(meta.blind_note)} ${meta.judge_is_contestant_note ? esc(meta.judge_is_contestant_note) : ''}</p>` : ''}`;
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

/* ───────── admin-only benchmark re-runs (local server) ───────── */
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
  if (!state.admin) { toast('Admin controls are disabled for this page', true); return; }
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
  if (!state.admin) return;
  const el = $('#runbar');
  const st = await apiGet('/api/run');
  if (!st) return;
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

async function initAdmin() {
  const st = await apiGet('/api/run');
  state.localServer = !!st && typeof st.state === 'string';
  const params = new URLSearchParams(location.search);
  const wantsAdmin = params.get('admin') === '1' || localStorage.getItem(LS_ADMIN) === '1';
  state.admin = state.localServer && wantsAdmin;
  if (state.admin) localStorage.setItem(LS_ADMIN, '1');
  document.body.classList.toggle('is-admin', state.admin);
  $('#rerun-group').hidden = !state.admin;
  $('#admin-badge').hidden = !state.admin;
  if (!state.admin) return;

  $('#btn-rerun-failed').addEventListener('click', () => launchRerun(failedPairs(), 'Re-run failed pairs'));
  $('#btn-rerun-all').addEventListener('click', () => launchRerun(allPairs(), 'Re-run EVERYTHING'));
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
    state.sort = state.sort.key === key
      ? { key, dir: -state.sort.dir }
      : { key, dir: key === 'judge' ? -1 : 1 };  // judge: highest first on first click
    renderMatrix();
  });

  // stars + notes + run output + artwork preview (delegated)
  $('#runs-matrix').addEventListener('click', e => {
    const starBtn = e.target.closest('.star');
    if (starBtn) {
      const wrap = starBtn.closest('.stars');
      setStars(wrap.dataset.id, +starBtn.dataset.star);
      renderMatrix();
      if (state.previewRunId === wrap.dataset.id) showPreview(state.runs.find(r => r.id === state.previewRunId));
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
    const runBtn = e.target.closest('.run-output.cell-run');
    if (runBtn) {
      e.stopPropagation();
      const run = state.runs.find(r => r.id === runBtn.dataset.id);
      if (run) runOutput(run);
      return;
    }
    const adminBtn = e.target.closest('.rerun-row');
    if (adminBtn) {
      e.stopPropagation();
      if (!state.admin) return;
      const run = state.runs.find(r => r.id === adminBtn.dataset.id);
      if (run) launchRerun([[run.model, run.task]], `Re-run ${run.model_id}`);
      return;
    }
    const thumb = e.target.closest('.matrix-thumb');
    if (thumb) { window.open(thumb.src, '_blank', 'noopener'); return; }
    const cell = e.target.closest('td.matrix-cell[data-id]');
    if (cell) {
      const run = state.runs.find(r => r.id === cell.dataset.id);
      if (run) showPreview(run);
    }
  }, true); // capture phase so per-cell actions win over the cell preview
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
    if (cell) {
      const run = state.runs.find(r => r.id === cell.dataset.id);
      if (run) showPreview(run);
    }
  };
  $('#runs-matrix').addEventListener('mouseover', cellEnter);
  $('#runs-matrix').addEventListener('focusin', cellEnter);

  // preview: private rating, note
  $('#preview').addEventListener('click', e => {
    const starBtn = e.target.closest('.star');
    if (starBtn) {
      const wrap = starBtn.closest('.stars');
      const id = wrap.dataset.id;
      setStars(id, +starBtn.dataset.star);
      renderMatrix();
      showPreview(state.runs.find(r => r.id === id));
      return;
    }
    const noteBtn = e.target.closest('.note-btn');
    if (noteBtn) {
      const note = $('#preview-note');
      note.hidden = !note.hidden;
      if (!note.hidden) note.querySelector('textarea').focus();
      return;
    }
    const revStar = e.target.closest('.rev-star');
    if (revStar) {
      const form = revStar.closest('#review-form');
      if (!form) return;
      const stars = +revStar.dataset.star;
      form.dataset.stars = String(stars);
      $$('.rev-star', form).forEach(b => b.classList.toggle('on', +b.dataset.star <= stars));
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
      renderMatrix();
    }, 350);
  });

  // public review submit
  $('#public-reviews').addEventListener('submit', async e => {
    const form = e.target.closest('#review-form');
    if (!form) return;
    e.preventDefault();
    const run = state.runs.find(r => r.id === form.dataset.id);
    if (!run) return;
    const stars = +form.dataset.stars;
    const note = form.querySelector('[name="note"]').value.trim();
    const reviewer = form.querySelector('[name="reviewer"]').value.trim();
    if (!stars) { toast('Pick a star rating first', true); return; }
    if (note.length < 3) { toast('Write at least a few words', true); return; }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    submit.textContent = 'Publishing…';
    try {
      localStorage.setItem(LS_REVIEWER, reviewer);
      const created = await publishReview(run, reviewer, stars, note);
      if (created) {
        state.reviews = mergeReviews(state.reviews, [created]);
        indexReviews();
      }
      await refreshPublicReviews({ quiet: true });
      showPreview(run);
      renderMatrix();
      renderChips();
      toast('Review published — thanks!');
    } catch (err) {
      toast(`Could not publish review: ${err.message}`, true);
      submit.disabled = false;
      submit.textContent = 'Publish review';
    }
  });

  $$('.tab').forEach(t => t.addEventListener('click', () => {
    if (t.dataset.view) setView(t.dataset.view);
    else if (t.dataset.scope) setScope(t.dataset.scope);
  }));
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
  if (state.view === 'ranking') renderRanking();
}

async function boot() {
  let data;
  try {
    data = await fetchData();
  } catch (e) {
    document.body.innerHTML = `<div class="prose" style="padding:40px"><h2>Couldn't load the benchmark data</h2>
      <p>${esc(e.message)}. Rebuild the static site with:</p>
      <pre>cd svg-bench &amp;&amp; python3 build_site.py</pre>
      <p>or regenerate just the data with <code>python3 leaderboard.py</code>.</p></div>`;
    return;
  }
  state.data = data;
  state.runs = data.runs || [];
  state.seedReviews = Array.isArray(data.reviews) ? data.reviews : [];
  state.reviews = state.seedReviews.slice();
  indexReviews();
  state.lastSig = JSON.stringify(data);

  const tasks = [...new Set(state.runs.map(r => r.task))].sort();
  $('#f-task').innerHTML = '<option value="all">All tasks</option>' + tasks.map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join('');
  const providers = [...new Set(state.runs.map(r => r.provider))].sort();
  $('#f-provider').innerHTML = '<option value="all">All providers</option>' + providers.map(p => `<option value="${esc(p)}">${esc(p)}</option>`).join('');

  renderChips();
  bindEvents();
  await initAdmin();
  render();
  markUpdated(data.generated_utc);
  const visible = scopedRuns();
  const first = visible.find(r => r.result_status === 'ok' && r.svg_rel) || visible[0];
  if (first) showPreview(first);
  refreshPublicReviews({ quiet: true });
  setInterval(poll, 10_000);
  setInterval(() => refreshPublicReviews({ quiet: true }), 30_000);
}

boot();
