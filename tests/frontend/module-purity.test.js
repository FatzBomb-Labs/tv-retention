'use strict';

/* Every shipped module must import clean: definitions and nothing else — no DOM, no
 * timers, no storage, no network, no listeners — because imports evaluate before the
 * entry body can guard any of it. `app.js` alone may look up `#tv-retention` while
 * importing: finding the page is the entry's one job, and every module it will ever
 * import gets no such exception.
 *
 * Each import runs in a child process over a staged copy of the assets, with failing
 * spies on every global a leak would touch, so a side effect fails loudly instead of
 * scheduling a timer inside the test runner. The staging directory carries a
 * `package.json` marking the copies as ES modules, which asks for `.js` module
 * semantics explicitly rather than leaving them to Node's syntax detection.
 */

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');
const assert = require('node:assert');

const ASSETS = path.join(__dirname, '..', '..', 'src', 'assets');

function probe(target, isEntry) {
  return `
const fail = (what) => { console.error(what); process.exit(1); };
const deny = (what) => () => fail('import-time ' + what);

globalThis.document = new Proxy({}, {
  get(_, property) {
    if (property === 'getElementById') {
      return (id) => {
        if (${isEntry} && id === 'tv-retention') return null;
        return fail('import-time DOM access: getElementById(' + id + ')');
      };
    }
    return fail('import-time DOM access: document.' + String(property));
  },
});
globalThis.window = new Proxy({}, {
  get(_, property) {
    if (property === 'addEventListener') return deny('event binding');
    return fail('import-time window access: ' + String(property));
  },
  set(_, property) { return fail('import-time window write: ' + String(property)); },
});
globalThis.localStorage = {
  getItem: deny('storage access'),
  setItem: deny('storage access'),
  removeItem: deny('storage access'),
};
globalThis.fetch = deny('network request');
globalThis.setInterval = deny('timer');
globalThis.setTimeout = deny('timer');
globalThis.requestAnimationFrame = deny('timer');

import { pathToFileURL } from 'node:url';
await import(pathToFileURL(${JSON.stringify(target)}).href);
console.log('clean');
`;
}

test('every shipped module imports without side effects', () => {
  const shipped = fs.readdirSync(ASSETS).filter((name) => name.endsWith('.js')).sort();
  assert.ok(shipped.length > 0, 'the assets directory holds no modules to test');
  assert.ok(shipped.includes('app.js'), 'the entry module is not where the tests expect it');

  // A copy, not the originals: the probe stays read-only where it matters, and the
  // package.json beside the copies is what makes .js mean ES module.
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), 'tvr-modules-'));
  try {
    fs.writeFileSync(path.join(staging, 'package.json'), '{"type":"module"}\n');
    for (const name of shipped) {
      fs.copyFileSync(path.join(ASSETS, name), path.join(staging, name));
    }
    for (const name of shipped) {
      const result = spawnSync(process.execPath,
        ['--input-type=module', '-e', probe(path.join(staging, name), name === 'app.js')],
        { encoding: 'utf8', timeout: 30000 });
      assert.strictEqual(result.status, 0,
        `${name} is not side-effect-free at import time:\n${result.stderr || result.stdout}`);
    }
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
});
