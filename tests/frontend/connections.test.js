'use strict';

/* Regression coverage for the Sonarr instance enable/disable toggle.
 *
 * The lookup here runs synchronously, before any await, so the only way it can miss is
 * a card left on screen after its instance was already removed from settings — a stale
 * render rather than a mid-flight race. Guarded the same way regardless: a silent
 * `target.enabled = wanted` on `undefined` must not become a bare TypeError, and the
 * stale card must not go on pretending the instance is still there.
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
    this.textContent = '';
    this.children = [];
    this.listeners = {};
  }

  append(...nodes) { nodes.forEach((node) => { if (node != null) this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  click() {
    if (this.disabled) return;
    if (this.tagName === 'INPUT') this.checked = !this.checked;
    (this.listeners.change || []).slice().forEach((fn) => fn({ target: this }));
  }
  setAttribute(name, value) { this[name] = String(value); }
  scrollIntoView() {}
}

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

async function loadCreateConnections() {
  const file = path.join(__dirname, '..', '..', 'src', 'assets', 'connections.js');
  const module = await import(pathToFileURL(file).href);
  return module.createConnections;
}

test('toggling a stale instance card is reported, not a crash that invents the instance back', async () => {
  const elements = installFakeDom();
  const createConnections = await loadCreateConnections();

  const instance = { id: 'i1', name: 'Sonarr', url: 'http://sonarr.test', enabled: true };
  const settings = { instances: [instance] };
  let saveCalls = 0;

  const connections = createConnections({
    api: async () => ({}),
    getSettings: () => settings,
    getSnapshot: () => ({ health: {} }),
    saveSettings: async () => { saveCalls += 1; },
    forgetSeriesCache: () => {},
  });

  connections.renderInstances();
  const container = elements.get('tvr-instances');
  const toggleInput = allInputs(container)[0];
  assert.ok(toggleInput, 'the card must render an enabled/disabled toggle');

  // The card goes stale: something else already removed this instance from settings,
  // without this rendered card having been refreshed yet.
  settings.instances = [];

  toggleInput.click();
  await flush();

  assert.equal(saveCalls, 0, 'a stale toggle must not write settings at all');
  const message = elements.get('tvr-notice').textContent;
  assert.match(message, /no longer here/, 'the operator is told plainly, not shown a TypeError');
});
