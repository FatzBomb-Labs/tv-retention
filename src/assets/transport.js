/* The RPC surface, and the busy overlay that reports it.
 *
 * Every request settles. A call that never returns would otherwise leave the overlay
 * covering the page with nothing on screen explaining why, so each one carries a
 * deadline and the timeout is phrased as something a reader can act on.
 *
 * The overlay counts rather than toggles: background checks and a foreground save can
 * overlap, and the last one to finish is the one that clears it. Work that must not
 * raise it at all passes `quiet` — per-series Sonarr reads run that way, which is the
 * rule that they never raise the busy overlay.
 */
'use strict';

import { $ } from './dom.js';

let busyDepth = 0;
function busy(on, label) {
  busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
  $('tvr-busy').hidden = busyDepth === 0;
  if (on && label) $('tvr-busy-text').textContent = label;
}

// Per-action deadlines. A run may legitimately take an hour; a progress poll that has
// not answered in thirty seconds has failed. Anything not named here gets the default.
const TIMEOUTS = { run: 3600000, preview: 900000, 'remove-series': 900000, series: 120000,
                          'test-instance': 90000, match: 300000, 'test-tmdb': 60000,
                          'check-rule': 300000, progress: 30000, log: 30000, alerts: 60000 };
const DEFAULT_TIMEOUT = 60000;

/* Whatever happens, the page must end up interactive. Both window-level handlers and
 * the failed first load call this: depth is unknowable after an error escaped, so it
 * is reset rather than decremented.
 */
function resetBusy() {
  busyDepth = 0;
  $('tvr-busy').hidden = true;
}

/* The endpoint and CSRF token come off the page's root element, so they are handed in
 * once at start-up rather than read here. Nothing in this module touches the DOM or
 * the network until something calls it.
 */
function createApi(endpoint, csrf) {
  return async function api(action, payload, label, quiet) {
    if (!quiet) busy(true, label);
    const controller = new AbortController();
    const limit = TIMEOUTS[action] || DEFAULT_TIMEOUT;
    const timer = setTimeout(() => controller.abort(), limit);
    try {
      const body = new URLSearchParams();
      body.set('csrf_token', csrf);
      body.set('payload', JSON.stringify(Object.assign({ action }, payload || {})));
      let response;
      try {
        response = await fetch(endpoint, { method: 'POST', body, credentials: 'same-origin', signal: controller.signal });
      } catch (error) {
        if (error.name === 'AbortError') {
          throw new Error(`The server did not answer "${action}" within ${Math.round(limit / 1000)}s. `
                          + 'Check the TV Retention worker in the system log.');
        }
        throw new Error(`Could not reach the TV Retention backend (${error.message}). Reload the page.`);
      }
      let data;
      try {
        data = await response.json();
      } catch (error) {
        data = { ok: false, error: `The backend replied with HTTP ${response.status} and no usable JSON. `
                                   + 'If this says 403, reload the Unraid page to refresh the session token.' };
      }
      // A session that has gone — the container restarted, or it simply aged out — is not
      // an error the reader can do anything with. Sessions live in memory on purpose, so
      // this is the ordinary consequence of a restart and the page should just go and log
      // in again.
      if (data.expired) { window.location.href = '/login'; throw new Error('Signing in again…'); }
      if (!data.ok) throw new Error(data.error || 'Request failed');
      return data;
    } finally {
      clearTimeout(timer);
      if (!quiet) busy(false);
    }
  };
}

export { busy, resetBusy, createApi, TIMEOUTS, DEFAULT_TIMEOUT };
