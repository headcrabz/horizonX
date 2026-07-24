/* HorizonX Dashboard — vanilla JS SPA */
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  currentRunId: null,
  currentSessionId: null,
  eventSource: null,
  pollTimer: null,
  rightTab: 'goals',
  pinScroll: true,
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function badge(status) {
  return `<span class="badge badge-${status}">${esc(status)}</span>`;
}

function reltime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  const sec = Math.floor((Date.now() - d) / 1000);
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return d.toLocaleDateString();
}

function duration(startIso, endIso) {
  if (!startIso) return '—';
  const end = endIso ? new Date(endIso) : new Date();
  const sec = Math.floor((end - new Date(startIso)) / 1000);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function fmtUSD(n) { return n != null ? `$${Number(n).toFixed(3)}` : '—'; }
function fmtTokens(n) { return n ? (n >= 1000 ? `${(n / 1000).toFixed(1)}K` : n) : '0'; }

function evClass(type) {
  const prefix = type.split('.')[0];
  const map = { run: 'ev-run', session: 'ev-session', step: 'ev-step', goal: 'ev-goal',
    validator: 'ev-validator', spin: 'ev-spin', hitl: 'ev-hitl', budget: 'ev-budget',
    summary: 'ev-summary', fork: 'ev-fork', retry: 'ev-retry' };
  return map[prefix] || 'ev-step';
}

function stepTypeColor(type) {
  const m = { thought: 'text-slate-300', reasoning: 'text-purple-300', tool_call: 'text-blue-300',
    observation: 'text-green-300', file_change: 'text-yellow-300', error: 'text-red-300',
    hitl_pause: 'text-yellow-400', hitl_decision: 'text-yellow-300', spin: 'text-orange-300',
    usage: 'text-slate-500', system: 'text-slate-600' };
  return m[type] || 'text-slate-400';
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

function navigate(hash) {
  window.location.hash = hash;
}

window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', () => { route(); });

function route() {
  const hash = window.location.hash || '#/';
  if (hash.startsWith('#/run/')) {
    showRunDetail(hash.slice(6));
  } else if (hash === '#/launch') {
    showLaunch();
  } else {
    showRunsList();
  }
}

// ---------------------------------------------------------------------------
// View: Runs list
// ---------------------------------------------------------------------------

function showRunsList() {
  clearRunDetail();
  $('view-runs').style.display = '';
  $('view-run-detail').style.display = 'none';
  $('view-launch').style.display = 'none';
  $('nav-runs').classList.add('text-white');
  $('nav-launch').classList.remove('text-white');
  loadRunsList();
}

async function loadRunsList() {
  const status = $('runs-filter').value || undefined;
  const qs = status ? `?status=${status}&limit=100` : '?limit=100';
  try {
    const runs = await api('/api/runs' + qs);
    renderRunsTable(runs);
  } catch (e) {
    $('runs-tbody').innerHTML = `<tr><td colspan="8" class="text-red-400 px-4 py-3">Error: ${esc(e.message)}</td></tr>`;
  }
}

function renderRunsTable(runs) {
  if (!runs.length) {
    $('runs-tbody').innerHTML = `<tr><td colspan="8" class="text-center text-slate-500 px-4 py-8">No runs yet. <a href="#/launch" class="text-indigo-400 hover:underline">Launch one →</a></td></tr>`;
    return;
  }
  $('runs-tbody').innerHTML = runs.map(r => `
    <tr class="border-b border-slate-800 hover:bg-slate-900 cursor-pointer" onclick="navigate('#/run/${esc(r.id)}')">
      <td class="px-4 py-3 font-mono text-xs text-slate-400">${esc(r.id.slice(0, 14))}…</td>
      <td class="px-4 py-3 font-medium">${esc(r.task_name || r.task_id || '—')}</td>
      <td class="px-4 py-3">${badge(r.status)}</td>
      <td class="px-4 py-3 text-slate-400 text-xs">${reltime(r.started_at)}</td>
      <td class="px-4 py-3 text-slate-400 text-xs">${duration(r.started_at, r.completed_at)}</td>
      <td class="px-4 py-3 text-right text-slate-400">${r.sessions_count}</td>
      <td class="px-4 py-3 text-right text-slate-400">${r.steps_count}</td>
      <td class="px-4 py-3 text-right text-emerald-400">${fmtUSD(r.usd)}</td>
    </tr>
  `).join('');
}

// ---------------------------------------------------------------------------
// View: Run detail
// ---------------------------------------------------------------------------

async function showRunDetail(runId) {
  clearRunDetail();
  $('view-runs').style.display = 'none';
  $('view-run-detail').style.display = '';
  $('view-launch').style.display = 'none';
  $('nav-runs').classList.remove('text-white');
  $('nav-launch').classList.remove('text-white');

  state.currentRunId = runId;

  try {
    const run = await api(`/api/runs/${runId}`);
    renderRunHeader(run);
    renderHitlBanner(run);
  } catch (e) {
    $('run-header').innerHTML = `<span class="text-red-400">Error loading run: ${esc(e.message)}</span>`;
    return;
  }

  loadSessions(runId);
  connectEventSource(runId);
  // Poll run status while it's active
  state.pollTimer = setInterval(async () => {
    try {
      const run = await api(`/api/runs/${runId}`);
      renderRunHeader(run);
      renderHitlBanner(run);
      if (['completed', 'failed', 'aborted', 'timed_out'].includes(run.status)) {
        clearInterval(state.pollTimer);
      }
    } catch { /* ignore */ }
  }, 5000);

  // Load right-panel data
  setRightTab(state.rightTab);
}

function renderRunHeader(run) {
  const cm = run.cumulative || {};
  $('run-breadcrumb').textContent = run.task?.name || run.id;
  $('run-status-badge').innerHTML = badge(run.status) +
    (run.status === 'running' ? ' <span class="pulse-dot inline-block w-2 h-2 rounded-full bg-blue-400 ml-1"></span>' : '');
  $('run-metrics').innerHTML = `
    <span class="text-slate-400">tokens in</span> <span class="text-slate-200">${fmtTokens(cm.tokens_in)}</span>
    <span class="mx-2 text-slate-700">·</span>
    <span class="text-slate-400">out</span> <span class="text-slate-200">${fmtTokens(cm.tokens_out)}</span>
    <span class="mx-2 text-slate-700">·</span>
    <span class="text-slate-400">cost</span> <span class="text-emerald-400">${fmtUSD(cm.usd)}</span>
    <span class="mx-2 text-slate-700">·</span>
    <span class="text-slate-400">sessions</span> <span class="text-slate-200">${cm.sessions_count ?? 0}</span>
    <span class="mx-2 text-slate-700">·</span>
    <span class="text-slate-400">steps</span> <span class="text-slate-200">${cm.steps_count ?? 0}</span>
    <span class="mx-2 text-slate-700">·</span>
    <span class="text-slate-400">wall</span> <span class="text-slate-200">${duration(run.started_at, run.completed_at)}</span>
  `;
}

function renderHitlBanner(run) {
  const banner = $('hitl-banner');
  if (run.status === 'paused_hitl') {
    banner.style.display = '';
    banner.innerHTML = `
      <div class="flex items-center gap-4">
        <span class="text-yellow-300 font-semibold">⏸ Run paused — waiting for human input</span>
        <button onclick="resolveHitl('${esc(run.id)}','approve')" class="px-3 py-1 bg-green-700 hover:bg-green-600 text-white rounded text-sm">Approve</button>
        <button onclick="showModifyHitl('${esc(run.id)}')" class="px-3 py-1 bg-yellow-700 hover:bg-yellow-600 text-white rounded text-sm">Modify</button>
        <button onclick="resolveHitl('${esc(run.id)}','abort')" class="px-3 py-1 bg-red-800 hover:bg-red-700 text-white rounded text-sm">Abort</button>
      </div>
      <div id="hitl-modify-form" style="display:none" class="mt-3 flex gap-2">
        <input id="hitl-instruction" type="text" placeholder="Instruction for the agent…"
          class="flex-1 bg-slate-900 border border-slate-700 rounded px-3 py-1 text-sm text-white" />
        <button onclick="resolveHitlModify('${esc(run.id)}')" class="px-3 py-1 bg-yellow-600 hover:bg-yellow-500 text-white rounded text-sm">Send</button>
      </div>
    `;
  } else {
    banner.style.display = 'none';
  }
}

window.showModifyHitl = () => { $('hitl-modify-form').style.display = ''; };

window.resolveHitl = async (runId, action, instruction = '') => {
  try {
    await api(`/api/runs/${runId}/hitl`, {
      method: 'POST',
      body: JSON.stringify({ action, instruction, operator: 'dashboard' }),
    });
    $('hitl-banner').innerHTML = '<span class="text-green-400">Decision sent. Waiting for agent to resume…</span>';
  } catch (e) {
    alert('Error: ' + e.message);
  }
};

window.resolveHitlModify = (runId) => {
  const instr = $('hitl-instruction').value.trim();
  resolveHitl(runId, 'modify', instr);
};

// ---------------------------------------------------------------------------
// Sessions panel
// ---------------------------------------------------------------------------

async function loadSessions(runId) {
  try {
    const sessions = await api(`/api/runs/${runId}/sessions`);
    renderSessions(sessions, runId);
    if (sessions.length > 0 && !state.currentSessionId) {
      // Auto-select most recent session
      const latest = sessions[sessions.length - 1];
      selectSession(latest.id, runId);
    }
  } catch (e) {
    $('sessions-list').innerHTML = `<div class="text-red-400 text-xs px-3">${esc(e.message)}</div>`;
  }
}

function renderSessions(sessions, runId) {
  if (!sessions.length) {
    $('sessions-list').innerHTML = '<div class="text-slate-500 text-xs px-3 py-2">No sessions yet</div>';
    return;
  }
  $('sessions-list').innerHTML = sessions.map((s, i) => `
    <div class="session-item${s.id === state.currentSessionId ? ' active' : ''}"
         id="sess-${esc(s.id)}"
         onclick="selectSession('${esc(s.id)}','${esc(runId)}')">
      <div class="flex items-center justify-between">
        <span class="text-slate-300 font-medium text-xs">#${s.sequence_index + 1}</span>
        <span class="badge badge-${s.status} text-xs">${esc(s.status)}</span>
      </div>
      <div class="text-slate-500 text-xs mt-0.5">${s.steps_count} steps · ${fmtTokens(s.tokens_used)} tok</div>
    </div>
  `).reverse().join('');
}

window.selectSession = async (sessionId, runId) => {
  state.currentSessionId = sessionId;
  // Update active styling
  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
  const el = $(`sess-${sessionId}`);
  if (el) el.classList.add('active');

  $('steps-panel').innerHTML = '<div class="text-slate-500 text-xs p-3">Loading steps…</div>';
  try {
    const steps = await api(`/api/runs/${runId}/sessions/${sessionId}/steps?limit=500`);
    renderSteps(steps);
  } catch (e) {
    $('steps-panel').innerHTML = `<div class="text-red-400 text-xs p-3">${esc(e.message)}</div>`;
  }
};

function renderSteps(steps) {
  if (!steps.length) {
    $('steps-panel').innerHTML = '<div class="text-slate-500 text-xs p-3">No steps recorded</div>';
    return;
  }
  $('steps-panel').innerHTML = steps.map(s => {
    const snippet = extractSnippet(s);
    return `
      <div class="border-b border-slate-800 px-3 py-2">
        <div class="flex items-center gap-2 mb-1">
          <span class="${stepTypeColor(s.type)} text-xs font-mono font-semibold">${esc(s.type)}</span>
          ${s.tool_name ? `<span class="text-blue-400 text-xs font-mono">${esc(s.tool_name)}</span>` : ''}
          <span class="text-slate-600 text-xs ml-auto">#${s.sequence}</span>
        </div>
        ${snippet ? `<div class="step-content">${esc(snippet)}</div>` : ''}
      </div>
    `;
  }).join('');
}

function extractSnippet(step) {
  const c = step.content || {};
  if (c.text) return c.text.slice(0, 300);
  if (c.command) return c.command;
  if (c.output) return String(c.output).slice(0, 300);
  if (c.patch) return c.patch.slice(0, 300);
  return JSON.stringify(c).slice(0, 200);
}

// ---------------------------------------------------------------------------
// Live event stream
// ---------------------------------------------------------------------------

function connectEventSource(runId) {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = new EventSource(`/api/runs/${runId}/events`);
  state.eventSource.onmessage = (e) => appendEvent(JSON.parse(e.data));
  // Named events come as event: <type>
  for (const evType of [
    'run.started','run.completed','run.failed','run.paused_hitl',
    'session.started','session.completed','session.timeout',
    'step.recorded','goal.in_progress','goal.done','goal.failed',
    'validator.passed','validator.failed','validator.paused',
    'spin.detected','hitl.requested','hitl.resolved',
    'budget.threshold','summary.created','fork.created','fork.merged','retry.attempted',
  ]) {
    state.eventSource.addEventListener(evType, (e) => appendEvent(JSON.parse(e.data)));
  }
  state.eventSource.onerror = () => {
    // SSE will auto-reconnect; suppress noise
  };
}

function appendEvent(event) {
  const container = $('event-stream');
  const el = document.createElement('div');
  el.className = `event-entry ${evClass(event.type)}`;
  const payload = event.payload && Object.keys(event.payload).length
    ? ' — ' + JSON.stringify(event.payload).slice(0, 120)
    : '';
  el.innerHTML = `<span class="text-slate-500 text-xs">${new Date(event.timestamp).toLocaleTimeString()}</span>
    <span class="text-slate-300 ml-2 font-medium">${esc(event.type)}</span>
    <span class="text-slate-500 text-xs">${esc(payload)}</span>`;
  if (state.pinScroll) {
    container.prepend(el);
  } else {
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  // Refresh sessions list on session events
  if (event.type.startsWith('session.') || event.type === 'step.recorded') {
    if (state.currentRunId) {
      loadSessions(state.currentRunId);
    }
  }
  // Reload right panel on goal/validator events
  if (event.type.startsWith('goal.') || event.type.startsWith('validator.')) {
    if (state.currentRunId) loadRightTab(state.currentRunId);
  }
  // Show HITL banner
  if (event.type === 'hitl.requested') {
    api(`/api/runs/${state.currentRunId}`).then(run => renderHitlBanner(run)).catch(() => {});
  }
  if (event.type === 'hitl.resolved') {
    $('hitl-banner').style.display = 'none';
  }
}

// ---------------------------------------------------------------------------
// Right panel (Goals / Validations / HITL)
// ---------------------------------------------------------------------------

window.setRightTab = (tab) => {
  state.rightTab = tab;
  ['goals', 'validations', 'hitl'].forEach(t => {
    $(`tab-${t}`).classList.toggle('active', t === tab);
    $(`panel-${t}`).style.display = t === tab ? '' : 'none';
  });
  if (state.currentRunId) loadRightTab(state.currentRunId);
};

async function loadRightTab(runId) {
  if (state.rightTab === 'goals') await loadGoals(runId);
  else if (state.rightTab === 'validations') await loadValidations(runId);
  else await loadHitlHistory(runId);
}

async function loadGoals(runId) {
  try {
    const goals = await api(`/api/runs/${runId}/goals`);
    renderGoals(goals);
  } catch (e) {
    $('panel-goals').innerHTML = `<div class="text-red-400 text-xs p-3">${esc(e.message)}</div>`;
  }
}

function renderGoals(goals) {
  if (!goals.length) {
    $('panel-goals').innerHTML = '<div class="text-slate-500 text-xs p-3">No goals recorded yet</div>';
    return;
  }
  const byId = Object.fromEntries(goals.map(g => [g.id, g]));
  const roots = goals.filter(g => !g.parent_id || !byId[g.parent_id]);

  function renderNode(goal, depth = 0) {
    const statusIcon = { done: '✓', failed: '✗', in_progress: '⟳', pending: '○', blocked: '⊘', skipped: '⊝' }[goal.status] || '?';
    const statusColor = { done: 'text-green-400', failed: 'text-red-400', in_progress: 'text-blue-400', pending: 'text-slate-500', blocked: 'text-orange-400', skipped: 'text-slate-600' }[goal.status] || 'text-slate-400';
    const children = goals.filter(g => g.parent_id === goal.id);
    const indent = depth * 16;
    const hasChildren = children.length > 0;
    return `
      <div class="goal-node" style="padding-left:${indent}px">
        ${hasChildren ? `<details${goal.status === 'in_progress' ? ' open' : ''}>
          <summary class="flex items-center gap-2 py-1 select-none">
            <span class="${statusColor} font-bold w-4 text-center">${statusIcon}</span>
            <span class="text-slate-200 text-xs">${esc(goal.name)}</span>
            ${goal.progress_pct ? `<span class="text-slate-500 text-xs ml-auto">${Math.round(goal.progress_pct)}%</span>` : ''}
          </summary>
          <div>${children.map(c => renderNode(c, depth + 1)).join('')}</div>
        </details>` : `
          <div class="flex items-center gap-2 py-1">
            <span class="${statusColor} font-bold w-4 text-center">${statusIcon}</span>
            <span class="text-slate-300 text-xs">${esc(goal.name)}</span>
            ${goal.progress_pct ? `<span class="text-slate-500 text-xs ml-auto">${Math.round(goal.progress_pct)}%</span>` : ''}
          </div>`}
      </div>
    `;
  }

  $('panel-goals').innerHTML = `<div class="p-2">${roots.map(r => renderNode(r)).join('')}</div>`;
}

async function loadValidations(runId) {
  try {
    const validations = await api(`/api/runs/${runId}/validations`);
    renderValidations(validations);
  } catch (e) {
    $('panel-validations').innerHTML = `<div class="text-red-400 text-xs p-3">${esc(e.message)}</div>`;
  }
}

function renderValidations(vals) {
  if (!vals.length) {
    $('panel-validations').innerHTML = '<div class="text-slate-500 text-xs p-3">No validations yet</div>';
    return;
  }
  $('panel-validations').innerHTML = vals.map(v => {
    const decColor = v.decision === 'continue' ? 'text-green-400' : v.decision === 'abort' ? 'text-red-400' : 'text-yellow-400';
    return `
      <div class="border-b border-slate-800 px-3 py-2">
        <div class="flex items-center gap-2">
          <span class="${decColor} text-xs font-semibold">${esc(v.decision)}</span>
          <span class="text-slate-300 text-xs">${esc(v.validator)}</span>
          ${v.score != null ? `<span class="text-slate-400 text-xs ml-auto">${Number(v.score).toFixed(2)}</span>` : ''}
        </div>
        <div class="text-slate-500 text-xs mt-0.5 truncate">${esc(v.reason)}</div>
      </div>
    `;
  }).join('');
}

async function loadHitlHistory(runId) {
  try {
    const events = await api(`/api/runs/${runId}/hitl`);
    renderHitlHistory(events);
  } catch (e) {
    $('panel-hitl').innerHTML = `<div class="text-red-400 text-xs p-3">${esc(e.message)}</div>`;
  }
}

function renderHitlHistory(events) {
  if (!events.length) {
    $('panel-hitl').innerHTML = '<div class="text-slate-500 text-xs p-3">No HITL events</div>';
    return;
  }
  $('panel-hitl').innerHTML = events.map(e => `
    <div class="border-b border-slate-800 px-3 py-2">
      <div class="flex items-center gap-2">
        <span class="text-yellow-400 text-xs font-semibold">HITL</span>
        <span class="text-slate-300 text-xs truncate">${esc(e.trigger)}</span>
        ${e.decision ? `<span class="text-green-400 text-xs ml-auto">${esc(e.decision)}</span>` : '<span class="text-yellow-500 text-xs ml-auto">pending</span>'}
      </div>
      ${e.instruction ? `<div class="text-slate-500 text-xs mt-0.5">${esc(e.instruction)}</div>` : ''}
    </div>
  `).join('');
}

// ---------------------------------------------------------------------------
// View: Launch
// ---------------------------------------------------------------------------

function showLaunch() {
  clearRunDetail();
  $('view-runs').style.display = 'none';
  $('view-run-detail').style.display = 'none';
  $('view-launch').style.display = '';
  $('nav-runs').classList.remove('text-white');
  $('nav-launch').classList.add('text-white');
}

window.submitLaunch = async () => {
  const yaml = window.jsyaml;
  const raw = $('launch-yaml').value.trim();
  if (!raw) { alert('Paste a task definition (YAML or JSON)'); return; }

  let task;
  try {
    task = yaml ? yaml.load(raw) : JSON.parse(raw);
  } catch (e) {
    try { task = JSON.parse(raw); } catch { alert('Invalid YAML/JSON: ' + e.message); return; }
  }

  $('launch-status').textContent = 'Launching…';
  $('launch-btn').disabled = true;

  try {
    const result = await api('/api/runs', { method: 'POST', body: JSON.stringify({ task }) });
    $('launch-status').textContent = `Started run: ${result.run_id}`;
    setTimeout(() => navigate(`#/run/${result.run_id}`), 500);
  } catch (e) {
    $('launch-status').innerHTML = `<span class="text-red-400">Error: ${esc(e.message)}</span>`;
    $('launch-btn').disabled = false;
  }
};

// ---------------------------------------------------------------------------
// Cleanup
// ---------------------------------------------------------------------------

function clearRunDetail() {
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  state.currentRunId = null;
  state.currentSessionId = null;
  const stream = $('event-stream');
  if (stream) stream.innerHTML = '';
  const steps = $('steps-panel');
  if (steps) steps.innerHTML = '<div class="text-slate-500 text-xs p-3">Select a session</div>';
}
