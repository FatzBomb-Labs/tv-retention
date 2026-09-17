'use strict';

// Exercise the real editor factory and removal Undo with replaceable settings, fake
// DOM/timers and fixture-only RPCs. No network or worker is involved.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.classList = { add() {} };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute() {}
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() { (this.listeners.click || []).forEach((fn) => fn()); }
  scrollIntoView() {}
}

const copy = (value) => JSON.parse(JSON.stringify(value));
const ruleFixture = (overrides = {}) => ({
  id: 'r1', instance_id: 'i1', series_id: 1, series_title: 'One',
  enabled: true, keep_days: 30, combine: 'any', exclusions: [],
  auto_reenable: false, auto_reenable_after: '', queue: { removal: null },
  ...overrides,
});
const findText = (node, value) => node.textContent === value ? node
  : (node.children || []).map((child) => findText(child, value)).find(Boolean);

async function harness(initialRules, match = (settings) => settings) {
  const elements = new Map();
  const context = vm.createContext({
    document: {
      createElement: (tag) => new FakeElement(tag),
      createTextNode: (textContent) => ({ textContent }),
      getElementById: (id) => {
        if (!elements.has(id)) elements.set(id, new FakeElement('div'));
        return elements.get(id);
      },
    },
    // Background counts and notice expiry are deliberately not fired by save tests.
    setTimeout: () => 1, clearTimeout() {},
  });
  const modules = new Map();
  const assets = path.join(__dirname, '../../src/assets');
  const load = (file) => {
    if (!modules.has(file)) modules.set(file, new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), {
      context, identifier: file,
    }));
    return modules.get(file);
  };
  const entry = new vm.SourceTextModule(
    "export { createSeriesEditor } from './series-editor.js'; export { createRemoval } from './series-removal.js';",
    { context, identifier: path.join(assets, 'test-entry.js') });
  await entry.link((name, from) => load(path.resolve(path.dirname(from.identifier), name)));
  await entry.evaluate();

  let settings = { instances: [{ id: 'i1' }], rules: copy(initialRules), profiles: [] };
  const saves = [], calls = [], checks = [];
  const api = async (action, payload) => {
    calls.push({ action, payload: copy(payload) });
    if (action === 'match') return { settings: copy(match(copy(settings))) };
    if (action === 'scope-pass' || action === 'set-monitored') return {};
    throw new Error(`Unexpected RPC: ${action}`);
  };
  const saveSettings = async () => {
    saves.push(copy(settings));
    settings = copy(settings); // a save/refresh replaces the whole document
    settings.rules.forEach((rule) => { if (!rule.id) rule.id = 'new-rule'; });
  };
  let editor;
  const removal = entry.namespace.createRemoval({ api, getSettings: () => settings, saveSettings,
    renderDetails: () => editor.renderDetails() });
  editor = entry.namespace.createSeriesEditor({
    api, getSettings: () => settings, getSnapshot: () => ({}), getMonitoring: () => ({}),
    getLibrary: () => [], applySaved: (value) => { settings = value; }, saveSettings,
    conditionFields: (rule) => ({
      node: new FakeElement('div'),
      days: Object.assign(new FakeElement('input'), { value: String(rule.keep_days || '') }),
      episodes: new FakeElement('input'), seasons: new FakeElement('input'),
      combine: Object.assign(new FakeElement('select'), { value: rule.combine }), valid: () => true,
    }),
    presetSummary: () => [], posterNode: () => new FakeElement('img'), sonarrLink: () => null,
    seriesAlerts: () => [], queuedBanner: removal.queuedBanner,
    queueChecks: (ids) => checks.push(...ids),
    forgetLibrary() {}, render() {}, renderLibrary() {}, openLibraryView() {},
  });
  return {
    editor, saves, calls, checks, elements,
    get settings() { return settings; },
    replace: (rules) => { settings = { ...settings, rules: copy(rules) }; },
    open: (rule = settings.rules[0], series) => {
      editor.openEditor(rule, series);
      return editor.editing();
    },
    save: (form) => {
      form.context.conditions.days.value = '60';
      return form.save(form.context, true);
    },
  };
}

