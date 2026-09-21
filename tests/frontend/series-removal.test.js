'use strict';

/* Regression coverage for the queue-removal dialog's rule lookup.
 *
 * `deleteSeries`'s confirm handler can await a Sonarr write (`set-monitored`) that takes
 * up to ninety seconds. A concurrent settings refresh landing in that window can replace
 * the rule list entirely, and the lookup that follows used to crash with a bare
 * "Cannot set properties of undefined" — which `guarded` still caught, but the message
 * explained nothing, and a monitoring change that really did just reach Sonarr looked
 * exactly like one that silently vanished.
 *
 * `createRemoval` and the real `monitorTree`/`dialog`/`guarded` it calls are exercised
 * directly against a minimal fake DOM, so the actual await-then-vanished-rule race is
 * reproduced rather than asserted about in the abstract.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.id = '';
    this.className = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.indeterminate = false;
    this.textContent = '';
    this.value = '';
    this.placeholder = '';
    this.children = [];
    this.listeners = {};
    this.returnValue = '';
  }

  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  getAttribute(name) { return (this.attributes || {})[name] ?? null; }
  append(...nodes) { nodes.forEach((node) => { if (node != null) this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  dispatch(type, event) { (this.listeners[type] || []).slice().forEach((fn) => fn(event || { target: this })); }
  click() {
    if (this.disabled) return;
    if (this.tagName === 'INPUT') this.checked = !this.checked;
    this.dispatch('click', { target: this, preventDefault() {} });
    this.dispatch('change', { target: this });
  }
  showModal() {}
  scrollIntoView() {}
  querySelector(selector) {
    const isClass = selector.startsWith('.');
    const wanted = isClass ? selector.slice(1) : selector.toUpperCase();
    const matches = (node) => (isClass
      ? String(node.className || '').split(/\s+/).includes(wanted)
      : node.tagName === wanted);
    const stack = [...this.children];
    while (stack.length) {
      const node = stack.shift();
      if (matches(node)) return node;
      stack.push(...(node.children || []));
    }
    return null;
  }
}

// Every INPUT anywhere under a node, in document order — the same walk the picker tests
// use, generalised past a single className since this dialog mixes a select, a text
// field and a whole monitoring tree.
function allInputs(node) {
  const found = [];
  (function visit(current) {
    if (!current) return;
    if (current.tagName === 'INPUT') found.push(current);
    (current.children || []).forEach(visit);
  }(node));
  return found;
}

function installFakeDom() {
  const elements = new Map();
  global.document = {
    getElementById: (id) => {
      if (!elements.has(id)) { const node = new FakeElement('div'); node.id = id; elements.set(id, node); }
      return elements.get(id);
    },
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (value) => ({ textContent: value }),
  };
  return elements;
}

async function flush(rounds = 10) {
  for (let round = 0; round < rounds; round += 1) await new Promise((resolve) => setImmediate(resolve));
}

async function loadCreateRemoval() {
  const file = path.join(__dirname, '..', '..', 'src', 'assets', 'series-removal.js');
  const module = await import(pathToFileURL(file).href);
  return module.createRemoval;
}

test('a rule that vanished while a monitoring write was in flight is reported, not silently dropped', async () => {
  const elements = installFakeDom();
  const createRemoval = await loadCreateRemoval();

  const rule = { id: 'r1', series_id: 1, instance_id: 'i1', series_title: 'Show One', queue: {} };
  const settings = { rules: [rule] };
  let savedCalls = 0;

  const removal = createRemoval({
    api: async (action, payload) => {
      if (action === 'episodes') {
        return { seasons: [{ season: 1, episodes: [
          { episode_id: 42, season: 1, episode: 1, monitored: false, has_file: true },
        ] }] };
      }
      if (action === 'set-monitored') {
        // The concurrent refresh: it lands while this write is still in flight, and
        // replaces the settings document the way a real sync does.
        settings.rules = [];
        return { monitored: (payload.monitor || []).length, unmonitored: (payload.unmonitor || []).length };
      }
      throw new Error(`unexpected action ${action}`);
    },
    getSettings: () => settings,
    saveSettings: async () => { savedCalls += 1; },
    renderDetails: () => {},
  });

  removal.deleteSeries(rule);

  const body = elements.get('tvr-dialog-body');
  const monitorToggle = allInputs(body).find((input) => input.type === 'checkbox');
  assert.ok(monitorToggle, 'the "Set monitoring in Sonarr before it goes" switch must exist');
  monitorToggle.click();     // turns the tree on; reads episodes and builds it
  await flush();

  const seriesAllBox = allInputs(body).find((input) => input !== monitorToggle && input.type === 'checkbox');
  assert.ok(seriesAllBox, 'the monitor tree\'s "All of it" box must exist once the tree is built');
  seriesAllBox.click();      // monitors every episode, so the confirm handler has a real write to make

  const box = elements.get('tvr-dialog');
  box.returnValue = 'ok';
  box.dispatch('close');
  await flush();

  assert.equal(savedCalls, 0, 'nothing was queued once the rule was gone');
  const message = elements.get('tvr-notice').textContent;
  assert.match(message, /no longer in the library/, 'the operator is told plainly, not shown a TypeError');
  assert.match(message, /monitored/i, 'the Sonarr write that already happened is not left unexplained');
});
