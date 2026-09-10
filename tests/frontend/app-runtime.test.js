'use strict';

/* Executable regression coverage for the browser flows the source-string tests cannot
 * reach: the background check queue, sweep polling, and the Run confirmation.
 *
 * The real page script is evaluated in a vm context over a fake DOM, a fetch that
 * answers from fixtures, and timers the test fires by hand. Nothing touches a network
 * and nothing can write to Sonarr: every "run" is answered from a fixture, so no test
 * here can perform a live deletion.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert');

const APP = path.join(__dirname, '..', '..', 'src', 'assets', 'app.js');
const SOURCE = fs.readFileSync(APP, 'utf8');

const SECTIONS = {
  series: ['series-all', 'series-connected', 'series-unconnected'],
  media: ['media-stats', 'media-presets', 'media-automation', 'media-schedule', 'media-radarr'],
  settings: ['settings-alerts', 'settings-notifications', 'settings-connections',
             'settings-api', 'settings-safety', 'settings-logging'],
  system: ['system-about', 'system-backup', 'system-logs'],
  help: ['help-adding', 'help-connecting', 'help-presets', 'help-monitoring',
         'help-rules', 'help-scheduling'],
};

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.hidden = false;
    this.textContent = '';
    this.className = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.title = '';
    this.returnValue = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.shown = 0;
    this.style = { setProperty() {} };
    this._query = new Map();
    const self = this;
    const classes = () => self.className.split(/\s+/).filter(Boolean);
    this.classList = {
      add(...names) { const set = new Set(classes()); names.forEach((n) => set.add(n)); self.className = [...set].join(' '); },
      remove(...names) { const set = new Set(classes()); names.forEach((n) => set.delete(n)); self.className = [...set].join(' '); },
      toggle(name, force) {
        const set = new Set(classes());
        const want = force === undefined ? !set.has(name) : !!force;
        if (want) set.add(name); else set.delete(name);
        self.className = [...set].join(' ');
        return want;
      },
      contains: (name) => classes().includes(name),
    };
  }
  append(...nodes) { nodes.forEach((node) => { if (node != null) this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  dispatch(type, event) { (this.listeners[type] || []).slice().forEach((fn) => fn(event)); }
  click() {
    if (this.disabled) return;   // a disabled control never fires, in a browser either
    this.dispatch('click', { target: this, stopPropagation() {}, preventDefault() {} });
  }
  close(reason) { this.returnValue = reason; this.dispatch('close'); }
  showModal() { this.shown += 1; }
  scrollIntoView() {}
  focus() {}
  querySelector(selector) {
    if (!this._query.has(selector)) this._query.set(selector, new FakeElement('div'));
    return this._query.get(selector);
  }
  querySelectorAll() { return []; }
  closest() { return null; }
}

function collectText(node) {
  if (!node) return '';
  const own = typeof node.textContent === 'string' ? node.textContent : String(node.textContent ?? '');
  if (!Array.isArray(node.children) || node.children.length === 0) return own;
  return [own, ...node.children.map(collectText)].join(' ');
}

function makeDocument() {
  const elements = new Map();
  const root = new FakeElement('div');
  root.dataset.api = '/api';
  root.dataset.csrf = 'test-csrf';
  elements.set('tv-retention', root);

  const viewButtons = [];
  const heads = [];
  const groups = [];
  Object.entries(SECTIONS).forEach(([section, views]) => {
    const group = new FakeElement('div');
    group.dataset.section = section;
    const head = new FakeElement('button');
    head.dataset.sectionHead = section;
    group._query.set('[data-section-head]', head);
    group._query.set('.fa', new FakeElement('i'));
    heads.push(head);
    views.forEach((view) => {
      const button = new FakeElement('button');
      button.dataset.view = view;
      button.closest = () => group;
      viewButtons.push(button);
    });
    groups.push(group);
  });

  const documentElement = new FakeElement('html');
  return {
    document: {
      hidden: true,
      documentElement,
      getElementById: (id) => {
        if (!elements.has(id)) elements.set(id, new FakeElement('div'));
        return elements.get(id);
      },
      createElement: (tag) => new FakeElement(tag),
      createTextNode: (value) => ({ textContent: value }),
      querySelector: (selector) => {
        const byView = /\.tvr-side \[data-view="([a-z-]+)"\]/.exec(selector);
        if (byView) return viewButtons.find((b) => b.dataset.view === byView[1]) || null;
        const bySection = /\[data-section="([a-z]+)"\] \[data-view\]/.exec(selector);
        if (bySection) return viewButtons.find((b) => b.dataset.view === SECTIONS[bySection[1]][0]) || null;
        return null;
      },
      querySelectorAll: (selector) => {
        if (selector === '.tvr-side [data-view]') return viewButtons.slice();
        if (selector === '.tvr-side [data-section-head]') return heads.slice();
        if (selector === '.tvr-side [data-section]') return groups.slice();
        return [];
      },
      addEventListener() {},
    },
    elements,
  };
}

function snapshotFixture(overrides = {}) {
  const rules = overrides.rulesDisabled
    ? [{ id: 'rule-1', enabled: false, instance_id: 'inst-1', series_id: 1,
        series_title: 'Show One', path: '/tv/show-one' }]
    : [
      { id: 'rule-1', enabled: true, instance_id: 'inst-1', series_id: 1,
        series_title: 'Show One', path: '/tv/show-one', keep_days: 30, combine: 'earliest' },
      { id: 'rule-2', enabled: true, instance_id: 'inst-1', series_id: 2,
        series_title: 'Show Two', path: '/tv/show-two', keep_days: 90, combine: 'earliest' },
    ];
  return {
    version: '0.0.0-test',
    settings: {
      instances: [{ id: 'inst-1', name: 'Sonarr', url: 'http://sonarr.test', api_key: '', enabled: true }],
      rules,
      profiles: [],
      schedule: { enabled: false, test_mode: overrides.testMode === false ? false : true },
      retention: {}, air_dates: {}, automation: {}, alerts: {}, notifications: {}, logging: {},
      state_dir: '/tmp/tvr-test-state',
    },
    health: { rules: {}, instances: { 'inst-1': { reachable: true } } },
    alerts: overrides.alerts || [],
    runs: [],
    plan: overrides.plan || { actionable: 0, trustworthy: false },
    progress: overrides.progress || { running: false },
    stale_rules: overrides.stale_rules || [],
    sync: { synced_at: new Date().toISOString() },
  };
}

function loadPage(setup) {
  const fixtures = setup();
  const { document, elements } = makeDocument();
  const fetchLog = [];
  const confirmCalls = [];
  const intervals = new Map();
  let timerSeq = 1;

  const routes = {
    snapshot: () => fixtures.snapshot,
    series: () => ({ series: fixtures.series || [] }),
    'check-rule': (payload) => (fixtures.checkRule ? fixtures.checkRule(payload)
      : { rule_id: payload.rule_id, state: {}, alerts: [] }),
    progress: () => (fixtures.progress ? fixtures.progress() : { progress: { running: false } }),
    alerts: () => ({ alerts: (fixtures.alerts && fixtures.alerts()) || [] }),
    run: () => (fixtures.run ? fixtures.run()
      : { result: { dry_run: true, planned: 0, deleted: 0, rules: [], duration_seconds: 0, freed_bytes: 0 } }),
  };

  const fetchImpl = async (url, opts) => {
    const payload = JSON.parse(opts.body.get('payload'));
    fetchLog.push({ action: payload.action, payload });
    const body = routes[payload.action] ? await routes[payload.action](payload) : {};
    return { status: 200, json: async () => Object.assign({ ok: true }, body) };
  };

  const sandbox = {
    document,
    window: {
      addEventListener() {},
      confirm: (message) => {
        confirmCalls.push(message);
        return fixtures.confirm ? fixtures.confirm() : false;
      },
      location: { href: '' },
    },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    fetch: fetchImpl,
    setInterval: (fn) => { const id = timerSeq++; intervals.set(id, fn); return id; },
    clearInterval: (id) => { intervals.delete(id); },
    setTimeout: () => timerSeq++,
    clearTimeout() {},
    AbortController,
    URLSearchParams,
    Event: class Event { constructor(type) { this.type = type; } },
    console,
  };
  sandbox.window.document = document;
  vm.createContext(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: 'app.js' });

  async function flush(rounds = 25) {
    for (let round = 0; round < rounds; round += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
  }

  return {
    fetchLog,
    confirmCalls,
    intervals,
    $: (id) => document.getElementById(id),
    flush,
    click: (id) => { document.getElementById(id).click(); },
    fire: (id) => { const fn = intervals.get(id); return fn ? fn() : undefined; },
    notice: () => {
      const box = document.getElementById('tvr-notice');
      return { text: box.textContent, kind: box.className, hidden: box.hidden };
    },
    actions: (name) => fetchLog.filter((entry) => entry.action === name).length,
    elements,
  };
}

test('queued background checks all complete, update the counts, and fetch nothing else', async () => {
  const page = loadPage(() => ({
    snapshot: snapshotFixture({ stale_rules: ['rule-1', 'rule-2'] }),
    checkRule: (payload) => (payload.rule_id === 'rule-2'
      ? { rule_id: 'rule-2', state: { ok: true, label: 'Checked' },
          alerts: [{ scope: 'rule', rule_id: 'rule-2', severity: 'warning', title: 'Nearly ended',
                     detail: 'The series has ended and its window is empty', blocking: false }] }
      : { rule_id: 'rule-1', state: { ok: true, label: 'Checked' }, alerts: [] }),
  }));
  await page.flush();

  assert.equal(page.actions('check-rule'), 2, 'the queue drains past its first completed rule');
  assert.equal(page.actions('stats'), 0, 'counts update without fetching the Stats page');
  assert.equal(page.notice().text, '', 'no error surfaced');

  const badge = page.$('tvr-badge-series-connected');
  assert.equal(badge.hidden, false, 'the alert that arrived with the check is counted');
  assert.equal(String(badge.textContent), '1');
  assert.ok(badge.className.includes('warning'));
  const total = page.$('tvr-alert-total');
  assert.equal(total.hidden, false);
  assert.equal(String(total.textContent), '1 alert');

  assert.equal(page.intervals.size, 1, 'only the heartbeat interval: checks do not start polling');
});

test('sweep polling continues while the sweep runs and stops when it finishes', async () => {
  let running = true;
  const page = loadPage(() => ({
    snapshot: snapshotFixture({ progress: { running: true, phase: 'rules', current: 'rule-1',
                                             done: 1, total: 3, scheduled: true } }),
    progress: () => ({
      health: {},
      progress: { running, phase: 'rules', current: 'rule-1', done: running ? 1 : 3,
                  total: 3, scheduled: true, current_title: 'Show One' },
    }),
  }));
  await page.flush();

  const pollId = [...page.intervals.keys()].pop();
  const banner = page.$('tvr-checking');
  assert.equal(banner.hidden, false, 'a running sweep shows its banner');
  assert.ok(collectText(banner).includes('1 of 3 read'));
  assert.equal(page.actions('progress'), 1);

  await page.fire(pollId);
  await page.flush();
  assert.ok(page.intervals.has(pollId), 'a tick while still running keeps polling');
  assert.equal(banner.hidden, false);
  assert.equal(page.actions('progress'), 2);

  running = false;
  await page.fire(pollId);
  await page.flush();
  assert.equal(page.intervals.has(pollId), false, 'completion clears the interval normally');
  assert.equal(banner.hidden, true, 'and hides the banner');
  assert.equal(page.actions('progress'), 3);
  assert.equal(page.actions('stats'), 0, 'polling never fetches the Stats page');
  assert.equal(page.$('tvr-busy').hidden, true, 'polling is quiet');
});

test('an actionable Run states the actual plan, and accepting it sends exactly one run', async () => {
  const page = loadPage(() => ({
    snapshot: snapshotFixture({
      plan: { actionable: 7, trustworthy: true, delete: 2, delete_bytes: 2048,
              monitor: 1, unmonitor: 3, removals_by_action: { 'delete-series': 1 } },
    }),
    confirm: () => true,
    run: () => ({ result: { dry_run: true, planned: 7, deleted: 0, rules: [],
                            duration_seconds: 1, freed_bytes: 0 } }),
  }));
  await page.flush();

  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 1, 'the confirmation is reached');
  const text = page.confirmCalls[0];
  assert.ok(text.startsWith('Run 2 series now?'));
  assert.ok(text.includes('Scheduled changes:'));
  assert.ok(text.includes('1 series will be deleted from Sonarr, keeping files'));
  assert.ok(text.includes('2 episodes scheduled for deletion (2.0 KiB)'));
  assert.ok(text.includes('1 episode will be set to monitored'));
  assert.ok(text.includes('3 episodes will be set to unmonitored'));
  assert.ok(text.includes('Test mode is on, so this changes nothing'));
  assert.doesNotMatch(text, /This will \d/, 'the summary must read as sentences, not be spliced into one');
  assert.ok(!/will \d+ (series|episodes?) will/.test(text), 'no double verb from two description systems meeting');

  assert.equal(page.actions('run'), 1, 'accepting sends exactly one run request');
  assert.equal(page.actions('stats'), 0);
  assert.equal(page.notice().text, '');
  assert.equal(page.$('tvr-busy').hidden, true, 'the busy overlay settled');
  assert.equal(page.$('tvr-dialog').shown, 1, 'the run report opened');
});

test('cancelling the Run confirmation sends no run request', async () => {
  const page = loadPage(() => ({
    snapshot: snapshotFixture({
      plan: { actionable: 3, trustworthy: true, delete: 3, delete_bytes: 1024 },
    }),
    confirm: () => false,
  }));
  await page.flush();

  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 1);
  assert.equal(page.actions('run'), 0, 'a cancelled confirmation runs nothing');
  assert.equal(page.notice().text, '');
});

test('a blocked Run shows what is stopping it and sends nothing', async () => {
  const page = loadPage(() => ({
    snapshot: snapshotFixture({
      plan: { actionable: 2, trustworthy: true, delete: 2, delete_bytes: 1024 },
      alerts: [{ scope: 'system', severity: 'error', blocking: true, title: 'Sonarr unreachable',
                 detail: 'No instance answered' }],
    }),
  }));
  await page.flush();

  assert.ok(page.$('tvr-run').className.includes('blocked'));
  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 0, 'blocked never reaches a confirmation');
  assert.equal(page.actions('run'), 0);
  assert.equal(page.$('tvr-dialog').shown, 1, 'the reasons are shown instead');
});

test('a Run with no enabled series says so and sends nothing', async () => {
  const page = loadPage(() => ({ snapshot: snapshotFixture({ rulesDisabled: true }) }));
  await page.flush();

  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 0);
  assert.equal(page.actions('run'), 0);
  assert.equal(page.notice().text, 'There are no enabled series ready to run.');
  assert.equal(page.notice().kind, 'bad');
});

test('a Run with nothing scheduled stays disabled rather than promising nothing', async () => {
  const page = loadPage(() => ({
    snapshot: snapshotFixture({ plan: { actionable: 0, trustworthy: true } }),
  }));
  await page.flush();

  const button = page.$('tvr-run');
  assert.equal(button.disabled, true, 'hidden on a complete, current reading');
  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 0, 'a disabled control does nothing');
  assert.equal(page.actions('run'), 0);
});

test('a live Run confirmation says what a real run does', async () => {
  const page = loadPage(() => ({
    snapshot: snapshotFixture({
      testMode: false,
      plan: { actionable: 2, trustworthy: true, delete: 2, delete_bytes: 1024 },
    }),
    confirm: () => false,
  }));
  await page.flush();

  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 1);
  assert.ok(page.confirmCalls[0].includes(
    'This deletes episode files through Sonarr and cannot be undone from here.'));
  assert.equal(page.actions('run'), 0);
});
