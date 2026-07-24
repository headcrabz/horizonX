'use strict';

// ─────────────────────────────────────────────────────────────────────────────
// State
// ─────────────────────────────────────────────────────────────────────────────
const state = {
  currentRunId: null,
  currentSessionId: null,
  eventSource: null,
  pollTimer: null,
  centerTab: 'stream',
  runsFilter: '',
};

// ─────────────────────────────────────────────────────────────────────────────
// Utilities
// ─────────────────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function badge(status) {
  return `<span class="badge badge-${esc(status)}">${esc(status)}</span>`;
}

function reltime(iso) {
  if (!iso) return '—';
  const sec = Math.floor((Date.now() - new Date(iso)) / 1000);
  if (sec < 60)    return `${sec}s ago`;
  if (sec < 3600)  return `${Math.floor(sec/60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

function duration(start, end) {
  if (!start) return '—';
  const sec = Math.floor(((end ? new Date(end) : new Date()) - new Date(start)) / 1000);
  if (sec < 60)   return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec/60)}m ${sec%60}s`;
  return `${Math.floor(sec/3600)}h ${Math.floor((sec%3600)/60)}m`;
}

const fmtUSD = n => n != null ? `$${Number(n).toFixed(3)}` : '$0.000';
const fmtTok = n => n ? (n>=1e6 ? `${(n/1e6).toFixed(2)}M` : n>=1000 ? `${(n/1000).toFixed(1)}K` : String(n)) : '0';

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: {'Content-Type':'application/json'}, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

function setNav(id) {
  ['nav-overview','nav-runs','nav-launch'].forEach(n => {
    const el = $(n);
    if (el) el.classList.toggle('active', n === id);
  });
}

