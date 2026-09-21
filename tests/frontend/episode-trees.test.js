'use strict';

/* Regression coverage for the season exclusion picker.
 *
 * Turning the whole-season box off must restore whatever was hand-picked before it went
 * on. Before this fix there was no restoration at all: every tick stayed checked, and
 * `refresh()` immediately ticked the season box straight back on, so the only way out
 * was un-ticking every episode by hand — and the box's own stored meaning is "the whole
 * season, including episodes that have not aired", so leaving it on silently kept that
 * claim alive even when the operator meant to withdraw it.
 *
 * Loaded as a real ES module rather than under the VM harness in app-runtime.test.js:
 * `episode-trees.js` only imports the two other pure, DOM-free modules it needs
 * (`dom.js`, `format.js`), so Node's own loader can resolve it directly once a minimal
 * `document` exists for `dom.js`'s `el()` to call.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.className = '';
    this.checked = false;
    this.disabled = false;
    this.title = '';
    this.textContent = '';
    this.children = [];
    this.listeners = {};
  }

  setAttribute(name, value) { (this.attributes ||= {})[name] = String(value); }
  getAttribute(name) { return (this.attributes || {})[name] ?? null; }
  append(...nodes) { nodes.forEach((node) => { if (node != null) this.children.push(node); }); }

  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }

  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }

  // A disabled checkbox never dispatches 'change' in a real browser either; skipping
  // that rule here would hide the exact failure mode the picker guards against.
  click() {
    if (this.disabled) return;
    if (this.tagName === 'INPUT') this.checked = !this.checked;
    (this.listeners.change || []).slice().forEach((fn) => fn({ target: this }));
  }
}

function installFakeDom() {
  global.document = {
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (value) => ({ textContent: value }),
  };
}

// Every checkbox this picker offers for exclusion — the season box and each episode
// tick — shares the one className; the Sonarr-monitored checkbox next to it does not,
// so this walk finds exactly the controls the bug is about, in the order they render.
function pickCheckboxes(node) {
  const found = [];
  (function visit(current) {
    if (!current) return;
    if (current.tagName === 'INPUT' && current.className === 'tvr-pick') found.push(current);
    (current.children || []).forEach(visit);
  }(node));
  return found;
}

function episode(number, overrides) {
  return Object.assign({
    episode_id: number, season: 1, episode: number, title: `E${number}`,
    air_date: '2026-01-01', has_file: true, monitored: false, in_scope: false, excluded: '',
  }, overrides || {});
}

async function loadExclusionTree() {
  installFakeDom();
  const file = path.join(__dirname, '..', '..', 'src', 'assets', 'episode-trees.js');
  const module = await import(pathToFileURL(file).href);
  return module.exclusionTree;
}

test('turning the season box off restores what was hand-picked before it went on', async () => {
  const exclusionTree = await loadExclusionTree();
  const rows = [episode(1), episode(2), episode(3)];
  // Episode 2 was already excluded on its own, from a prior save.
  const tree = exclusionTree([{ season: 1, episodes: rows }], [{ season: 1, episode: 2 }]);
  const [seasonBox, tick1, tick2, tick3] = pickCheckboxes(tree.node);

  assert.equal(tick1.checked, false);
  assert.equal(tick2.checked, true);
  assert.equal(tick3.checked, false);
  assert.deepEqual(tree.picked(), [{ season: 1, episode: 2 }]);

  seasonBox.click();   // the whole season, on
  assert.equal(tick1.checked, true);
  assert.equal(tick2.checked, true);
  assert.equal(tick3.checked, true);
  assert.equal(tick1.disabled, true, 'individual ticks cannot disagree with the season while it is on');
  assert.deepEqual(tree.picked(), [{ season: 1, episode: null }]);

  seasonBox.click();   // the whole season, off again
  assert.equal(tick1.disabled, false);
  assert.equal(tick1.checked, false, 'never hand-picked, so it does not stay ticked');
  assert.equal(tick2.checked, true, 'was hand-picked before the season box, so it still is');
  assert.equal(tick3.checked, false);
  assert.deepEqual(tree.picked(), [{ season: 1, episode: 2 }],
    'the season entry is gone; it must not have been silently replaced by an enumerated list');
});

test('a pick made while the season box is off survives a later round trip through it', async () => {
  const exclusionTree = await loadExclusionTree();
  const rows = [episode(1), episode(2)];
  const tree = exclusionTree([{ season: 1, episodes: rows }], []);
  const [seasonBox, tick1, tick2] = pickCheckboxes(tree.node);

  tick1.click();
  assert.deepEqual(tree.picked(), [{ season: 1, episode: 1 }]);

  seasonBox.click();
  seasonBox.click();
  assert.equal(tick1.checked, true);
  assert.equal(tick2.checked, false);
  assert.deepEqual(tree.picked(), [{ season: 1, episode: 1 }]);
});

test('an episode excluded by Automation stays locked through a whole-season toggle', async () => {
  const exclusionTree = await loadExclusionTree();
  const rows = [episode(1, { excluded: 'specials' }), episode(2)];
  const tree = exclusionTree([{ season: 1, episodes: rows }], []);
  const [seasonBox, autoTick, tick2] = pickCheckboxes(tree.node);

  assert.equal(autoTick.disabled, true);
  assert.equal(autoTick.checked, true);

  seasonBox.click();
  assert.equal(autoTick.disabled, true, 'the season box never overrides what Automation excluded');
  assert.equal(autoTick.checked, true);

  seasonBox.click();
  assert.equal(autoTick.checked, true, 'still excluded automatically, whole-season or not');
  assert.equal(tick2.checked, false);
});