test('retention save preserves latest episode/whole-season exclusions and independent metadata after reload', async () => {
  const h = await harness([ruleFixture()]);
  const form = h.open();
  const latest = ruleFixture({
    exclusions: [{ season: 1, episode: 2 }, { season: 3, episode: null }],
    auto_reenable: true, auto_reenable_after: '2026-09-16',
    queue: { removal: { action: 'remove', request_id: 'latest' } },
    match_status: 'matched', slug: 'one-current',
  });
  h.replace([latest]);
  form.context.autoReenable.input.checked = true;
  await h.save(form);
  const expected = { ...latest, keep_days: '60', keep_episodes: null, keep_seasons: null,
    profile_id: '', include_specials: '' };
  assert.deepEqual(h.saves[0].rules[0], expected);
  assert.deepEqual(copy(h.settings.rules[0]), expected);
  // A null episode remains a season-wide exclusion, not a list of currently known episodes.
  assert.deepEqual(h.settings.rules[0].exclusions[1], { season: 3, episode: null });
});

test('Undo while the editor is open stays undone on redraw, save and reopening', async () => {
  const h = await harness([ruleFixture({ queue: { removal: {
    action: 'remove', created_at: '2026-09-17T00:00:00Z', request_id: 'old',
  } } })]);
  const form = h.open();
  const pane = h.elements.get('tvr-details');
  assert.ok(findText(pane, 'Undo'));
  findText(pane, 'Undo').click();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.settings.rules[0].queue.removal, null);
  assert.equal(findText(pane, 'Undo'), undefined, 'redraw must not use the captured queue');
  await h.save(form);
  assert.equal(h.saves.at(-1).rules[0].queue.removal, null);
  h.open();
  assert.equal(findText(pane, 'Undo'), undefined);
});

test('existing save and all follow-up actions target its stable ID after reordering', async () => {
  const other = ruleFixture({ id: 'r2', series_id: 2, series_title: 'Two' });
  const h = await harness([ruleFixture(), other], (settings) => ({ ...settings,
    rules: settings.rules.sort((a, b) => a.id.localeCompare(b.id)) }));
  const form = h.open();
  h.replace([other, ruleFixture()]);
  form.context.tree = () => ({ changes: () => ({ monitor: [42], unmonitor: [43] }) });
  await h.save(form);
  assert.deepEqual(h.saves[0].rules.map((rule) => rule.id), ['r2', 'r1']);
  assert.deepEqual(h.settings.rules.find((rule) => rule.id === 'r2'), other);
  assert.deepEqual(h.calls.filter((call) => call.action !== 'match').map((call) => call.payload.rule_id),
    ['r1', 'r1']);
  assert.deepEqual(h.checks, ['r1']);
});

test('new rule follow-up uses instance/series identity, not matched array position', async () => {
  const other = ruleFixture({ instance_id: 'i2' }); // same series number, different instance
  const h = await harness([other], (settings) => ({ ...settings, rules: settings.rules.reverse() }));
  const form = h.open(null, { instance_id: 'i1', series_id: 1, title: 'New', path: '/tv/new' });
  await h.save(form);
  assert.deepEqual(h.calls.filter((call) => call.action === 'scope-pass').map((call) => call.payload.rule_id),
    ['new-rule']);
  assert.deepEqual(h.checks, ['new-rule']);
});

test('an existing rule removed while editing is rejected without saving or resurrecting it', async () => {
  const h = await harness([ruleFixture()]);
  const form = h.open();
  // A new rule with the same Sonarr binding is not the removed rule.
  const replacement = ruleFixture({ id: 'replacement' });
  h.replace([replacement]);
  await assert.rejects(h.save(form), /no longer in the library/);
  assert.deepEqual(h.settings.rules, [replacement]);
  assert.equal(h.saves.length, 0);
  assert.equal(h.calls.length, 0);
  assert.equal(h.checks.length, 0);
});

test('a rule missing after matching cannot redirect monitoring or checks to another rule', async () => {
  const other = ruleFixture({ id: 'r2', series_id: 2 });
  const h = await harness([ruleFixture(), other], (settings) => ({ ...settings,
    rules: settings.rules.filter((rule) => rule.id !== 'r1') }));
  const form = h.open();
  await assert.rejects(h.save(form), /No monitoring changes were sent/);
  assert.equal(h.saves.length, 1, 'the initial save happened before matching');
  assert.deepEqual(h.settings.rules, [other]);
  assert.deepEqual(h.calls.map((call) => call.action), ['match']);
  assert.deepEqual(h.checks, []);
});
