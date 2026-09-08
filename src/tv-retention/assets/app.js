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

  // A "?" the reader can ask, rather than a paragraph under every row shouting at once.
  function hint(explanation) {
    const mark = el('button', { type: 'button', className: 'tvr-hint', textContent: '?',
                                title: explanation, 'aria-label': explanation });
    mark.addEventListener('click', (event) => { event.preventDefault(); window.alert(explanation); });
    return mark;
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

  // -- tabs --------------------------------------------------------------
  // Read from the markup rather than listed here. A hand-kept copy disagreed with the page
  // the moment a tab was removed: the lookup for the departed panel returned null, setting
  // hidden on null threw, and the loop that shows one panel and hides the rest died at
  // that point — so every tab listed after the missing one stopped appearing at all. The
  // list the markup already carries is the one to trust.
  const TABS = [...document.querySelectorAll('.tvr-tabs button')].map((button) => button.dataset.tab);
  document.querySelectorAll('.tvr-tabs button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.tvr-tabs button').forEach((other) => other.classList.toggle('active', other === button));
      TABS.forEach((name) => { $(`tvr-panel-${name}`).hidden = name !== button.dataset.tab; });
      if (button.dataset.tab === 'log') startLog();
      else stopLog();
    });
  });

  function browseFolder(startPath, onPick) {
    let current = startPath || '/mnt/user';
    dialog('Choose a folder', (body) => {
      const crumb = el('div', { className: 'tvr-mono' });
      const list = el('div', { className: 'tvr-rules' });
      const use = el('button', { type: 'button', className: 'tvr-primary', textContent: 'Use this folder' });
      use.addEventListener('click', (event) => { event.preventDefault(); $('tvr-dialog').close('cancel'); onPick(current); });
      body.append(crumb, list, use);
      const load = (path) => guarded('', async () => {
        const data = await api('browse', { path }, 'Reading folder…');
        current = data.path;
        crumb.textContent = data.path;
        list.replaceChildren();
        if (data.parent) {
          const up = el('button', { type: 'button', textContent: 'Up one level' });
          up.addEventListener('click', (event) => { event.preventDefault(); load(data.parent); });
          list.append(up);
        }
        data.entries.forEach((entry) => {
          const button = el('button', { type: 'button', textContent: entry.name, className: 'tvr-folder' });
          button.addEventListener('click', (event) => { event.preventDefault(); load(entry.path); });
          list.append(button);
        });
        if (!data.entries.length) list.append(el('p', { className: 'tvr-empty', textContent: 'No sub-folders here.' }));
      });
      load(current);
      return {};
    }, null, 'Close');
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
    else if (snapshot.array_ready) queueChecks(snapshot.stale_rules || []);
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
  }

  const seriesAlerts = (ruleId) => alertsByRule[ruleId] || [];
  const seriesAlertList = () => Object.values(alertsByRule).reduce((all, list) => all.concat(list), []);
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
    if (!snapshot || !snapshot.array_ready) return;
    let data;
    try {
      data = await api('watch', {}, '', true);
    } catch (error) {
      return;  // a heartbeat that misses a beat is not worth interrupting anyone for
    }
    if (!data.array_ready) return;
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
    $('tvr-version').textContent = snapshot.version || '';
    $('tvr-about-version').textContent = snapshot.version || '';
    $('tvr-array').hidden = !!snapshot.array_ready;
    const banner = ((settings || {}).alerts || {}).test_banner || 'full';
    $('tvr-test-banner').hidden = !snapshot.test_mode || banner === 'chip';
    $('tvr-test-chip').hidden = !snapshot.test_mode || banner !== 'chip';
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

  function showView(name) {
    if (!VIEWS.includes(name)) name = 'series-all';
    currentView = name;
    const panel = LIBRARY[name] ? 'series-all' : name;
    [...new Set(VIEWS)].forEach((view) => {
      const section = $(`tvr-view-${LIBRARY[view] ? 'series-all' : view}`);
      if (section) section.hidden = (LIBRARY[view] ? 'series-all' : view) !== panel;
    });
    document.querySelectorAll('.tvr-side [data-view]').forEach((button) => {
      button.classList.toggle('active', button.dataset.view === name);
    });
    if (LIBRARY[name]) {
      libraryFilter = LIBRARY[name];
      $('tvr-library-title').textContent = TITLES[libraryFilter];
      renderLibrary();          // it fetches itself if what it needs is not in hand
    }
    if (name === 'system-stats') guarded('', renderStatsView);
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
  function changeSummary(plan) {
    const rows = [];
    Object.entries((plan.removals_by_action) || {}).forEach(([action, count]) => {
      rows.push({ kind: 'remove', tone: action.startsWith('delete') ? 'delete' : 'unmonitor',
                  text: (REMOVAL_SUMMARY[action] || REMOVAL_SUMMARY.remove)(count) });
    });
    if (plan.delete) {
      rows.push({ kind: 'delete', tone: 'delete',
                  text: `${plural(plan.delete, 'episode')} scheduled for deletion (${bytes(plan.delete_bytes)})` });
    }
    if (plan.monitor) {
      rows.push({ kind: 'monitor', tone: 'monitor',
                  text: `${plural(plan.monitor, 'episode')} will be set to monitored` });
    }
    if (plan.unmonitor) {
      rows.push({ kind: 'unmonitor', tone: 'unmonitor',
                  text: `${plural(plan.unmonitor, 'episode')} will be set to unmonitored` });
    }
    return rows;
  }

  function changeLines(plan, onOpen) {
    const rows = changeSummary(plan);
    const list = el('div', { className: 'tvr-plan-list' });
    // The total only earns its line when more than one kind of thing is happening.
    if (rows.length > 1 && onOpen) {
      const total = el('button', { type: 'button', className: 'tvr-plan-total',
                                   textContent: plural(plan.actionable, 'scheduled change') });
      total.addEventListener('click', () => onOpen('all'));
      list.append(total);
    }
    rows.forEach((row) => {
      const line = onOpen
        ? el('button', { type: 'button', className: `tvr-plan ${row.tone}`, textContent: row.text })
        : el('div', { className: `tvr-plan ${row.tone}`, textContent: row.text });
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
    // The run button is the play: nothing to run means nothing to press.
    $('tvr-run').disabled = nothing || !snapshot.array_ready;
    $('tvr-run').title = nothing ? 'Nothing is scheduled to change' : 'Run now';

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
    setBadge($('tvr-badge-media-connections'), instances);
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

  // The one overview left: what is wrong, and where to go and fix it.
  $('tvr-alert-total').addEventListener('click', () => {
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
  });

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
    const runnable = (settings.rules || []).filter((rule) => rule.enabled && !isBlocked(rule.id));
    if (!runnable.length) throw new Error('There are no enabled series ready to run.');
    const plan = snapshot.plan || {};
    // The confirmation states the actual plan, because a manual run is always live and
    // this dialog is the only thing standing in front of it.
    let warning = `Run ${plural(runnable.length, 'series')} now?\n\n`;
    warning += plan.actionable ? `This will ${planText(plan)}.\n\n` : 'No changes are currently expected.\n\n';
    if (snapshot.test_mode) {
      warning += 'Test mode is active on the scheduler, so this is NOT what the schedule would do — '
        + 'a manual run makes real changes to your files and to Sonarr.\n\n'
        + 'Cancel and choose "Show scheduled changes" if you wanted to look first.';
    } else {
      warning += 'This deletes episode files through Sonarr and cannot be undone from here.';
    }
    if (!window.confirm(warning)) return;
    const data = await api('run', {}, 'Running…');
    await refresh();
    showResult(data.result, 'Run');
  }));

  // -- series ------------------------------------------------------------
  const ATTENTION_RANK = { error: 0, warning: 1, notice: 2, '': 3 };

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

  function keepWindow(rule) {
    // One comparable number for "how much is kept", so series with different conditions
    // still order sensibly. Days dominate; episodes and seasons approximate.
    const source = presetFor(rule) || rule;
    const candidates = [];
    if (source.keep_days) candidates.push(Number(source.keep_days));
    if (source.keep_episodes) candidates.push(Number(source.keep_episodes) * 7);
    if (source.keep_seasons) candidates.push(Number(source.keep_seasons) * 365);
    return candidates.length ? Math.min(...candidates) : Number.MAX_SAFE_INTEGER;
  }

  function sortRules(rules) {
    const byTitle = (a, b) => (a.series_title || a.path).localeCompare(b.series_title || b.path,
                                                                      undefined, { sensitivity: 'base' });
    const mode = $('tvr-sort').value;
    const sorted = rules.slice();
    if (mode === 'title') return sorted.sort(byTitle);
    if (mode === 'title-desc') return sorted.sort((a, b) => byTitle(b, a));
    if (mode === 'checked') {
      return sorted.sort((a, b) => {
        const at = (monitoring[a.id] || {}).checked_at || '';
        const bt = (monitoring[b.id] || {}).checked_at || '';
        if (!at !== !bt) return at ? 1 : -1;   // never checked first: least known, not most recent
        return at.localeCompare(bt) || byTitle(a, b);
      });
    }
    if (mode === 'keep') return sorted.sort((a, b) => keepWindow(a) - keepWindow(b) || byTitle(a, b));
    if (mode === 'preset') {
      return sorted.sort((a, b) => ((presetFor(a) || {}).name || '').localeCompare((presetFor(b) || {}).name || '')
        || byTitle(a, b));
    }
    if (mode === 'instance') {
      const name = (rule) => ((settings.instances || []).find((i) => i.id === rule.instance_id) || {}).name || '';
      return sorted.sort((a, b) => name(a).localeCompare(name(b)) || byTitle(a, b));
    }
    return sorted.sort((a, b) => {
      const rank = (rule) => (isBlocked(rule.id) ? -1 : ATTENTION_RANK[worstSeverity(seriesAlerts(rule.id))] ?? 3);
      return rank(a) - rank(b) || byTitle(a, b);
    });
  }

  // Enabling a series is the most frequent change anyone makes, so it lives on the card.
  // It saves immediately and reverts visibly if the save is refused.
  function enableToggle(rule) {
    const control = toggle(rule.enabled ? 'Enabled' : 'Disabled', rule.enabled, null, {
      className: `tvr-card-switch${rule.enabled ? ' on' : ''}`,
      disabled: isChecking(rule.id),
      label: `${rule.series_title || rule.path} enabled`,
      title: 'Disabled series are skipped by runs and by the checks',
    });
    control.input.addEventListener('change', () => {
      const wanted = control.input.checked;
      control.input.disabled = true;
      control.caption.textContent = 'Saving…';
      guarded('', async () => {
        const target = (settings.rules || []).find((other) => other.id === rule.id);
        if (!target) throw new Error('That series is no longer in the list.');
        target.enabled = wanted;
        try {
          await saveSettings(null, true);
        } catch (error) {
          target.enabled = !wanted;
          renderRules();
          throw error;
        }
      });
    });
    return control.node;
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

  // Series problems are acted on from the series card, so their roll-up belongs on this
  // tab — and for the same reason the Alerts tab never counts them.
  function renderSeriesRollup() {
    const rollup = $('tvr-series-rollup');
    const list = seriesAlertList();
    rollup.hidden = bulkChecking || list.length === 0;
    if (rollup.hidden) return;
    const affected = new Set(list.map((alert) => alert.rule_id)).size;
    const bySeverity = { error: 0, warning: 0, notice: 0 };
    list.forEach((alert) => { bySeverity[alert.severity] += 1; });
    rollup.replaceChildren(
      el('strong', { textContent: `${plural(list.length, 'alert')} need to be addressed` }),
      el('span', { textContent: ` across ${plural(affected, 'series')} — ${bySeverity.error} critical, `
        + `${bySeverity.warning} warning, ${bySeverity.notice} notice. Open this to show only those series.` }));
  }

  // Remembered per browser, because it is a preference about looking rather than a
  // setting about behaviour — it belongs to the person at the screen, not to the plugin.
  const remember = (name, value) => { try { localStorage.setItem(`tvr.${name}`, value); } catch (error) { /* private window */ } };
  const remembered = (name, fallback) => {
    try { return localStorage.getItem(`tvr.${name}`) || fallback; } catch (error) { return fallback; }
  };
  let layout = remembered('layout', 'list');
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
  function visibleLibrary() {
    if (library === null) return [];
    const term = ($('tvr-search').value || '').trim().toLowerCase();
    const hideEnded = $('tvr-hide-ended').checked;
    const onlyAlerts = $('tvr-only-alerts').checked;
    const rows = [];
    library.forEach((series) => {
      const rule = ruleFor(series);
      if (libraryFilter === 'connected' && !rule) return;
      if (libraryFilter === 'unconnected' && rule) return;
      // The toggle hides what is ended *and* unmanaged. A connected series is never
      // hidden: it is your own rule, and an ended one is where retention matters most.
      if (hideEnded && series.ended && !rule) return;
      const alertsHere = rule ? seriesAlerts(rule.id) : [];
      if (onlyAlerts && !alertsHere.length) return;
      if (term && !(`${series.title} ${series.path || ''}`.toLowerCase().includes(term))) return;
      rows.push({ series, rule, alerts: alertsHere });
    });
    const order = $('tvr-sort').value;
    const rank = (row) => (row.rule && isBlocked(row.rule.id) ? -1
      : (ATTENTION_RANK[worstSeverity(row.alerts)] ?? (row.rule ? 3 : 4)));
    rows.sort((a, b) => (
      order === 'title-desc' ? String(b.series.sort_title || b.series.title).localeCompare(String(a.series.sort_title || a.series.title))
      : order === 'added' ? String(b.series.added || '').localeCompare(String(a.series.added || ''))
      : order === 'size' ? (b.series.size_on_disk || 0) - (a.series.size_on_disk || 0)
      : order === 'episodes' ? (b.series.total_episode_count || 0) - (a.series.total_episode_count || 0)
      : order === 'keep' ? keepRank(a.rule) - keepRank(b.rule)
      : order === 'attention' ? (rank(a) - rank(b)
          || String(a.series.sort_title || a.series.title).localeCompare(String(b.series.sort_title || b.series.title)))
      : String(a.series.sort_title || a.series.title).localeCompare(String(b.series.sort_title || b.series.title))));
    return rows;
  }

  const keepRank = (rule) => {
    if (!rule) return Number.MAX_SAFE_INTEGER;
    const active = presetFor(rule) || rule;
    return Number(active.keep_days || active.keep_episodes || active.keep_seasons || Number.MAX_SAFE_INTEGER);
  };

  function posterNode(series, className) {
    const art = el('div', { className: className || 'tvr-poster' });
    if (series.poster) {
      art.append(el('img', { loading: 'lazy', alt: '',
                             src: `${API}?poster=${series.series_id}&instance=${encodeURIComponent(series.instance_id)}`
                                  + `&csrf_token=${encodeURIComponent(CSRF)}` }));
    } else {
      art.textContent = (series.title || '?').slice(0, 1);
    }
    return art;
  }

  function renderLibrary() {
    renderSeriesRollup();
    const container = $('tvr-rules');
    const rows = visibleLibrary();
    container.className = layout === 'grid' ? 'tvr-rules tvr-rules-grid' : 'tvr-rules';
    container.replaceChildren();
    $('tvr-only-alerts-wrap').hidden = seriesAlertList().length === 0;
    selected = new Set([...selected].filter((id) => (settings.rules || []).some((r) => r.id === id)));
    $('tvr-selected-count').hidden = selected.size === 0;
    $('tvr-selected-count').textContent = `${plural(selected.size, 'series')} selected`;
    $('tvr-select-none').hidden = selected.size === 0;
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
    $('tvr-rules-empty').hidden = rows.length > 0;
    // Three thousand cards is not a list anyone reads, and it is not a page any browser
    // enjoys laying out. Search and the filters are how you get to the rest.
    rows.slice(0, LIBRARY_LIMIT).forEach((row) => container.append(
      row.rule && queuedRemoval(row.rule) ? queuedCard(row.rule) : libraryCard(row)));
    if (rows.length > LIBRARY_LIMIT) {
      container.append(el('p', { className: 'tvr-empty',
                                 textContent: `${rows.length - LIBRARY_LIMIT} more — search, or narrow the filters.` }));
    }
  }
  const renderRules = renderLibrary;

  // One card for a series, whether or not it has a rule. The check is the difference, and
  // it is the only difference the eye needs: everything else follows from it.
  function libraryCard(row) {
    const { series, rule } = row;
    const blocked = rule ? isBlocked(rule.id) : false;
    const card = el('div', { className: `tvr-rule ${rule ? (blocked ? 'blocked' : 'ok') : 'loose'}`
                                        + (rule && !rule.enabled ? ' disabled' : '')
                                        + (rule && selected.has(rule.id) ? ' selected' : '') });
    card.addEventListener('click', (event) => {
      if (event.target.closest('button, input, select, a, label')) return;
      if (rule && (event.shiftKey || event.ctrlKey || event.metaKey)) {
        // Building a selection: no editor, because the next click may make it a mass edit.
        editing = null;
        if (selected.has(rule.id)) selected.delete(rule.id); else selected.add(rule.id);
      } else if (rule) {
        // One series, one panel: opening it *is* opening its settings. A read-only card
        // with an Edit button was a step that only ever had one answer.
        if (selected.has(rule.id) && selected.size === 1 && editing) {
          selected = new Set();
          editing = null;
        } else {
          selected = new Set([rule.id]);
          openEditor(rule);
          renderLibrary();      // the card has to show that it is the one being edited
          return;
        }
      } else {
        // Nothing to select on a series without a rule: opening it *is* adding it.
        selected = new Set();
        openEditor(null, series);
        return;
      }
      renderLibrary();
      renderDetails();
    });

    card.append(posterNode(series, 'tvr-poster tvr-poster-row'));
    const main = el('div', { className: 'tvr-rule-main' });

    const head = el('div', { className: 'tvr-rule-head' });
    if (rule) {
      const tick = el('input', { type: 'checkbox', className: 'tvr-pick', checked: selected.has(rule.id) });
      tick.addEventListener('change', () => {
        if (tick.checked) selected.add(rule.id); else selected.delete(rule.id);
        editing = null;
        renderLibrary();
        renderDetails();
      });
      head.append(tick, alertBadge(rule));
    }
    head.append(el('span', { className: `tvr-connected ${rule ? 'yes' : 'no'}`,
                             title: rule ? 'Has a retention rule' : 'No rule yet' },
                   [el('i', { className: `fa fa-${rule ? 'check-circle' : 'circle-o'}` })]));
    head.append(el('span', { className: 'tvr-rule-title', textContent: series.title }));
    const link = rule ? sonarrLink(rule) : null;
    if (link) head.append(link);
    if (rule) head.append(enableToggle(rule));
    main.append(head);

    const facts = [series.year, series.network,
                   series.season_count ? plural(series.season_count, 'season') : '',
                   series.total_episode_count ? `${series.episode_file_count}/${series.total_episode_count} episodes` : '',
                   series.size_on_disk ? bytes(series.size_on_disk) : '',
                   series.ended ? 'ended' : (series.next_airing ? `next ${when(series.next_airing)}` : '')]
      .filter(Boolean);
    main.append(el('div', { className: 'tvr-card-series-facts', textContent: facts.join(' · ') }));

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
  applyLayout();
  ['tvr-hide-ended', 'tvr-only-alerts', 'tvr-search', 'tvr-sort'].forEach((id) => {
    $(id).addEventListener('input', renderLibrary);
    $(id).addEventListener('change', renderLibrary);
  });
  $('tvr-select-shown').addEventListener('click', () => {
    visibleLibrary().forEach((row) => { if (row.rule) selected.add(row.rule.id); });
    editing = null;
    renderLibrary();
    renderDetails();
  });
  $('tvr-select-none').addEventListener('click', () => {
    selected = new Set();
    editing = null;
    renderLibrary();
    renderDetails();
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
    const card = el('div', { className: `tvr-alert-card ${severity}` });
    const head = el('div', { className: 'tvr-alert-card-head' }, [
      el('span', { className: 'tvr-rule-title', textContent: rule.series_title || rule.path }),
      el('span', { className: 'tvr-alert-count', textContent: plural(list.length, 'issue') }),
    ]);
    if (blocked) head.append(el('span', { className: 'tvr-tag blocking', textContent: 'blocked' }));
    const state = monitoring[rule.id];
    head.append(el('span', { className: 'tvr-alert-age',
                             textContent: state ? `Sonarr read ${ago(state.read_at || state.checked_at)}` : 'not checked' }));
    card.append(head);
    list.forEach((alert) => card.append(alertItem(alert)));
    const foot = el('div', { className: 'tvr-alert-foot' });
    if (!options.hideOpen) {
      const open = el('button', { type: 'button', className: 'tvr-small', textContent: 'Show in Series' });
      open.addEventListener('click', () => {
        document.querySelector('.tvr-tabs button[data-tab="series"]').click();
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
        document.querySelector('.tvr-tabs button[data-tab="settings"]').click();
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

  $('tvr-series-rollup').addEventListener('click', () => {
    $('tvr-only-alerts').checked = true;
    $('tvr-search').value = '';
    renderLibrary();
  });

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

    return {
      title: existing ? 'Edit series' : 'Add series',
      rule,
      series,
      existing: !!existing,
      build: (body) => {
      // The navigator already said which series this is, so there is no picker and no
      // instance to choose: the series carries its own. A rule *is* its binding to one
      // series, so pointing it at another is delete and add, not an edit.
      const facts = [series.year, series.network, series.certification,
                     series.season_count ? plural(series.season_count, 'season') : '',
                     series.total_episode_count
                       ? `${series.episode_file_count}/${series.total_episode_count} episodes` : '',
                     series.size_on_disk ? bytes(series.size_on_disk) : '',
                     series.ended ? 'ended' : (series.next_airing ? `next ${when(series.next_airing)}` : '')]
        .filter(Boolean);
      const enabled = toggle(rule.enabled ? 'Enabled' : 'Disabled', rule.enabled, null, {});
      const identity = el('div', { className: 'tvr-identity' }, [
        posterNode(series, 'tvr-poster tvr-poster-panel'),
        el('div', { className: 'tvr-identity-body' }, [
          el('div', { className: 'tvr-identity-title', textContent: series.title || rule.series_title }),
          el('div', { className: 'tvr-details-facts', textContent: facts.join(' · ') }),
          el('div', { className: 'tvr-details-facts tvr-mono', textContent: series.path || rule.path || '' }),
          enabled.node,
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
      const globalMonitoring = ((settings.retention || {}).monitoring || 'unmonitor-only') === 'full-sync'
        ? 'Full sync' : 'Unmonitor only';
      const specials = options(el('select'), [['', `[Default] ${globalSpecials}`], ['no', 'Exclude specials'],
                                              ['yes', 'Include specials']],
        rule.include_specials === true ? 'yes' : (rule.include_specials === false ? 'no' : ''));
      const monitoring = options(el('select'), [['', `[Default] ${globalMonitoring}`],
                                                ['unmonitor-only', 'Unmonitor only'],
                                                ['full-sync', 'Full sync']],
                                 rule.monitoring || '');

      // Two one-time actions, not settings. They happen when you save and never again,
      // which is why each says so and says what it will ask Sonarr to do.
      const before = existing ? scopeOf(rule) : null;
      const monitorNew = toggle('Once, on save: monitor the episodes this newly covers',
                                false, null, { className: 'tvr-row-switch' });
      // What each action would actually touch, against the window as it stands in the
      // form. Without it the two toggles are a decision made blind.
      const monitorCount = el('div', { className: 'tvr-once-count' });
      const unmonitorCount = el('div', { className: 'tvr-once-count' });
      const scopeRow = el('div', { className: 'tvr-once' }, [
        el('div', { className: 'tvr-once-title', textContent: 'One-time actions' }),
        monitorNew.node,
        el('small', { textContent: 'Asks Sonarr to download any of them it does not have. '
                                   + 'Leave this off and nothing is fetched — the window simply '
                                   + 'covers them from now on.' }),
        monitorCount,
      ]);
      // Not a toggle. A run unmonitors everything outside the window whatever anyone
      // chooses, so offering the choice here only decided whether it happened now or
      // within a day — and off by default meant Sonarr spent that day fetching episodes
      // the next run would delete. It is stated instead, because it still happens.
      const unmonitorNote = el('div', { className: 'tvr-once' }, [
        el('div', { className: 'tvr-once-title', textContent: 'On save' }),
        unmonitorCount,
      ]);

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
          if (!counts.known) {
            monitorCount.textContent = unmonitorCount.textContent = '';
            return;
          }
          monitorCount.textContent = counts.would_monitor
            ? `${counts.would_monitor}/${counts.in_scope} episodes in ${title}'s keep scope are currently not monitored`
            : `Nothing to monitor — every episode in ${title}'s keep scope is already monitored`;
          // A widened rule only offers the part the widening added; the rest were
          // unmonitored by hand, and this pass leaves those alone.
          const byHand = counts.in_scope_unmonitored - counts.would_monitor;
          if (byHand > 0) {
            monitorCount.textContent += ` (${byHand} more were unmonitored by hand and are left alone)`;
          }
          unmonitorCount.textContent = counts.out_scope_monitored
            ? `${counts.out_scope_monitored}/${counts.out_scope} episodes out of ${title}'s keep scope are still `
              + 'monitored, and will be unmonitored so Sonarr stops fetching them'
            : `Nothing outside ${title}'s keep scope is monitored`;
        }), 250);
      };
      const draftScope = () => ({
        profile_id: presetSelect.value || '',
        keep_days: presetSelect.value ? null : (conditions.days.value || null),
        keep_episodes: presetSelect.value ? null : (conditions.episodes.value || null),
        keep_seasons: presetSelect.value ? null : (conditions.seasons.value || null),
        combine: conditions.combine.value,
      });
      const updateScopeRow = () => {
        const mode = monitoring.value || (settings.retention || {}).monitoring || 'unmonitor-only';
        const widens = !existing || widensScope(before, draftScope());
        // Full sync monitors the window continuously, so a one-time pass over it is a
        // thing already happening; and a window that has not grown has covered nothing new.
        scopeRow.hidden = mode === 'full-sync' || !widens;
        if (scopeRow.hidden) monitorNew.input.checked = false;
      };
      [monitoring, presetSelect, conditions.days, conditions.episodes, conditions.seasons,
       conditions.combine, specials].forEach((input) => {
        input.addEventListener('change', () => { updateScopeRow(); refreshCounts(); });
        input.addEventListener('input', refreshCounts);
      });
      setTimeout(() => { updateScopeRow(); refreshCounts(); }, 0);

      body.append(identity);
      // What the next run would do to this series, above the settings that decide it.
      if (existing) {
        const state = monitoring[rule.id] || {};
        const plan = state.plan;
        const alertsHere = seriesAlerts(rule.id);
        if (alertsHere.length) body.append(seriesAlertCard(rule, alertsHere, { hideOpen: true }));
        const box = el('div', { className: 'tvr-panel-plan' });
        box.append(el('div', { className: 'tvr-once-title', textContent: 'Next run' }));
        if (plan && (plan.delete || plan.monitor || plan.unmonitor)) {
          box.append(changeLines(plan, (kind) => guarded('', async () => {
            const data = await api('preview', { rule_ids: [rule.id] }, 'Working out what would change…');
            changeList(data.result, `${rule.series_title}: scheduled changes`, kind);
          })));
        } else {
          box.append(el('div', { className: 'tvr-plan-quiet',
                                 textContent: plan ? 'Nothing scheduled.' : 'Not read yet.' }));
        }
        const reread = el('button', { type: 'button', className: 'tvr-small',
                                      textContent: 'Re-read from Sonarr' });
        reread.addEventListener('click', () => queueChecks([rule.id], true));
        box.append(reread);
        body.append(box);
      }
      body.append(
        field('Retention', presetSelect, (settings.profiles || []).length
          ? 'Presets are managed under Media management.' : 'No presets yet — create one to reuse values.'),
        conditions.node,
        field('Season 0 / specials', specials),
        field('Monitoring', monitoring, 'Unmonitor only never asks Sonarr to fetch anything.'),
        scopeRow, unmonitorNote);
      return { presetSelect, conditions, specials, monitoring, monitorNew,
               before, enabled,
               // A rule needs somewhere to keep from: a preset, or at least one value.
               valid: () => !!(presetSelect.value || conditions.days.value
                               || conditions.episodes.value || conditions.seasons.value),
               state: () => JSON.stringify({ profile_id: presetSelect.value,
                                        keep_days: conditions.days.value,
                                        keep_episodes: conditions.episodes.value,
                                        keep_seasons: conditions.seasons.value,
                                        combine: conditions.combine.value,
                                        include_specials: specials.value,
                                        monitoring: monitoring.value,
                                        enabled: enabled.input.checked,
                                        once: monitorNew.input.checked }) };
    },
      save: async (context) => {
      const draft = {
        id: rule.id || undefined,
        enabled: context.enabled.input.checked,
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
        const pass = await api('scope-pass', {
          rule_id: saved.id,
          monitor_new: context.monitorNew.input.checked,
          // Always: a run does this regardless, so leaving it until then only gives Sonarr
          // a day to fetch episodes that run would delete.
          unmonitor_outside: true,
          previous_scope: context.before,
        }, 'Setting monitoring in Sonarr…');
        const parts = [];
        if (pass.monitored) parts.push(`${plural(pass.monitored, 'episode')} monitored`);
        if (pass.unmonitored) parts.push(`${plural(pass.unmonitored, 'episode')} unmonitored`);
        done = parts.length ? ` ${parts.join(', ')} in Sonarr.` : ' Nothing needed changing in Sonarr.';
      }
      if (saved) queueChecks([saved.id]);
      notice(`Series saved.${done}`, 'ok');
      selected = saved ? new Set([saved.id]) : new Set();
      renderDetails();
    },
    };
  }

  // -- the details pane --------------------------------------------------
  // Editing happens here rather than in a dialog: the list stays visible beside it, so
  // what you are changing is never the only thing on screen.
  let selected = new Set();
  let editing = null;          // the form currently open in the pane, if any

  function openEditor(existing, preselect) {
    const form = ruleForm(existing, preselect);
    if (!form) return;
    editing = form;
    if (currentView !== 'series-list') showView('series-list');
    renderDetails();
    $('tvr-details').scrollIntoView({ block: 'nearest' });
  }

  function renderDetails() {
    const pane = $('tvr-details');
    const shell = $('tvr-series-shell');
    const chosen = [...selected].map((id) => (settings.rules || []).find((rule) => rule.id === id))
      .filter(Boolean);
    const open = !!editing || chosen.length > 0;
    shell.classList.toggle('open', open);
    pane.hidden = !open;
    if (!open) return;
    pane.replaceChildren();

    const head = el('div', { className: 'tvr-details-head' });
    const close = el('button', { type: 'button', className: 'tvr-icon-button', title: 'Close' },
                    [el('i', { className: 'fa fa-times' })]);
    close.addEventListener('click', () => { editing = null; selected = new Set(); renderRules(); renderDetails(); });

    if (editing) {
      head.append(el('h3', { textContent: editing.title }), el('span', { className: 'tvr-spacer' }), close);
      pane.append(head);
      const body = el('div', { className: 'tvr-details-body' });
      pane.append(body);
      const context = editing.build(body);
      const actions = el('div', { className: 'tvr-actions' });
      const primary = el('button', { type: 'button', className: 'tvr-primary',
                                     textContent: editing.existing ? 'Update' : 'Add series' });
      // Adding needs a keep window to be worth anything; updating needs something to have
      // changed as well, so the button says whether pressing it would do something.
      const settled = context.state();
      const check = () => {
        const valid = context.valid();
        primary.disabled = !valid || (editing.existing && context.state() === settled);
        primary.title = !valid ? 'Set a preset, or at least one keep value'
          : (primary.disabled ? 'Nothing has changed' : '');
      };
      body.addEventListener('input', check);
      body.addEventListener('change', check);
      primary.addEventListener('click', () => guarded('', async () => {
        await editing.save(context);
        editing = null;
        renderDetails();
      }));
      const cancel = el('button', { type: 'button', className: 'tvr-secondary', textContent: 'Cancel' });
      cancel.addEventListener('click', () => { editing = null; renderDetails(); });
      actions.append(primary, cancel);
      if (editing.existing) {
        const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small', textContent: 'Delete…' });
        remove.addEventListener('click', () => deleteSeries(editing.rule));
        actions.append(el('span', { className: 'tvr-spacer' }), remove);
      }
      pane.append(actions);
      check();
      return;
    }

    // One selected and nothing open means the editor was closed: show it again rather than
    // a read-only copy of the same facts.
    if (chosen.length === 1) { openEditor(chosen[0]); return; }
    renderMassEdit(pane, head, close, chosen);
  }

  // Several at once. Only the fields where "the same for all of them" is a sensible thing
  // to say: a keep window, a preset, the monitoring mode, specials, enabled.
  function renderMassEdit(pane, head, close, rules) {
    head.append(el('h3', { textContent: `${plural(rules.length, 'series')} selected` }),
                el('span', { className: 'tvr-spacer' }), close);
    pane.append(head);
    const body = el('div', { className: 'tvr-details-body' });
    body.append(el('p', { className: 'tvr-lede',
                          textContent: 'Leave a field on “Unchanged” and it is left alone on every '
                                       + 'selected series. Nothing is written until you apply.' }));
    body.append(el('div', { className: 'tvr-details-facts',
                            textContent: rules.map((rule) => rule.series_title || rule.path).join(', ') }));

    const presetSelect = options(el('select'),
      [['', 'Unchanged'], ['custom', 'Custom — clear the preset']].concat(
        (settings.profiles || []).map((preset) => [preset.id, preset.name])), '');
    const monitoringSelect = options(el('select'), [['', 'Unchanged'], ['inherit', 'Use the global setting'],
                                                    ['unmonitor-only', 'Unmonitor only'],
                                                    ['full-sync', 'Full sync']], '');
    const specialsSelect = options(el('select'), [['', 'Unchanged'], ['inherit', 'Use the global setting'],
                                                  ['no', 'Exclude specials'], ['yes', 'Include specials']], '');
    const enabledSelect = options(el('select'), [['', 'Unchanged'], ['yes', 'Enabled'], ['no', 'Disabled']], '');
    const conditions = conditionFields({ keep_days: '', keep_episodes: '', keep_seasons: '', combine: '' });
    conditions.combine.prepend(el('option', { value: '', textContent: 'Unchanged' }));
    conditions.combine.value = '';

    body.append(field('Preset', presetSelect), conditions.node,
                el('small', { textContent: 'A blank keep value is left alone; set one to apply it to all.' }),
                field('Monitoring', monitoringSelect), field('Season 0 / specials', specialsSelect),
                field('State', enabledSelect));
    pane.append(body);

    const actions = el('div', { className: 'tvr-actions' });
    const apply = el('button', { type: 'button', className: 'tvr-primary',
                                 textContent: `Apply to ${plural(rules.length, 'series')}` });
    apply.addEventListener('click', () => guarded('', async () => {
      const ids = new Set(rules.map((rule) => rule.id));
      settings.rules = (settings.rules || []).map((rule) => {
        if (!ids.has(rule.id)) return rule;
        const draft = Object.assign({}, rule);
        if (presetSelect.value) draft.profile_id = presetSelect.value === 'custom' ? '' : presetSelect.value;
        if (monitoringSelect.value) draft.monitoring = monitoringSelect.value === 'inherit' ? '' : monitoringSelect.value;
        if (specialsSelect.value) {
          draft.include_specials = specialsSelect.value === 'inherit' ? null : specialsSelect.value === 'yes';
        }
        if (enabledSelect.value) draft.enabled = enabledSelect.value === 'yes';
        ['days', 'episodes', 'seasons'].forEach((name) => {
          const value = conditions[name].value;
          if (value) draft[`keep_${name}`] = value;
        });
        if (conditions.combine.value) draft.combine = conditions.combine.value;
        return draft;
      });
      await saveSettings(`${plural(rules.length, 'series')} updated.`);
      queueChecks([...ids]);
      renderDetails();
    }));
    actions.append(apply);
    pane.append(actions);
  }


  // A rule's keep window, as the one-time pass needs to remember it.
  function scopeOf(rule) {
    return { keep_days: rule.keep_days ?? null, keep_episodes: rule.keep_episodes ?? null,
             keep_seasons: rule.keep_seasons ?? null, combine: rule.combine || 'earliest',
             profile_id: rule.profile_id || '' };
  }

  // Whether a save can only have grown the window. A preset changing either way is treated
  // as widening, because the preset's values are not in front of us to compare.
  function widensScope(before, after) {
    if (!before) return true;
    if (before.profile_id !== after.profile_id || after.profile_id) return true;
    const grew = (was, now) => (was == null ? now != null : (now != null && Number(now) > Number(was)));
    const shrank = (was, now) => (was != null && (now == null || Number(now) < Number(was)));
    const keys = ['keep_days', 'keep_episodes', 'keep_seasons'];
    if (keys.some((key) => shrank(before[key], after[key]))) return keys.some((key) => grew(before[key], after[key]));
    return keys.some((key) => grew(before[key], after[key])) || before.combine !== after.combine;
  }

  // -- add series --------------------------------------------------------
  // Everything Sonarr holds that has no rule yet, from the stored reading. A search rather
  // than a wall: three thousand cards is not a list anyone reads.
  const ADD_LIMIT = 60;


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
  const queuedFixes = (rule) => ((rule.queue || {}).fixes || []).map((entry) => entry.kind);

  // A queued series states its intent on the card and can be taken back until a run
  // applies it. Nothing has happened yet, so the card says exactly what will.
  function queuedCard(rule) {
    const queued = queuedRemoval(rule);
    const card = el('div', { className: 'tvr-rule queued' });
    card.append(el('div', { className: 'tvr-rule-head' }, [
      el('span', { className: 'tvr-queued-mark', textContent: '×' }),
      el('span', { className: 'tvr-rule-title', textContent: rule.series_title || rule.path }),
      el('span', { className: 'tvr-alert-age', textContent: `queued ${ago(queued.created_at)}` }),
    ]));
    const lines = el('div', { className: 'tvr-queued-lines' });
    lines.append(el('div', { textContent: 'Queued for removal from TV Retention at the next run.' }));
    lines.append(el('div', { textContent: REMOVAL_SONARR[queued.action] || '' }));
    lines.append(el('div', { className: REMOVAL_FILES[queued.action] ? 'tvr-queued-danger' : '',
                             textContent: REMOVAL_FILES[queued.action] || 'No files will be removed' }));
    card.append(lines);
    const undo = el('button', { type: 'button', className: 'tvr-action', textContent: 'Undo' });
    undo.addEventListener('click', () => guarded('', async () => {
      const target = (settings.rules || []).find((other) => other.id === rule.id);
      target.queue = Object.assign({}, target.queue, { removal: null });
      await saveSettings('Removal cancelled.');
    }));
    card.append(el('div', { className: 'tvr-alert-foot' }, [undo]));
    return card;
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
      review();
      return { action, confirm };
    }, async (context) => {
      const word = REMOVAL_CONFIRM[context.action.value];
      if (word && context.confirm.value.trim() !== word) {
        throw new Error(`Type ${word} to confirm. Nothing was queued.`);
      }
      const target = (settings.rules || []).find((other) => other.id === rule.id);
      target.queue = Object.assign({}, target.queue,
                                   { removal: { action: context.action.value, created_at: new Date().toISOString() } });
      await saveSettings('Queued. It will be applied at the next run, and can be undone until then.');
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
    ['ended-expired', 'A series has ended with nothing left in its window'],
  ];
  const mutedInputs = {};

  function renderSettings() {
    const retention = settings.retention || {};
    $('tvr-include-specials').checked = !!retention.include_specials;
    $('tvr-estimated-dates').checked = retention.allow_estimated_dates !== false;
    $('tvr-search-after').checked = !!retention.search_after_monitor;
    $('tvr-monitoring').value = retention.monitoring || 'unmonitor-only';
    const describeMonitoring = () => {
      $('tvr-monitoring-help').textContent = $('tvr-monitoring').value === 'full-sync'
        ? 'Episodes inside the keep window are set to monitored, including ones with no file — '
          + 'which asks Sonarr to download them. On a large library that can be hundreds of episodes.'
        : 'Nothing is ever set to monitored. Widening a series’ keep window will not start '
          + 'downloads for the seasons it now covers; the series editor offers a one-time pass for that.';
    };
    $('tvr-monitoring').onchange = describeMonitoring;
    describeMonitoring();
    $('tvr-tmdb-key').value = (settings.tmdb || {}).api_key || '';
    $('tvr-state-dir').value = settings.state_dir || '';
    $('tvr-history-size').value = settings.log_retention_runs;
    $('tvr-log-level').value = (settings.logging || {}).level || 'info';
    $('tvr-ttl-hours').value = String((settings.health || {}).ttl_hours || 24);

    const box = $('tvr-notifications');
    box.replaceChildren();
    NOTIFICATIONS.forEach(([name, label]) => {
      const control = toggle(label, (settings.notifications || {})[name], null, { className: 'tvr-notify-row' });
      notifyInputs[name] = control.input;
      box.append(control.node);
    });
  }

  function collectSettings() {
    const notifications = {};
    NOTIFICATIONS.forEach(([name]) => { notifications[name] = notifyInputs[name].checked; });
    return Object.assign({}, settings, {
      retention: {
        include_specials: $('tvr-include-specials').checked,
        allow_estimated_dates: $('tvr-estimated-dates').checked,
        search_after_monitor: $('tvr-search-after').checked,
        monitoring: $('tvr-monitoring').value,
      },
      tmdb: { api_key: $('tvr-tmdb-key').value },
      state_dir: $('tvr-state-dir').value.trim(),
      log_retention_runs: $('tvr-history-size').value,
      logging: Object.assign({}, settings.logging, { level: $('tvr-log-level').value }),
      alerts: {
        header: $('tvr-alert-header').value,
        acknowledge: $('tvr-alert-ack').checked,
        test_banner: $('tvr-test-banner-mode').value,
        muted: MUTABLE_KINDS.map(([kind]) => kind).filter((kind) => mutedInputs[kind] && mutedInputs[kind].checked),
      },
      health: Object.assign({}, settings.health, { ttl_hours: $('tvr-ttl-hours').value }),
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
  document.querySelectorAll('.tvr-view[id^="tvr-view-settings"]').forEach((view) => {
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
      ['Test Mode', snapshot.test_mode ? 'on — a scheduled run changes nothing' : 'off'],
      ['Storage', settings.state_dir || ''],
    ];
    box.replaceChildren(...rows.map(([name, value]) => el('div', { className: 'tvr-inline-row' }, [
      el('span', { className: 'tvr-inline-label', textContent: name }),
      el('span', { textContent: String(value) }),
    ])));
  }
  $('tvr-browse-state').addEventListener('click', () => {
    browseFolder($('tvr-state-dir').value || '/mnt/user/appdata', (picked) => { $('tvr-state-dir').value = picked; });
  });
  $('tvr-tmdb-test').addEventListener('click', () => guarded('', async () => {
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
