'use strict';

/* Regression coverage for a failed library read.
 *
 * Before this fix, `loadLibrary()`'s failure was caught by an empty handler that only
 * re-cleared a flag `loadLibrary` itself already clears in its own `finally`. `library`
 * stayed `null` forever, so `renderLibrary()` kept showing "Reading the stored library…"
 * and kept calling `loadLibrary()` again on every render — an unbounded retry loop that
 * reported nothing to the person looking at it.
 *
 * `createLibrary` is exercised directly here, with only the callbacks the failing path
 * actually reaches stubbed in, rather than through the full page: the null/error branch
 * this test is about returns before touching cards, filters, alerts or the editor.
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
    this.hidden = false;
    this.textContent = '';
    this.children = [];
    this.listeners = {};
    this.style = { setProperty() {} };
  }

  append(...nodes) { nodes.forEach((node) => { if (node != null) this.children.push(node); }); }
  replaceChildren(...nodes) { this.children = [...nodes]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  click() {
    if (this.disabled) return;
    (this.listeners.click || []).slice().forEach((fn) => fn({ target: this, preventDefault() {} }));
  }
}

function collectText(node) {
  if (!node) return '';
  const own = typeof node.textContent === 'string' ? node.textContent : '';
  if (!Array.isArray(node.children) || node.children.length === 0) return own;
  return [own, ...node.children.map(collectText)].join(' ');
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

async function loadCreateLibrary() {
  const file = path.join(__dirname, '..', '..', 'src', 'assets', 'library.js');
  const module = await import(pathToFileURL(file).href);
  return module.createLibrary;
}

// Only what the failing null/error branch actually reaches: it returns before any card,
// filter, alert or editor callback would run.
function minimalDeps(overrides) {
  return Object.assign({
    api: async () => ({}),
    getSettings: () => ({ instances: [{ id: 'i1', enabled: true }], rules: [] }),
    getMonitoring: () => ({}),
    bulkChecking: () => false,
    seriesAlerts: () => [],
    isChecking: () => false,
    worstSeverity: () => 'ok',
    isBlocked: () => false,
    showSeriesAlerts: () => {},
    syncedAgo: () => '',
    queuedRemoval: () => null,
    getLibraryFilter: () => 'all',
    isLibraryView: () => true,
    isOpen: () => false,
    closeEditor: () => {},
    renderDetails: () => {},
    openEditor: () => {},
    presetSummary: () => [],
  }, overrides || {});
}

test('a failed library read is shown, not retried silently on every render', async () => {
  const elements = installFakeDom();
  const createLibrary = await loadCreateLibrary();
  let seriesCalls = 0;
  const library = createLibrary(minimalDeps({
    api: async (action) => {
      if (action === 'series') { seriesCalls += 1; throw new Error('Sonarr is unreachable'); }
      return {};
    },
  }));

  library.renderLibrary();
  await flush();
  assert.equal(seriesCalls, 1, 'the first render attempts exactly one read');
  const container = elements.get('tvr-rules');
  assert.ok(collectText(container).includes('Sonarr is unreachable'),
    'the failure is shown, not left as a permanent "Reading the stored library…"');

  // An ordinary re-render — a sweep landing, a save completing, anything that calls
  // render() again — must not turn a shown failure back into a silent retry.
  library.renderLibrary();
  await flush();
  assert.equal(seriesCalls, 1, 'a second render does not ask Sonarr again on its own');
  assert.ok(collectText(elements.get('tvr-rules')).includes('Sonarr is unreachable'),
    'the failure is still shown after the render that did not retry');
});

test('the retry control asks again, once, and can succeed', async () => {
  const elements = installFakeDom();
  const createLibrary = await loadCreateLibrary();
  let seriesCalls = 0;
  const library = createLibrary(minimalDeps({
    api: async (action) => {
      seriesCalls += 1;
      if (action === 'series') {
        if (seriesCalls === 1) throw new Error('Sonarr is unreachable');
        return { series: [] };
      }
      return {};
    },
  }));

  library.renderLibrary();
  await flush();
  assert.equal(seriesCalls, 1);

  const container = elements.get('tvr-rules');
  const retry = container.children.find((node) => node.tagName === 'BUTTON');
  assert.ok(retry, 'a failed read offers an explicit way to try again');
  retry.click();
  await flush();
  assert.equal(seriesCalls, 2, 'the explicit retry is the one thing allowed to ask again');
  assert.ok(!collectText(elements.get('tvr-rules')).includes('unreachable'),
    'a successful retry clears the failure rather than leaving it on screen');
});
