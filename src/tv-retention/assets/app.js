/* TV Retention web UI.
 *
 * The browser holds no authority: it renders what the worker reports and posts whole
 * settings documents back for re-validation. API keys are never sent to the page; a mask
 * is shown instead, and echoing the mask back means "keep the stored key".
 *
 * Two rules run through everything here. Sonarr is never read on the main path — cached
 * results are shown at once and refreshed in the background, per series. And Preview is a
 * mode, not a button: when it is on, every action reports and changes nothing.
 */
(function () {
  'use strict';

  const root = document.getElementById('tv-retention');
  if (!root) return;
  const API = root.dataset.api;
  const CSRF = root.dataset.csrf;

  let snapshot = null;      // last payload from the worker
  let settings = null;      // working copy, saved as a whole document
  let monitoring = {};      // rule id -> cached check result
  let alertsByRule = {};    // rule id -> that series' alerts
  let systemAlerts = [];
  let seriesCache = {};

  const $ = (id) => document.getElementById(id);
  const el = (tag, props, children) => {
    const node = Object.assign(document.createElement(tag), props || {});
    (children || []).forEach((child) => node.append(child));
    return node;
  };
  const text = (value) => document.createTextNode(value);

  // -- transport ---------------------------------------------------------
  let busyDepth = 0;
  function busy(on, label) {
    busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
    $('tvr-busy').hidden = busyDepth === 0;
    if (on && label) $('tvr-busy-text').textContent = label;
  }

  // A request must always settle. Without this, one stalled call leaves the busy overlay
  // covering the page with nothing on screen explaining why. Background work passes
  // quiet: it updates one card and must never block the page.
  const TIMEOUTS = { run: 3600000, preview: 900000, 'remove-series': 900000, series: 120000,
                     'test-instance': 90000, match: 300000, 'test-tmdb': 60000,
                     'check-rule': 300000, progress: 30000, log: 30000, alerts: 60000 };
  const DEFAULT_TIMEOUT = 60000;

  async function api(action, payload, label, quiet) {
    if (!quiet) busy(true, label);
    const controller = new AbortController();
    const limit = TIMEOUTS[action] || DEFAULT_TIMEOUT;
    const timer = setTimeout(() => controller.abort(), limit);
    try {
      const body = new URLSearchParams();
      body.set('csrf_token', CSRF);
      body.set('payload', JSON.stringify(Object.assign({ action }, payload || {})));
      let response;
      try {
        response = await fetch(API, { method: 'POST', body, credentials: 'same-origin', signal: controller.signal });
      } catch (error) {
        if (error.name === 'AbortError') {
          throw new Error(`The server did not answer "${action}" within ${Math.round(limit / 1000)}s. `
                          + 'Check the plugin worker in the system log.');
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
  }

  function notice(message, kind) {
    const box = $('tvr-notice');
    box.textContent = message;
    box.className = kind || '';
    box.hidden = !message;
    if (message) box.scrollIntoView({ block: 'nearest' });
  }

  async function guarded(label, work) {
    try { notice(''); await work(); } catch (error) { notice(error.message, 'bad'); }
  }

  // -- formatting --------------------------------------------------------
  const bytes = (value) => {
    if (!value) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let index = 0, size = Number(value);
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return `${size.toFixed(size < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
  };
  const when = (iso) => (iso ? new Date(iso).toLocaleString() : '—');
  // "series" is already plural; nothing ending in s takes another one.
  const plural = (count, word) => `${count} ${word}${count === 1 || word.endsWith('s') ? '' : 's'}`;
  function ago(stamp) {
    if (!stamp) return 'never checked';
    const seconds = Math.max(0, (Date.now() - new Date(stamp).getTime()) / 1000);
    if (seconds < 90) return 'just now';
    if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
    return `${Math.round(seconds / 86400)} days ago`;
  }

  // -- shared controls ---------------------------------------------------
  // One switch implementation, so a toggle looks and behaves the same everywhere it
  // appears: on a series card, in a dialog, or in the page header.
  function toggle(label, checked, onChange, options) {
    const config = options || {};
    const input = el('input', { type: 'checkbox', checked: !!checked, disabled: !!config.disabled });
    if (config.label) input.setAttribute('aria-label', config.label);
    const caption = el('span', { className: 'tvr-switch-text', textContent: label });
    const node = el('label', { className: `tvr-switch${config.className ? ' ' + config.className : ''}`,
                               title: config.title || '' },
                    [input, el('span', { className: 'tvr-slider' }), caption]);
    if (onChange) input.addEventListener('change', () => onChange(input.checked, input, caption));
    return { node, input, caption };
  }


  function field(label, control, note) {
    const wrapper = el('label', { className: 'tvr-field' }, [el('span', { textContent: label }), control]);
    if (note) wrapper.append(el('small', { textContent: note }));
    return wrapper;
  }

  function options(select, values, selected) {
    select.replaceChildren();
    values.forEach(([value, label]) => select.append(el('option', { value: String(value), textContent: label })));
    if (selected !== undefined && selected !== null) select.value = String(selected);
    return select;
  }

  const range = (from, to, pad) => {
    const out = [];
    for (let index = from; index <= to; index += 1) {
      out.push([index, pad ? String(index).padStart(2, '0') : String(index)]);
    }
    return out;
  };

  // -- dialog ------------------------------------------------------------
  function dialog(title, buildBody, onOk, okLabel) {
    const box = $('tvr-dialog');
    const body = $('tvr-dialog-body');
    body.replaceChildren(el('h3', { textContent: title }));
    $('tvr-dialog-extra').replaceChildren();
    $('tvr-dialog-ok').disabled = false;
    const context = buildBody(body);
    $('tvr-dialog-ok').textContent = okLabel || 'Save';
    $('tvr-dialog-ok').hidden = !onOk;
    const handler = async () => {
      box.removeEventListener('close', handler);
      if (box.returnValue !== 'ok' || !onOk) return;
      await guarded('', () => onOk(context));
    };
    box.addEventListener('close', handler);
    box.showModal();
  }

  // -- snapshot and background checking ----------------------------------
  const checking = new Set();
  const forced = new Set();
  let checkQueue = [];
  let checkRunning = false;
  let bulkChecking = false;
  let pollTimer = null;

  const isChecking = (ruleId) => checking.has(ruleId);

  async function refresh() {
    snapshot = await api('snapshot', {}, 'Loading…');
    settings = snapshot.settings;
    applyHealth(snapshot.health);
    applyAlerts(snapshot.alerts || []);
    render();
    const progress = snapshot.progress || {};
    if (progress.running) startPolling();
    else queueChecks(snapshot.stale_rules || []);
  }

  function applyHealth(health) {
    monitoring = (health && health.rules) || {};
    if (snapshot) snapshot.health = health || {};
  }

  function applyAlerts(list) {
    alertsByRule = {};
    systemAlerts = [];
    (list || []).forEach((alert) => {
      if (alert.scope === 'system') { systemAlerts.push(alert); return; }
      (alertsByRule[alert.rule_id] = alertsByRule[alert.rule_id] || []).push(alert);
    });
    if (snapshot) snapshot.alerts = list || [];
    // A system alert appearing or clearing changes whether a run can happen at all, and
    // alerts arrive on their own schedule without ever passing through renderTopBar.
    if (snapshot) renderRunButton();
  }

  const seriesAlerts = (ruleId) => alertsByRule[ruleId] || [];

  // From the settings rather than the snapshot. `snapshot.test_mode` is only refreshed by a
  // full snapshot call, so turning Test Mode off and saving left the top bar and the Run
  // button describing the mode the page had loaded with — which is exactly the moment
  // somebody is reading that button to see whether it will delete something.
  const testMode = () => {
    const schedule = (settings || {}).schedule;
    return schedule ? !!schedule.test_mode : !!(snapshot || {}).test_mode;
  };
  // What is counted. A switched-off series still carries its alerts — the card needs them
  // to colour its own border — but it contributes to no count and no badge, which is what
  // "a series that is off raises nothing" has always meant.
  const seriesAlertList = () => Object.values(alertsByRule)
    .reduce((all, list) => all.concat(list), [])
    .filter((alert) => !alert.unmanaged);
  const isBlocked = (ruleId) => seriesAlerts(ruleId).some((alert) => alert.blocking);
  const worstSeverity = (list) => (list.some((a) => a.severity === 'error') ? 'error'
    : list.some((a) => a.severity === 'warning') ? 'warning'
    : list.length ? 'notice' : '');

  // `force` is what the refresh buttons mean: read this series from Sonarr again. Without
  // it a check re-decides from the episodes already stored, which needs no call at all.
  function queueChecks(ruleIds, force) {
    const wanted = (ruleIds || []).filter((id) => !checkQueue.includes(id) && !checking.has(id));
    if (!wanted.length) return;
    if (force) wanted.forEach((id) => forced.add(id));
    checkQueue = checkQueue.concat(wanted);
    wanted.forEach((id) => checking.add(id));
    // A sweep empties the list: a card left standing during a re-read looks like a
    // result, and it would be a stale one.
    if (checking.size > 1) bulkChecking = true;
    renderRules();
    drainChecks();
  }

  async function drainChecks() {
    if (checkRunning) return;
    checkRunning = true;
    try {
      while (checkQueue.length) {
        const ruleId = checkQueue.shift();
        try {
          const data = await api('check-rule', { rule_id: ruleId, force: forced.has(ruleId) }, '', true);
          if (data.busy) {
            checkQueue.forEach((id) => checking.delete(id));
            checkQueue = [];
            checking.delete(ruleId);
            forced.clear();
            startPolling();
            break;
          }
          monitoring[data.rule_id] = data.state;
          // The alerts arrive with the check that produced them; asking separately cost a
          // second request and a second worker process for every series.
          applyAlerts(data.alerts);
        } catch (error) {
          monitoring[ruleId] = Object.assign({}, monitoring[ruleId], {
            ok: false, label: 'Check failed', error: error.message, checked_at: new Date().toISOString(),
          });
        } finally {
          checking.delete(ruleId);
          forced.delete(ruleId);
          renderRules();
          renderAlerts();
          renderStats();
        }
      }
    } finally {
      checkRunning = false;
      bulkChecking = false;
      renderRules();
    }
  }

  function startPolling() {
    if (pollTimer) return;
    const tick = async () => {
      try {
        const data = await api('progress', {}, '', true);
        applyHealth(data.health);
        const progress = data.progress || {};
        checking.clear();
        if (progress.running && progress.current) checking.add(progress.current);
        bulkChecking = !!progress.running;
        const fresh = await api('alerts', {}, '', true);
        applyAlerts(fresh.alerts);
        renderRules();
        renderAlerts();
        renderStats();
        renderCheckBanner(progress);
        if (!progress.running) { clearInterval(pollTimer); pollTimer = null; renderCheckBanner({}); }
      } catch (error) {
        clearInterval(pollTimer);
        pollTimer = null;
        renderCheckBanner({});
      }
    };
    pollTimer = setInterval(tick, 2500);
    tick();
  }

  // -- heartbeat ---------------------------------------------------------
  // One small question to Sonarr — what changed? — and the series it names are re-read
  // through the same queue a manual refresh uses, so each card is seen being read. The
  // cron tick asks the same question every minute whether or not anyone is here, which is
  // why a notification never waits for someone to open this page.
  const WATCH_SECONDS = 15;
  // The heartbeat costs no network at all now — it re-decides every plan from the stored
  // reading, which is what makes a page left open all day still correct about time.
  let watchStamp = '';

  async function watchTick() {
    if (document.hidden || checkRunning || checkQueue.length || pollTimer) return;
    if (!snapshot) return;
    let data;
    try {
      data = await api('watch', {}, '', true);
    } catch (error) {
      return;  // a heartbeat that misses a beat is not worth interrupting anyone for
    }
    if ((data.progress || {}).running) { startPolling(); return; }
    snapshot.sync = data.sync || snapshot.sync;
    // Re-rendering on a timer would fight with whatever is being read on screen, so it
    // only happens when the reply actually differs from the last one.
    const stamp = JSON.stringify([data.alerts, data.plan, data.stale_rules,
                                  Object.values((data.health || {}).rules || {}).map((r) => r.checked_at)]);
    if (stamp === watchStamp) return;
    watchStamp = stamp;
    applyHealth(data.health);
    applyAlerts(data.alerts);
    snapshot.plan = data.plan;
    render();
    if ((data.stale_rules || []).length) queueChecks(data.stale_rules);
  }

  setInterval(watchTick, WATCH_SECONDS * 1000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) watchTick(); });

  const CHECK_PHASE = { instances: 'verifying the Sonarr instances',
                        matching: 'matching series to Sonarr', rules: 'reading series' };

  function renderCheckBanner(progress) {
    const box = $('tvr-checking');
    box.replaceChildren();
    const active = progress && progress.running;
    box.hidden = !active;
    if (!active) return;
    const phase = CHECK_PHASE[progress.phase] || 'checking';
    let detail = `${progress.scheduled ? 'Scheduled check' : 'Check'} in progress — ${phase}`;
    if (progress.phase === 'rules') {
      detail += `, ${progress.done || 0} of ${progress.total || 0} read`;
      if (progress.current_title) detail += `, now reading ${progress.current_title}`;
    }
    box.append(el('span', { className: 'tvr-spinner' }), text(`${detail}. Series update as they finish.`));
  }

  function render() {
    $('tvr-version').textContent = snapshot.version ? `v${snapshot.version}` : '';
    $('tvr-about-version').textContent = snapshot.version || '';
    const banner = ((settings || {}).alerts || {}).test_banner || 'full';
    $('tvr-test-banner').hidden = !testMode() || banner === 'chip';
    $('tvr-test-chip').hidden = !testMode() || banner !== 'chip';
    renderCounts();
    renderTopBar();
    renderRules();
    renderAlerts();
    renderPresets();
    renderInstances();
    renderSchedule();
    renderSettings();
    renderAlertSettings();
    renderHistory();
    renderAbout();
  }

  // -- navigation --------------------------------------------------------
  // One view at a time, named by the sidebar item that reaches it. The list comes from the
  // markup so the two cannot disagree, which is the failure that blanked four tabs.
  const VIEWS = [...document.querySelectorAll('.tvr-side [data-view]')].map((b) => b.dataset.view);
  let currentView = 'series-list';

  // The three series views are one panel with a different filter, because that is what
  // they are: one library, narrowed. A series Sonarr knows about belongs here whether or
  // not it has a rule, which is why "add" is no longer a separate place.
  const LIBRARY = { 'series-all': 'all', 'series-connected': 'connected',
                    'series-unconnected': 'unconnected' };
  const TITLES = { all: 'All series', connected: 'Connected series', unconnected: 'Not connected' };
  let libraryFilter = 'all';

  // -- sections ----------------------------------------------------------
  // One open at a time. Nineteen items in five groups is a wall; four collapsed headings
  // and the group you are working in is a list.
  const sectionOf = (view) => {
    const button = document.querySelector(`.tvr-side [data-view="${view}"]`);
    return button ? button.closest('[data-section]').dataset.section : null;
  };

  // Where a section opens when you have never been in it. Series is the exception: with
  // nothing connected yet, "All" is the only list with anything in it.
  function sectionDefault(section) {
    if (section === 'series') {
      return (settings.rules || []).length ? 'series-connected' : 'series-all';
    }
    const first = document.querySelector(`[data-section="${section}"] [data-view]`);
    return first ? first.dataset.view : 'series-all';
  }

  function openSection(section) {
    document.querySelectorAll('.tvr-side [data-section]').forEach((group) => {
      const open = group.dataset.section === section;
      group.classList.toggle('open', open);
      const head = group.querySelector('[data-section-head]');
      head.setAttribute('aria-expanded', String(open));
      head.querySelector('.fa').className = `fa fa-caret-${open ? 'down' : 'right'}`;
    });
  }

  document.querySelectorAll('.tvr-side [data-section-head]').forEach((head) => {
    head.addEventListener('click', () => {
      const section = head.dataset.sectionHead;
      // Clicking the section you are already in collapses nothing: there would be no open
      // section and no view to show. It just returns you to where you were.
      //
      // Where you were is in the browser, and it outlives the view it names. Renaming
      // `media-rules` to `media-automation` left every existing browser remembering a view
      // that no longer exists: `showView` fell back to `series-all`, which is in another
      // section, so clicking Media management appeared to do nothing at all. A remembered
      // name is only worth having if it still names something.
      const last = remembered(`last.${section}`, '');
      showView(VIEWS.includes(last) ? last : sectionDefault(section));
    });
  });

  function showView(name) {
    if (!VIEWS.includes(name)) name = 'series-all';
    // Unsaved edits belong to the library. Leaving it closes the pane, and a draft kept
    // past that would be a second copy of the settings, invisible until it reappeared
    // over whatever the rule had become in the meantime.
    if (!LIBRARY[name] && LIBRARY[currentView]) { forgetDrafts(); editing = null; renderDetails(); }
    currentView = name;
    const panel = LIBRARY[name] ? 'series-all' : name;
    [...new Set(VIEWS)].forEach((view) => {
      const section = $(`tvr-view-${LIBRARY[view] ? 'series-all' : view}`);
      if (section) section.hidden = (LIBRARY[view] ? 'series-all' : view) !== panel;
    });
    document.querySelectorAll('.tvr-side [data-view]').forEach((button) => {
      button.classList.toggle('active', button.dataset.view === name);
    });
    // Where you were, per section, so a heading is a place you return to rather than a
    // label that always drops you at the top.
    const section = sectionOf(name);
    if (section) { remember(`last.${section}`, name); openSection(section); }
    if (LIBRARY[name]) {
      libraryFilter = LIBRARY[name];
      $('tvr-library-title').textContent = TITLES[libraryFilter];
      renderLibrary();          // it fetches itself if what it needs is not in hand
    }
    if (name === 'media-stats') guarded('', renderStatsView);
    if (name === 'system-logs') startLog(); else stopLog();
  }

  document.querySelectorAll('.tvr-side [data-view]').forEach((button) => {
    button.addEventListener('click', () => showView(button.dataset.view));
  });

  // Each line opens exactly what it names. The total replaces the old button: a count you
  // can read is more use than a button that only promises one.
  // -- changes -----------------------------------------------------------
  // One description of what a change is, used everywhere one is shown: the header, a
  // series card, the detail view and the report after a run. Adding a kind of change means
  // adding it here once, rather than in four places that then drift apart.
  const REMOVAL_SUMMARY = {
    'remove': (n) => `${plural(n, 'series')} will stop being managed here`,
    'monitor-all': (n) => `${plural(n, 'series')} will be set to fully monitored in Sonarr`,
    'unmonitor-all': (n) => `${plural(n, 'series')} will be set to fully unmonitored in Sonarr`,
    'monitor-in-frame': (n) => `${plural(n, 'series')} will have their kept episodes monitored`,
    'delete-series': (n) => `${plural(n, 'series')} will be deleted from Sonarr, keeping files`,
    'delete-series-files': (n) => `${plural(n, 'series')} will be deleted from Sonarr with their files`,
  };

  // kind -> [tone, count, description]. Tone is the colour; the verb lives in the text.
  // Every row carries both lengths: the sentence for a list with room for one, and the
  // count for a panel where these sit among other one-line facts. The long form is the
  // tooltip there, so nothing is lost by shortening it.
  function changeSummary(plan) {
    const rows = [];
    Object.entries((plan.removals_by_action) || {}).forEach(([action, count]) => {
      rows.push({ kind: 'remove', tone: action.startsWith('delete') ? 'delete' : 'unmonitor',
                  short: `${plural(count, 'series removal')} scheduled`,
                  text: (REMOVAL_SUMMARY[action] || REMOVAL_SUMMARY.remove)(count) });
    });
    if (plan.delete) {
      rows.push({ kind: 'delete', tone: 'delete',
                  short: `${plural(plan.delete, 'deletion')} scheduled`,
                  text: `${plural(plan.delete, 'episode')} scheduled for deletion (${bytes(plan.delete_bytes)})` });
    }
    if (plan.monitor) {
      rows.push({ kind: 'monitor', tone: 'monitor',
                  short: `${plan.monitor} to be monitored`,
                  text: `${plural(plan.monitor, 'episode')} will be set to monitored` });
    }
    if (plan.unmonitor) {
      rows.push({ kind: 'unmonitor', tone: 'unmonitor',
                  short: `${plan.unmonitor} to be unmonitored`,
                  text: `${plural(plan.unmonitor, 'episode')} will be set to unmonitored` });
    }
    return rows;
  }

  function changeLines(plan, onOpen, compact) {
    const rows = changeSummary(plan);
    const list = el('div', { className: `tvr-plan-list${compact ? ' compact' : ''}` });
    // The total only earns its line when more than one kind of thing is happening.
    if (rows.length > 1 && onOpen && !compact) {
      const total = el('button', { type: 'button', className: 'tvr-plan-total',
                                   textContent: plural(plan.actionable, 'scheduled change') });
      total.addEventListener('click', () => onOpen('all'));
      list.append(total);
    }
    rows.forEach((row) => {
      const shown = compact ? row.short : row.text;
      const line = onOpen
        ? el('button', { type: 'button', className: `tvr-plan ${row.tone}`, textContent: shown,
                         title: `${row.text} — click to list them` })
        : el('div', { className: `tvr-plan ${row.tone}`, textContent: shown, title: row.text });
      if (onOpen) line.addEventListener('click', () => onOpen(row.kind));
      list.append(line);
    });
    if (plan.newly_scoped) {
      list.append(el('div', { className: 'tvr-plan-quiet',
                              textContent: `includes ${plural(plan.newly_scoped, 'newly scoped episode')}, once` }));
    }
    return list;
  }

  // Every episode a change touches, coloured by what happens to it. Shown in full: if you
  // opened the detail you already asked for it, and hiding it behind a second click only
  // meant the monitoring changes were never visible at all.
  function changeRows(rule, shows) {
    const rows = el('div', { className: 'tvr-changes' });
    const label = (item) => `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')}`
      + ` — ${item.title || ''}`;
    if (shows('delete')) {
      (rule.deleted || []).slice(0, 300).forEach((item) => rows.append(el('div', { className: 'tvr-change delete' }, [
        el('span', { className: 'tvr-change-verb', textContent: 'delete' }),
        el('span', { textContent: `${label(item)} · ${item.air_date || 'undated'} (${item.air_source}) · ${bytes(item.size)}` }),
        el('span', { className: 'tvr-change-why', textContent: item.reason || '' }),
      ])));
    }
    if (shows('monitor')) {
      (rule.monitor_list || []).slice(0, 300).forEach((item) => rows.append(el('div', { className: 'tvr-change monitor' }, [
        el('span', { className: 'tvr-change-verb', textContent: 'monitor' }),
        el('span', { textContent: `${label(item)}${item.has_file ? '' : ' · missing'}` }),
      ])));
    }
    if (shows('unmonitor')) {
      (rule.unmonitor_list || []).slice(0, 300).forEach((item) => rows.append(el('div', { className: 'tvr-change unmonitor' }, [
        el('span', { className: 'tvr-change-verb', textContent: 'unmonitor' }),
        el('span', { textContent: `${label(item)}${item.has_file ? '' : ' · missing'}` }),
      ])));
    }
    return rows;
  }

  // Everything on screen is answered from one reading, so its age is said once, here,
  // rather than repeated against every series.
  function syncedAgo() {
    const stamp = (snapshot.sync || {}).synced_at;
    return stamp ? `synced with Sonarr ${ago(stamp)}` : '';
  }

  // Three states, and the colour is the sentence. Green: this will change things. Orange:
  // this will report and change nothing, because Test Mode is on — and Test Mode now means
  // nothing writes at all, so the button can say so without lying. Red: something is
  // stopping every run, and pressing it shows you what rather than doing nothing.
  //
  // Red is reserved for a fault that stops the *whole* run — no instance answering, or no
  // instance at all. One broken series among thirty-five healthy ones is skipped, not a
  // reason to call the button disabled.
  function runState() {
    if (!(settings.instances || []).length) return 'blocked';
    if (systemAlerts.some((alert) => alert.blocking)) return 'blocked';
    return testMode() ? 'test' : 'live';
  }

  const RUN_STATES = {
    live: ['Run', 'Run now. This deletes episode files through Sonarr.'],
    test: ['Run Test', 'Test mode is on: this reports exactly what it would do and writes nothing.'],
    blocked: ['Disabled', 'Something is stopping every run. Click to see what.'],
  };

  function renderRunButton() {
    // It works out its own "nothing to do", so anything that changes what the button
    // should say can simply call it.
    const plan = snapshot.plan || { actionable: 0, trustworthy: false };
    const nothing = plan.trustworthy && !plan.actionable;
    const state = runState();
    const [text, why] = RUN_STATES[state];
    const button = $('tvr-run');
    button.className = `tvr-run ${state}`;
    $('tvr-run-label').textContent = text;
    // Blocked stays pressable on purpose: it is the shortest route to the reason.
    button.disabled = state !== 'blocked' && nothing;
    button.title = button.disabled ? 'Nothing is scheduled to change' : why;
  }

  // The top bar carries one number and opens what it counts. Nothing drops when there is
  // nothing scheduled: an empty menu is a promise the plugin cannot keep.
  function renderTopBar() {
    const plan = snapshot.plan || { actionable: 0, trustworthy: false, unknown: 0 };
    const rows = changeSummary(plan);
    const button = $('tvr-changes-button');
    const label = $('tvr-changes-label');
    const menu = $('tvr-changes-menu');
    const nothing = plan.trustworthy && !plan.actionable;

    label.textContent = nothing ? 'No scheduled changes'
      : (plan.trustworthy ? plural(plan.actionable, 'scheduled change')
         : `${plural(plan.actionable, 'scheduled change')} so far`);
    button.classList.toggle('quiet', nothing);
    $('tvr-changes-caret').hidden = nothing;
    button.disabled = nothing;
    $('tvr-synced').textContent = syncedAgo();
    renderRunButton();

    menu.hidden = true;
    menu.replaceChildren();
    if (nothing) return;
    const open = (kind) => guarded('', async () => {
      menu.hidden = true;
      const data = await api('preview', {}, 'Working out what would change…');
      changeList(data.result, 'Scheduled changes', kind);
    });
    const all = el('button', { type: 'button', className: 'tvr-changes-row all',
                               textContent: `All ${plural(plan.actionable, 'change')}` });
    all.addEventListener('click', () => open('all'));
    menu.append(all);
    rows.forEach((row) => {
      const line = el('button', { type: 'button', className: `tvr-changes-row ${row.tone}`,
                                  textContent: row.text });
      line.addEventListener('click', () => open(row.kind));
      menu.append(line);
    });
    if (syncedAgo()) menu.append(el('div', { className: 'tvr-changes-foot', textContent: syncedAgo() }));
  }

  $('tvr-changes-button').addEventListener('click', (event) => {
    event.stopPropagation();
    const menu = $('tvr-changes-menu');
    menu.hidden = !menu.hidden;
    $('tvr-changes-button').setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('#tvr-changes')) $('tvr-changes-menu').hidden = true;
  });

  function setBadge(badge, list) {
    const live = (list || []).filter((alert) => !alert.acknowledged);
    badge.hidden = live.length === 0;
    badge.textContent = live.length || '';
    badge.className = `tvr-tab-badge ${worstSeverity(live) || 'notice'}`;
  }

  // The sidebar carries the counts. Two badges for two audiences: a series problem belongs
  // to Series, where it is fixed; an installation problem belongs to Alerts. Neither
  // counts the other's.
  // A badge next to the thing it is about, rather than one list of everything wrong. The
  // total in the top bar is the one place that still answers "is anything wrong at all?".
  function renderCounts() {
    const rules = settings.rules || [];
    const connectedAlerts = seriesAlertList();
    const instances = systemAlerts.filter((alert) => alert.kind !== 'run-aborted');

    $('tvr-count-connected').textContent = rules.length;
    $('tvr-count-all-series').textContent = library ? library.length : rules.length;
    $('tvr-count-unconnected').textContent = library
      ? library.filter((series) => !ruleFor(series)).length : 0;
    $('tvr-count-presets').textContent = (settings.profiles || []).length;

    setBadge($('tvr-badge-series-all'), connectedAlerts);
    setBadge($('tvr-badge-series-connected'), connectedAlerts);
    setBadge($('tvr-badge-series-unconnected'), []);
    setBadge($('tvr-badge-media-connections'), instances);   // now under Settings
    setBadge($('tvr-badge-media-schedule'), []);
    setBadge($('tvr-badge-media-presets'), []);
    const failed = (snapshot.runs || []).slice(0, 1).filter((run) => (run.errors || []).length);
    setBadge($('tvr-badge-system-history'), failed.map(() => ({ severity: 'warning' })));

    // What the header counts is a setting; what the badges count is not. An acknowledged
    // alert stops being counted anywhere, but is still there to be found.
    const everything = connectedAlerts.concat(systemAlerts);
    const wanted = ((settings || {}).alerts || {}).header || 'all';
    const ranked = { errors: ['error'], warnings: ['error', 'warning'] }[wanted]
                   || ['error', 'warning', 'notice'];
    const counted = everything.filter((alert) => ranked.includes(alert.severity) && !alert.acknowledged);
    const total = $('tvr-alert-total');
    total.hidden = counted.length === 0;
    total.textContent = `${counted.length} ${counted.length === 1 ? 'alert' : 'alerts'}`;
    total.className = `tvr-alert-total ${worstSeverity(counted) || 'notice'}`;
  }

  // The one overview left: what is wrong, and where to go and fix it. Two things open it —
  // the count, and the Run button when something is stopping every run — because "why can
  // I not run?" and "what is wrong?" are the same question.
  function showEverythingNeedingAttention() {
    const everything = seriesAlertList().concat(systemAlerts);
    dialog('Everything needing attention', (body) => {
      if (!everything.length) { body.append(el('p', { textContent: 'Nothing.' })); return {}; }
      const byRule = new Map();
      everything.forEach((alert) => {
        const key = alert.rule_id || 'system';
        byRule.set(key, (byRule.get(key) || []).concat([alert]));
      });
      byRule.forEach((list, key) => {
        const rule = (settings.rules || []).find((candidate) => candidate.id === key);
        body.append(rule ? seriesAlertCard(rule, list, { hideOpen: false })
                         : systemAlertCard('TV Retention', list));
      });
      return {};
    }, null, 'Close');
  }
  $('tvr-alert-total').addEventListener('click', showEverythingNeedingAttention);

  function changeList(result, title, kind) {
    const wanted = kind && kind !== 'all' ? kind : null;
    const shows = (key) => !wanted || wanted === key;
    dialog(title, (body) => {
      const rows = result.rules.filter((rule) =>
        (shows('delete') && rule.deleted.length)
        || (shows('unmonitor') && (rule.unmonitor_list || []).length)
        || (shows('monitor') && (rule.monitor_list || []).length));
      const removals = shows('remove') ? (result.removals || []) : [];
      if (!rows.length && !removals.length) {
        body.append(el('p', { textContent: 'Nothing would change.' }));
        return {};
      }
      const totals = { delete: 0, delete_bytes: 0, monitor: 0, unmonitor: 0, newly_scoped: 0,
                       removals_by_action: {}, actionable: 0 };
      removals.forEach((record) => {
        totals.removals_by_action[record.action] = (totals.removals_by_action[record.action] || 0) + 1;
      });
      rows.forEach((rule) => {
        totals.delete += rule.deleted.length;
        totals.delete_bytes += rule.freed_bytes || 0;
        totals.monitor += (rule.monitor_list || []).length;
        totals.unmonitor += (rule.unmonitor_list || []).length;
        totals.newly_scoped += rule.newly_scoped || 0;
      });
      totals.actionable = totals.delete + totals.monitor + totals.unmonitor + removals.length;
      body.append(el('div', { className: 'tvr-plan-summary' }, [changeLines(totals, null)]));

      // A list of the affected series down the side: the quickest read of who is touched,
      // and a way to reach one without scrolling past the episodes of everything before it.
      const view = el('div', { className: 'tvr-change-view' });
      const side = el('div', { className: 'tvr-change-nav' });
      const main = el('div', { className: 'tvr-change-main' });
      const jump = (name, card, count, tone) => {
        const entry = el('button', { type: 'button', className: `tvr-change-jump ${tone}` }, [
          el('span', { className: 'tvr-change-jump-name', textContent: name }),
          el('span', { className: 'tvr-change-jump-count', textContent: String(count) }),
        ]);
        entry.addEventListener('click', () => {
          card.scrollIntoView({ block: 'start', behavior: 'smooth' });
          [...side.children].forEach((other) => other.classList.toggle('active', other === entry));
        });
        side.append(entry);
      };
      removals.forEach((record) => {
        const card = el('div', { className: 'tvr-change-series' }, [
          el('strong', { textContent: record.series_title }),
          el('div', { className: 'tvr-changes' }, [
            el('div', { className: 'tvr-plan delete', textContent: record.label })]),
        ]);
        main.append(card);
        jump(record.series_title, card, 1, (record.action || '').startsWith('delete') ? 'delete' : 'unmonitor');
      });
      rows.forEach((rule) => {
        const card = el('div', { className: 'tvr-change-series' });
        card.append(el('strong', { textContent: rule.series_title }));
        card.append(changeRows(rule, shows));
        main.append(card);
        jump(rule.series_title, card, card.querySelectorAll('.tvr-change').length,
             (shows('delete') && rule.deleted.length) ? 'delete'
             : ((shows('unmonitor') && (rule.unmonitor_list || []).length) ? 'unmonitor' : 'monitor'));
      });
      if (removals.length + rows.length < 2) view.classList.add('solo');
      view.append(side, main);
      body.append(view);
      return {};
    }, null, 'Close');
  }

  // The one control that waits on Sonarr, and it says so. Everything else on this page is
  // answered from the stored reading, which is why nothing else makes you wait.
  $('tvr-refresh-all').addEventListener('click', () => guarded('Reading Sonarr…', async () => {
    const data = await api('sync', {}, 'Reading Sonarr…');
    settings = data.settings;
    snapshot.settings = settings;
    applyHealth(data.health);
    applyAlerts(data.alerts);
    snapshot.plan = data.plan;
    snapshot.sync = data.sync;
    // Sonarr has just been read: what the page is holding is the reading before it.
    forgetLibrary();
    render();
    const report = data.report || {};
    const moved = [];
    if (report.series_added && report.series_added.length) moved.push(`${plural(report.series_added.length, 'series')} added`);
    if (report.series_changed) moved.push(`${plural(report.series_changed, 'series')} changed`);
    if (report.series_removed) moved.push(`${plural(report.series_removed, 'series')} gone`);
    if ((report.episodes_changed || []).length) moved.push(`${plural(report.episodes_changed.length, 'managed series')} moved`);
    notice(moved.length ? `Synced with Sonarr — ${moved.join(', ')}.` : 'Synced with Sonarr. Nothing had changed.', 'ok');
  }));

  $('tvr-run').addEventListener('click', () => guarded('', async () => {
    if (runState() === 'blocked') return void showEverythingNeedingAttention();
    const runnable = (settings.rules || []).filter((rule) => rule.enabled && !isBlocked(rule.id));
    if (!runnable.length) throw new Error('There are no enabled series ready to run.');
    const plan = snapshot.plan || {};
    // The confirmation states the actual plan rather than describing runs in general.
    let warning = `Run ${plural(runnable.length, 'series')} now?\n\n`;
    warning += plan.actionable ? `This will ${planText(plan)}.\n\n` : 'No changes are currently expected.\n\n';
    if (testMode()) {
      warning += 'Test mode is on, so this changes nothing: it reports exactly what it would '
        + 'have done and writes neither to your files nor to Sonarr.';
    } else {
      warning += 'This deletes episode files through Sonarr and cannot be undone from here.';
    }
    if (!window.confirm(warning)) return;
    const data = await api('run', {}, 'Running…');
    await refresh();
    showResult(data.result, 'Run');
  }));

  // -- series ------------------------------------------------------------

  function presetFor(rule) {
    return (settings.profiles || []).find((preset) => preset.id === rule.profile_id) || null;
  }

  function ruleSummary(rule) {
    const source = presetFor(rule) || rule;
    const parts = [];
    if (source.keep_days) parts.push(`keep ${plural(source.keep_days, 'day')}`);
    if (source.keep_episodes) parts.push(`keep ${plural(source.keep_episodes, 'episode')}`);
    if (source.keep_seasons) parts.push(`keep ${plural(source.keep_seasons, 'season')}`);
    return parts;
  }

  // A small round badge, left of the title, the way a count belongs. It carries the worst
  // severity present and nothing else: the detail is one click away and does not need to
  // compete with the series name for space.
  function alertBadge(rule) {
    const list = seriesAlerts(rule.id);
    if (isChecking(rule.id)) {
      return el('span', { className: 'tvr-dot-badge checking', title: 'Reading this series from Sonarr', textContent: '' });
    }
    if (!list.length) {
      const state = monitoring[rule.id];
      return el('span', {
        className: `tvr-dot-badge ${state ? 'clear' : 'unknown'}`,
        title: state ? `No problems — ${syncedAgo() || 'read ' + ago(state.read_at || state.checked_at)}` : 'Not checked yet',
        textContent: '',
      });
    }
    const severity = worstSeverity(list);
    const blocked = isBlocked(rule.id);
    const button = el('button', {
      type: 'button',
      className: `tvr-dot-badge ${severity}${blocked ? ' blocked' : ''}`,
      textContent: String(list.length),
      title: (blocked ? 'Blocked — skipped by every run.\n' : '')
             + list.map((alert) => `• ${alert.title}: ${alert.detail}`).join('\n'),
    });
    button.addEventListener('click', () => showSeriesAlerts(rule));
    return button;
  }



  // Opens the series where it actually lives. Sonarr's own slug, so no URL is guessed.
  function sonarrLink(rule) {
    const instance = (settings.instances || []).find((i) => i.id === rule.instance_id);
    if (!instance || !rule.slug) return null;
    const link = el('a', { className: 'tvr-sonarr-link', target: '_blank', rel: 'noopener',
                           href: `${instance.url.replace(/\/+$/, '')}/series/${encodeURIComponent(rule.slug)}`,
                           title: `Open ${rule.series_title} in Sonarr` });
    link.append(el('i', { className: 'fa fa-external-link' }));
    return link;
  }

  // Remembered per browser, because it is a preference about looking rather than a
  // setting about behaviour — it belongs to the person at the screen, not to the plugin.
  const remember = (name, value) => { try { localStorage.setItem(`tvr.${name}`, value); } catch (error) { /* private window */ } };
  const remembered = (name, fallback) => {
    try { return localStorage.getItem(`tvr.${name}`) || fallback; } catch (error) { return fallback; }
  };
  let layout = remembered('layout', 'list');

  // Three states, because "follow the system" is a real answer rather than the absence of
  // one — and the two explicit ones have to win in both directions, or somebody on a dark
  // desktop can never choose light. The attribute goes on <html>: the body's background is
  // painted from the same tokens, and it is outside this element.
  const THEMES = [
    ['auto', 'fa-adjust', 'Theme: follows your system. Click for light.'],
    ['light', 'fa-sun-o', 'Theme: light. Click for dark.'],
    ['dark', 'fa-moon-o', 'Theme: dark. Click to follow your system.'],
  ];
  function applyTheme(name) {
    const [chosen, icon, title] = THEMES.find(([value]) => value === name) || THEMES[0];
    if (chosen === 'auto') delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = chosen;
    const button = $('tvr-theme');
    button.replaceChildren(el('i', { className: `fa ${icon}` }));
    button.title = title;
    remember('theme', chosen);
  }
  applyTheme(remembered('theme', 'auto'));
  $('tvr-theme').addEventListener('click', () => {
    const at = THEMES.findIndex(([value]) => value === remembered('theme', 'auto'));
    applyTheme(THEMES[(at + 1) % THEMES.length][0]);
  });
  let library = null;          // every series Sonarr holds, from the stored reading
  let libraryLoading = false;
  const LIBRARY_LIMIT = 150;

  // Loaded from the store, and reloaded whenever something makes the copy in hand wrong —
  // a sync, or a rule that has just been added. Opening a view is never a reason to ask
  // Sonarr anything: the sync already did, and its age is stated at the top of the page.
  async function loadLibrary() {
    if (libraryLoading) return;
    libraryLoading = true;
    try {
      const gathered = [];
      for (const instance of (settings.instances || [])) {
        if (instance.enabled === false) continue;
        gathered.push(...await seriesFor(instance.id, ''));
      }
      library = gathered;
    } finally {
      libraryLoading = false;
    }
    renderLibrary();
  }

  // Whoever invalidates the copy in hand does not have to remember to reload it: the
  // render notices and asks. Forgetting that left the list saying "reading the stored
  // library" until something else happened to navigate.
  function forgetLibrary() {
    library = null;
    seriesCache = {};
    if (LIBRARY[currentView]) loadLibrary().catch(() => { libraryLoading = false; });
  }

  const ruleFor = (series) => (settings.rules || []).find(
    (rule) => rule.series_id === series.series_id && rule.instance_id === series.instance_id) || null;

  // What the three sidebar items and the two toggles come to, together.
  // Two lists out of one library: everything the filters allow, and — separately —
  // everything with an alert, whatever the filters say. A series with a problem is not
  // hidden by being on the wrong tab, which is what an "alerts only" toggle was for and
  // why it could be left switched off with the problem still there.
  //
  // Search still applies to both. It is a question rather than a filter: typing a title
  // and being shown thirty unrelated series with alerts would not be help.
  // Whether the next run will do anything at all to this series. A queued removal counts
  // even on a switched-off rule: removals are the one thing that ignores the enabled flag,
  // and hiding the only destructive thing still going to happen would be the wrong way
  // round. A blocked series never counts, because no run will reach it.
  function scheduledFor(rule) {
    if (!rule) return false;
    if (queuedRemoval(rule)) return true;
    if (!rule.enabled || isBlocked(rule.id)) return false;
    const plan = (monitoring[rule.id] || {}).plan;
    return !!(plan && (plan.delete || plan.monitor || plan.unmonitor));
  }

  // Three questions about the same library, so three passes over it rather than three
  // lists kept in step. `alerts` and `scheduled` ignore the filters on purpose: a problem
  // is not less true for being on another tab. Search still narrows every one of them,
  // because search is a question rather than a filter.
  function visibleLibrary(mode, useSearch = true) {
    if (library === null) return [];
    const term = useSearch ? ($('tvr-search').value || '').trim().toLowerCase() : '';
    const hideEnded = $('tvr-hide-ended').checked;
    const rows = [];
    library.forEach((series) => {
      const rule = ruleFor(series);
      const alertsHere = rule ? seriesAlerts(rule.id) : [];
      if (mode === 'alerts') {
        if (!alertsHere.length) return;
      } else if (mode === 'scheduled') {
        if (!scheduledFor(rule)) return;
      } else {
        if (libraryFilter === 'connected' && !rule) return;
        if (libraryFilter === 'unconnected' && rule) return;
        // The toggle hides what is ended *and* unmanaged. A connected series is never
        // hidden: it is your own rule, and an ended one is where retention matters most.
        if (hideEnded && series.ended && !rule) return;
      }
      if (term && !(`${series.title} ${series.path || ''}`.toLowerCase().includes(term))) return;
      rows.push({ series, rule, alerts: alertsHere });
    });
    // One order, applied to both lists. Sorting problems to the top of the main list was
    // how you found them before there was a section for them; doing both puts the same
    // series in two places for the same reason, and makes the list underneath jump about
    // as alerts come and go.
    const order = $('tvr-sort').value;
    const byTitle = (a, b) => String(a.series.sort_title || a.series.title)
      .localeCompare(String(b.series.sort_title || b.series.title));
    rows.sort((a, b) => (
      order === 'title-desc' ? -byTitle(a, b)
      : order === 'added' ? String(b.series.added || '').localeCompare(String(a.series.added || ''))
      : order === 'size' ? (b.series.size_on_disk || 0) - (a.series.size_on_disk || 0)
      : order === 'episodes' ? (b.series.total_episode_count || 0) - (a.series.total_episode_count || 0)
      : order === 'keep' ? keepRank(a.rule) - keepRank(b.rule)
      : byTitle(a, b)));
    return rows;
  }

  const keepRank = (rule) => {
    if (!rule) return Number.MAX_SAFE_INTEGER;
    const active = presetFor(rule) || rule;
    return Number(active.keep_days || active.keep_episodes || active.keep_seasons || Number.MAX_SAFE_INTEGER);
  };

  function posterNode(series, className, rule) {
    const art = el('div', { className: className || 'tvr-poster' });
    if (rule && queuedRemoval(rule)) {
      art.append(el('span', { className: 'tvr-queued-x', textContent: '×',
                              title: 'Queued for removal at the next run' }));
    }
    if (series.poster) {
      // Sonarr's own path for the artwork, carried through as a stamp rather than read:
      // it changes when the artwork does, and it is the only thing that can tell the
      // cache the picture is a different picture. Without it the proxy kept the first
      // poster it ever fetched, for good.
      // A GET on its own route, guarded by the session cookie the browser already sends.
      art.append(el('img', { loading: 'lazy', alt: '',
                             src: `/poster?series=${series.series_id}`
                                  + `&instance=${encodeURIComponent(series.instance_id)}`
                                  + `&stamp=${encodeURIComponent(series.poster)}` }));
    } else {
      art.textContent = (series.title || '?').slice(0, 1);
    }
    return art;
  }

  // Hard sizes rather than a poster fitted to whatever box the layout produced. The two
  // columns are the two layouts; the row is the step on the slider.
  const SCALES = {
    grid: [[100, 143], [125, 179], [150, 215], [175, 250], [200, 286]],
    list: [[25, 36], [38, 54], [50, 72], [75, 107], [100, 143]],
  };
  // Three is the size the type was drawn for, so it is 100% and the rest step around it.
  const FONT_SCALE = [0.8, 0.9, 1, 1.1, 1.2];
  let scale = Math.min(5, Math.max(1, Number(remembered('scale', '3')) || 3));
  let showSleeping = !!remembered('sleeping', '');

  function applyScale(container) {
    const [width, height] = SCALES[layout === 'grid' ? 'grid' : 'list'][scale - 1];
    container.style.setProperty('--poster-w', `${width}px`);
    container.style.setProperty('--poster-h', `${height}px`);
    container.style.setProperty('--card-font', String(FONT_SCALE[scale - 1]));
  }

  const cardsInto = (box, rows, section) => rows.forEach(
    (row) => box.append(libraryCard(Object.assign({ section }, row))));

  // A band and the list under it. Collapsing is remembered per section, because which of
  // the three you are working in is a habit rather than a decision — and Scheduled actions
  // starts closed, since it answers a question you go and ask rather than one you want
  // answered every time you open the page.
  const BAND_OPEN = { attention: true, scheduled: false, all: true };
  const bandOpen = (name) => (remembered(`band.${name}`, '') || (BAND_OPEN[name] ? 'open' : 'shut')) === 'open';

  // What a band is currently showing of what it holds. Off hides the rows; it never makes
  // the alert stop counting — the header total and the card badges are about what is true,
  // and this is about what you want in front of you while you work through it.
  const bandShows = (key) => remembered(`show.${key}`, 'yes') === 'yes';
  const setBandShows = (key, on) => { remember(`show.${key}`, on ? 'yes' : 'no'); renderLibrary(); };

  // Severity for the attention band, kind of work for the scheduled one. Errors are absent
  // on purpose: one stops a series from running, and hiding that would not stop it being
  // true — the same reason an error can never be acknowledged.
  const ATTENTION_FILTERS = [
    ['warning', 'Warnings', (row) => worstSeverity(row.alerts) === 'warning'],
    ['notice', 'Notices', (row) => worstSeverity(row.alerts) === 'notice'],
  ];
  const SCHEDULED_FILTERS = [
    ['removals', 'Series removals', (row) => !!queuedRemoval(row.rule)],
    ['deletions', 'Episode deletions', (row) => !!((monitoring[row.rule.id] || {}).plan || {}).delete],
    ['monitoring', 'Monitoring changes', (row) => {
      const plan = (monitoring[row.rule.id] || {}).plan || {};
      return !!(plan.monitor || plan.unmonitor);
    }],
  ];

  // A row survives if any kind it carries is switched on. A row carrying only kinds that
  // are switched off is what the toggles are for; one carrying none of them — an error in
  // the attention band — is never filtered out by them.
  function applyBandFilters(rows, filters) {
    return rows.filter((row) => {
      const carried = filters.filter(([, , holds]) => holds(row));
      return !carried.length || carried.some(([key]) => bandShows(key));
    });
  }

  function bandFilterSwitches(band, rows, filters) {
    filters.forEach(([key, label, holds]) => {
      const count = rows.filter(holds).length;
      if (!count) return;          // nothing of this kind: nothing to offer hiding
      const control = toggle(`${label} (${count})`, bandShows(key),
                             (on) => setBandShows(key, on), { className: 'tvr-band-switch' });
      band.head.append(control.node);
    });
  }

  function sectionBand(name, label, shown, total) {
    const open = bandOpen(name);
    const box = el('div', { className: layout === 'grid' ? 'tvr-rules tvr-rules-grid' : 'tvr-rules',
                            hidden: !open });
    const toggleButton = el('button', { type: 'button', className: 'tvr-band-toggle',
                                        'aria-expanded': String(open) }, [
      el('i', { className: `fa fa-caret-${open ? 'down' : 'right'}` }),
      el('span', { textContent: label }),
      // Both numbers, because the pair is the information: the first alone cannot say
      // whether the filters are hiding anything.
      el('span', { className: 'tvr-band-count', textContent: `${shown}/${total}` }),
    ]);
    toggleButton.addEventListener('click', () => {
      remember(`band.${name}`, open ? 'shut' : 'open');
      renderLibrary();
    });
    return { head: el('div', { className: 'tvr-band' }, [toggleButton]), box };
  }

  function renderLibrary() {
    const container = $('tvr-rules');
    container.className = 'tvr-library';
    container.replaceChildren();
    applyScale(container);
    if (library === null) {
      $('tvr-rules-empty').hidden = true;
      container.append(el('p', { className: 'tvr-empty', textContent: 'Reading the stored library…' }));
      if (!libraryLoading) loadLibrary().catch(() => { libraryLoading = false; });
      return;
    }
    if (bulkChecking) {
      $('tvr-rules-empty').hidden = true;
      container.append(el('div', { className: 'tvr-empty' },
                          [el('span', { className: 'tvr-spinner' }), text(' Reading from Sonarr…')]));
      return;
    }
    const rows = visibleLibrary('filtered');
    const alerting = visibleLibrary('alerts');
    const scheduled = visibleLibrary('scheduled');
    // A switched-off series raises nothing, so it is not in the section about things that
    // need doing — but it is still counted, and offered, because "I turned that off and
    // forgot" is a real way to lose track of a problem.
    const attention = alerting.filter((row) => row.rule && row.rule.enabled);
    const sleeping = alerting.filter((row) => row.rule && !row.rule.enabled);
    $('tvr-rules-empty').hidden = rows.length > 0 || alerting.length > 0 || scheduled.length > 0;

    if (attention.length || sleeping.length) {
      const held = attention.concat(showSleeping ? sleeping : []);
      const shown = applyBandFilters(held, ATTENTION_FILTERS);
      const band = sectionBand('attention', 'Needs attention', shown.length,
                               visibleLibrary('alerts', false).length);
      if (sleeping.length) {
        const show = toggle(`Show ${plural(sleeping.length, 'disabled series')}`, showSleeping,
                            (on) => { showSleeping = on; remember('sleeping', on ? '1' : ''); renderLibrary(); },
                            { className: 'tvr-band-switch' });
        band.head.append(show.node);
      }
      bandFilterSwitches(band, held, ATTENTION_FILTERS);
      cardsInto(band.box, shown, 'attention');
      container.append(band.head, band.box);
    }

    // Every series the next run will touch, whatever the filters say. The header already
    // counts the changes; this says which shows they land on.
    if (scheduled.length) {
      const shown = applyBandFilters(scheduled, SCHEDULED_FILTERS);
      const band = sectionBand('scheduled', 'Scheduled actions', shown.length,
                               visibleLibrary('scheduled', false).length);
      bandFilterSwitches(band, scheduled, SCHEDULED_FILTERS);
      cardsInto(band.box, shown, 'scheduled');
      container.append(band.head, band.box);
    }

    // Always, unlike the two above it. The count is the reason: "573 of 3022" is the
    // answer to why a show you expected is not on screen, and that question is asked far
    // more often than it is worth saving a line to avoid.
    const band = sectionBand('all', 'All', rows.length, library.length);
    // Three thousand cards is not a list anyone reads, and it is not a page any browser
    // enjoys laying out. Search and the filters are how you get to the rest.
    cardsInto(band.box, rows.slice(0, LIBRARY_LIMIT), 'all');
    container.append(band.head, band.box);
    if (rows.length > LIBRARY_LIMIT) {
      container.append(el('p', { className: 'tvr-empty',
                                 textContent: `${rows.length - LIBRARY_LIMIT} more — search, or narrow the filters.` }));
    }
  }
  const renderRules = renderLibrary;

  // One card for a series, whether or not it has a rule. The check is the difference, and
  // it is the only difference the eye needs: everything else follows from it.
  function libraryCard(row) {
    return layout === 'grid' ? gridCard(row) : listCard(row);
  }

  // The frame carries the state: green where a rule runs, dim where one is turned off,
  // plain where there is no rule yet. A tick saying "this has a rule" said the same thing
  // twice, and the retention pill says it a third time on hover.
  // The border is the state, and only one thing can be said at a time, so they are ranked:
  // what you are looking at, then what is wrong with it, then whether it is yours, then
  // that it is merely known about. Severity orders itself within the second.
  function cardTone(row) {
    const { series, rule } = row;
    if (isOpen(rule)) return 'selected';
    const worst = rule ? worstSeverity(seriesAlerts(rule.id)) : '';
    if (worst) return `alert-${worst}`;
    if (rule) return 'watched';
    return 'loose';
  }

  function cardShell(row) {
    const { series, rule } = row;
    const marks = ['tvr-rule', cardTone(row)];
    if (rule && !rule.enabled) marks.push('disabled');
    if (rule && queuedRemoval(rule)) marks.push('queued');
    // Grey means "finished, and it matters that it has". In the two bands above, that is
    // the whole point. In the library it depends on whether the series is yours: one you
    // watch has a retention decision behind it that will now only ever shrink, and one you
    // do not is just something Sonarr happens to hold — greying three thousand of those
    // would be a different page.
    if (series.ended && (row.section !== 'all' || rule)) marks.push('ended');
    const card = el('div', { className: marks.join(' ') });
    card.addEventListener('click', (event) => {
      if (event.target.closest('button, input, select, a, label')) return;
      if (isOpen(rule)) { editing = null; renderLibrary(); renderDetails(); return; }
      openEditor(rule, rule ? undefined : series);
      renderLibrary();
    });
    return card;
  }

  const seriesFacts = (series) => [series.year, series.network,
    series.season_count ? plural(series.season_count, 'season') : '',
    series.total_episode_count ? `${series.episode_file_count}/${series.total_episode_count} episodes` : '',
    series.size_on_disk ? bytes(series.size_on_disk) : '',
    series.ended ? 'ended' : (series.next_airing ? `next ${when(series.next_airing)}` : '')].filter(Boolean);

  // What the next run would do, as a badge and a number. The detail is a tooltip because
  // on a poster there is room for the count and nothing else.
  const CHANGE_MARKS = [
    ['delete', 'fa-trash', (n, plan) => `${plural(n, 'episode')} scheduled for deletion (${bytes(plan.delete_bytes)})`],
    ['monitor', 'fa-bookmark', (n) => `${plural(n, 'episode')} will be set to monitored`],
    ['unmonitor', 'fa-bookmark-o', (n) => `${plural(n, 'episode')} will be set to unmonitored`],
  ];

  function changeMarks(rule, plan) {
    const box = el('div', { className: 'tvr-card-changes' });
    CHANGE_MARKS.forEach(([kind, icon, describe]) => {
      const count = plan[kind] || 0;
      if (!count) return;
      const mark = el('button', { type: 'button', className: `tvr-card-mark ${kind}`,
                                  title: describe(count, plan) }, [
        el('i', { className: `fa ${icon}` }), el('span', { textContent: String(count) }),
      ]);
      mark.addEventListener('click', (event) => {
        event.stopPropagation();
        guarded('', async () => {
          const data = await api('preview', { rule_ids: [rule.id] }, 'Working out what would change…');
          changeList(data.result, `${rule.series_title}: scheduled changes`, kind);
        });
      });
      box.append(mark);
    });
    return box;
  }

  // Severities stacked, worst first, each with its own count. One glance says both what
  // kind of trouble and how much.
  function alertMarks(rule) {
    const box = el('div', { className: 'tvr-card-alerts' });
    const found = seriesAlerts(rule.id);
    ['error', 'warning', 'notice'].forEach((severity) => {
      const here = found.filter((alert) => alert.severity === severity && !alert.acknowledged);
      if (!here.length) return;
      const mark = el('button', { type: 'button', className: `tvr-card-alert ${severity}`,
                                  title: here.map((alert) => `${alert.title}: ${alert.detail}`).join('\n') },
                      [el('span', { textContent: String(here.length) })]);
      mark.addEventListener('click', (event) => { event.stopPropagation(); showSeriesAlerts(rule); });
      box.append(mark);
    });
    return box;
  }

  function retentionPill(rule) {
    if (!rule) return el('span', { className: 'tvr-card-pill loose', textContent: 'No rule — click to add' });
    const preset = presetFor(rule);
    const detail = preset ? presetSummary(preset).join(', ') : ruleSummary(rule).join(' · ');
    return el('span', { className: `tvr-card-pill${preset ? ' preset' : ''}`,
                        title: detail || 'no keep window set',
                        textContent: preset ? preset.name : 'Custom' });
  }

  // Poster first, name under it, everything else laid over the artwork: alerts top right,
  // what the next run would do down the left, and the retention on hover along the bottom.
  function gridCard(row) {
    const { series, rule } = row;
    const card = cardShell(row);
    const art = posterNode(series, 'tvr-poster tvr-poster-row', rule);
    if (rule) {
      const state = monitoring[rule.id] || {};
      const plan = state.plan;
      if (plan && (plan.delete || plan.monitor || plan.unmonitor)) art.append(changeMarks(rule, plan));
      art.append(alertMarks(rule));
    }
    // Always there, unlike the retention pill below it: grayscale says something is
    // different about this poster, and this says what.
    if (series.ended) art.append(el('span', { className: 'tvr-card-ended', textContent: 'Ended' }));
    art.append(retentionPill(rule));
    card.append(art);
    card.append(el('div', { className: 'tvr-rule-main' }, [
      el('div', { className: 'tvr-grid-title', textContent: series.title, title: series.title }),
      el('div', { className: 'tvr-grid-sub', textContent: [series.year, series.network].filter(Boolean).join(' · ') }),
    ]));
    return card;
  }

  function listCard(row) {
    const { series, rule } = row;
    const blocked = rule ? isBlocked(rule.id) : false;
    const card = cardShell(row);
    card.append(posterNode(series, 'tvr-poster tvr-poster-row', rule));
    const main = el('div', { className: 'tvr-rule-main' });

    const head = el('div', { className: 'tvr-rule-head' });
    if (rule) head.append(alertBadge(rule));
    head.append(el('span', { className: `tvr-connected ${rule ? 'yes' : 'no'}`,
                             title: rule ? 'Has a retention rule' : 'No rule yet' },
                   [el('i', { className: `fa fa-${rule ? 'check-circle' : 'circle-o'}` })]));
    head.append(el('span', { className: 'tvr-rule-title', textContent: series.title }));
    main.append(head);
    main.append(el('div', { className: 'tvr-card-series-facts', textContent: seriesFacts(series).join(' · ') }));

    if (rule) {
      const retention = el('span', { className: 'tvr-retention' });
      const preset = presetFor(rule);
      if (preset) retention.append(el('span', { className: 'tvr-chip preset', textContent: preset.name }));
      else ruleSummary(rule).forEach((label) => retention.append(el('span', { className: 'tvr-chip', textContent: label })));
      main.append(retention);

      const state = monitoring[rule.id] || {};
      const plan = state.plan;
      if (isChecking(rule.id)) {
        main.append(el('div', { className: 'tvr-plan-quiet', textContent: 'Reading from Sonarr…' }));
      } else if (blocked) {
        main.append(el('div', { className: 'tvr-plan-quiet', textContent: 'Blocked — nothing will run for this series.' }));
      } else if (plan && (plan.delete || plan.monitor || plan.unmonitor)) {
        main.append(changeLines(plan, (kind) => guarded('', async () => {
          const data = await api('preview', { rule_ids: [rule.id] }, 'Working out what would change…');
          changeList(data.result, `${rule.series_title}: scheduled changes`, kind);
        })));
      } else if (plan) {
        main.append(el('div', { className: 'tvr-plan-quiet', textContent: 'Nothing scheduled.' }));
      }
    }
    card.append(main);
    return card;
  }

  const applyLayout = () => {
    ['list', 'grid'].forEach((other) => $(`tvr-layout-${other}`).classList.toggle('active', other === layout));
  };
  ['list', 'grid'].forEach((mode) => {
    $(`tvr-layout-${mode}`).addEventListener('click', () => {
      layout = mode;
      remember('layout', mode);
      applyLayout();
      renderLibrary();
    });
  });
  // The slider only ever changes numbers on the container, so it redraws nothing: the
  // cards already on screen resize under it as it moves.
  $('tvr-scale').value = String(scale);
  $('tvr-scale').addEventListener('input', () => {
    scale = Number($('tvr-scale').value) || 3;
    remember('scale', scale);
    applyScale($('tvr-rules'));
  });
  applyLayout();
  ['tvr-hide-ended', 'tvr-search', 'tvr-sort'].forEach((id) => {
    $(id).addEventListener('input', renderLibrary);
    $(id).addEventListener('change', renderLibrary);
  });

  // -- alerts ------------------------------------------------------------
  // One card per series, not one per problem. The series is the thing you act on, so it
  // owns the card; each problem inside it is a short labelled line. Severity is carried by
  // the card frame and the badge, and nowhere else — a card tinted end to end says nothing
  // a coloured edge does not.
  const ALERT_TAG = {
    'unmatched': 'No Sonarr match',
    'folder-missing': 'Folder missing',
    'unknown-files': 'Unknown files',
    'ended': 'Series ended',
    'ended-expired': 'Series ended',
    'sonarr-unreachable': 'Sonarr unreachable',
  };
  const ACTION_LABEL = {
    'rematch': 'Re-check against Sonarr',
    'remove-rule': 'Remove from TV Retention',
    'open-instance': 'Open Sonarr settings',
    'test-instance': 'Test the connection',
    'enable-recycle-bin': 'Give Sonarr a recycle bin',
  };

  // A small mark before each problem, sized and coloured by rule. Geometric characters
  // rather than emoji: these have no colour-emoji presentation to fall back to, so they
  // render at the size the stylesheet asks for on every platform.
  function alertItem(alert) {
    const item = el('div', { className: 'tvr-alert-item' });
    const line = el('div', { className: 'tvr-alert-line' }, [
      el('span', { className: `tvr-sev ${alert.severity}`, title: alert.severity }),
      el('span', { className: 'tvr-alert-kind', textContent: ALERT_TAG[alert.kind] || alert.title }),
      el('span', { className: 'tvr-alert-detail', textContent: alert.detail }),
    ]);
    if (alert.blocking) line.append(el('span', { className: 'tvr-tag blocking', textContent: 'blocks runs' }));
    line.append(el('span', { className: 'tvr-alert-age', textContent: ago(alert.first_seen) }));
    item.append(line);
    // The explanation is the point of a notice — hiding it behind a link left one saying
    // nothing at all. It is short, so it stays on the page.
    item.append(el('p', { className: 'tvr-alert-help', textContent: alert.help }));

    const foot = el('div', { className: 'tvr-alert-foot' });
    if (alert.action) {
      const button = el('button', { type: 'button', className: 'tvr-action',
                                    textContent: `Quick action: ${ACTION_LABEL[alert.action] || 'Fix'}` });
      button.addEventListener('click', () => runAlertAction(alert));
      foot.append(button);
    }
    // Acknowledging is not dismissing: it hides this alert as it stands, and the alert
    // comes back if what it says changes. An error is never offered it.
    const ackable = alert.severity !== 'error' && !alert.blocking
                    && ((settings || {}).alerts || {}).acknowledge !== false;
    if (ackable) {
      const ack = el('button', { type: 'button', className: 'tvr-small',
                                 textContent: alert.acknowledged ? 'Show again' : 'Acknowledge' });
      ack.addEventListener('click', () => guarded('', async () => {
        const data = await api('acknowledge', { key: alert.key, undo: !!alert.acknowledged },
                               'Saving…', true);
        applyAlerts(data.alerts);
        render();
        notice(alert.acknowledged ? 'Shown again.' : 'Acknowledged — it will return if it changes.', 'ok');
      }));
      foot.append(ack);
    }
    if (alert.acknowledged) item.classList.add('acknowledged');
    if ((alert.data || {}).files) {
      const files = el('div', { className: 'tvr-alert-files', hidden: true });
      ((alert.data || {}).files || []).slice(0, 10).forEach((path) =>
        files.append(el('div', { className: 'tvr-mono', textContent: path })));
      const show = el('button', { type: 'button', className: 'tvr-link', textContent: `List ${alert.count} file(s)` });
      show.addEventListener('click', () => { files.hidden = !files.hidden; });
      foot.append(show);
      item.append(files);
    }
    if (foot.children.length) item.append(foot);
    return item;
  }

  function seriesAlertCard(rule, list, config) {
    const options = config || {};
    const severity = worstSeverity(list);
    const blocked = list.some((alert) => alert.blocking);
    // Compact is the series editor, where the panel above has already named the series,
    // counted its issues, dated the reading and offered a re-read. Repeating all four
    // under a heading made an ended series with no episodes say the same thing four ways.
    const compact = !!options.compact;
    const card = el('div', { className: `tvr-alert-card ${severity}${compact ? ' compact' : ''}` });
    if (!compact) {
      const head = el('div', { className: 'tvr-alert-card-head' }, [
        el('span', { className: 'tvr-rule-title', textContent: rule.series_title || rule.path }),
        el('span', { className: 'tvr-alert-count', textContent: plural(list.length, 'issue') }),
      ]);
      if (blocked) head.append(el('span', { className: 'tvr-tag blocking', textContent: 'blocked' }));
      const state = monitoring[rule.id];
      head.append(el('span', { className: 'tvr-alert-age',
                               textContent: state ? `Sonarr read ${ago(state.read_at || state.checked_at)}` : 'not checked' }));
      card.append(head);
    }
    list.forEach((alert) => card.append(alertItem(alert)));
    if (compact) return card;
    const foot = el('div', { className: 'tvr-alert-foot' });
    if (!options.hideOpen) {
      const open = el('button', { type: 'button', className: 'tvr-small', textContent: 'Show in Series' });
      open.addEventListener('click', () => {
        showView('series-all');
        $('tvr-search').value = rule.series_title || rule.path;
        renderRules();
      });
      foot.append(open);
    }
    const recheck = el('button', { type: 'button', className: 'tvr-small', textContent: 'Re-check now' });
    recheck.addEventListener('click', () => { $('tvr-dialog').close('cancel'); queueChecks([rule.id], true); });
    foot.append(recheck);
    card.append(foot);
    return card;
  }

  function systemAlertCard(instanceName, list) {
    const severity = worstSeverity(list);
    const card = el('div', { className: `tvr-alert-card ${severity}` });
    card.append(el('div', { className: 'tvr-alert-card-head' }, [
      el('span', { className: 'tvr-rule-title', textContent: instanceName }),
      el('span', { className: 'tvr-alert-count', textContent: plural(list.length, 'issue') }),
    ]));
    list.forEach((alert) => card.append(alertItem(alert)));
    return card;
  }

  function runAlertAction(alert) {
    return guarded('', async () => {
      if (alert.action === 'enable-recycle-bin') {
        // Writes to Sonarr's own configuration, so it asks for the path and says plainly
        // that the change applies to everything Sonarr deletes.
        const instance = (settings.instances || []).find((i) => i.id === alert.instance_id);
        dialog('Give Sonarr a recycle bin', (body) => {
          body.append(el('p', { textContent:
            'Sonarr will move deleted files here instead of removing them, and clean the folder '
            + 'out after a while. This changes Sonarr’s own setting, so it applies to everything '
            + 'Sonarr deletes — not only to TV Retention.' }));
          const path = el('input', { type: 'text', spellcheck: false, placeholder: '/tv/.recycle',
                                     value: '' });
          body.append(field('Recycle bin path, as Sonarr sees it', path,
                            'A path inside Sonarr, on the same filesystem as your library so moves '
                            + 'are instant. Sonarr creates it if it does not exist.'));
          return { path };
        }, async (inner) => {
          const data = await api('enable-recycle-bin',
                                 { instance_id: alert.instance_id, path: inner.path.value.trim() },
                                 'Updating Sonarr…');
          queueChecks((settings.rules || []).filter((r) => r.instance_id === alert.instance_id)
            .map((r) => r.id).slice(0, 1));
          notice(data.ok_message, 'ok');
        }, 'Set it');
        return;
      }
      if (alert.action === 'open-instance' || alert.action === 'test-instance') {
        showView('settings-connections');
        const instance = (settings.instances || []).find((i) => i.id === alert.instance_id);
        if (instance) editInstance(instance);
        return;
      }
      const data = await api('alert-action', { kind: alert.action, rule_id: alert.rule_id },
                             'Applying the fix…');
      if (data.settings) { settings = data.settings; snapshot.settings = settings; }
      if (data.monitoring) monitoring[alert.rule_id] = data.monitoring;
      const fresh = await api('alerts', {}, '', true);
      applyAlerts(fresh.alerts);
      snapshot.alert_summary = fresh.summary;
      render();
      notice(data.ok_message || 'Done.', 'ok');
    });
  }

  function showSeriesAlerts(rule) {
    dialog(rule.series_title || rule.path, (body) => {
      const list = seriesAlerts(rule.id);
      const blocked = isBlocked(rule.id);
      body.append(el('div', { className: `tvr-status ${blocked ? 'bad' : 'ok'}` }, [
        el('strong', { textContent: blocked ? 'Blocked' : 'Runs normally' }),
        el('span', { textContent: blocked
          ? 'Skipped by every run until the errors below are resolved.'
          : 'The items below are advisory and do not stop this series.' }),
      ]));
      body.append(seriesAlertCard(rule, list, { hideOpen: true }));
      return {};
    }, null, 'Close');
  }

  function renderAlerts() {
    // The counts describe what this tab shows — system problems — with series problems
    // summarised by the roll-up, because they are acted on from the series card.
    const system = systemAlerts.slice();
    const matches = () => true;
    const systemBox = $('tvr-alerts-system');
    systemBox.replaceChildren();
    const shown = system.filter(matches);
    $('tvr-alerts-system-empty').hidden = shown.length > 0;
    const byInstance = new Map();
    shown.forEach((alert) => {
      const instance = (settings.instances || []).find((i) => i.id === alert.instance_id);
      const name = instance ? instance.name : 'TV Retention';
      byInstance.set(name, (byInstance.get(name) || []).concat([alert]));
    });
    byInstance.forEach((list, name) => systemBox.append(systemAlertCard(name, list)));
  }

  $('tvr-recheck-all').addEventListener('click', () => guarded('', async () => {
    const ids = (settings.rules || []).filter((rule) => rule.enabled).map((rule) => rule.id);
    if (!ids.length) throw new Error('There are no enabled series to check.');
    queueChecks(ids, true);
    notice(`Re-reading ${plural(ids.length, 'series')} in the background.`, 'ok');
  }));

  // -- presets -----------------------------------------------------------
  function presetSummary(preset) {
    const parts = [];
    if (preset.keep_days) parts.push(`keep ${plural(preset.keep_days, 'day')}`);
    if (preset.keep_episodes) parts.push(`keep ${plural(preset.keep_episodes, 'episode')}`);
    if (preset.keep_seasons) parts.push(`keep ${plural(preset.keep_seasons, 'season')}`);
    return parts;
  }

  function renderPresets() {
    const container = $('tvr-presets');
    const presets = settings.profiles || [];
    container.replaceChildren();
    $('tvr-presets-empty').hidden = presets.length > 0;
    presets.forEach((preset) => {
      const users = (settings.rules || []).filter((rule) => rule.profile_id === preset.id);
      const card = el('div', { className: 'tvr-rule ok' });
      card.append(el('div', { className: 'tvr-rule-head' }, [
        el('span', { className: 'tvr-rule-title', textContent: preset.name }),
        el('span', { className: 'tvr-chip', textContent: `used by ${plural(users.length, 'series')}` }),
      ]));
      const body = el('div', { className: 'tvr-rule-body' });
      presetSummary(preset).forEach((label) => body.append(el('span', { className: 'tvr-chip on', textContent: label })));
      body.append(el('span', { className: 'tvr-chip', textContent: `combine: ${preset.combine}` }));
      const actions = el('div', { className: 'tvr-rule-actions' });
      const editButton = el('button', { type: 'button', textContent: 'Edit' });
      editButton.addEventListener('click', () => editPreset(preset));
      actions.append(editButton);
      body.append(actions);
      card.append(body);
      container.append(card);
    });
  }

  function conditionFields(source) {
    const days = el('input', { type: 'number', min: '1', max: '36500', value: source.keep_days || '' });
    const episodes = el('input', { type: 'number', min: '1', max: '100000', value: source.keep_episodes || '' });
    const seasons = el('input', { type: 'number', min: '1', max: '1000', value: source.keep_seasons || '' });
    const combine = options(el('select'), [
      ['earliest', 'Earliest — keep if any condition keeps it (safest)'],
      ['latest', 'Latest — delete only if every condition agrees'],
      ['any', 'Any — delete if any condition says so (most aggressive)'],
    ], source.combine || 'earliest');
    const node = el('div', {}, [
      el('div', { className: 'tvr-row' }, [field('Keep days', days), field('Keep episodes', episodes),
                                           field('Keep seasons', seasons)]),
      el('small', { textContent: 'Leave a box empty to switch that condition off. At least one is required.' }),
      field('Combine conditions', combine),
    ]);
    return { days, episodes, seasons, combine, node };
  }

  function editPreset(existing) {
    const preset = Object.assign({ id: '', name: '', keep_days: '', keep_episodes: '',
                                   keep_seasons: '', combine: 'earliest' }, existing || {});
    dialog(existing ? 'Edit preset' : 'Add preset', (body) => {
      const name = el('input', { type: 'text', value: preset.name, placeholder: 'Keep 30 days' });
      const conditions = conditionFields(preset);
      const users = (settings.rules || []).filter((rule) => rule.profile_id === preset.id);
      body.append(field('Preset name', name), conditions.node);
      if (existing) {
        const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small', textContent: 'Remove preset' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          guarded('', async () => {
            if (users.length) throw new Error(`${plural(users.length, 'series')} still use "${preset.name}".`);
            if (!window.confirm(`Remove the preset "${preset.name}"?`)) return;
            $('tvr-dialog').close('cancel');
            settings.profiles = settings.profiles.filter((other) => other.id !== preset.id);
            await saveSettings('Preset removed.');
          });
        });
        $('tvr-dialog-extra').replaceChildren(remove);
      }
      if (users.length) {
        body.append(el('p', { textContent: `${plural(users.length, 'series')} use this preset and will change with it:` }));
        users.forEach((rule) => body.append(el('div', { className: 'tvr-mono', textContent: rule.series_title || rule.path })));
        if ((settings.retention || {}).auto_monitor) {
          body.append(el('div', { className: 'tvr-banner', textContent:
            'Auto monitor is on: widening this preset will put previously removed episodes back on '
            + 'Sonarr’s wanted list at the next run.' }));
        }
      }
      return { name, conditions };
    }, async (context) => {
      settings.profiles = (settings.profiles || []).filter((other) => other.id !== preset.id).concat([{
        id: preset.id || undefined,
        name: context.name.value,
        keep_days: context.conditions.days.value || null,
        keep_episodes: context.conditions.episodes.value || null,
        keep_seasons: context.conditions.seasons.value || null,
        combine: context.conditions.combine.value,
      }]);
      await saveSettings('Preset saved.');
    });
  }

  $('tvr-add-preset').addEventListener('click', () => editPreset(null));

  // -- monitoring tree ---------------------------------------------------
  // Seasons and episodes with a checkbox each, checked where Sonarr monitors them now.
  // The series and season boxes are three-state, because "some of this" is a real answer
  // and a box that can only say yes or no would have to lie about it.
  // Every reason core can give, said in the picker's own words. A reason with no entry
  // here falls back to naming Automation rather than to a template with a hole in it.
  const EXCLUDED_WHY = {
    specials: () => 'specials',
    season: (episode) => `excluded season ${episode.excluded_by}`,
    folder: (episode) => `folder matches “${episode.excluded_by}”`,
    episode: (episode) => `matches “${episode.excluded_by}”`,
  };

  // Excluding is not monitoring, so this is its own tree rather than a mode of the other
  // one. A tick here means "never touch this", a tick there means "Sonarr should have
  // this", and the day those two share a code path is the day one of them is wrong.
  //
  // A season's own box means the whole season, including episodes that do not exist yet —
  // which is the only thing ticking every episode individually cannot say, and the reason
  // the stored list has an entry shaped that way.
  function exclusionTree(seasons, current) {
    const node = el('div', { className: 'tvr-tree' });
    const wholeSeasons = new Set((current || [])
      .filter((entry) => entry.episode === null || entry.episode === undefined)
      .map((entry) => entry.season));
    const picked = new Set((current || [])
      .filter((entry) => entry.episode !== null && entry.episode !== undefined)
      .map((entry) => `${entry.season}:${entry.episode}`));
    const seasonRows = [];

    (seasons || []).forEach((season) => {
      const rows = season.episodes || [];
      if (!rows.length) return;
      // An episode excluded by Automation is shown and not offered. The rule that put it
      // there is global, so unticking it here would be an override with nowhere to live —
      // and saying that out loud is more use than a box that springs back.
      const auto = rows.filter((episode) => episode.excluded && episode.excluded !== 'manual');
      const locked = auto.length === rows.length;
      const box = el('input', { type: 'checkbox', className: 'tvr-pick',
                                checked: wholeSeasons.has(season.season), disabled: locked });
      const count = el('span', { className: 'tvr-tree-count' });
      const caret = el('button', { type: 'button', className: 'tvr-tree-caret' },
                       [el('i', { className: 'fa fa-caret-right' })]);
      const list = el('div', { className: 'tvr-tree-episodes', hidden: true });
      caret.addEventListener('click', (event) => {
        event.preventDefault();
        list.hidden = !list.hidden;
        caret.firstChild.className = `fa fa-caret-${list.hidden ? 'right' : 'down'}`;
      });
      const header = el('label', { className: 'tvr-tree-row tvr-tree-season',
                                   title: locked ? 'Every episode of this season is excluded by Automation'
                                                 : 'Exclude the whole season, including episodes not yet aired' }, [
        box, el('span', { textContent: season.season === 0 ? 'Specials' : `Season ${season.season}` }), count,
      ]);
      const ticks = [];
      rows.forEach((episode) => {
        const isAuto = episode.excluded && episode.excluded !== 'manual';
        const tick = el('input', { type: 'checkbox', className: 'tvr-pick',
                                   checked: isAuto || picked.has(`${episode.season}:${episode.episode}`),
                                   disabled: isAuto });
        // Named, not inferred. "Anything that is not a season is a pattern" renders a
        // reason nobody taught this about as `matches “undefined”`, which is worse than
        // saying less.
        const why = !isAuto ? '' : (EXCLUDED_WHY[episode.excluded] || (() => 'excluded by Automation'))(episode);
        list.append(el('label', { className: `tvr-tree-row tvr-tree-episode${isAuto ? ' tvr-tree-auto' : ''}` }, [
          tick,
          el('span', { textContent: `E${String(episode.episode).padStart(2, '0')} · ${episode.title || ''}` }),
          el('span', { className: 'tvr-tree-note tvr-tree-why', textContent: why }),
        ]));
        if (!isAuto) ticks.push({ tick, episode });
        tick.addEventListener('change', refresh);
      });
      box.addEventListener('change', () => {
        ticks.forEach((entry) => { entry.tick.disabled = box.checked;
                                   if (box.checked) entry.tick.checked = true; });
        refresh();
      });
      ticks.forEach((entry) => { entry.tick.disabled = box.checked; });
      seasonRows.push({ season: season.season, box, ticks, count, total: rows.length, auto: auto.length });
      node.append(el('div', { className: 'tvr-tree-season-wrap' },
                     [el('div', { className: 'tvr-tree-head' }, [caret, header]), list]));
    });

    function refresh() {
      seasonRows.forEach((row) => {
        const on = row.box.checked ? row.total
          : row.auto + row.ticks.filter((entry) => entry.tick.checked).length;
        row.count.textContent = `${on}/${row.total} excluded`;
      });
    }
    refresh();

    return {
      node,
      empty: !seasonRows.length,
      // Only the hand-picked half. What Automation excludes is not stored per series, so
      // reading the ticked boxes back wholesale would bake a global rule into this one
      // series and leave it there after the rule changed.
      picked: () => {
        const found = [];
        seasonRows.forEach((row) => {
          if (row.box.checked) { found.push({ season: row.season, episode: null }); return; }
          row.ticks.forEach((entry) => {
            if (entry.tick.checked) found.push({ season: row.season, episode: entry.episode.episode });
          });
        });
        return found;
      },
    };
  }

  function monitorTree(seasons, options) {
    const only = (options || {}).only;                // a filter over which episodes show
    const state = new Map();                          // episode id -> wanted, as displayed
    const episodeBoxes = new Map();
    const seasonBoxes = [];
    const node = el('div', { className: 'tvr-tree' });

    const seriesBox = el('input', { type: 'checkbox', className: 'tvr-pick' });
    const seriesRow = el('label', { className: 'tvr-tree-row tvr-tree-series' }, [
      seriesBox, el('span', { textContent: 'All of it' }),
      el('span', { className: 'tvr-tree-count' }),
    ]);
    node.append(seriesRow);

    const setThree = (box, on, off) => {
      box.checked = on > 0 && off === 0;
      box.indeterminate = on > 0 && off > 0;
    };
    const refresh = () => {
      let allOn = 0, allOff = 0;
      seasonBoxes.forEach((entry) => {
        let on = 0, off = 0;
        entry.ids.forEach((id) => { if (state.get(id)) on += 1; else off += 1; });
        setThree(entry.box, on, off);
        entry.count.textContent = `${on}/${entry.ids.length} monitored`;
        allOn += on; allOff += off;
      });
      setThree(seriesBox, allOn, allOff);
      seriesRow.querySelector('.tvr-tree-count').textContent = `${allOn}/${allOn + allOff} monitored`;
      if (options && options.onChange) options.onChange();
    };

    (seasons || []).forEach((season) => {
      const rows = (season.episodes || []).filter((episode) => !only || only(episode));
      if (!rows.length) return;
      const ids = rows.map((episode) => episode.episode_id);
      ids.forEach((id, index) => state.set(id, !!rows[index].monitored));

      const box = el('input', { type: 'checkbox', className: 'tvr-pick' });
      const count = el('span', { className: 'tvr-tree-count' });
      const caret = el('button', { type: 'button', className: 'tvr-tree-caret' },
                       [el('i', { className: 'fa fa-caret-right' })]);
      const list = el('div', { className: 'tvr-tree-episodes', hidden: true });
      const header = el('label', { className: 'tvr-tree-row tvr-tree-season' }, [
        box, el('span', { textContent: season.season === 0 ? 'Specials' : `Season ${season.season}` }), count,
      ]);
      caret.addEventListener('click', (event) => {
        event.preventDefault();
        list.hidden = !list.hidden;
        caret.firstChild.className = `fa fa-caret-${list.hidden ? 'right' : 'down'}`;
      });
      const wrap = el('div', { className: 'tvr-tree-season-wrap' },
                      [el('div', { className: 'tvr-tree-head' }, [caret, header]), list]);

      rows.forEach((episode) => {
        const tick = el('input', { type: 'checkbox', className: 'tvr-pick',
                                   checked: !!episode.monitored });
        tick.addEventListener('change', () => { state.set(episode.episode_id, tick.checked); refresh(); });
        episodeBoxes.set(episode.episode_id, tick);
        list.append(el('label', { className: 'tvr-tree-row tvr-tree-episode' }, [
          tick,
          el('span', { textContent: `E${String(episode.episode).padStart(2, '0')} · ${episode.title || ''}` }),
          el('span', { className: 'tvr-tree-note',
                       textContent: episode.has_file ? '' : 'no file' }),
        ]));
      });

      box.addEventListener('change', () => {
        ids.forEach((id) => {
          state.set(id, box.checked);
          episodeBoxes.get(id).checked = box.checked;
        });
        refresh();
      });
      seasonBoxes.push({ box, ids, count });
      node.append(wrap);
    });

    seriesBox.addEventListener('change', () => {
      state.forEach((value, id) => {
        state.set(id, seriesBox.checked);
        episodeBoxes.get(id).checked = seriesBox.checked;
      });
      refresh();
    });
    refresh();

    return {
      node,
      empty: state.size === 0,
      // Only what differs from what Sonarr already has: pressing save without touching a
      // box should ask Sonarr for nothing.
      changes: () => {
        const monitor = [], unmonitor = [];
        (seasons || []).forEach((season) => (season.episodes || []).forEach((episode) => {
          if (!state.has(episode.episode_id)) return;
          const wanted = state.get(episode.episode_id);
          if (wanted === !!episode.monitored) return;
          (wanted ? monitor : unmonitor).push(episode.episode_id);
        }));
        return { monitor, unmonitor };
      },
    };
  }

  // -- adding and editing a series ---------------------------------------
  async function seriesFor(instanceId, exceptRule, force) {
    const key = instanceId + ':' + (exceptRule || '');
    if (force || !seriesCache[key]) {
      const data = await api('series', { instance_id: instanceId, except_rule: exceptRule || '', force: !!force },
                             'Loading series from Sonarr…');
      seriesCache[key] = data.series;
    }
    return seriesCache[key];
  }

  function ruleForm(existing, preselect) {
    const rule = Object.assign({
      id: '', enabled: true,
      instance_id: (preselect && preselect.instance_id) || (settings.instances[0] || {}).id || '',
      series_id: null, series_title: '', tvdb_id: null, path: '',
      profile_id: '',
      keep_days: '', keep_episodes: '', keep_seasons: '', combine: 'earliest',
      include_specials: null,
    }, existing || {});

    if (!settings.instances.length) {
      notice('Add a Sonarr instance first — every series must be bound to a Sonarr record.', 'bad');
      return null;
    }
    // Sonarr's own record for this series: the one being added, or the one the rule is
    // already bound to. Everything the panel shows about the series comes from here.
    const series = preselect
      || (library || []).find((entry) => entry.series_id === rule.series_id
                                         && entry.instance_id === rule.instance_id)
      || { series_id: rule.series_id, instance_id: rule.instance_id, title: rule.series_title,
           path: rule.path };
    if (!existing) {
      Object.assign(rule, { instance_id: series.instance_id, series_id: series.series_id,
                            series_title: series.title, tvdb_id: series.tvdb_id,
                            path: series.path });
    }

    const draftKey = existing ? `rule:${rule.id}`
                              : `add:${series.instance_id}:${series.series_id}`;
    return {
      title: existing ? 'Edit series' : 'Add series',
      rule,
      series,
      existing: !!existing,
      draftKey,
      build: (body, top) => {
      // The navigator already said which series this is, so there is no picker and no
      // instance to choose: the series carries its own. A rule *is* its binding to one
      // series, so pointing it at another is delete and add, not an edit.
      // One fact per line rather than a single run of dots: they answer different
      // questions, and a reader looking for the episode count should not have to find it
      // among the network and the certificate.
      const originLine = el('div', { className: 'tvr-identity-line' });
      const countLine = el('div', { className: 'tvr-identity-line' });
      const nextLine = el('div', { className: 'tvr-identity-line tvr-identity-next' });
      // One number on the line, coloured by the thing anyone actually wants to know: not
      // how many episodes are monitored, but whether the ones the rule keeps are. Green
      // is the state a run leaves the series in; anything else is a difference from it.
      // Everything behind the colour is in the tooltip rather than on the line.
      const monitorCountText = el('span', { className: 'tvr-monitor-count' });
      const sayCounts = (counts) => {
        const parts = [];
        if (series.season_count) parts.push(plural(series.season_count, 'season'));
        const total = (counts && counts.episodes) || series.total_episode_count || 0;
        countLine.replaceChildren();
        countLine.hidden = !total && !parts.length;
        if (parts.length) countLine.append(text(`${parts.join(' · ')}${total ? ' · ' : ''}`));
        if (!total) return;
        const held = counts && counts.episodes_on_disk != null
          ? counts.episodes_on_disk : series.episode_file_count;
        const disk = [`${held} of ${total} episodes are on disk`];
        if (series.size_on_disk) disk.push(`${bytes(series.size_on_disk)} on disk`);
        if (!existing) {
          const plain = el('span', { textContent: plural(total, 'episode'),
                                     title: disk.join('\n') });
          countLine.append(plain);
          return;
        }
        countLine.append(text(`${plural(total, 'episode')} (`), monitorCountText, text(')'));
        const monitored = counts && counts.episodes_monitored != null
          ? counts.episodes_monitored : null;
        monitorCountText.textContent = `${monitored == null ? '?' : monitored} monitored`;
        // Scope, not the whole series: a finished show with two seasons kept and six
        // unmonitored is exactly right, and colouring it orange would say otherwise.
        const scope = counts && counts.known ? counts.in_scope : null;
        const inScopeMonitored = scope == null ? null : scope - counts.in_scope_unmonitored;
        monitorCountText.className = 'tvr-monitor-count'
          + (inScopeMonitored == null || !scope ? ''
             : (counts.in_scope_unmonitored === 0 ? ' all'
                : (inScopeMonitored > 0 ? ' some' : ' none')));
        const lines = disk.slice();
        if (scope != null) {
          lines.push(`${inScopeMonitored} of ${scope} episodes inside the keep window are monitored`);
          lines.push(counts.out_scope_monitored
            ? `${counts.out_scope_monitored} of ${counts.out_scope} outside it are still monitored`
              + ' — a run will unmonitor them'
            : `none of the ${counts.out_scope} outside it are monitored`);
        }
        monitorCountText.title = lines.join('\n');
      };
      // Three answers, and only one of them is a date: a series that has ended is not
      // "nothing scheduled", it is finished, and the difference decides what retention on
      // it even means.
      const stamp = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
      const sayNext = (counts) => {
        const next = counts && counts.next_episode;
        const at = series.next_airing
          ? stamp(series.next_airing)
          : (next ? new Date(`${next.air_date}T00:00:00`).toLocaleDateString() : '');
        if (at) {
          nextLine.textContent = `Next episode: ${at}`;
          // Which episode it is belongs to whoever asks for it: the line is about when.
          nextLine.title = next
            ? `S${String(next.season).padStart(2, '0')}E${String(next.episode).padStart(2, '0')}`
              + `${next.title ? `: ${next.title}` : ''}${next.estimated ? '\nDate estimated, not from Sonarr' : ''}`
            : '';
        } else if (series.ended) {
          nextLine.textContent = 'Series ended';
          nextLine.title = '';
        } else {
          nextLine.textContent = 'Nothing scheduled';
          nextLine.title = '';
        }
      };
      originLine.textContent = [series.year, series.network, series.certification]
        .filter(Boolean).join(' · ');
      originLine.hidden = !originLine.textContent;
      sayCounts(null);
      sayNext(null);
      // Not a field on a form: switching a series off is a thing you do, not a change you
      // save, so it takes effect where it is clicked and the list redraws behind it. No
      // caption either — the tooltip says which way it is, and one word beside the name
      // was a word that never changed.
      const enabled = toggle('', rule.enabled, null, { className: 'tvr-identity-switch' });
      const sayState = () => {
        const on = enabled.input.checked;
        enabled.node.title = existing
          ? (on ? 'Enabled — every run includes this series. Click to disable it.'
                : 'Disabled — every run skips this series. Click to enable it.')
          : (on ? 'Will be added enabled — every run will include this series.'
                : 'Will be added disabled — no run will touch this series until it is enabled.');
        enabled.input.setAttribute('aria-label', on ? 'Enabled' : 'Disabled');
      };
      sayState();
      enabled.node.hidden = !existing;
      if (existing) {
        enabled.input.addEventListener('change', () => {
          const on = enabled.input.checked;
          const target = (settings.rules || []).find((other) => other.id === rule.id);
          if (!target) return;
          enabled.input.disabled = true;
          guarded('', async () => {
            try {
              target.enabled = rule.enabled = on;
              await saveSettings(null, true);
              sayState();
            } catch (error) {
              // Nothing was written, so nothing should look as though it was.
              target.enabled = rule.enabled = !on;
              enabled.input.checked = !on;
              sayState();
              throw error;
            } finally {
              enabled.input.disabled = false;
            }
          });
        });
      }
      // What the next run would do, with the facts rather than in a box of its own below
      // them: it is a fact about this series, and it was the thing being scrolled past.
      const planLines = el('div', { className: 'tvr-identity-plan', hidden: true });
      const readLine = el('div', { className: 'tvr-identity-read' });
      const sayPlan = (plan) => {
        if (!existing) return;
        planLines.hidden = false;
        planLines.replaceChildren(
          plan && (plan.delete || plan.monitor || plan.unmonitor)
            ? changeLines(plan, (kind) => guarded('', async () => {
                const data = await api('preview', { rule_ids: [rule.id] },
                                       'Working out what would change…');
                changeList(data.result, `${rule.series_title}: scheduled changes`, kind);
              }), true)
            : el('div', { className: 'tvr-plan-quiet',
                          textContent: plan ? 'Nothing scheduled for the next run'
                                            : 'Not read from Sonarr yet' }));
      };
      // How old everything above it is, with the button that renews it on the same line —
      // the age is the reason anyone presses it. A series being added has no reading of
      // its own, so it is dated by the catalogue reading it was drawn from.
      const readText = el('span');
      // A series with no rule is dated by the catalogue reading it was drawn from, until
      // someone refreshes it on its own — after which it is dated by that.
      let readAt = null;
      const sayRead = () => {
        const reading = existing ? (monitoring[rule.id] || {}) : {};
        const stamp = existing ? (reading.read_at || reading.checked_at)
                               : (readAt || (snapshot.sync || {}).synced_at);
        readText.textContent = `Last refreshed: ${stamp ? ago(stamp) : 'never'}`;
      };
      readLine.append(readText);
      sayRead();
      if (existing) sayPlan((monitoring[rule.id] || {}).plan);

      // Offered while adding too. There it has no rule to check, but the counts and the
      // episode list behind them come straight from Sonarr, so it is the same question
      // asked of the same series — and the one being added is the one most likely to have
      // changed since the catalogue was read. The button lives in the pane's own bar; this
      // is only what pressing it does.
      const reread = (button) => guarded('', async () => {
        if (button) button.disabled = true;
        try {
          if (existing) {
            const data = await api('check-rule', { rule_id: rule.id, force: true },
                                   'Reading from Sonarr…', true);
            if (data.busy) throw new Error('A run is in progress. Try again when it finishes.');
            if (data.state) monitoring[data.rule_id] = data.state;
            applyAlerts(data.alerts);
            renderRules();
            sayRead();
            sayPlan((monitoring[rule.id] || {}).plan);
          } else {
            // No rule to check, so the catalogue entry itself is what goes stale: the
            // title, the season count, the next airing and the artwork all come from it.
            const data = await api('refresh-series', { instance_id: series.instance_id,
                                                       series_id: series.series_id },
                                   'Reading from Sonarr…', true);
            if (data.series) {
              Object.assign(series, data.series);
              const row = (library || []).find(
                (entry) => entry.series_id === series.series_id
                           && entry.instance_id === series.instance_id);
              if (row) Object.assign(row, data.series);
              renderLibrary();
            }
            readAt = data.read_at || new Date().toISOString();
            sayRead();
          }
          refreshCounts();
        } finally {
          if (button) button.disabled = false;
        }
      });
      const controls = el('div', { className: 'tvr-identity-controls' }, [enabled.node]);
      const identity = el('div', { className: 'tvr-identity' }, [
        controls,
        posterNode(series, 'tvr-poster tvr-poster-panel'),
        el('div', { className: 'tvr-identity-body' }, [
          el('div', { className: 'tvr-identity-title' }, [
            el('span', { className: 'tvr-identity-name',
                         textContent: series.title || rule.series_title }),
            // The one place it belongs: beside the name, where you are already looking at
            // this series. On every card in a list it was three thousand links to nowhere
            // anyone was going.
            sonarrLink(Object.assign({}, rule, { slug: series.slug || rule.slug,
                                                 instance_id: series.instance_id })) || text(''),
          ]),
          // The path is Sonarr's business. Nothing here is decided by it, nothing here
          // reads it, and it was the one line long enough to wrap the panel.
          originLine, countLine, nextLine, planLines,
        ]),
      ]);

      // Custom first, and what a new series starts on. A preset is a decision to share
      // values with other series, which is not what adding one usually is; offering the
      // first preset by default made that decision for you and quietly.
      const presetSelect = el('select');
      presetSelect.append(el('option', { value: '', textContent: 'Custom — values for this series only' }));
      (settings.profiles || []).forEach((preset) => presetSelect.append(
        el('option', { value: preset.id, textContent: `${preset.name} — ${presetSummary(preset).join(', ')}` })));
      presetSelect.value = rule.profile_id || '';
      const conditions = conditionFields(rule);
      const applyPreset = () => { conditions.node.hidden = !!presetSelect.value; };
      presetSelect.addEventListener('change', applyPreset);
      applyPreset();

      // Inheriting says what it will inherit. "Use the global setting" made you go and
      // look; naming the value means the row already answers the question.
      const globalSpecials = (settings.retention || {}).include_specials ? 'Include specials' : 'Exclude specials';
      const globalMonitoring = MONITOR_NAMES[(settings.retention || {}).monitoring || 'unmonitor-only'];
      const specials = options(el('select'), [['', `[Default] ${globalSpecials}`], ['no', 'Exclude specials'],
                                              ['yes', 'Include specials']],
        rule.include_specials === true ? 'yes' : (rule.include_specials === false ? 'no' : ''));
      const monitorMode = options(el('select'), [['', `[Default] ${globalMonitoring}`],
                                                ['unmonitor-only', 'Unmonitor only'],
                                                ['full-sync', 'Full sync']],
                                 rule.monitoring || '');

      // Two one-time actions, not settings. They happen when you save and never again,
      // which is why each says so and says what it will ask Sonarr to do.
      const before = existing ? scopeOf(rule) : null;
      const monitorNew = toggle('Change monitor status for episodes within scope',
                                false, null, { className: 'tvr-row-switch' });
      // What each action would actually touch, against the window as it stands in the
      // form. Without it the two toggles are a decision made blind.
      const monitorCount = el('div', { className: 'tvr-once-count' });
      const unmonitorCount = el('div', { className: 'tvr-once-count' });
      const empty = el('div', { className: 'tvr-plan-quiet', hidden: true,
                                textContent: 'Nothing to monitor — the keep window holds no episodes.' });
      // Turning it on opens the window's own seasons and episodes, checked where Sonarr
      // monitors them now, so the choice is which ones rather than all or nothing.
      const treeBox = el('div', { className: 'tvr-tree-box', hidden: true });
      let tree = null;
      const scopeRow = el('div', { className: 'tvr-once' }, [
        el('div', { className: 'tvr-once-title', textContent: 'Monitoring in Sonarr' }),
        monitorNew.node,
        monitorCount,
        treeBox,
        empty,
      ]);
      const loadTree = () => guarded('', async () => {
        const data = await api('episodes', {
          rule_id: rule.id || '', instance_id: series.instance_id, series_id: series.series_id,
          draft: Object.assign({}, draftScope(), { include_specials: specials.value }),
        }, 'Reading episodes…', true);
        tree = monitorTree(data.seasons, { only: (episode) => episode.in_scope });
        treeBox.replaceChildren(tree.node);
      });
      monitorNew.input.addEventListener('change', () => {
        treeBox.hidden = !monitorNew.input.checked;
        if (monitorNew.input.checked) loadTree(); else { tree = null; treeBox.replaceChildren(); }
      });
      // Not a toggle. A run unmonitors everything outside the window whatever anyone
      // chooses, so offering the choice here only decided whether it happened now or
      // within a day — and off by default meant Sonarr spent that day fetching episodes
      // the next run would delete. It is stated instead, because it still happens.
      // Shown only when there is something to say. "Nothing outside the scope is
      // monitored" is a heading and a sentence to report that nothing will happen.
      const unmonitorNote = el('div', { className: 'tvr-once', hidden: true }, [
        el('div', { className: 'tvr-once-title', textContent: 'On save' }),
        unmonitorCount,
      ]);

      // Everything being done to this series that nobody asked for on this screen. The
      // monitoring mode and the specials setting have controls below, and the exclusions
      // come from Automation and from this series' own list — which is exactly why they
      // are worth stating together. "Why is this episode never deleted" should be
      // answerable here rather than by opening another page and matching in your head.
      const autoLines = el('div', { className: 'tvr-auto-lines' });
      const autoEdit = el('button', { type: 'button', className: 'tvr-action tvr-small',
                                      textContent: 'Edit…' });
      const autoRow = el('div', { className: 'tvr-once', hidden: true }, [
        el('div', { className: 'tvr-once-title' },
           [text('Automation'), el('span', { className: 'tvr-spacer' }), autoEdit]),
        autoLines,
      ]);
      // Kept here rather than read back off `rule` each time, so the pane shows what was
      // just saved without waiting for the settings to come round again.
      let manualExclusions = (rule.exclusions || []).slice();
      const sayAutomation = (data) => {
        if (!existing) return;
        autoRow.hidden = false;
        const found = Object.assign({ seasons: [], folders: [], episode_patterns: [],
                                      manual: 0, specials: 0, total: 0 },
                                    (data && data.exclusions) || {});
        const line = (className, label) => el('div', { className: `tvr-auto-line ${className}`,
                                                       textContent: label });
        const from = (n) => `${plural(n, 'episode')}, from Automation`;
        const lines = [
          line('tvr-auto-plain', `Monitoring: ${MONITOR_NAMES[data.monitoring] || data.monitoring}`
            + (data.monitoring_default ? ' — from Automation' : ' — set on this series')),
        ];
        if (!data.specials_default) lines.push(line('tvr-auto-plain',
          `Specials: ${data.specials ? 'kept' : 'excluded'} — set on this series`));
        if (found.specials) lines.push(line('tvr-auto-rule', `Specials excluded — ${from(found.specials)}`));
        found.seasons.forEach((entry) => lines.push(line('tvr-auto-rule',
          `Season ${entry.season} excluded — ${from(entry.episodes)}`)));
        found.folders.forEach((entry) => lines.push(line('tvr-auto-rule',
          `Folder matches “${entry.pattern}” — ${from(entry.episodes)}`)));
        found.episode_patterns.forEach((entry) => lines.push(line('tvr-auto-rule',
          `Matches “${entry.pattern}” — ${from(entry.episodes)}`)));
        if (found.manual) lines.push(line('tvr-auto-manual',
          `${plural(found.manual, 'episode')} excluded on this series`));
        if (!found.total) lines.push(line('tvr-auto-plain', 'Nothing is excluded from this series'));
        autoLines.replaceChildren(...lines);
      };
      const loadAutomation = () => {
        if (!existing) return;
        guarded('', async () => sayAutomation(await api('exclusions', { rule_id: rule.id }, '', true)));
      };
      // The same episode list the monitoring tree reads, so the picker and the pane agree
      // about which episodes exist and which are already spoken for.
      autoEdit.addEventListener('click', () => guarded('', async () => {
        const data = await api('episodes', {
          rule_id: rule.id, instance_id: series.instance_id, series_id: series.series_id,
          draft: Object.assign({}, draftScope(), { include_specials: specials.value }),
        }, 'Reading episodes…', true);
        const tree = exclusionTree(data.seasons, manualExclusions);
        dialog(`Exclusions — ${title}`, (box) => {
          box.append(el('p', { textContent:
            'An excluded episode is never deleted and never unmonitored, whatever this '
            + 'series’ rule says. Nothing is restored or removed by saving: an exclusion '
            + 'decides what a run may touch, and a run is still the only thing that acts.' }));
          box.append(el('p', { className: 'tvr-lede', textContent:
            'Greyed episodes are excluded by Automation, which applies to every series and '
            + 'changes there. A season’s own box excludes the whole season, including '
            + 'episodes that have not aired yet.' }));
          box.append(tree.empty
            ? el('p', { className: 'tvr-empty', textContent: 'Sonarr has no episodes for this series.' })
            : el('div', { className: 'tvr-tree-box' }, [tree.node]));
          return tree;
        }, async (picked) => {
          const target = (settings.rules || []).find((other) => other.id === rule.id);
          if (!target) return;
          manualExclusions = picked.picked();
          target.exclusions = rule.exclusions = manualExclusions;
          await saveSettings('Exclusions saved.');
          loadAutomation();
          refreshCounts();
        });
      }));

      // Counted by the worker from the episodes it already holds, and re-counted when the
      // window moves. Debounced because typing a keep value changes it on every keystroke.
      const title = series.title || rule.series_title || 'this series';
      let countTimer = null;
      const refreshCounts = () => {
        clearTimeout(countTimer);
        countTimer = setTimeout(() => guarded('', async () => {
          const scope = draftScope();
          const counts = await api('scope-counts', {
            rule_id: rule.id || '', instance_id: series.instance_id, series_id: series.series_id,
            draft: Object.assign({}, scope, { include_specials: specials.value,
                                              previous_scope: existing ? before : null }),
          }, '', true);
          sayCounts(counts.known ? counts : null);
          sayNext(counts.known ? counts : null);
          if (counts.plan) sayPlan(counts.plan);
          if (!counts.known) {
            monitorCount.textContent = unmonitorCount.textContent = '';
            return;
          }
          const monitored = counts.in_scope - counts.in_scope_unmonitored;
          monitorCount.textContent =
            `${monitored} of ${counts.in_scope} episodes in ${title}'s keep scope are monitored`;
          // Nothing in the window means nothing to decide, so the toggle goes and says why.
          scopeRow.hidden = false;
          monitorNew.node.hidden = counts.in_scope === 0;
          monitorCount.hidden = counts.in_scope === 0;
          empty.hidden = counts.in_scope > 0;
          if (counts.in_scope === 0) {
            monitorNew.input.checked = false;
            treeBox.hidden = true;
          }
          unmonitorNote.hidden = !counts.out_scope_monitored;
          unmonitorCount.textContent = counts.out_scope_monitored
            ? `${counts.out_scope_monitored} of ${counts.out_scope} episodes outside ${title}'s keep scope are `
              + 'monitored, and will be unmonitored so Sonarr stops fetching them'
            : '';
        }), 250);
      };
      const draftScope = () => ({
        profile_id: presetSelect.value || '',
        keep_days: presetSelect.value ? null : (conditions.days.value || null),
        keep_episodes: presetSelect.value ? null : (conditions.episodes.value || null),
        keep_seasons: presetSelect.value ? null : (conditions.seasons.value || null),
        combine: conditions.combine.value,
      });
      // The window's own contents are always worth showing; whether they are worth
      // changing is the operator's business, not a rule about widening.
      const updateScopeRow = () => { if (monitorNew.input.checked && tree) loadTree(); };
      [monitorMode, presetSelect, conditions.days, conditions.episodes, conditions.seasons,
       conditions.combine, specials].forEach((input) => {
        input.addEventListener('change', () => { updateScopeRow(); refreshCounts(); });
        input.addEventListener('input', refreshCounts);
      });
      setTimeout(() => { updateScopeRow(); refreshCounts(); loadAutomation(); }, 0);

      top.append(identity);
      // The banner names what this form does, under the series it does it to. Enabling a
      // series is not one of the things it does, which is why the switch sits above it.
      top.append(el('div', { className: 'tvr-form-banner',
                             textContent: existing ? 'Edit series' : 'Add series' }));
      if (existing) {
        const queued = queuedBanner(rule);
        if (queued) body.append(queued);
        const alertsHere = seriesAlerts(rule.id);
        if (alertsHere.length) body.append(seriesAlertCard(rule, alertsHere, { compact: true }));
      }
      body.append(
        field('Retention', presetSelect, (settings.profiles || []).length
          ? 'Presets are managed under Media management.' : 'No presets yet — create one to reuse values.'),
        conditions.node,
        field('Season 0 / specials', specials),
        field('Monitoring', monitorMode, 'Unmonitor only never asks Sonarr to fetch anything.'),
        autoRow, scopeRow, unmonitorNote);
      const formState = () => JSON.stringify({ profile_id: presetSelect.value,
                                        keep_days: conditions.days.value,
                                        keep_episodes: conditions.episodes.value,
                                        keep_seasons: conditions.seasons.value,
                                        combine: conditions.combine.value,
                                        include_specials: specials.value,
                                        monitoring: monitorMode.value,
                                        once: monitorNew.input.checked });
      // What Update compares against: the rule as saved, captured before any half-typed
      // draft is put back. Taken after the restore it would call the draft the baseline,
      // and Update would sit disabled over changes nobody had saved.
      const saved = formState();
      const held = drafts.get(draftKey);
      if (held && held !== saved) {
        try {
          const values = JSON.parse(held);
          presetSelect.value = values.profile_id || '';
          conditions.days.value = values.keep_days || '';
          conditions.episodes.value = values.keep_episodes || '';
          conditions.seasons.value = values.keep_seasons || '';
          conditions.combine.value = values.combine || 'earliest';
          specials.value = values.include_specials || '';
          monitorMode.value = values.monitoring || '';
          applyPreset();
          sayState();
        } catch (error) {
          drafts.delete(draftKey);      // unreadable is not worth carrying
        }
      }
      return { presetSelect, conditions, specials, monitoring: monitorMode, monitorNew,
               before, enabled, draftKey, saved, reread, readLine,
               tree: () => (monitorNew.input.checked ? tree : null),
               // A rule needs somewhere to keep from: a preset, or at least one value.
               valid: () => !!(presetSelect.value || conditions.days.value
                               || conditions.episodes.value || conditions.seasons.value),
               state: formState };
    },
      save: async (context, startEnabled) => {
      const draft = {
        id: rule.id || undefined,
        enabled: existing ? context.enabled.input.checked : !!startEnabled,
        instance_id: series.instance_id,
        profile_id: context.presetSelect.value || '',
        keep_days: context.presetSelect.value ? null : (context.conditions.days.value || null),
        keep_episodes: context.presetSelect.value ? null : (context.conditions.episodes.value || null),
        keep_seasons: context.presetSelect.value ? null : (context.conditions.seasons.value || null),
        combine: context.conditions.combine.value,
        include_specials: context.specials.value,
        monitoring: context.monitoring.value,
        queue: rule.queue || undefined,
      };
      if (!existing && series.selectable === false) {
        throw new Error(`${series.title} cannot be used: ${series.reason}.`);
      }
      Object.assign(draft, { series_id: series.series_id, series_title: series.title || rule.series_title,
                             tvdb_id: series.tvdb_id, path: series.path || rule.path });
      settings.rules = (settings.rules || []).filter((other) => other.id !== rule.id).concat([draft]);
      await saveSettings(null);
      const matched = await api('match', {}, 'Matching against Sonarr…');
      settings = matched.settings;
      snapshot.settings = settings;
      render();
      forgetLibrary();           // one more series with a rule
      const saved = settings.rules[settings.rules.length - 1];
      let done = '';
      // Applied now, against the saved rule, because the unmonitor half exists to stop
      // downloads that would otherwise happen before the next run.
      if (saved) {
        // Outside the window is not a choice: a run unmonitors it regardless, so waiting
        // only gives Sonarr a day to fetch what that run would delete.
        const pass = await api('scope-pass', {
          rule_id: saved.id, monitor_new: false, unmonitor_outside: true,
        }, 'Setting monitoring in Sonarr…');
        const parts = [];
        if (pass.unmonitored) parts.push(`${plural(pass.unmonitored, 'episode')} unmonitored`);
        // Inside the window is entirely the operator's: exactly what the tree was left
        // showing, and only where it differs from what Sonarr already has.
        const wanted = context.tree && context.tree();
        const changes = wanted ? wanted.changes() : { monitor: [], unmonitor: [] };
        if (changes.monitor.length || changes.unmonitor.length) {
          const applied = await api('set-monitored', Object.assign({ rule_id: saved.id }, changes),
                                    'Setting monitoring in Sonarr…');
          if (applied.monitored) parts.push(`${plural(applied.monitored, 'episode')} monitored`);
          if (applied.unmonitored) parts.push(`${plural(applied.unmonitored, 'episode')} unmonitored`);
        }
        done = parts.length ? ` ${parts.join(', ')} in Sonarr.` : '';
      }
      if (saved) queueChecks([saved.id]);
      notice(`Series saved.${done}`, 'ok');
      renderDetails();
    },
    };
  }

  // -- the details pane --------------------------------------------------
  // Editing happens here rather than in a dialog: the list stays visible beside it, so
  // what you are changing is never the only thing on screen.
  // One series at a time, and only ever the one being edited. There is no selection to
  // build: the bulk mechanisms are a preset, which moves every series pointing at it, and
  // a default, which moves every series inheriting it — each one edit with a blast radius
  // you can name. Thirty ticked boxes and a forgotten one is not a third, and this app
  // deletes things.
  let editing = null;          // the form currently open in the pane, if any
  const isOpen = (rule) => !!(editing && rule && editing.rule && editing.rule.id === rule.id);

  // Half-typed edits, kept while the library is on screen. Clicking a second poster to
  // check something and clicking back is browsing, not abandoning: nothing was saved, so
  // nothing should be lost. Leaving the library is leaving, and clears them — held any
  // longer they would be a second, invisible copy of the settings.
  const drafts = new Map();
  const forgetDrafts = () => drafts.clear();

  function openEditor(existing, preselect) {
    const form = ruleForm(existing, preselect);
    if (!form) return;
    editing = form;
    if (!LIBRARY[currentView]) showView('series-all');
    renderDetails();
    // A different series starts at its own top. The pane is the scroller, and replacing
    // its contents leaves the scroll position where the last series had put it.
    $('tvr-details').scrollTop = 0;
    $('tvr-details').scrollIntoView({ block: 'nearest' });
  }

  function renderDetails() {
    const pane = $('tvr-details');
    const shell = $('tvr-series-shell');
    // Always open. It used to appear on selection, which narrowed the list beside it and
    // reflowed the grid — sliding the card you had just clicked out from under the
    // pointer. An empty pane costs a column and removes that entirely.
    shell.classList.add('open');
    pane.replaceChildren();
    if (!editing) {
      pane.append(el('div', { className: 'tvr-details-head' },
                     [el('h3', { textContent: 'Series details' })]));
      pane.append(el('div', { className: 'tvr-details-idle' },
                     [el('p', { textContent: "Select a series to view or modify its details." })]));
      return;
    }

    const head = el('div', { className: 'tvr-details-head' });
    const close = el('button', { type: 'button', className: 'tvr-icon-button', title: 'Close' },
                    [el('i', { className: 'fa fa-times' })]);
    close.addEventListener('click', () => { editing = null; renderLibrary(); renderDetails(); });

    {
      // The pane's own title, not the form's: what is below it is this series, whether it
      // is being added or edited, and both buttons belong to the pane rather than to the
      // form under it.
      const refresh = el('button', { type: 'button', className: 'tvr-icon-button',
                                     title: 'Re-read this series from Sonarr' },
                         [el('i', { className: 'fa fa-refresh' })]);
      // Two lines in the space the buttons already take: the second is set small enough
      // that the bar is still as tall as the icons beside it and no taller.
      const headMain = el('div', { className: 'tvr-details-head-main' },
                          [el('h3', { textContent: 'Series details' })]);
      head.append(headMain, el('span', { className: 'tvr-spacer' }), refresh, close);
      pane.append(head);
      // Fixed: the series, and what the next run would do to it. Only the settings below
      // scroll, so neither can be scrolled out from under the other.
      const top = el('div', { className: 'tvr-details-top' });
      const body = el('div', { className: 'tvr-details-body' });
      pane.append(top, body);
      const context = editing.build(body, top);
      editing.context = context;
      refresh.addEventListener('click', () => context.reread(refresh));
      headMain.append(context.readLine);
      const actions = el('div', { className: 'tvr-actions' });
      const commit = (startEnabled) => guarded('', async () => {
        await editing.save(context, startEnabled);
        drafts.delete(context.draftKey);
        editing = null;
        renderDetails();
      });
      // Adding a series and switching it on are two decisions, so they are two buttons
      // rather than a switch that has to be found first. Nothing runs against a series
      // saved with the plain one until someone says so.
      const primary = el('button', { type: 'button', className: 'tvr-primary',
                                     textContent: editing.existing ? 'Update' : 'Save and enable' });
      primary.addEventListener('click', () => commit(true));
      const buttons = [primary];
      actions.append(primary);
      if (!editing.existing) {
        const quiet = el('button', { type: 'button', className: 'tvr-secondary', textContent: 'Save' });
        quiet.dataset.hint = 'Added switched off. No run touches it until it is enabled.';
        quiet.addEventListener('click', () => commit(false));
        buttons.push(quiet);
        actions.append(quiet);
      }
      // Adding needs a keep window to be worth anything; updating needs something to have
      // changed as well, so the button says whether pressing it would do something.
      const check = () => {
        const valid = context.valid();
        const now = context.state();
        // Kept as it stands, so clicking away and back returns to what was typed.
        if (now === context.saved) drafts.delete(context.draftKey);
        else drafts.set(context.draftKey, now);
        buttons.forEach((button) => {
          button.disabled = !valid || (editing.existing && now === context.saved);
          button.title = !valid ? 'Set a preset, or at least one keep value'
            : (button.disabled ? 'Nothing has changed' : (button.dataset.hint || ''));
        });
      };
      body.addEventListener('input', check);
      body.addEventListener('change', check);
      if (editing.existing) {
        const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small', textContent: 'Delete…' });
        remove.addEventListener('click', () => deleteSeries(editing.rule));
        actions.append(el('span', { className: 'tvr-spacer' }), remove);
      }
      pane.append(actions);
      check();
    }
  }

  // Said in three places, and it read as two different settings the first time they
  // disagreed about capitalisation.
  const MONITOR_NAMES = { 'unmonitor-only': 'Unmonitor only', 'full-sync': 'Full sync' };

  // A rule's keep window, as the one-time pass needs to remember it.
  function scopeOf(rule) {
    return { keep_days: rule.keep_days ?? null, keep_episodes: rule.keep_episodes ?? null,
             keep_seasons: rule.keep_seasons ?? null, combine: rule.combine || 'earliest',
             profile_id: rule.profile_id || '' };
  }




  const REMOVAL_ACTIONS = [
    ['remove', 'Leave the series untouched in Sonarr'],
    ['monitor-all', 'Set the entire series to monitored'],
    ['unmonitor-all', 'Set the entire series to unmonitored'],
    ['monitor-in-frame', 'Set only episodes inside the keep window to monitored'],
    ['delete-series', 'Ask Sonarr to delete the series, keeping the files'],
    ['delete-series-files', 'Ask Sonarr to delete the series and its files'],
  ];
  const REMOVAL_CONFIRM = { 'delete-series': 'DELETE', 'delete-series-files': 'DELETE ALL' };
  const REMOVAL_SONARR = {
    'remove': 'Sonarr will not be touched',
    'monitor-all': 'Sonarr will monitor the whole series',
    'unmonitor-all': 'Sonarr will unmonitor the whole series',
    'monitor-in-frame': 'Sonarr will monitor the episodes inside the keep window',
    'delete-series': 'Sonarr will delete the series record',
    'delete-series-files': 'Sonarr will delete the series record',
  };
  const REMOVAL_FILES = {
    'delete-series-files': 'Sonarr will delete its files',
  };

  const queuedRemoval = (rule) => (rule.queue || {}).removal || null;

  // A queued series states its intent on the card and can be taken back until a run
  // applies it. Nothing has happened yet, so the card says exactly what will.
  // Queued removal, said where the rest of this series' state is said. It used to replace
  // the whole card in the list, which meant a series about to be removed was the one thing
  // in the library you could not see the poster of — and undo has to stay one click away,
  // because nothing has happened yet and that is the whole point of queueing.
  function queuedBanner(rule) {
    const queued = queuedRemoval(rule);
    if (!queued) return null;
    const box = el('div', { className: 'tvr-queued-box' });
    box.append(el('div', { className: 'tvr-queued-lines' }, [
      el('div', { textContent: `Queued for removal at the next run, ${ago(queued.created_at)}.` }),
      el('div', { textContent: REMOVAL_SONARR[queued.action] || '' }),
      el('div', { className: REMOVAL_FILES[queued.action] ? 'tvr-queued-danger' : '',
                  textContent: REMOVAL_FILES[queued.action] || 'No files will be removed' }),
    ]));
    const undo = el('button', { type: 'button', className: 'tvr-action', textContent: 'Undo' });
    undo.addEventListener('click', () => guarded('', async () => {
      const target = (settings.rules || []).find((other) => other.id === rule.id);
      target.queue = Object.assign({}, target.queue, { removal: null });
      await saveSettings('Removal cancelled.');
      renderDetails();
    }));
    box.append(undo);
    return box;
  }

  // Removing a series is an intent, not an act: it queues, and the two destructive Sonarr
  // options each demand their own word before they can be queued at all.
  function deleteSeries(rule) {
    dialog(`Remove ${rule.series_title || rule.path}`, (body) => {
      body.append(el('p', { textContent:
        'This queues the series for removal from TV Retention. Nothing happens until the next '
        + 'run, and it can be undone from the series card until then.' }));
      const action = options(el('select'), REMOVAL_ACTIONS, 'remove');
      const warning = el('div', { className: 'tvr-danger-box', hidden: true });
      const confirm = el('input', { type: 'text', autocomplete: 'off', spellcheck: false });
      const confirmField = field('Confirm', confirm);
      confirmField.hidden = true;
      const review = () => {
        const word = REMOVAL_CONFIRM[action.value];
        warning.hidden = !word;
        confirmField.hidden = !word;
        confirm.placeholder = word || '';
        confirmField.querySelector('span').textContent = word ? `Type ${word} to confirm` : 'Confirm';
        // The button itself is unavailable until the word matches; throwing after a click
        // tells you the same thing later and less kindly.
        $('tvr-dialog-ok').disabled = !!word && confirm.value.trim() !== word;
        if (!word) return;
        warning.replaceChildren(
          el('strong', { textContent: 'Sonarr will delete this series.' }),
          el('span', { textContent: action.value === 'delete-series-files'
            ? ' Its episode files go too, and only Sonarr’s own recycle bin will hold them. '
              + 'TV Retention does not remove the series itself — it asks Sonarr to.'
            : ' The files stay on disk; only Sonarr’s record of the series is removed.' }));
      };
      action.addEventListener('change', review);
      confirm.addEventListener('input', review);
      body.append(field('Sonarr action', action,
                        'What Sonarr should do as the series leaves TV Retention.'), warning, confirmField);

      // Leaving is the moment to put monitoring back the way you want it, because after
      // this the plugin stops having an opinion about this series at all. Every season,
      // not only the window's — the window is about to stop mattering.
      const restore = toggle('Set monitoring in Sonarr before it goes', false, null,
                             { className: 'tvr-row-switch' });
      const treeBox = el('div', { className: 'tvr-tree-box', hidden: true });
      let tree = null;
      restore.input.addEventListener('change', () => guarded('', async () => {
        treeBox.hidden = !restore.input.checked;
        if (!restore.input.checked) { tree = null; treeBox.replaceChildren(); return; }
        const data = await api('episodes', { rule_id: rule.id }, 'Reading episodes…', true);
        tree = monitorTree(data.seasons, {});
        treeBox.replaceChildren(tree.node);
      }));
      body.append(restore.node, treeBox);
      review();
      return { action, confirm, tree: () => (restore.input.checked ? tree : null) };
    }, async (context) => {
      const word = REMOVAL_CONFIRM[context.action.value];
      if (word && context.confirm.value.trim() !== word) {
        throw new Error(`Type ${word} to confirm. Nothing was queued.`);
      }
      // Monitoring is set now rather than queued: it is reversible in a click, and the
      // point of it is that Sonarr behaves as you want between now and the run.
      const wanted = context.tree && context.tree();
      const changes = wanted ? wanted.changes() : { monitor: [], unmonitor: [] };
      let done = '';
      if (changes.monitor.length || changes.unmonitor.length) {
        const applied = await api('set-monitored', Object.assign({ rule_id: rule.id }, changes),
                                  'Setting monitoring in Sonarr…');
        const parts = [];
        if (applied.monitored) parts.push(`${plural(applied.monitored, 'episode')} monitored`);
        if (applied.unmonitored) parts.push(`${plural(applied.unmonitored, 'episode')} unmonitored`);
        done = parts.length ? ` ${parts.join(', ')} in Sonarr.` : '';
      }
      const target = (settings.rules || []).find((other) => other.id === rule.id);
      target.queue = Object.assign({}, target.queue,
                                   { removal: { action: context.action.value, created_at: new Date().toISOString() } });
      await saveSettings('Queued. It will be applied at the next run, and can be undone until then.' + done);
    }, 'Queue removal');
  }

  // -- schedule ----------------------------------------------------------
  const WEEKDAYS = [[0, 'Sunday'], [1, 'Monday'], [2, 'Tuesday'], [3, 'Wednesday'],
                    [4, 'Thursday'], [5, 'Friday'], [6, 'Saturday']];

  function renderSchedule() {
    const schedule = settings.schedule || {};
    $('tvr-schedule-enabled').checked = !!schedule.enabled;
    $('tvr-test-mode').checked = schedule.test_mode !== false;
    options($('tvr-weekday'), WEEKDAYS, schedule.weekday ?? 0);
    options($('tvr-monthly-day'), range(1, 28, true), schedule.monthly_day ?? 1);
    options($('tvr-monthly-weekday'), [['', 'Day of the month']].concat(WEEKDAYS),
            schedule.monthly_weekday === '' ? '' : (schedule.monthly_weekday ?? ''));
    options($('tvr-hour'), range(0, 23, true), schedule.hour ?? 4);
    options($('tvr-minute'), range(0, 59, true), schedule.minute ?? 0);
    $('tvr-freq').value = schedule.frequency || 'daily';
    $('tvr-monthly-mode').value = schedule.monthly_mode || 'day';
    $('tvr-cron').value = schedule.cron || '0 4 * * *';
    $('tvr-schedule-summary').textContent = snapshot.schedule_text || 'Off';

    applyScheduleVisibility();
  }

  // Only the fields that mean something for the chosen frequency are shown, so the form
  // never asks for a weekday that will be ignored.
  function applyScheduleVisibility() {
    const frequency = $('tvr-freq').value;
    const monthlyMode = $('tvr-monthly-mode').value;
    const show = (id, on) => { $(id).hidden = !on; };
    show('tvr-field-weekday', frequency === 'weekly');
    show('tvr-field-monthly-mode', frequency === 'monthly');
    show('tvr-field-monthly-day', frequency === 'monthly' && monthlyMode === 'day');
    show('tvr-field-monthly-weekday', frequency === 'monthly' && monthlyMode !== 'day');
    show('tvr-field-hour', frequency !== 'hourly' && frequency !== 'custom');
    show('tvr-field-minute', frequency !== 'custom');
    show('tvr-field-cron', frequency === 'custom');
  }
  ['tvr-freq', 'tvr-monthly-mode'].forEach((id) => $(id).addEventListener('change', applyScheduleVisibility));

  function collectSchedule() {
    return {
      enabled: $('tvr-schedule-enabled').checked,
      test_mode: $('tvr-test-mode').checked,
      frequency: $('tvr-freq').value,
      minute: $('tvr-minute').value,
      hour: $('tvr-hour').value,
      weekday: $('tvr-weekday').value,
      monthly_mode: $('tvr-monthly-mode').value,
      monthly_day: $('tvr-monthly-day').value,
      monthly_weekday: $('tvr-monthly-weekday').value,
      cron: $('tvr-cron').value.trim(),
    };
  }

  // Every control here saves itself the moment it changes. A switch that looks live and is
  // not is what lost a schedule: it was set, it read as set on every later visit, and no
  // run ever came, because the value only left the page if you found the Save button.
  // Programmatic assignment does not fire `change`, so rendering never triggers a save.
  async function saveScheduleNow() {
    settings.schedule = collectSchedule();
    await saveSettings(null, true);
    $('tvr-schedule-summary').textContent = snapshot.schedule_text || 'Off';
  }

  ['tvr-schedule-enabled', 'tvr-test-mode', 'tvr-freq', 'tvr-minute', 'tvr-hour', 'tvr-weekday',
   'tvr-monthly-mode', 'tvr-monthly-day', 'tvr-monthly-weekday', 'tvr-cron'
  ].forEach((id) => $(id).addEventListener('change', () => guarded('', saveScheduleNow)));

  // -- Sonarr instances --------------------------------------------------
  function renderInstances() {
    const container = $('tvr-instances');
    container.replaceChildren();
    (settings.instances || []).forEach((instance) => {
      const health = ((snapshot.health || {}).instances || {})[instance.id] || {};
      const reachable = health.reachable !== false;
      const card = el('div', { className: `tvr-instance ${reachable ? 'ok' : 'bad'}` });
      const line = el('div', { className: 'tvr-instance-line' });
      line.append(el('span', { className: `tvr-dot ${reachable ? 'ok' : 'bad'}`,
                               title: reachable ? 'Answering' : (health.error || 'Not answering') }));
      line.append(el('span', { className: 'tvr-rule-title', textContent: instance.name }));
      line.append(el('span', { className: 'tvr-chip tvr-mono', textContent: instance.url }));
      if (health.sonarr_version) line.append(el('span', { className: 'tvr-chip', textContent: `Sonarr ${health.sonarr_version}` }));
      if (!reachable) line.append(el('span', { className: 'tvr-tag blocking', textContent: 'unreachable' }));
      const edit = el('button', { type: 'button', className: 'tvr-small', textContent: 'Edit' });
      edit.addEventListener('click', () => editInstance(instance));
      line.append(el('span', { className: 'tvr-spacer' }));
      line.append(edit);
      // Enabling an instance is a switch on the card, like enabling a series.
      const control = toggle(instance.enabled ? 'Enabled' : 'Disabled', instance.enabled, null,
                             { className: 'tvr-card-switch', label: `${instance.name} enabled` });
      control.input.addEventListener('change', () => {
        const wanted = control.input.checked;
        control.input.disabled = true;
        guarded('', async () => {
          const target = (settings.instances || []).find((other) => other.id === instance.id);
          target.enabled = wanted;
          try {
            await saveSettings(null, true);
          } catch (error) {
            target.enabled = !wanted;
            renderInstances();
            throw error;
          }
        });
      });
      line.append(control.node);
      card.append(line);
      container.append(card);
    });
  }

  // Sonarr owns the filesystem, so the editor is only a connection: address, key, and
  // whether it answers. There is nothing left to map.
  function editInstance(existing) {
    const instance = Object.assign({ id: '', name: '', url: '', api_key: '', enabled: true,
                                     verify_tls: true }, existing || {});
    dialog(existing ? `Edit ${instance.name}` : 'Add Sonarr instance', (body) => {
      let verified = !!existing;
      const name = el('input', { type: 'text', value: instance.name, placeholder: 'Sonarr — Series' });
      const url = el('input', { type: 'text', value: instance.url, placeholder: 'http://192.168.1.10:8989', spellcheck: false });
      const key = el('input', { type: 'password', value: instance.api_key || '', autocomplete: 'off',
                                placeholder: 'Sonarr API key' });
      const enabled = toggle(instance.enabled ? 'Enabled' : 'Disabled', instance.enabled, null,
                             { className: 'tvr-card-switch' });
      const verify = toggle('Verify the TLS certificate', instance.verify_tls, null, { className: 'tvr-row-switch' });
      const testButton = el('button', { type: 'button', className: 'tvr-primary', textContent: 'Test connection' });
      const testResult = el('span', { className: 'tvr-result' });

      const gate = () => {
        $('tvr-dialog-ok').disabled = !verified;
        $('tvr-dialog-ok').textContent = verified ? 'Save' : 'Test first';
      };
      testButton.addEventListener('click', (event) => {
        event.preventDefault();
        guarded('', async () => {
          const data = await api('test-instance', {
            instance: { id: instance.id, name: name.value, url: url.value, api_key: key.value,
                        enabled: enabled.input.checked, verify_tls: verify.input.checked },
          }, 'Contacting Sonarr…');
          verified = true;
          testResult.textContent = `Connected — Sonarr ${data.sonarr_version}, ${data.series_count} series`
            + (data.recycle_bin ? '' : ' · no recycle bin');
          testResult.className = 'tvr-result ok';
          gate();
        });
      });

      body.append(
        el('div', { className: 'tvr-card-head' },
           [el('span', { className: 'tvr-card-title', textContent: 'Connection' }), enabled.node]),
        field('Name', name),
        field('URL', url, 'Include the port, and any base URL Sonarr is configured with.'),
        field('API key', key, existing ? 'Leave the masked value to keep the stored key.' : 'Sonarr: Settings → General → API Key.'),
        verify.node,
        el('div', { className: 'tvr-row tvr-inline' }, [testButton, testResult]),
      );
      if (existing) {
        const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small',
                                      textContent: 'Remove this instance' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          guarded('', async () => {
            const used = (settings.rules || []).filter((rule) => rule.instance_id === instance.id);
            if (used.length) throw new Error(`${plural(used.length, 'series')} still use ${instance.name}.`);
            if (!window.confirm(`Remove the Sonarr instance ${instance.name}?`)) return;
            $('tvr-dialog').close('cancel');
            settings.instances = settings.instances.filter((other) => other.id !== instance.id);
            await saveSettings('Instance removed.');
          });
        });
        body.append(el('div', { className: 'tvr-editor-foot' }, [remove]));
      }
      gate();
      return { name, url, key, enabled, verify, verified: () => verified };
    }, async (context) => {
      if (!context.verified()) throw new Error('Test the connection before saving.');
      settings.instances = (settings.instances || []).filter((other) => other.id !== instance.id).concat([{
        id: instance.id || undefined,
        name: context.name.value,
        url: context.url.value,
        api_key: context.key.value,
        enabled: context.enabled.input.checked,
        verify_tls: context.verify.input.checked,
        verified_at: new Date().toISOString(),
      }]);
      seriesCache = {};
      await saveSettings('Sonarr instance saved.');
    }, 'Test first');
  }

  $('tvr-add-instance').addEventListener('click', () => editInstance(null));

  const NOTIFICATIONS = [
    ['run_started', 'A run has started'],
    ['run_completed', 'A run has finished'],
    ['series_removed', 'A series was removed from Sonarr'],
    ['series_ended', 'Sonarr reports a series has ended'],
    ['series_added', 'Sonarr has a new series this plugin does not manage'],
    ['health_problems', 'A check found something wrong'],
    ['health_ok', 'A check found nothing wrong'],
    ['errors', 'Any error'],
  ];
  const notifyInputs = {};
  // Only the two that are a preference rather than a fault. The blocking kinds are absent
  // on purpose: hiding "this series will not run" does not stop it being true.
  const MUTABLE_KINDS = [
    ['no-recycle-bin', 'Sonarr has no recycle bin'],
    ['ended', 'A series has ended and still has episodes'],
    ['ended-expired', 'A series has ended with nothing left in its window'],
  ];
  const mutedInputs = {};

  // -- automation --------------------------------------------------------
  // The questions, their answers, and which answer a fresh install starts on. Radios
  // rather than a dropdown because the answers are the point: a closed select says
  // "Ask me" and hides the two things it could have done instead, which is how a default
  // ends up being something nobody chose because nobody saw it.
  //
  // The wording matches core's own ANSWERS maps, because the journal explains a run using
  // those, and a run explaining itself differently from the page that configured it is
  // worse than either wording alone.
  const AUTOMATION_QUESTIONS = [
    ['monitoring', 'tvr-auto-monitoring', [
      ['in_scope_unmonitored', 'If an episode within the keep scope is unmonitored', [
        ['monitor', 'Monitor all episodes within the keep scope automatically'],
        ['ignore', 'Do not change monitoring status'],
        ['ask', 'Ask me'],
      ]],
      ['out_scope_monitored', 'If an episode outside the keep scope is monitored', [
        ['unmonitor', 'Unmonitor all episodes outside the keep scope automatically'],
        ['exclude', 'Keep monitored, exclude from deletions'],
        ['ask', 'Ask me'],
      ]],
    ]],
    ['persistence', 'tvr-auto-persistence', [
      ['unmonitored_in_scope',
       'When Sonarr has unmonitored a previously monitored episode within the keep scope', [
        ['ignore', 'Ignore'],
        ['notice', 'Ignore, mark as notice'],
        ['remonitor', 'Remonitor that episode automatically'],
      ]],
      ['monitored_out_scope',
       'When Sonarr has monitored a previously unmonitored episode outside the keep scope', [
        ['notice-exclude', 'Mark as notice and add to exclusion list'],
        ['unmonitor', 'Unmonitor automatically'],
      ]],
    ]],
  ];
  // Stored as a flag rather than a word, because it only ever had two answers. Shown as
  // two answers anyway: "off" is not a thing anybody decided, and "wait for the RSS pass"
  // is.
  const SEARCH_QUESTION = [
    'When TV Retention has marked a previously unmonitored episode to monitor in Sonarr', [
      ['wait', 'Wait for the Sonarr RSS pass to search for the newly monitored episode'],
      ['search', 'Tell Sonarr to immediately begin a search on that series'],
    ]];
  const questionInputs = {};

  function questionNode(group, name, label, answers, chosen) {
    const boxes = [];
    const rows = answers.map(([value, caption]) => {
      const input = el('input', { type: 'radio', name: `tvr-${group}-${name}`,
                                  value, checked: value === chosen });
      boxes.push(input);
      return el('label', { className: 'tvr-answer' }, [input, el('span', { textContent: caption })]);
    });
    questionInputs[`${group}.${name}`] = () =>
      (boxes.find((box) => box.checked) || boxes[boxes.length - 1]).value;
    return el('div', { className: 'tvr-question' },
              [el('div', { className: 'tvr-question-ask', textContent: label }), ...rows]);
  }

  // A list of typed phrases, each with the button that removes it and one that adds
  // another. A textarea would hold the same strings, but a row per phrase is what the
  // thing actually is, and it makes an empty list look like an empty list.
  function phraseList(box, values) {
    const rows = [];
    const draw = () => {
      box.replaceChildren(...rows.map((row) => row.node),
                          el('div', { className: 'tvr-phrase-add' }, [add]));
    };
    const addRow = (value) => {
      const input = el('input', { type: 'text', spellcheck: false, value: value || '' });
      const drop = el('button', { type: 'button', className: 'tvr-icon-button tvr-phrase-drop',
                                  title: 'Remove this' }, [el('i', { className: 'fa fa-times' })]);
      const row = { node: el('div', { className: 'tvr-phrase' }, [input, drop]), input };
      drop.addEventListener('click', () => {
        rows.splice(rows.indexOf(row), 1);
        draw();
        box.dispatchEvent(new Event('change', { bubbles: true }));
      });
      rows.push(row);
      return row;
    };
    const add = el('button', { type: 'button', className: 'tvr-secondary tvr-small',
                               textContent: 'Add' });
    add.addEventListener('click', () => { const row = addRow(''); draw(); row.input.focus(); });
    (values || []).forEach(addRow);
    draw();
    return () => rows.map((row) => row.input.value.trim()).filter((value) => value !== '');
  }
  let folderPhrases = () => [];
  let episodePhrases = () => [];

  // -- air dates ---------------------------------------------------------
  // Every service that could answer "when did this air", with why it might not be
  // available. Listed even when it cannot be reached: a provider missing from the page is
  // indistinguishable from one nobody thought of, and "why isn't Plex here" is a question
  // the page should answer rather than provoke.
  const AIR_PROVIDERS = {
    tmdb: { name: 'TMDB', needs: 'Add an API key under Connections' },
    tvmaze: { name: 'TVMaze', needs: 'Not built yet — no key needed when it is' },
    anilist: { name: 'AniList', needs: 'Not built yet — no key needed when it is' },
    imdb: { name: 'IMDB', needs: 'No public API exists' },
    plex: { name: 'Plex', needs: 'Needs a Plex connection' },
    jellyfin: { name: 'Jellyfin', needs: 'Needs a Jellyfin connection' },
  };
  const AIR_QUESTIONS = [
    ['unresolved', 'tvr-air-unresolved', 'If none of those can resolve an air date', [
      ['estimate', 'Estimate the air date from neighbouring episodes or position'],
      ['leave', 'Leave it unresolved'],
    ]],
    ['still_unresolved', 'tvr-air-still', 'If the air date is still unresolved', [
      ['exclude', 'Add the episode to the exclusion list'],
      ['disable', 'Disable the series and raise an error'],
    ]],
  ];
  let airOrder = [];
  let airEnabled = new Set();

  // Only TMDB is wired to anything today, so only TMDB can be ticked. The rest carry the
  // reason instead of a box that does nothing, because a checkbox that saves a preference
  // no code reads is worse than an honest "not yet".
  const airReady = (name) => name === 'tmdb' && !!((settings.tmdb || {}).api_key || '').trim();

  function renderAirProviders() {
    const box = $('tvr-air-providers');
    box.replaceChildren(...airOrder.map((name, index) => {
      const meta = AIR_PROVIDERS[name] || { name, needs: '' };
      const ready = airReady(name);
      const tick = el('input', { type: 'checkbox', className: 'tvr-pick',
                                 checked: ready && airEnabled.has(name), disabled: !ready });
      tick.addEventListener('change', () => {
        if (tick.checked) airEnabled.add(name); else airEnabled.delete(name);
        settingsDirty(true);
      });
      const move = (to) => {
        if (to < 0 || to >= airOrder.length) return;
        airOrder.splice(to, 0, airOrder.splice(index, 1)[0]);
        renderAirProviders();
        settingsDirty(true);
      };
      const up = el('button', { type: 'button', className: 'tvr-icon-button', title: 'Ask this earlier',
                                disabled: index === 0 }, [el('i', { className: 'fa fa-caret-up' })]);
      const down = el('button', { type: 'button', className: 'tvr-icon-button', title: 'Ask this later',
                                  disabled: index === airOrder.length - 1 },
                      [el('i', { className: 'fa fa-caret-down' })]);
      up.addEventListener('click', () => move(index - 1));
      down.addEventListener('click', () => move(index + 1));
      return el('div', { className: `tvr-provider${ready ? '' : ' tvr-provider-off'}` }, [
        el('span', { className: 'tvr-provider-rank', textContent: String(index + 1) }),
        tick,
        el('span', { className: 'tvr-provider-name', textContent: meta.name }),
        el('span', { className: 'tvr-provider-why', textContent: ready ? '' : meta.needs }),
        up, down,
      ]);
    }));
  }

  function renderSettings() {
    const retention = settings.retention || {};
    $('tvr-monitoring').value = retention.monitoring || 'unmonitor-only';
    const air = settings.air_dates || {};
    airOrder = (air.providers || Object.keys(AIR_PROVIDERS)).slice();
    airEnabled = new Set(air.enabled || []);
    renderAirProviders();
    AIR_QUESTIONS.forEach(([name, target, label, answers]) => {
      $(target).replaceChildren(questionNode('air', name, label, answers, air[name]));
    });
    const describeMonitoring = () => {
      $('tvr-monitoring-help').textContent = $('tvr-monitoring').value === 'full-sync'
        ? 'Episodes inside the keep window are set to monitored, including ones with no file — '
          + 'which asks Sonarr to download them. On a large library that can be hundreds of episodes.'
        : 'Nothing is ever set to monitored. Widening a series’ keep window will not start '
          + 'downloads for the seasons it now covers; the series editor offers a one-time pass for that.';
    };
    $('tvr-monitoring').onchange = describeMonitoring;
    describeMonitoring();
    const automation = settings.automation || {};
    AUTOMATION_QUESTIONS.forEach(([group, target, questions]) => {
      $(target).replaceChildren(...questions.map(([name, label, answers]) =>
        questionNode(group, name, label, answers, (automation[group] || {})[name])));
    });
    $('tvr-auto-search').replaceChildren(
      questionNode('search', 'after_monitor', SEARCH_QUESTION[0], SEARCH_QUESTION[1],
                   automation.search_after_monitor ? 'search' : 'wait'));
    $('tvr-exclude-specials').checked = automation.exclude_specials !== false;
    $('tvr-exclude-seasons').value = (automation.exclude_seasons || []).join(', ');
    folderPhrases = phraseList($('tvr-exclude-folders'), automation.exclude_folders);
    episodePhrases = phraseList($('tvr-exclude-episodes'), automation.exclude_episodes);
    $('tvr-tmdb-key').value = (settings.tmdb || {}).api_key || '';
    $('tvr-history-size').value = settings.log_retention_runs;
    $('tvr-log-level').value = (settings.logging || {}).level || 'info';

    const box = $('tvr-notifications');
    box.replaceChildren();
    NOTIFICATIONS.forEach(([name, label]) => {
      const control = toggle(label, (settings.notifications || {})[name], null, { className: 'tvr-notify-row' });
      notifyInputs[name] = control.input;
      box.append(control.node);
    });
  }

  // A typed list, one per line or one per comma. Blank lines are how a list is edited, not
  // an entry, so they go — including the trailing one every textarea ends with.
  const typedList = (value, separator) => (value || '').split(separator)
    .map((entry) => entry.trim()).filter((entry) => entry !== '');

  function collectSettings() {
    const notifications = {};
    NOTIFICATIONS.forEach(([name]) => { notifications[name] = notifyInputs[name].checked; });
    return Object.assign({}, settings, {
      // `allow_estimated_dates` is not sent: it is derived from the air-date answer on the
      // way in, so posting it as well would be two sources for one decision.
      retention: { monitoring: $('tvr-monitoring').value },
      air_dates: {
        providers: airOrder.slice(),
        enabled: airOrder.filter((name) => airEnabled.has(name)),
        unresolved: questionInputs['air.unresolved'](),
        still_unresolved: questionInputs['air.still_unresolved'](),
      },
      automation: {
        monitoring: {
          in_scope_unmonitored: questionInputs['monitoring.in_scope_unmonitored'](),
          out_scope_monitored: questionInputs['monitoring.out_scope_monitored'](),
        },
        persistence: {
          unmonitored_in_scope: questionInputs['persistence.unmonitored_in_scope'](),
          monitored_out_scope: questionInputs['persistence.monitored_out_scope'](),
        },
        search_after_monitor: questionInputs['search.after_monitor']() === 'search',
        exclude_specials: $('tvr-exclude-specials').checked,
        exclude_seasons: typedList($('tvr-exclude-seasons').value, ','),
        exclude_folders: folderPhrases(),
        exclude_episodes: episodePhrases(),
      },
      tmdb: { api_key: $('tvr-tmdb-key').value },
      log_retention_runs: $('tvr-history-size').value,
      logging: Object.assign({}, settings.logging, { level: $('tvr-log-level').value }),
      alerts: {
        header: $('tvr-alert-header').value,
        acknowledge: $('tvr-alert-ack').checked,
        test_banner: $('tvr-test-banner-mode').value,
        muted: MUTABLE_KINDS.map(([kind]) => kind).filter((kind) => mutedInputs[kind] && mutedInputs[kind].checked),
      },
      // No longer a control. It was one number doing two jobs — when the daily sweep is
      // due, and when a reading counts as stale — and a resident loop watching the change
      // feed every thirty seconds answers both without being asked. Kept in the settings
      // so the value survives, and left where it is.
      health: settings.health,
      notifications,
    });
  }

  async function saveSettings(message, quiet) {
    const data = await api('settings', { settings: collectSettings() }, 'Saving…', quiet);
    settings = data.settings;
    snapshot.settings = settings;
    snapshot.schedule_text = data.schedule_text || snapshot.schedule_text;
    render();
    if (message) notice(message, 'ok');
  }

  // This panel keeps an explicit Save — it holds text you type, and saving a half-typed
  // path on every keystroke would be worse. What it must not do is let a change leave the
  // page unsaved without saying so.
  // The settings are spread across several views now, each with its own Save. They all
  // post the whole document, so saving from one view keeps what another holds.
  const settingsDirty = (on) => {
    document.querySelectorAll('.tvr-dirty-mark').forEach((mark) => { mark.hidden = !on; });
  };
  document.querySelectorAll('.tvr-view:has(.tvr-save)').forEach((view) => {
    view.addEventListener('change', (event) => {
      if (event.target.closest('#tvr-instances')) return;   // instance cards save themselves
      if (event.target.closest('#tvr-view-settings-schedule')) return;  // and so does the schedule
      settingsDirty(true);
    });
    view.addEventListener('input', () => settingsDirty(true));
  });
  document.querySelectorAll('.tvr-save').forEach((button) => {
    button.addEventListener('click', () => guarded('', async () => {
      await saveSettings('Settings saved.');
      settingsDirty(false);
    }));
  });

  // -- stats -------------------------------------------------------------
  // Answered from what is already kept: the run journal for what has been reclaimed, and
  // the stored reading for the shape of the library. Nothing new is recorded for this.
  async function renderStatsView() {
    const data = await api('stats', {}, 'Totalling…', true);
    const summary = $('tvr-stats-summary');
    const runs = data.runs || {};
    $('tvr-stats-age').textContent = runs.first ? `since ${when(runs.first)}` : 'no runs yet';
    $('tvr-stats-empty').hidden = (runs.count || 0) > 0;
    summary.replaceChildren(...[
      ['RECLAIMED', bytes(runs.freed_bytes || 0), `${plural(runs.deleted || 0, 'episode')} deleted`],
      ['RUNS', String(runs.count || 0), runs.last ? `last ${when(runs.last)}` : 'none yet'],
      ['UNDER A RULE', plural((data.library || {}).managed || 0, 'series'),
       `of ${plural((data.library || {}).series || 0, 'series')} in Sonarr`],
      ['MANAGED SIZE', bytes((data.library || {}).managed_bytes || 0),
       `library holds ${bytes((data.library || {}).bytes || 0)}`],
    ].map(([name, value, note]) => el('div', {}, [
      el('span', { textContent: name }), el('strong', { textContent: value }),
      el('small', { textContent: note }),
    ])));

    const months = $('tvr-stats-months');
    months.replaceChildren();
    const rows = data.months || [];
    const peak = Math.max(1, ...rows.map((row) => row.freed_bytes || 0));
    if (!rows.length) months.append(el('p', { className: 'tvr-plan-quiet', textContent: 'Nothing reclaimed yet.' }));
    rows.forEach((row) => {
      const bar = el('div', { className: 'tvr-bar-fill' });
      bar.style.width = `${Math.max(2, Math.round((row.freed_bytes / peak) * 100))}%`;
      months.append(el('div', { className: 'tvr-bar-row' }, [
        el('span', { className: 'tvr-bar-label', textContent: row.month }),
        el('div', { className: 'tvr-bar' }, [bar]),
        el('span', { className: 'tvr-bar-value', textContent: bytes(row.freed_bytes) }),
      ]));
    });

    const shape = data.library || {};
    $('tvr-stats-library').replaceChildren(...[
      ['Series in Sonarr', String(shape.series || 0)],
      ['Episodes on disk', `${shape.files || 0} of ${shape.episodes || 0}`],
      ['Ended series', String(shape.ended || 0)],
      ['Largest series', shape.largest ? `${shape.largest.title} — ${bytes(shape.largest.bytes)}` : '—'],
    ].map(([name, value]) => el('div', { className: 'tvr-inline-row' }, [
      el('span', { className: 'tvr-inline-label', textContent: name }),
      el('span', { textContent: value }),
    ])));

    const body = $('tvr-stats-series').querySelector('tbody');
    body.replaceChildren();
    (data.series || []).forEach((row) => body.append(el('tr', {}, [
      el('td', { textContent: row.title }),
      el('td', { textContent: String(row.runs) }),
      el('td', { textContent: String(row.deleted) }),
      el('td', { textContent: bytes(row.freed_bytes) }),
    ])));
    $('tvr-stats-series').hidden = !(data.series || []).length;
  }

  function renderAlertSettings() {
    const options = (settings || {}).alerts || {};
    $('tvr-alert-header').value = options.header || 'all';
    $('tvr-alert-ack').checked = options.acknowledge !== false;
    $('tvr-test-banner-mode').value = options.test_banner || 'full';
    const box = $('tvr-alert-muted');
    box.replaceChildren(el('p', { className: 'tvr-lede',
                                  textContent: 'A kind hidden here is never shown and never notified '
                                               + 'about — including ones you have not seen yet. It still '
                                               + 'blocks a series if that is what it does.' }));
    MUTABLE_KINDS.forEach(([kind, label]) => {
      const control = toggle(label, (options.muted || []).includes(kind), null,
                             { className: 'tvr-row-switch' });
      control.input.dataset.kind = kind;
      mutedInputs[kind] = control.input;
      box.append(control.node);
    });
  }

  function renderAbout() {
    const box = $('tvr-about-state');
    if (!box) return;
    const sync = (snapshot.sync || {});
    const rows = [
      ['Series with a rule', plural((settings.rules || []).length, 'series')],
      ['Sonarr last read', sync.synced_at ? ago(sync.synced_at) : 'not yet'],
      ['Schedule', (settings.schedule || {}).enabled ? (snapshot.schedule_text || 'on') : 'off'],
      ['Test Mode', testMode() ? 'on — nothing writes, scheduled or manual' : 'off'],
      ['Storage', settings.state_dir || ''],
    ];
    box.replaceChildren(...rows.map(([name, value]) => el('div', { className: 'tvr-inline-row' }, [
      el('span', { className: 'tvr-inline-label', textContent: name }),
      el('span', { textContent: String(value) }),
    ])));
  }  $('tvr-tmdb-test').addEventListener('click', () => guarded('', async () => {
    const result = $('tvr-tmdb-result');
    try {
      await api('test-tmdb', { tmdb: { api_key: $('tvr-tmdb-key').value } }, 'Contacting TMDB…');
      result.textContent = 'Key accepted';
      result.className = 'tvr-result ok';
    } catch (error) {
      result.textContent = 'Key rejected';
      result.className = 'tvr-result bad';
      throw error;
    }
  }));

  // -- run results and history -------------------------------------------
  function showResult(result, title) {
    dialog(`${title}: ${result.dry_run ? 'nothing was changed' : `${result.deleted} files deleted`}`, (body) => {
      (result.blocked || []).forEach((message) => body.append(el('div', { className: 'tvr-warning', textContent: message })));
      body.append(el('p', { textContent:
        `${result.planned} file(s) across ${result.rules.length} series in ${result.duration_seconds}s.`
        + (result.dry_run ? ' Nothing was changed.' : ` ${bytes(result.freed_bytes)} reclaimed.`) }));
      (result.removals || []).forEach((record) => {
        body.append(el('div', { className: 'tvr-change-series' }, [
          el('strong', { textContent: record.series_title }),
          el('div', { className: 'tvr-plan delete', textContent: record.label }),
        ]));
      });
      result.rules.forEach((rule) => {
        if (!rule.deleted.length && !(rule.monitor_list || []).length
            && !(rule.unmonitor_list || []).length && !rule.error) return;
        const card = el('div', { className: 'tvr-change-series' });
        card.append(el('strong', { textContent: rule.series_title }));
        if (rule.error) card.append(el('div', { className: 'tvr-error', textContent: rule.error }));
        if (rule.note) card.append(el('small', { textContent: rule.note }));
        // The same renderer the scheduled view uses, so a report and a plan read alike.
        card.append(changeRows(rule, () => true));
        body.append(card);
      });
      return {};
    }, null, 'Close');
  }

  function renderHistory() {
    const runs = snapshot.runs || [];
    const tbody = $('tvr-history-table').querySelector('tbody');
    tbody.replaceChildren();
    $('tvr-history-empty').hidden = runs.length > 0;
    $('tvr-history-table').hidden = runs.length === 0;
    runs.forEach((run) => {
      const tr = el('tr');
      [when(run.started), run.scheduled ? 'schedule' : 'manual', run.dry_run ? 'preview' : 'live',
       run.planned, run.deleted, bytes(run.freed_bytes), run.aborted || (run.errors || []).join('; ') || '']
        .forEach((value) => tr.append(el('td', { textContent: String(value) })));
      tbody.append(tr);
    });
    const last = $('tvr-last-run');
    last.replaceChildren();
    if (snapshot.last_run) {
      const button = el('button', { type: 'button', className: 'tvr-secondary', textContent: 'Show the last run report' });
      button.addEventListener('click', () => showResult(snapshot.last_run, 'Last run'));
      last.append(button);
    }
  }

  $('tvr-clear-history').addEventListener('click', () => guarded('', async () => {
    if (!window.confirm('Clear the run history? The on-disk journal is kept.')) return;
    await api('clear-history', {}, 'Clearing…');
    await refresh();
    notice('History cleared.', 'ok');
  }));

  // -- live log ----------------------------------------------------------
  let logTimer = null;
  let logOffset = 0;

  function startLog() {
    if (logTimer) return;
    logOffset = 0;
    $('tvr-log').textContent = '';
    const tick = async () => {
      try {
        const data = await api('log', { offset: logOffset }, '', true);
        logOffset = data.offset;
        if (data.text) {
          const pane = $('tvr-log');
          pane.append(text(data.text));
          // Keep the pane bounded; the file itself is the record, this is just the view.
          if (pane.textContent.length > 400000) pane.textContent = pane.textContent.slice(-200000);
          if ($('tvr-log-follow').checked) pane.scrollTop = pane.scrollHeight;
        }
        $('tvr-log-status').textContent = `${(settings.logging || {}).level || 'warning'} · ${bytes(data.size)}`;
      } catch (error) {
        $('tvr-log-status').textContent = 'log unavailable';
      }
    };
    logTimer = setInterval(tick, 1500);
    tick();
  }

  function stopLog() {
    if (!logTimer) return;
    clearInterval(logTimer);
    logTimer = null;
  }

  $('tvr-log-clear').addEventListener('click', () => { $('tvr-log').textContent = ''; });

  // -- start -------------------------------------------------------------
  // Whatever happens, the page must end up interactive with a readable message.
  refresh().catch((error) => {
    busyDepth = 0;
    $('tvr-busy').hidden = true;
    notice(`TV Retention could not load: ${error.message}`, 'bad');
  });
  window.addEventListener('error', () => { busyDepth = 0; $('tvr-busy').hidden = true; });
  window.addEventListener('unhandledrejection', () => { busyDepth = 0; $('tvr-busy').hidden = true; });
})();