function showView(id) {
  ['view-overview','view-runs','view-run-detail','view-launch'].forEach(v => {
    const el = $(v);
    if (el) el.classList.toggle('active', v === id);
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Health
// ─────────────────────────────────────────────────────────────────────────────
async function checkHealth() {
  try {
    await api('/api/health');
    const d = $('health-dot'); if (d) d.style.background = 'var(--green)';
    const l = $('health-label'); if (l) l.textContent = 'API connected';
  } catch {
    const d = $('health-dot'); if (d) d.style.background = 'var(--red)';
    const l = $('health-label'); if (l) l.textContent = 'API unavailable';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Router
// ─────────────────────────────────────────────────────────────────────────────
window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', () => { checkHealth(); route(); });

function route() {
  const hash = window.location.hash || '#/';
  clearRunDetail();
  const filterEl = $('runs-filter'); if (filterEl) filterEl.style.display = 'none';
  const refBtn   = $('refresh-btn'); if (refBtn)   refBtn.style.display   = 'none';

  if      (hash.startsWith('#/run/'))  showRunDetail(hash.slice(6));
  else if (hash.startsWith('#/runs'))  showRunsList();
  else if (hash === '#/launch')        showLaunch();
  else                                 showOverview();
}

window.navigate          = hash => { window.location.hash = hash; };
window.refreshCurrentView = () => {
  const h = window.location.hash;
  if (h.startsWith('#/run/'))   loadRunDetailData(state.currentRunId);
  else if (h.startsWith('#/runs')) loadRunsList();
  else                             loadOverviewData();
};
window.setFilter = status => { state.runsFilter = status; };
window.applyFilter = () => loadRunsList();

// ─────────────────────────────────────────────────────────────────────────────
// Overview
// ─────────────────────────────────────────────────────────────────────────────
function showOverview() {
  setNav('nav-overview');
  showView('view-overview');
  $('topbar-title').textContent = 'Overview';
  $('refresh-btn').style.display = '';
  loadOverviewData();
}

async function loadOverviewData() {
  try {
    const runs = await api('/api/runs?limit=200');
    const active = runs.filter(r => r.status === 'running').length;
    const done   = runs.filter(r => r.status === 'completed').length;
    const hitl   = runs.filter(r => r.status === 'paused_hitl').length;
    const failed = runs.filter(r => r.status === 'failed').length;

    $('stat-active').textContent  = active;
    $('stat-active-sub').textContent = active ? `${active} agent${active>1?'s':''} running now` : 'none running';
    $('stat-done').textContent    = done;
    $('stat-done-sub').textContent = `${runs.length} total runs`;
    $('stat-hitl').textContent    = hitl;
    $('stat-failed').textContent  = failed;

    const recent = runs.slice(0, 10);
    if (!recent.length) {
      $('overview-tbody').innerHTML = `<tr><td colspan="5"><div class="empty-state">
        <div class="empty-icon">◈</div><p>No runs yet. <a href="#/launch">Launch your first run →</a></p>
      </div></td></tr>`;
      return;
    }
    $('overview-tbody').innerHTML = recent.map(r => `
      <tr onclick="navigate('#/run/${esc(r.id)}')">
        <td style="font-weight:600;">${esc(r.task_name || r.task_id || '—')}</td>
        <td>${badge(r.status)}</td>
        <td style="color:var(--faint);font-size:12px;">${reltime(r.started_at)}</td>
        <td style="color:var(--muted);">${r.sessions_count ?? 0}</td>
        <td class="right" style="color:var(--green);font-family:monospace;">${fmtUSD(r.usd)}</td>
      </tr>`).join('');
  } catch (e) {
    $('overview-tbody').innerHTML = `<tr><td colspan="5" style="color:var(--red);padding:12px;">${esc(e.message)}</td></tr>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Runs list
// ─────────────────────────────────────────────────────────────────────────────
function showRunsList() {
  setNav('nav-runs');
  showView('view-runs');
  $('topbar-title').textContent = 'Runs';
  $('runs-filter').style.display = '';
  $('refresh-btn').style.display = '';
  if (state.runsFilter) $('runs-filter').value = state.runsFilter;
  loadRunsList();
}

async function loadRunsList() {
  const status = $('runs-filter').value || undefined;
  const qs = status ? `?status=${status}&limit=100` : '?limit=100';
  try {
    const runs = await api('/api/runs' + qs);
    if (!runs.length) {
      $('runs-tbody').innerHTML = `<tr><td colspan="9"><div class="empty-state">
        <div class="empty-icon">⊞</div><p>No runs found. <a href="#/launch">Launch one →</a></p>
      </div></td></tr>`;
      return;
    }
    $('runs-tbody').innerHTML = runs.map(r => `
      <tr onclick="navigate('#/run/${esc(r.id)}')">
        <td class="mono">${esc(r.id.slice(0,12))}…</td>
        <td style="font-weight:600;">${esc(r.task_name || r.task_id || '—')}</td>
        <td><span style="font-family:monospace;font-size:11.5px;color:var(--accent-lt);">${esc(r.strategy||'—')}</span></td>
        <td>${badge(r.status)}</td>
        <td style="color:var(--faint);font-size:12px;">${reltime(r.started_at)}</td>
        <td style="color:var(--faint);font-size:12px;">${duration(r.started_at,r.completed_at)}</td>
        <td class="right" style="color:var(--muted);">${r.sessions_count??0}</td>
        <td class="right" style="color:var(--muted);">${r.steps_count??0}</td>
        <td class="right" style="color:var(--green);font-family:monospace;">${fmtUSD(r.usd)}</td>
      </tr>`).join('');
  } catch (e) {
    $('runs-tbody').innerHTML = `<tr><td colspan="9" style="color:var(--red);padding:12px;">${esc(e.message)}</td></tr>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Run detail
// ─────────────────────────────────────────────────────────────────────────────
async function showRunDetail(runId) {
  setNav(null);
  showView('view-run-detail');
  $('topbar-title').textContent = 'Run Detail';
  $('refresh-btn').style.display = '';
  state.currentRunId = runId;
  await loadRunDetailData(runId);
}

async function loadRunDetailData(runId) {
  try {
    const run = await api(`/api/runs/${runId}`);
    renderRunHeader(run);
    renderHitlBanner(run);
  } catch (e) {
    $('run-breadcrumb').textContent = `Error: ${e.message}`;
    return;
  }
  loadSessions(runId);
  connectEventSource(runId);
  loadSpinReports(runId);
  loadHitlHistory(runId);

  state.pollTimer = setInterval(async () => {
    try {
      const run = await api(`/api/runs/${runId}`);
      renderRunHeader(run);
      renderHitlBanner(run);
      if (['completed','failed','aborted','timed_out'].includes(run.status))
        clearInterval(state.pollTimer);
    } catch { /* ignore */ }
  }, 5000);

  setCenterTab(state.centerTab);
}

function renderRunHeader(run) {
  const cm = run.cumulative || {};
  $('run-breadcrumb').textContent = run.task?.name || run.id;
  $('run-status-badge').innerHTML = badge(run.status) +
    (run.status === 'running' ? ' <span class="pulse" style="display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--blue);margin-left:4px;"></span>' : '');

  $('run-metrics-row').innerHTML = [
    `<div class="metric-chip"><span>Sessions</span><span class="val">${cm.sessions_count??0}</span></div>`,
    `<div class="metric-chip"><span>Steps</span><span class="val">${cm.steps_count??0}</span></div>`,
    `<div class="metric-chip green"><span>Cost</span><span class="val">${fmtUSD(cm.usd)}</span></div>`,
    `<div class="metric-chip"><span>Wall</span><span class="val">${duration(run.started_at,run.completed_at)}</span></div>`,
    `<div class="metric-chip"><span>Tok in</span><span class="val">${fmtTok(cm.tokens_in)}</span></div>`,
    `<div class="metric-chip"><span>Tok out</span><span class="val">${fmtTok(cm.tokens_out)}</span></div>`,
  ].join('');

  // Budget gauge
  const spent = cm.usd || 0;
  const limit = run.task?.resources?.max_total_usd;
  $('cost-spent').textContent = fmtUSD(spent);
  $('cost-limit').textContent = limit ? `$${limit} limit` : 'no limit set';
  $('tok-in').textContent  = fmtTok(cm.tokens_in);
  $('tok-out').textContent = fmtTok(cm.tokens_out);
  const pct = limit ? Math.min((spent/limit)*100, 100) : 0;
  const fill = $('cost-bar-fill');
  if (fill) {
    fill.style.width = `${pct}%`;
    fill.className = 'cost-bar-fill' + (pct>=90?' danger': pct>=75?' warn':'');
  }
  const bp = $('budget-pct');
  if (bp) bp.textContent = limit ? `${pct.toFixed(1)}%` : '—';
}

function renderHitlBanner(run) {
  const banner = $('hitl-banner');
  if (!banner) return;
  banner.style.display = run.status === 'paused_hitl' ? '' : 'none';
}

// ─────────────────────────────────────────────────────────────────────────────
// HITL actions
// ─────────────────────────────────────────────────────────────────────────────
window.toggleModifyForm = () => {
  const f = $('hitl-modify-form');
  f.style.display = f.style.display === 'none' || f.style.display === '' ? 'flex' : 'none';
};

window.resolveHitl = async (runId, action, instruction='') => {
  try {
    await api(`/api/runs/${runId}/hitl`, {
      method: 'POST',
      body: JSON.stringify({ action, instruction, operator: 'dashboard' }),
    });
    const banner = $('hitl-banner');
    if (banner) {
      banner.innerHTML = '<span style="color:var(--green);">✓ Decision sent — agent will resume shortly</span>';
      setTimeout(() => { banner.style.display = 'none'; }, 4000);
    }
  } catch (e) { alert('Error: ' + e.message); }
};

window.resolveHitlModify = () => {
  const instr = $('hitl-instruction')?.value.trim();
  if (!instr) { alert('Enter an instruction first'); return; }
  resolveHitl(state.currentRunId, 'modify', instr);
};

window.cancelRun = async () => {
  if (!state.currentRunId || !confirm('Cancel this run?')) return;
  try {
    await api(`/api/runs/${state.currentRunId}/cancel`, { method: 'POST' });
    alert('Cancel signal sent');
  } catch (e) { alert('Error: ' + e.message); }
};

// ─────────────────────────────────────────────────────────────────────────────
// Sessions
// ─────────────────────────────────────────────────────────────────────────────
async function loadSessions(runId) {
  try {
    const sessions = await api(`/api/runs/${runId}/sessions`);
    renderSessions(sessions, runId);
    if (sessions.length && !state.currentSessionId)
      selectSession(sessions[sessions.length-1].id, runId);
  } catch (e) {
    $('sessions-list').innerHTML = `<div style="color:var(--red);font-size:11px;padding:10px;">${esc(e.message)}</div>`;
  }
}

function renderSessions(sessions, runId) {
  const sc = $('session-count');
  if (sc) sc.textContent = `${sessions.length} total`;
  if (!sessions.length) {
    $('sessions-list').innerHTML = '<div class="empty-state"><p>No sessions yet</p></div>';
    return;
  }
  $('sessions-list').innerHTML = [...sessions].reverse().map(s => `
    <div class="session-item${s.id===state.currentSessionId?' active':''}" id="sess-${esc(s.id)}"
         onclick="selectSession('${esc(s.id)}','${esc(runId)}')">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <span class="s-num">Session #${(s.sequence_index??0)+1}</span>
        ${badge(s.status)}
      </div>
      <div class="s-meta">${s.steps_count??0} steps · ${fmtTok(s.tokens_used)} tok · ${duration(s.started_at,s.completed_at)}</div>
    </div>`).join('');
}

window.selectSession = async (sessionId, runId) => {
  state.currentSessionId = sessionId;
  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
  const el = $(`sess-${sessionId}`);
  if (el) el.classList.add('active');
  $('steps-panel').innerHTML = '<div class="empty-state"><div class="spinner"></div></div>';
  try {
    const steps = await api(`/api/runs/${runId}/sessions/${sessionId}/steps?limit=500`);
    renderSteps(steps);
  } catch (e) {
    $('steps-panel').innerHTML = `<div style="color:var(--red);font-size:11px;padding:10px;">${esc(e.message)}</div>`;
  }
};

function renderSteps(steps) {
  const sc = $('steps-count');
  if (sc) sc.textContent = `${steps.length} steps`;
  if (!steps.length) {
    $('steps-panel').innerHTML = '<div class="empty-state"><p>No steps recorded</p></div>';
    return;
  }
  const typeColor = {
    thought:'type-thought', tool_call:'type-tool_call', observation:'type-observation',
    file_change:'type-file_change', error:'type-error', reasoning:'type-reasoning',
  };
  $('steps-panel').innerHTML = steps.map(s => {
    const c = s.content || {};
    const snippet = (c.text||c.command||c.output||c.patch||JSON.stringify(c)).slice(0,260);
    return `<div class="step-row">
      <div style="display:flex;align-items:center;gap:6px;">
        <span class="step-type ${typeColor[s.type]||'type-thought'}">${esc(s.type)}</span>
        ${s.tool_name?`<span style="font-family:monospace;font-size:10.5px;color:var(--blue);">${esc(s.tool_name)}</span>`:''}
        <span style="margin-left:auto;font-family:monospace;font-size:10px;color:var(--faint);">#${s.sequence}</span>
      </div>
      ${snippet?`<div class="step-content">${esc(snippet)}</div>`:''}
    </div>`;
  }).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// Live event stream (SSE)
// ─────────────────────────────────────────────────────────────────────────────
function connectEventSource(runId) {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = new EventSource(`/api/runs/${runId}/events`);
  state.eventSource.onmessage = e => appendEvent(JSON.parse(e.data));
  [
    'run.started','run.completed','run.failed','run.paused_hitl',
    'session.started','session.completed','session.timeout',
    'step.recorded','goal.in_progress','goal.done','goal.failed',
    'validator.passed','validator.failed','validator.paused',
    'spin.detected','hitl.requested','hitl.resolved',
    'budget.threshold','budget.velocity_alert',
    'goals.re_decomposed','summary.created','retry.attempted',
  ].forEach(t => state.eventSource.addEventListener(t, e => appendEvent(JSON.parse(e.data))));
  state.eventSource.onerror = () => {};
}

function evCls(type) {
  const map = { run:'ev-run', session:'ev-session', step:'ev-step', goal:'ev-goal',
    goals:'ev-goal', validator:'ev-validator', spin:'ev-spin', hitl:'ev-hitl',
    budget:'ev-budget', summary:'ev-summary', fork:'ev-fork', retry:'ev-retry' };
  return map[type.split('.')[0]] || 'ev-step';
}

function appendEvent(event) {
  const container = $('panel-stream');
  if (!container) return;
  const el = document.createElement('div');
  el.className = `event-row ${evCls(event.type)}`;
  const pay = event.payload && Object.keys(event.payload).length
    ? ' · ' + JSON.stringify(event.payload).slice(0,100) : '';
  el.innerHTML = `<span class="event-time">${new Date(event.timestamp).toLocaleTimeString()}</span>
    <span class="event-type">${esc(event.type)}</span>
    <span class="event-payload">${esc(pay)}</span>`;
  const pinned = $('pin-scroll')?.checked;
  pinned ? container.prepend(el) : (container.appendChild(el), container.scrollTop=container.scrollHeight);

  if (event.type.startsWith('session.') || event.type==='step.recorded')
    if (state.currentRunId) loadSessions(state.currentRunId);
  if (event.type.startsWith('goal.') || event.type==='goals.re_decomposed')
    if (state.centerTab==='goals' && state.currentRunId) loadGoals(state.currentRunId);
  if (event.type.startsWith('validator.'))
    if (state.centerTab==='validations' && state.currentRunId) loadValidations(state.currentRunId);
  if (event.type==='spin.detected' && state.currentRunId)
    loadSpinReports(state.currentRunId);
  if (event.type==='hitl.requested') {
    api(`/api/runs/${state.currentRunId}`).then(renderHitlBanner).catch(()=>{});
    loadHitlHistory(state.currentRunId);
  }
  if (event.type==='hitl.resolved') {
    const b = $('hitl-banner'); if (b) b.style.display='none';
    loadHitlHistory(state.currentRunId);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Center tab
// ─────────────────────────────────────────────────────────────────────────────
window.setCenterTab = tab => {
  state.centerTab = tab;
  ['stream','goals','validations'].forEach(t => {
    const btn = $(`tab-${t}`); if (btn) btn.classList.toggle('active', t===tab);
    const pan = $(`panel-${t}`); if (pan) pan.style.display = t===tab ? '' : 'none';
  });
  if (!state.currentRunId) return;
  if (tab==='goals')       loadGoals(state.currentRunId);
  if (tab==='validations') loadValidations(state.currentRunId);
};

// ─────────────────────────────────────────────────────────────────────────────
// Goals
// ─────────────────────────────────────────────────────────────────────────────
async function loadGoals(runId) {
  try {
    const goals = await api(`/api/runs/${runId}/goals`);
    renderGoals(goals);
  } catch (e) {
    $('panel-goals').innerHTML = `<div style="color:var(--red);padding:10px;font-size:11px;">${esc(e.message)}</div>`;
  }
}

function renderGoals(goals) {
  if (!goals.length) {
    $('panel-goals').innerHTML = '<div class="empty-state"><p>No goals yet</p></div>';
    return;
  }
  const byId  = Object.fromEntries(goals.map(g => [g.id, g]));
  const roots = goals.filter(g => !g.parent_id || !byId[g.parent_id]);
  const icons = { done:'✓', failed:'✗', in_progress:'▶', pending:'○', blocked:'⊘', skipped:'⊝' };
  const cls   = { done:'done', failed:'failed', in_progress:'active', pending:'pending', blocked:'failed', skipped:'pending' };

  function node(g, depth=0) {
    const children = goals.filter(c => c.parent_id===g.id);
    return `<div class="goal-row" style="padding-left:${depth*14+8}px;">
      <span class="goal-icon ${cls[g.status]||'pending'}">${icons[g.status]||'?'}</span>
      <span class="goal-name">${esc(g.name)}</span>
      ${g.progress_pct!=null?`<span class="goal-pct">${Math.round(g.progress_pct)}%</span>`:''}
    </div>` + children.map(c=>node(c,depth+1)).join('');
  }
  $('panel-goals').innerHTML = roots.map(r=>node(r)).join('');
}

// ─────────────────────────────────────────────────────────────────────────────
// Validators
// ─────────────────────────────────────────────────────────────────────────────
async function loadValidations(runId) {
  try {
    const vals = await api(`/api/runs/${runId}/validations`);
    if (!vals.length) {
      $('panel-validations').innerHTML = '<div class="empty-state"><p>No validations yet</p></div>';
      return;
    }
    $('panel-validations').innerHTML = vals.map(v => `
      <div class="val-row">
        <span class="val-decision ${v.decision}">${esc(v.decision)}</span>
        <span class="val-name">${esc(v.validator)}</span>
        ${v.score!=null?`<span class="val-score">${Number(v.score).toFixed(2)}</span>`:''}
      </div>`).join('');
  } catch (e) {
    $('panel-validations').innerHTML = `<div style="color:var(--red);padding:10px;font-size:11px;">${esc(e.message)}</div>`;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Spin detection panel
// ─────────────────────────────────────────────────────────────────────────────
async function loadSpinReports(runId) {
  try {
    const reports = await api(`/api/runs/${runId}/spin-reports`);
    updateSpinPanel(reports);
  } catch { /* endpoint may not exist on all backends */ }
}

function updateSpinPanel(reports) {
  const layerMap = {
    exact_loop:1, edit_revert:2, score_plateau:3,
    tool_thrashing:4, bucketed_hash:5, semantic_progress:6, cross_session:7,
  };
  const fired = new Set(reports.map(r => r.layer));
  const summary = $('spin-summary');
  if (summary) {
    const n = reports.length;
    summary.textContent = n ? `${n} detection${n>1?'s':''}` : '7 layers · clear';
    summary.style.color = n ? 'var(--yellow)' : 'var(--green)';
  }
  Object.entries(layerMap).forEach(([name, num]) => {
    const dot    = $(`spin-${num}-dot`);
    const status = $(`spin-${num}-status`);
    if (!dot || !status) return;
    const active = fired.has(name);
    dot.className    = `spin-dot ${active?'fire':'ok'}`;
    status.className = `spin-status ${active?'fire':'ok'}`;
    status.textContent = active ? 'DETECTED' : 'clear';
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// HITL history
// ─────────────────────────────────────────────────────────────────────────────
async function loadHitlHistory(runId) {
  try {
    const events = await api(`/api/runs/${runId}/hitl`);
    const hc = $('hitl-count');
    if (hc) hc.textContent = events.length ? `${events.length} event${events.length>1?'s':''}` : '';
    if (!events.length) {
      $('panel-hitl').innerHTML = '<div class="empty-state"><p>No HITL events yet</p></div>';
      return;
    }
    $('panel-hitl').innerHTML = events.map(e => `
      <div class="hitl-row">
        <div class="hitl-trigger">⏸ ${esc(e.trigger||'operator pause')}</div>
        ${e.decision
          ? `<div class="hitl-decision">✓ ${esc(e.decision)}${e.instruction?` — "${esc(e.instruction)}"`:''}  </div>`
          : '<div class="hitl-pending">Awaiting decision…</div>'}
        ${e.resolved_at?`<div style="font-size:10.5px;color:var(--faint);margin-top:2px;">${reltime(e.resolved_at)}</div>`:''}
      </div>`).join('');
  } catch { /* ignore */ }
}

// ─────────────────────────────────────────────────────────────────────────────
// Launch — mode toggle (Form / YAML)
// ─────────────────────────────────────────────────────────────────────────────
let launchMode = 'form';
let validatorCount = 0;

function showLaunch() {
  setNav('nav-launch');
  showView('view-launch');
  $('topbar-title').textContent = 'Launch a Run';
  if (validatorCount === 0) addValidator(); // add default validator row
}

window.setLaunchMode = (mode) => {
  launchMode = mode;
  ['form','yaml'].forEach(m => {
    const btn = $(`mode-${m}`);
    if (btn) btn.classList.toggle('active', m === mode);
  });
  $('launch-form-mode').style.display = mode === 'form' ? 'flex' : 'none';
  $('launch-yaml-mode').style.display = mode === 'yaml' ? 'flex' : 'none';
  $('launch-mode-hint').textContent = mode === 'form'
    ? 'Configure your job using the form below'
    : 'Edit raw YAML — all fields supported';
  if (mode === 'yaml') syncFormToYaml();
};

// Strategy descriptions
const STRATEGY_HINTS = {
  single:       'One session runs to completion. Best for quick, well-defined tasks.',
  sequential:   'Breaks the goal into sub-goals with session handoffs and crash recovery.',
  pair:         'Driver agent builds; navigator agent reviews each session before proceeding.',
  self_critique:'Agent produces output, then critiques its own work in a second pass.',
  decomposition:'LLM first decomposes the goal into a goal graph, then executes sub-goals.',
  tree:         'Runs multiple parallel branches and keeps the best result.',
  monitor:      'Long-lived agent that watches for signals and reacts autonomously.',
  ralph:        'Reflection + iteration loop for tasks requiring progressive refinement.',
};

window.updateStrategyHint = () => {
  const v = $('f-strategy').value;
  const hint = $('strategy-hint');
  if (hint) hint.textContent = STRATEGY_HINTS[v] || '';
};

window.toggleHitl = () => {
  const enabled = $('f-hitl-enabled').checked;
  $('hitl-config').style.opacity = enabled ? '1' : '0.4';
  $('hitl-config').style.pointerEvents = enabled ? '' : 'none';
};

window.toggleValidators = () => {
  const disabled = $('f-no-validators').checked;
  $('validators-list').style.opacity = disabled ? '0.4' : '1';
  $('validators-list').style.pointerEvents = disabled ? 'none' : '';
};

window.addValidator = () => {
  const idx = validatorCount++;
  const row = document.createElement('div');
  row.className = 'validator-row';
  row.id = `val-row-${idx}`;
  row.innerHTML = `
    <div class="form-group">
      <label class="form-label">Type</label>
      <select class="form-input val-type" id="val-type-${idx}">
        <option value="test_suite">test_suite</option>
        <option value="shell">shell</option>
        <option value="llm_judge">llm_judge</option>
        <option value="metric">metric</option>
        <option value="goal_graph">goal_graph</option>
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">Runs</label>
      <select class="form-input" id="val-runs-${idx}">
        <option value="after_every_session">after every session</option>
        <option value="after_last_session">after last session</option>
        <option value="before_session">before session</option>
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">On fail</label>
      <select class="form-input" id="val-fail-${idx}">
        <option value="pause_for_hitl">pause for HITL</option>
        <option value="retry">retry session</option>
        <option value="abort">abort run</option>
        <option value="continue">continue</option>
      </select>
    </div>
    <button class="remove-btn" onclick="removeValidator(${idx})" title="Remove">✕</button>
    <div class="form-group" style="grid-column:1/-2;">
      <label class="form-label">Command / config</label>
      <input type="text" class="form-input" id="val-cmd-${idx}" placeholder="pytest tests/ -q" style="font-family:monospace;font-size:12px;"/>
    </div>
  `;
  $('validators-list').appendChild(row);
};

window.removeValidator = (idx) => {
  const row = $(`val-row-${idx}`);
  if (row) row.remove();
};

// Build task object from form values
function buildTaskFromForm() {
  const name    = $('f-name').value.trim();
  const prompt  = $('f-prompt').value.trim();
  if (!name)   { alert('Job Name is required'); return null; }
  if (!prompt) { alert('Prompt is required'); return null; }

  const taskId  = $('f-id').value.trim() || `job-${Date.now()}`;
  const strategy = $('f-strategy').value;
  const maxAttempts = parseInt($('f-max-attempts').value) || 3;
  const agent   = $('f-agent').value;
  const model   = $('f-model').value;
  const maxUsd   = parseFloat($('f-max-usd').value) || null;
  const maxTok   = parseInt($('f-max-tokens').value) || null;
  const maxHrs   = parseFloat($('f-max-hours').value) || null;
  const wsId     = $('f-workspace-id').value.trim() || null;
  const daily    = parseFloat($('f-daily-budget').value) || null;
  const gitCommit= $('f-git-commit').checked;

  const task = {
    id: taskId, name, prompt,
    strategy: {
      kind: strategy,
      config: { max_attempts_per_goal: maxAttempts, git_commit_each_session: gitCommit },
    },
    agent: { type: agent, model },
  };

  // Resources
  const res = {};
  if (maxUsd)  res.max_total_usd    = maxUsd;
  if (maxTok)  res.max_total_tokens = maxTok;
  if (maxHrs)  res.max_total_hours  = maxHrs;
  if (Object.keys(res).length) task.resources = res;

  // Workspace budget
  if (wsId) task.workspace = { workspace_id: wsId, daily_budget_usd: daily || 0 };

  // Validators
  if (!$('f-no-validators').checked) {
    const rows = document.querySelectorAll('.validator-row');
    const validators = [];
    rows.forEach((row, i) => {
      const idxEl = row.id.replace('val-row-','');
      const type = $(`val-type-${idxEl}`)?.value;
      const runs = $(`val-runs-${idxEl}`)?.value;
      const fail = $(`val-fail-${idxEl}`)?.value;
      const cmd  = $(`val-cmd-${idxEl}`)?.value.trim();
      if (!type) return;
      const v = { id: `${type}_${i}`, type, runs, on_fail: fail };
      if (cmd) v.config = { command: cmd };
      validators.push(v);
    });
    if (validators.length) task.milestone_validators = validators;
  }

  // HITL
  if ($('f-hitl-enabled').checked) {
    task.hitl = {
      notification_type:   $('f-hitl-type').value,
      notification_target: $('f-hitl-target').value.trim() || undefined,
      timeout_minutes:     parseInt($('f-hitl-timeout').value) || 30,
      escalation_action:   $('f-hitl-escalation').value,
    };
  }

  return task;
}

// Sync form → YAML textarea
window.syncFormToYaml = () => {
  const task = buildTaskFromForm();
  if (!task) return;
  if (window.jsyaml) {
    $('launch-yaml').value = window.jsyaml.dump(task, { indent: 2, lineWidth: 100 });
  } else {
    $('launch-yaml').value = JSON.stringify(task, null, 2);
  }
};

const EXAMPLES = {
  single: `id: quick-task-001
name: Quick debugging task
prompt: |
  Debug the failing test in tests/test_auth.py.
  Fix the root cause — do not delete the test.
strategy:
  kind: single
agent:
  type: claude_code
  model: claude-opus-4-8
resources:
  max_total_usd: 1.0
  max_total_tokens: 100000`,

  sequential: `id: build-oauth-001
name: Implement OAuth 2.0 Authorization Code + PKCE
prompt: |
  Implement OAuth 2.0 (auth code + PKCE + refresh + revoke) in the FastAPI app.
  All existing tests must pass. No secrets in logs.
strategy:
  kind: sequential
  config:
    max_attempts_per_goal: 3
    git_commit_each_session: true
agent:
  type: claude_code
  model: claude-opus-4-8
milestone_validators:
  - id: tests_pass
    type: test_suite
    runs: after_every_session
    on_fail: pause_for_hitl
    config:
      command: pytest tests/ -q
hitl:
  notification_type: slack
  notification_target: "#eng-alerts"
  timeout_minutes: 30
  escalation_action: approve
resources:
  max_total_usd: 5.0
  max_total_tokens: 500000`,

  pair: `id: refactor-auth-001
name: Refactor auth module — pair programming
prompt: |
  Refactor the auth module to use JWT tokens.
  Replace session cookies. Keep all tests passing.
strategy:
  kind: pair
agent:
  type: claude_code
  model: claude-opus-4-8
resources:
  max_total_usd: 3.0`,

  self_critique: `id: quality-uplift-001
name: Code quality uplift — self-critique
prompt: |
  Improve type coverage and add missing docstrings
  to horizonx/core/. Ruff and mypy must pass.
strategy:
  kind: self_critique
agent:
  type: claude_code
  model: claude-sonnet-4-6
milestone_validators:
  - id: ruff
    type: shell
    runs: after_every_session
    on_fail: pause_for_hitl
    config:
      command: ruff check horizonx/ --quiet
resources:
  max_total_usd: 2.0`,

  monitor: `id: sre-monitor-001
name: SRE monitor — watch error rate
prompt: |
  Watch the application logs. If error rate exceeds 5%
  for 3 consecutive minutes, open a GitHub issue.
strategy:
  kind: monitor
  config:
    poll_interval_seconds: 60
    max_watch_hours: 8
agent:
  type: claude_code
  model: claude-haiku-4-5
resources:
  max_total_usd: 2.0
  max_total_hours: 8.0`,
};

window.loadExample = key => {
  if (EXAMPLES[key]) {
    setLaunchMode('yaml');
    $('launch-yaml').value = EXAMPLES[key];
  }
};

window.submitLaunch = async () => {
  let task;
  if (launchMode === 'form') {
    task = buildTaskFromForm();
    if (!task) return;
  } else {
    const raw = $('launch-yaml').value.trim();
    if (!raw) { alert('Paste a task definition (YAML or JSON)'); return; }
    try { task = window.jsyaml ? window.jsyaml.load(raw) : JSON.parse(raw); }
    catch (e) { try { task = JSON.parse(raw); } catch { alert('Invalid YAML/JSON:\n' + e.message); return; } }
  }

  $('launch-status').innerHTML = '<span class="spinner"></span> Launching…';
  $('launch-btn').disabled = true;
  try {
    const result = await api('/api/runs', { method:'POST', body: JSON.stringify({ task }) });
    $('launch-status').innerHTML = `<span style="color:var(--green);">✓ Started: ${result.run_id}</span>`;
    setTimeout(() => navigate(`#/run/${result.run_id}`), 600);
  } catch (e) {
    $('launch-status').innerHTML = `<span style="color:var(--red);">Error: ${esc(e.message)}</span>`;
    $('launch-btn').disabled = false;
  }
};

// ─────────────────────────────────────────────────────────────────────────────
// Cleanup
// ─────────────────────────────────────────────────────────────────────────────
function clearRunDetail() {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  if (state.pollTimer)   { clearInterval(state.pollTimer); state.pollTimer = null; }
  state.currentRunId = null;
  state.currentSessionId = null;
  const ps = $('panel-stream');
  if (ps) ps.innerHTML = '';
}
