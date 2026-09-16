'use strict';

/* Executable regression coverage for the browser flows the source-string tests cannot
 * reach: the background check queue, sweep polling, and the Run confirmation.
 *
 * The real page is evaluated as a module graph in a vm context over a fake DOM, a fetch
 * that answers from fixtures, and timers the test fires by hand. Nothing touches a
 * network and nothing can write to Sonarr: every "run" is answered from a fixture, so no
 * test here can perform a live deletion.
 *
 * The entry is linked and evaluated the way the browser does it — imports resolved
 * relative to the importing file, each module instantiated once per page — so an import
 * the release namespace could not satisfy fails here rather than in a browser. This
 * needs `--experimental-vm-modules`; `tools/check-on-host.sh` passes it.
 */

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert');

const ASSETS = path.join(__dirname, '..', '..', 'src', 'assets');
const APP = path.join(ASSETS, 'app.js');

/* Link and evaluate the entry in `context`, resolving relative imports against the
 * assets directory. Each page gets its own module registry: a module instantiated once
 * per context is what keeps one page's state out of the next one's.
 */
async function evaluateGraph(context) {
  assert.equal(typeof vm.SourceTextModule, 'function',
    'run node with --experimental-vm-modules: the page is a module graph, not a script');

  const registry = new Map();

  const load = (file) => {
    const resolved = path.resolve(file);
    if (registry.has(resolved)) return registry.get(resolved);
    const module = new vm.SourceTextModule(fs.readFileSync(resolved, 'utf8'), {
      context,
      identifier: resolved,
      initializeImportMeta(meta) { meta.url = `file://${resolved.replace(/\\/g, '/')}`; },
    });
    registry.set(resolved, module);
    return module;
  };

  const linker = (specifier, referencing) => {
    const from = path.basename(referencing.identifier);
    assert.ok(specifier.startsWith('./') || specifier.startsWith('../'),
      `${from} imports "${specifier}": the page ships no bare specifiers`);
    const target = path.resolve(path.dirname(referencing.identifier), specifier);
    assert.ok(target.startsWith(ASSETS + path.sep),
      `${from} imports "${specifier}", which escapes the assets directory`);
    assert.ok(fs.existsSync(target),
      `${from} imports "${specifier}", which is not a shipped asset`);
    return load(target);
  };

  const entry = load(APP);
  await entry.link(linker);
  await entry.evaluate();
}

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
        series_title: 'Show One', path: '/tv/show-one', keep_days: 30, combine: 'any' },
      { id: 'rule-2', enabled: true, instance_id: 'inst-1', series_id: 2,
        series_title: 'Show Two', path: '/tv/show-two', keep_days: 90, combine: 'any' },
    ];
  return {
    version: '0.0.0-test',
    settings: {
      instances: [{ id: 'inst-1', name: 'Sonarr', url: 'http://sonarr.test', api_key: '', enabled: true }],
      rules,
      profiles: [],
      schedule: { enabled: false, test_mode: overrides.testMode === false ? false : true },
      retention: {}, air_dates: {}, automation: {}, alerts: {}, notifications: {}, logging: {},
      // Arrives masked, exactly as `core.redact` sends it.
      tmdb: { api_key: overrides.tmdbKey === undefined ? '********' : overrides.tmdbKey },
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

async function loadPage(setup) {
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
    // The worker answers a save with the stored document, redacted again.
    settings: (payload) => (fixtures.failSettings ? { ok: false, error: 'Refused.' } : {
      settings: Object.assign({}, payload.settings, {
        tmdb: { api_key: (payload.settings.tmdb || {}).api_key ? '********' : '' },
      }),
      schedule_text: 'Daily at 03:30',
    }),
  };

  const fetchImpl = async (url, opts) => {
    const payload = JSON.parse(opts.body.get('payload'));
    fetchLog.push({ action: payload.action, payload });
    const body = routes[payload.action] ? await routes[payload.action](payload) : {};
    // `ok` defaults to true but a fixture may refuse: the page treats `ok: false` as a
    // rejection, and what it does with a refused save is worth being able to test.
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
  const context = vm.createContext(sandbox);
  await evaluateGraph(context);

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
    change: (id) => {
      const node = document.getElementById(id);
      node.dispatch('change', { target: node, stopPropagation() {}, preventDefault() {} });
    },
    sent: (name) => fetchLog.filter((entry) => entry.action === name).map((entry) => entry.payload),
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
  const page = await loadPage(() => ({
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
  const page = await loadPage(() => ({
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
  const page = await loadPage(() => ({
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

test('a run that only removes queued series reports the removal, not "0 files deleted"', async () => {
  // Removals are a separate list from the retention pass's own deleted/freed_bytes
  // counters. A run with nothing to delete under retention but real series removed used
  // to headline "0 files deleted" even though whole series, files and all, had just gone.
  const page = await loadPage(() => ({
    snapshot: snapshotFixture({
      testMode: false,
      plan: { actionable: 3, trustworthy: true, removals_by_action: { 'delete-series-files': 3 } },
    }),
    confirm: () => true,
    run: () => ({ result: {
      dry_run: false, planned: 0, deleted: 0, freed_bytes: 0, duration_seconds: 2, rules: [],
      removals: [
        { rule_id: 'r1', series_title: 'Below Deck Sailing Yacht', action: 'delete-series-files',
          label: 'Ask Sonarr to delete the series and its files', ok: true, dry_run: false, error: '' },
        { rule_id: 'r2', series_title: "That's My Jam", action: 'delete-series-files',
          label: 'Ask Sonarr to delete the series and its files', ok: true, dry_run: false, error: '' },
        { rule_id: 'r3', series_title: 'Weakest Link', action: 'delete-series-files',
          label: 'Ask Sonarr to delete the series and its files', ok: true, dry_run: false, error: '' },
      ],
    } }),
  }));
  await page.flush();

  page.click('tvr-run');
  await page.flush();

  const text = collectText(page.$('tvr-dialog-body'));
  assert.ok(text.includes('3 series removed'), 'the headline must say what actually happened');
  assert.ok(!text.includes('0 files deleted'),
    'must not report "0 files deleted" when the retention pass never had anything to consider');
});

test('cancelling the Run confirmation sends no run request', async () => {
  const page = await loadPage(() => ({
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
  const page = await loadPage(() => ({
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
  const page = await loadPage(() => ({ snapshot: snapshotFixture({ rulesDisabled: true }) }));
  await page.flush();

  page.click('tvr-run');
  await page.flush();

  assert.equal(page.confirmCalls.length, 0);
  assert.equal(page.actions('run'), 0);
  assert.equal(page.notice().text, 'There are no enabled series ready to run.');
  assert.equal(page.notice().kind, 'bad');
});

test('a Run with nothing scheduled stays disabled rather than promising nothing', async () => {
  const page = await loadPage(() => ({
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
  const page = await loadPage(() => ({
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

/* The two contracts `settings.js` carries that no source-string test can see. Both are
 * silent when broken: a save that posts one panel loses whatever another held, and a
 * masked key echoed back as its mask overwrites the stored credential with asterisks.
 * Neither produces an error, and both survive a syntax check and a purity check.
 */

test('a save posts the whole settings document, not the panel that was edited', async () => {
  const page = await loadPage(() => ({ snapshot: snapshotFixture() }));
  await page.flush();

  // Change one control in the schedule panel, which saves itself on change.
  page.$('tvr-schedule-enabled').checked = true;
  page.$('tvr-freq').value = 'daily';
  page.change('tvr-schedule-enabled');
  await page.flush();

  const saves = page.sent('settings');
  assert.equal(saves.length, 1, 'the schedule panel saves itself the moment it changes');

  const posted = saves[0].settings;
  assert.equal(posted.schedule.enabled, true, 'the edited panel is in the payload');

  // Everything the other panels hold has to travel with it. These come from the fixture
  // and were never touched by the schedule view.
  assert.equal(posted.state_dir, '/tmp/tvr-test-state', 'a value no panel edits survives');
  assert.equal(posted.rules.length, 2, 'the rules survive a schedule save');
  assert.deepEqual(posted.instances.map((i) => i.id), ['inst-1'],
    'the Sonarr connections survive a schedule save');
  assert.ok('alerts' in posted && 'automation' in posted && 'notifications' in posted,
    'every settings panel is represented in a save from any one of them');
});

test('an unedited masked API key is echoed back as its mask, never as a new key', async () => {
  const page = await loadPage(() => ({ snapshot: snapshotFixture() }));
  await page.flush();

  // The key arrives masked and nobody touches the field.
  assert.equal(page.$('tvr-tmdb-key').value, '********',
    'the stored key is shown masked, never in clear');

  page.$('tvr-schedule-enabled').checked = true;
  page.change('tvr-schedule-enabled');
  await page.flush();

  const posted = page.sent('settings')[0].settings;
  assert.equal(posted.tmdb.api_key, '********',
    'the mask goes back unchanged, which the worker reads as "keep the stored key"');
});

test('an edited API key is posted as typed', async () => {
  const page = await loadPage(() => ({ snapshot: snapshotFixture() }));
  await page.flush();

  page.$('tvr-tmdb-key').value = 'a-real-tmdb-key-value';
  page.$('tvr-schedule-enabled').checked = true;
  page.change('tvr-schedule-enabled');
  await page.flush();

  const posted = page.sent('settings')[0].settings;
  assert.equal(posted.tmdb.api_key, 'a-real-tmdb-key-value',
    'a key someone actually typed is not mistaken for a mask');
});

test('a failed schedule save leaves the held document unchanged', async () => {
  const page = await loadPage(() => ({ snapshot: snapshotFixture(), failSettings: true }));
  await page.flush();

  page.$('tvr-schedule-enabled').checked = true;
  page.change('tvr-schedule-enabled');
  await page.flush();

  // The save was refused, so the next one must not carry the rejected schedule as though
  // it had been accepted: the schedule is collected at save time, not written into the
  // held document first.
  page.$('tvr-schedule-enabled').checked = false;
  page.change('tvr-schedule-enabled');
  await page.flush();

  const saves = page.sent('settings');
  assert.equal(saves.length, 2);
  assert.equal(saves[1].settings.schedule.enabled, false,
    'the second save reflects the control, not the refused first attempt');
});
