'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function element() {
  return {
    style: {},
    innerHTML: '',
    textContent: '',
    checked: true,
    scrollTop: 0,
    children: [],
    setAttribute() {},
    querySelectorAll() { return []; },
    addEventListener() {},
    appendChild(child) { this.children.push(child); },
    prepend(child) { this.children.unshift(child); },
    classList: { toggle() {}, remove() {} },
  };
}

const elements = new Map();
const sourceUrls = [];
const fetchUrls = [];
let initialTimelineFetchFails = false;
let intervalStarts = 0;
const getElement = id => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};

const pageEvents = Array.from({ length: 100 }, (_, index) => ({
  sequence: index + 1,
  id: `event-${index + 1}`,
  type: 'step.recorded',
  timestamp: '2026-08-06T12:00:00Z',
  entities: { run_id: 'timeline-run' },
}));

const context = {
  console,
  URL,
  Date,
  JSON,
  Math,
  Set,
  Map,
  Promise,
  localStorage: { getItem() { return null; }, setItem() {} },
  document: {
    documentElement: { setAttribute() {} },
    getElementById: getElement,
    querySelectorAll() { return []; },
    querySelector() { return null; },
    createElement: element,
  },
  window: { addEventListener() {}, location: { hash: '#/' } },
  EventSource: class {
    constructor(url) { sourceUrls.push(url); }
    close() {}
    addEventListener() {}
  },
  setInterval() { intervalStarts += 1; return intervalStarts; },
  clearInterval() {},
  setTimeout() { return 1; },
  clearTimeout() {},
  alert() {},
  confirm() { return true; },
  fetch: async url => {
    fetchUrls.push(url);
    const parsed = new URL(url, 'http://dashboard.test');
    const after = parsed.searchParams.get('after');
    if (parsed.pathname === '/api/runs/timeline-run') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({
          id: 'timeline-run', status: 'running', task: { name: 'Timeline' }, cumulative: {},
        }),
      };
    }
    if (parsed.pathname === '/api/runs/timeline-run/timeline' && initialTimelineFetchFails) {
      return { ok: false, status: 503, statusText: 'Unavailable', json: async () => ({}) };
    }
    if (parsed.pathname === '/api/runs/timeline-run/timeline/1001') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({
          sequence: 1001,
          id: 'event-1001',
          type: 'recovery.planned',
          timestamp: '2026-08-06T12:00:00Z',
          entities: { run_id: 'timeline-run' },
          payload: { secret: 'selected-detail-secret', tool_text: 'selected detail only' },
        }),
      };
    }
    if (parsed.pathname === '/api/runs/timeline-run/timeline/playback') {
      return {
        ok: true,
        status: 200,
        statusText: 'OK',
        json: async () => ({ run_id: 'timeline-run', sequence: 1001, graph: null }),
      };
    }
    if (parsed.pathname !== '/api/runs/timeline-run/timeline') {
      return { ok: true, status: 200, statusText: 'OK', json: async () => [] };
    }
    return {
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => after === '0'
        ? {
          run_id: 'timeline-run', run_status: 'running', events: pageEvents,
          next_after: 100, latest_sequence: 1000,
        }
        : { run_id: 'timeline-run', run_status: 'running', events: [], next_after: null },
    };
  },
};
context.globalThis = context;

const appSource = fs.readFileSync(
  path.join(__dirname, '..', 'horizonx', 'dashboard', 'static', 'app.js'), 'utf8',
);
vm.runInNewContext(
  `${appSource}\nglobalThis.__timelineTest = { state, loadTimelinePage, loadRunDetailData, appendEvent, connectEventSource, selectTimelineEvent, renderTimelineStatus };`,
  context,
);

async function main() {
  const app = context.__timelineTest;
  app.state.currentRunId = 'timeline-run';
  await app.loadTimelinePage('timeline-run', { reset: true });
  assert.strictEqual(app.state.timelineNextAfter, 100);
  assert.strictEqual(app.state.timelineLiveAfter, 1000);
  app.connectEventSource('timeline-run');
  assert.strictEqual(sourceUrls.at(-1), '/api/runs/timeline-run/events?cursor=1000');

  app.appendEvent({
    sequence: 1001,
    id: 'event-1001',
    type: 'recovery.planned',
    timestamp: '2026-08-06T12:00:00Z',
    run_id: 'timeline-run',
    payload: {
      hidden: 'must not enter the timeline list',
      secret: 'live-stream-secret',
      tool_text: 'live tool output must remain private',
    },
  });

  assert.strictEqual(app.state.timelineEvents.at(-1).sequence, 1001);
  assert.strictEqual(app.state.timelineLiveAfter, 1001);
  assert.strictEqual(app.state.timelineNextAfter, 100);
  assert.strictEqual(app.state.timelineEvents.length, 101);
  assert.ok(!app.state.timelineEvents.some(event => event.sequence > 100 && event.sequence < 1001));
  assert.ok(app.state.timelineEvents.every(event => !Object.hasOwn(event, 'payload')));
  const liveRow = getElement('panel-stream').children[0].innerHTML;
  assert.ok(!liveRow.includes('live-stream-secret'));
  assert.ok(!liveRow.includes('live tool output must remain private'));
  await app.selectTimelineEvent(1001);
  assert.ok(getElement('timeline-detail-body').innerHTML.includes('selected-detail-secret'));

  app.state.centerTab = 'goals';
  app.state.timelineSelectedSequence = null;
  app.appendEvent({
    sequence: 1002,
    id: 'event-1002',
    type: 'goals.graph_changed',
    timestamp: '2026-08-06T12:00:00Z',
    run_id: 'timeline-run',
    payload: {},
  });
  assert.ok(fetchUrls.includes('/api/runs/timeline-run/goals'));

  app.state.timelineRunStatus = 'completed';
  app.renderTimelineStatus();
  assert.strictEqual(
    getElement('timeline-status').textContent,
    'Terminal · completed · recovery recorded',
  );

  app.state.timelineSelectedSequence = 1002;
  getElement('timeline-detail-sequence').textContent = '#1002';
  getElement('timeline-detail-body').innerHTML = 'stale detail';
  context.window.returnToCurrentGraph();
  assert.strictEqual(app.state.timelineSelectedSequence, null);
  assert.strictEqual(getElement('timeline-playback-meta').textContent, 'Current graph');
  assert.strictEqual(getElement('timeline-detail-sequence').textContent, '—');
  assert.ok(getElement('timeline-detail-body').innerHTML.includes('Select an event'));

  const sourcesBeforeFailedInitialLoad = sourceUrls.length;
  const intervalsBeforeFailedInitialLoad = intervalStarts;
  const sideRequestsBeforeFailedInitialLoad = fetchUrls.length;
  initialTimelineFetchFails = true;
  await app.loadRunDetailData('timeline-run');
  assert.strictEqual(sourceUrls.length, sourcesBeforeFailedInitialLoad);
  assert.strictEqual(intervalStarts, intervalsBeforeFailedInitialLoad + 1);
  assert.ok(fetchUrls.slice(sideRequestsBeforeFailedInitialLoad).includes('/api/runs/timeline-run/sessions'));
  assert.ok(fetchUrls.slice(sideRequestsBeforeFailedInitialLoad).includes('/api/runs/timeline-run/spin-reports'));
  assert.ok(fetchUrls.slice(sideRequestsBeforeFailedInitialLoad).includes('/api/runs/timeline-run/hitl'));

  initialTimelineFetchFails = false;
  await context.window.retryTimeline('timeline-run');
  assert.strictEqual(sourceUrls.at(-1), '/api/runs/timeline-run/events?cursor=1000');
  assert.strictEqual(intervalStarts, intervalsBeforeFailedInitialLoad + 1);
  assert.strictEqual(
    fetchUrls.slice(sideRequestsBeforeFailedInitialLoad).filter(url =>
      ['/sessions', '/spin-reports', '/hitl'].some(suffix => url.endsWith(suffix))
    ).length,
    3,
  );
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
