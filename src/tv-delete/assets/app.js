/* TV Delete web UI.
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

  const root = document.getElementById('tv-delete');
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
    $('tvd-busy').hidden = busyDepth === 0;
    if (on && label) $('tvd-busy-text').textContent = label;
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
        throw new Error(`Could not reach the TV Delete backend (${error.message}). Reload the page.`);
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
    const box = $('tvd-notice');
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
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
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
    const caption = el('span', { className: 'tvd-switch-text', textContent: label });
    const node = el('label', { className: `tvd-switch${config.className ? ' ' + config.className : ''}`,
                               title: config.title || '' },
                    [input, el('span', { className: 'tvd-slider' }), caption]);
    if (onChange) input.addEventListener('change', () => onChange(input.checked, input, caption));
    return { node, input, caption };
  }

  // A "?" the reader can ask, rather than a paragraph under every row shouting at once.
  function hint(explanation) {
    const mark = el('button', { type: 'button', className: 'tvd-hint', textContent: '?',
                                title: explanation, 'aria-label': explanation });
    mark.addEventListener('click', (event) => { event.preventDefault(); window.alert(explanation); });
    return mark;
  }

  function field(label, control, note) {
    const wrapper = el('label', { className: 'tvd-field' }, [el('span', { textContent: label }), control]);
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
    const box = $('tvd-dialog');
    const body = $('tvd-dialog-body');
    body.replaceChildren(el('h3', { textContent: title }));
    $('tvd-dialog-extra').replaceChildren();
    $('tvd-dialog-ok').disabled = false;
    const context = buildBody(body);
    $('tvd-dialog-ok').textContent = okLabel || 'Save';
    $('tvd-dialog-ok').hidden = !onOk;
    const handler = async () => {
      box.removeEventListener('close', handler);
      if (box.returnValue !== 'ok' || !onOk) return;
      await guarded('', () => onOk(context));
    };
    box.addEventListener('close', handler);
    box.showModal();
  }

  // -- searchable picker -------------------------------------------------
  // A plain select is unusable at three thousand entries: it cannot be typed into beyond
  // the first letters and gives no reason why an option is unavailable.
  function comboBox(items, config) {
    const input = el('input', { type: 'text', placeholder: config.placeholder || 'Type to search…',
                                autocomplete: 'off', spellcheck: false, role: 'combobox' });
    const list = el('div', { className: 'tvd-combo-list', role: 'listbox', hidden: true });
    const holder = el('div', { className: 'tvd-combo' }, [input, list]);
    let chosen = null;

    const matches = (term) => {
      const needle = term.trim().toLowerCase();
      const scored = [];
      for (const item of items) {
        const label = config.label(item).toLowerCase();
        if (!needle) scored.push([2, item]);
        else if (label.startsWith(needle)) scored.push([0, item]);
        else if (label.includes(needle)) scored.push([1, item]);
        if (scored.length > 600) break;
      }
      scored.sort((a, b) => a[0] - b[0]);
      return scored.slice(0, config.limit || 40).map((pair) => pair[1]);
    };

    const render = () => {
      const rows = matches(input.value);
      list.replaceChildren();
      if (!rows.length) list.append(el('div', { className: 'tvd-combo-empty', textContent: 'No matches.' }));
      rows.forEach((item) => {
        const usable = config.usable ? config.usable(item) : true;
        const row = el('button', { type: 'button', role: 'option', disabled: !usable });
        row.append(el('span', { textContent: config.label(item) }));
        const note = config.note ? config.note(item) : '';
        if (note) row.append(el('small', { className: usable ? 'tvd-combo-note' : 'tvd-combo-blocked', textContent: note }));
        row.addEventListener('click', (event) => {
          event.preventDefault();
          event.stopPropagation();
          chosen = item;
          input.value = config.label(item);
          list.hidden = true;
          if (config.onPick) config.onPick(item);
        });
        list.append(row);
      });
      list.hidden = false;
    };

    input.addEventListener('input', () => { chosen = null; render(); });
    input.addEventListener('focus', render);
    input.addEventListener('click', (event) => { event.stopPropagation(); render(); });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { list.hidden = true; return; }
      if (event.key === 'ArrowDown') {
        const first = list.querySelector('button:not([disabled])');
        if (first) { event.preventDefault(); first.focus(); }
      }
    });
    list.addEventListener('keydown', (event) => {
      const buttons = [...list.querySelectorAll('button:not([disabled])')];
      const index = buttons.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' && index < buttons.length - 1) { event.preventDefault(); buttons[index + 1].focus(); }
      if (event.key === 'ArrowUp') { event.preventDefault(); (index > 0 ? buttons[index - 1] : input).focus(); }
      if (event.key === 'Escape') { list.hidden = true; input.focus(); }
    });
    document.addEventListener('click', () => { list.hidden = true; });

    return { node: holder, get value() { return chosen; },
             set(item) { chosen = item; input.value = item ? config.label(item) : ''; } };
  }

  // -- tabs --------------------------------------------------------------
  const TABS = ['series', 'alerts', 'presets', 'schedule', 'settings', 'history', 'log', 'help'];
  document.querySelectorAll('.tvd-tabs button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.tvd-tabs button').forEach((other) => other.classList.toggle('active', other === button));
      TABS.forEach((name) => { $(`tvd-panel-${name}`).hidden = name !== button.dataset.tab; });
      if (button.dataset.tab === 'log') startLog();
      else stopLog();
    });
  });

  function browseFolder(startPath, onPick) {
    let current = startPath || '/mnt/user';
    dialog('Choose a folder', (body) => {
      const crumb = el('div', { className: 'tvd-mono' });
      const list = el('div', { className: 'tvd-rules' });
      const use = el('button', { type: 'button', className: 'tvd-primary', textContent: 'Use this folder' });
      use.addEventListener('click', (event) => { event.preventDefault(); $('tvd-dialog').close('cancel'); onPick(current); });
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
          const button = el('button', { type: 'button', textContent: entry.name, className: 'tvd-folder' });
          button.addEventListener('click', (event) => { event.preventDefault(); load(entry.path); });
          list.append(button);
        });
        if (!data.entries.length) list.append(el('p', { className: 'tvd-empty', textContent: 'No sub-folders here.' }));
      });
      load(current);
      return {};
    }, null, 'Close');
  }

  // -- snapshot and background checking ----------------------------------
  const checking = new Set();
  let checkQueue = [];
  let checkRunning = false;
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
  const isBlocked = (ruleId) => seriesAlerts(ruleId).some((alert) => alert.blocking);
  const worstSeverity = (list) => (list.some((a) => a.severity === 'error') ? 'error'
    : list.some((a) => a.severity === 'warning') ? 'warning'
    : list.length ? 'notice' : '');

  function queueChecks(ruleIds) {
    const wanted = (ruleIds || []).filter((id) => !checkQueue.includes(id) && !checking.has(id));
    if (!wanted.length) return;
    checkQueue = checkQueue.concat(wanted);
    wanted.forEach((id) => checking.add(id));
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
          const data = await api('check-rule', { rule_id: ruleId }, '', true);
          if (data.busy) {
            checkQueue.forEach((id) => checking.delete(id));
            checkQueue = [];
            checking.delete(ruleId);
            startPolling();
            break;
          }
          monitoring[data.rule_id] = data.state;
          const fresh = await api('alerts', {}, '', true);
          applyAlerts(fresh.alerts);
        } catch (error) {
          monitoring[ruleId] = Object.assign({}, monitoring[ruleId], {
            ok: false, label: 'Check failed', error: error.message, checked_at: new Date().toISOString(),
          });
        } finally {
          checking.delete(ruleId);
          renderRules();
          renderAlerts();
          renderStats();
        }
      }
    } finally {
      checkRunning = false;
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

  const CHECK_PHASE = { instances: 'verifying the Sonarr instances',
                        matching: 'matching series to Sonarr', rules: 'reading series' };

  function renderCheckBanner(progress) {
    const box = $('tvd-checking');
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
    box.append(el('span', { className: 'tvd-spinner' }), text(`${detail}. Series update as they finish.`));
  }

  function render() {
    $('tvd-version').textContent = snapshot.version || '';
    $('tvd-array').hidden = !!snapshot.array_ready;
    $('tvd-test-banner').hidden = !snapshot.test_mode;
    renderStats();
    renderPlanHeader();
    renderRules();
    renderAlerts();
    renderPresets();
    renderInstances();
    renderSchedule();
    renderSettings();
    renderHistory();
  }

  // Each line opens exactly what it names. The total replaces the old button: a count you
  // can read is more use than a button that only promises one.
  const PLAN_KINDS = [
    ['remove', 'delete', (plan) => `${plural(plan.remove, 'series')} will be removed`],
    ['delete', 'delete', (plan) => `${plural(plan.delete, 'episode')} scheduled for deletion (${bytes(plan.delete_bytes)})`],
    ['monitor', 'monitor', (plan) => `${plural(plan.monitor, 'episode')} will be set to monitored`],
    ['unmonitor', 'unmonitor', (plan) => `${plural(plan.unmonitor, 'episode')} will be set to unmonitored`],
  ];

  function planLines(plan, onOpen, className) {
    const list = el('div', { className: `tvd-plan-list${className ? ' ' + className : ''}` });
    PLAN_KINDS.forEach(([key, tone, describe]) => {
      if (!plan[key]) return;
      const line = el('button', { type: 'button', className: `tvd-plan ${tone}`, textContent: describe(plan) });
      line.addEventListener('click', () => onOpen(key));
      list.append(line);
    });
    return list;
  }

  function renderPlanHeader() {
    const plan = snapshot.plan || { actionable: 0, trustworthy: false, unknown: 0 };
    const box = $('tvd-plan-header');
    box.replaceChildren();
    const quiet = plan.trustworthy && !plan.actionable;
    $('tvd-run').hidden = quiet;
    const label = $('tvd-uptodate');
    label.hidden = !quiet;
    if (quiet) {
      label.textContent = `Up to date, no changes scheduled${plan.oldest ? ` — checked ${ago(plan.oldest)}` : ''}`;
      return;
    }
    const open = (kind) => guarded('', async () => {
      const data = await api('preview', {}, 'Working out what would change…');
      changeList(data.result, 'Scheduled changes', kind);
    });
    const total = el('button', { type: 'button', className: 'tvd-plan-total',
                                 textContent: `${plural(plan.actionable, 'scheduled change')}` });
    total.addEventListener('click', () => open('all'));
    box.append(total, planLines(plan, open));
  }

  function renderStats() {
    const rules = settings.rules || [];
    $('tvd-stat-rules').textContent = rules.length;
    $('tvd-stat-rules-sub').textContent = `${rules.filter((r) => r.enabled).length} enabled`;
    const summary = snapshot.alert_summary || { total: 0, error: 0, blocking: 0 };
    $('tvd-stat-alerts').textContent = summary.total || 0;
    $('tvd-stat-alerts-sub').textContent = summary.blocking
      ? `${plural(summary.blocking, 'series')} blocked` : 'nothing blocking';
    const badge = $('tvd-tab-badge');
    badge.hidden = !summary.total;
    badge.textContent = summary.total || '';
    badge.className = `tvd-tab-badge ${summary.error ? 'error' : (summary.warning ? 'warning' : 'notice')}`;
    const last = (snapshot.runs || [])[0];
    $('tvd-stat-last').textContent = last ? when(last.started) : 'Never';
    $('tvd-stat-last-sub').textContent = last
      ? `${last.dry_run ? 'would delete' : 'deleted'} ${last.dry_run ? last.planned : last.deleted} files` : ' ';
    $('tvd-stat-schedule').textContent = (settings.schedule || {}).enabled ? 'On' : 'Off';
    $('tvd-stat-schedule-sub').textContent = snapshot.schedule_text || 'Manual runs only';
  }

  function changeList(result, title, kind) {
    const wanted = kind && kind !== 'all' ? kind : null;
    const shows = (key) => !wanted || wanted === key;
    dialog(title, (body) => {
      const rows = result.rules.filter((rule) =>
        (shows('delete') && rule.deleted.length)
        || (shows('unmonitor') && rule.unmonitored_frame)
        || (shows('monitor') && rule.monitored));
      const removals = wanted ? [] : (result.removals || []);
      if (!rows.length && !removals.length) {
        body.append(el('p', { textContent: 'Nothing would change.' }));
        return {};
      }
      const totals = { del: 0, bytes: 0, unmon: 0, mon: 0 };
      rows.forEach((rule) => {
        totals.del += rule.deleted.length;
        totals.bytes += rule.freed_bytes || 0;
        totals.unmon += rule.unmonitored_frame || 0;
        totals.mon += rule.monitored || 0;
      });
      const summary = el('div', { className: 'tvd-plan-list tvd-plan-summary' });
      if (removals.length) {
        summary.append(el('div', { className: 'tvd-plan delete',
                                   textContent: `${plural(removals.length, 'series')} will be removed` }));
      }
      if (totals.del) {
        summary.append(el('div', { className: 'tvd-plan delete',
                                   textContent: `${plural(totals.del, 'episode')} scheduled for deletion (${bytes(totals.bytes)})` }));
      }
      if (totals.mon) {
        summary.append(el('div', { className: 'tvd-plan monitor',
                                   textContent: `${plural(totals.mon, 'episode')} will be set to monitored` }));
      }
      if (totals.unmon) {
        summary.append(el('div', { className: 'tvd-plan unmonitor',
                                   textContent: `${plural(totals.unmon, 'episode')} will be set to unmonitored` }));
      }
      body.append(summary);

      removals.forEach((record) => {
        body.append(el('div', { className: 'tvd-change-series' }, [
          el('strong', { textContent: record.series_title }),
          el('div', { className: 'tvd-plan delete', textContent: record.label }),
        ]));
      });
      rows.forEach((rule) => {
        const card = el('div', { className: 'tvd-change-series' });
        card.append(el('strong', { textContent: rule.series_title }));
        const lines = el('div', { className: 'tvd-plan-list' });
        if (rule.deleted.length && shows('delete')) {
          lines.append(el('div', { className: 'tvd-plan delete',
                                   textContent: `${plural(rule.deleted.length, 'episode')} deleted (${bytes(rule.freed_bytes)})` }));
        }
        if (rule.monitored && shows('monitor')) {
          lines.append(el('div', { className: 'tvd-plan monitor',
                                   textContent: `${plural(rule.monitored, 'episode')} set to monitored` }));
        }
        if (rule.unmonitored_frame && shows('unmonitor')) {
          lines.append(el('div', { className: 'tvd-plan unmonitor',
                                   textContent: `${plural(rule.unmonitored_frame, 'episode')} set to unmonitored` }));
        }
        card.append(lines);
        if (rule.deleted.length && shows('delete')) {
          const detail = el('div', { className: 'tvd-alert-files', hidden: true });
          rule.deleted.slice(0, 200).forEach((item) => detail.append(el('div', {
            className: 'tvd-mono',
            textContent: `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')}`
              + ` — ${item.title || ''} · ${item.air_date || 'undated'} (${item.air_source}) · ${bytes(item.size)}`,
          })));
          const show = el('button', { type: 'button', className: 'tvd-link',
                                      textContent: `List ${plural(rule.deleted.length, 'episode')}` });
          show.addEventListener('click', () => { detail.hidden = !detail.hidden; });
          card.append(show, detail);
        }
        body.append(card);
      });
      return {};
    }, null, 'Close');
  }

  $('tvd-refresh-all').addEventListener('click', () => guarded('', async () => {
    const ids = (settings.rules || []).filter((rule) => rule.enabled).map((rule) => rule.id);
    if (!ids.length) throw new Error('There are no enabled series to check.');
    queueChecks(ids);
  }));

  $('tvd-run').addEventListener('click', () => guarded('', async () => {
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
    const mode = $('tvd-sort').value;
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
      className: `tvd-card-switch${rule.enabled ? ' on' : ''}`,
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
      return el('span', { className: 'tvd-dot-badge checking', title: 'Reading this series from Sonarr', textContent: '' });
    }
    if (!list.length) {
      const state = monitoring[rule.id];
      return el('span', {
        className: `tvd-dot-badge ${state ? 'clear' : 'unknown'}`,
        title: state ? `No problems — checked ${ago(state.checked_at)}` : 'Not checked yet',
        textContent: '',
      });
    }
    const severity = worstSeverity(list);
    const blocked = isBlocked(rule.id);
    const button = el('button', {
      type: 'button',
      className: `tvd-dot-badge ${severity}${blocked ? ' blocked' : ''}`,
      textContent: String(list.length),
      title: (blocked ? 'Blocked — skipped by every run.\n' : '')
             + list.map((alert) => `• ${alert.title}: ${alert.detail}`).join('\n'),
    });
    button.addEventListener('click', () => showSeriesAlerts(rule));
    return button;
  }

  function visibleRules() {
    const term = ($('tvd-search').value || '').toLowerCase();
    const filter = $('tvd-filter').value;
    return sortRules((settings.rules || []).filter((rule) => {
      if (term && !(`${rule.series_title} ${rule.path}`.toLowerCase().includes(term))) return false;
      if (filter === 'enabled') return rule.enabled;
      if (filter === 'disabled') return !rule.enabled;
      if (filter === 'attention') return seriesAlerts(rule.id).length > 0;
      if (filter === 'blocked') return isBlocked(rule.id);
      return true;
    }));
  }

  // Opens the series where it actually lives. Sonarr's own slug, so no URL is guessed.
  function sonarrLink(rule) {
    const instance = (settings.instances || []).find((i) => i.id === rule.instance_id);
    if (!instance || !rule.slug) return null;
    const link = el('a', { className: 'tvd-sonarr-link', target: '_blank', rel: 'noopener',
                           href: `${instance.url.replace(/\/+$/, '')}/series/${encodeURIComponent(rule.slug)}`,
                           title: `Open ${rule.series_title} in Sonarr` });
    link.append(el('i', { className: 'fa fa-external-link' }));
    return link;
  }

  function renderRules() {
    const container = $('tvd-rules');
    const rules = visibleRules();
    $('tvd-enable-all').textContent = `Enable shown (${rules.length})`;
    $('tvd-disable-all').textContent = `Disable shown (${rules.length})`;
    container.replaceChildren();
    $('tvd-rules-empty').hidden = (settings.rules || []).length > 0;
    rules.forEach((rule) => {
      // A queued series shows its intent instead of its retention: nothing about the
      // keep window matters once you have decided to stop managing it.
      if (queuedRemoval(rule)) { container.append(queuedCard(rule)); return; }
      const blocked = isBlocked(rule.id);
      const card = el('div', { className: `tvd-rule ${blocked ? 'blocked' : 'ok'}${rule.enabled ? '' : ' disabled'}` });
      const instance = (settings.instances || []).find((i) => i.id === rule.instance_id);
      const preset = presetFor(rule);
      const head = el('div', { className: 'tvd-rule-head' }, [
        alertBadge(rule),
        el('span', { className: 'tvd-rule-title', textContent: rule.series_title || '(unmatched)' }),
        el('span', { className: 'tvd-chip', textContent: instance ? instance.name : 'unknown instance' }),
      ]);
      // Retention sits with the identity, spaced away from the instance. A preset says all
      // of it in one pill; only a custom rule needs its numbers spelled out.
      const retention = el('span', { className: 'tvd-retention' });
      if (preset) {
        retention.append(el('span', { className: 'tvd-chip preset', textContent: preset.name }));
      } else {
        ruleSummary(rule).forEach((label) => retention.append(el('span', { className: 'tvd-chip', textContent: label })));
        retention.append(el('span', { className: 'tvd-chip', textContent: `combine: ${rule.combine}` }));
      }
      if (rule.include_specials === true) retention.append(el('span', { className: 'tvd-chip', textContent: 'specials in' }));
      if (rule.include_specials === false) retention.append(el('span', { className: 'tvd-chip', textContent: 'specials out' }));
      const link = sonarrLink(rule);
      if (link) head.insertBefore(link, head.children[2]);
      head.append(retention);
      head.append(enableToggle(rule));
      card.append(head);

      const body = el('div', { className: 'tvd-rule-body' });
      const state = monitoring[rule.id] || {};
      const plan = state.plan;
      const hasWork = plan && (plan.delete || plan.unmonitor || plan.monitor);
      const line = el('div', { className: 'tvd-plan-line' });
      const refresh = el('button', { type: 'button', className: 'tvd-icon-button',
                                     title: 'Re-read this series from Sonarr' },
                        [el('i', { className: 'fa fa-refresh' })]);
      refresh.addEventListener('click', () => queueChecks([rule.id]));
      line.append(refresh);
      if (isChecking(rule.id)) {
        line.append(el('span', { className: 'tvd-plan-quiet', textContent: 'Reading from Sonarr…' }));
      } else if (blocked) {
        line.append(el('span', { className: 'tvd-plan-quiet', textContent: 'Blocked — nothing will run for this series.' }));
      } else if (!plan) {
        line.append(el('span', { className: 'tvd-plan-quiet', textContent: 'Not checked yet.' }));
      } else if (plan.delete || plan.unmonitor || plan.monitor) {
        const open = (kind) => guarded('', async () => {
          const data = await api('preview', { rule_ids: [rule.id] }, 'Working out what would change…');
          changeList(data.result, `${rule.series_title}: scheduled changes`, kind);
        });
        line.append(planLines(plan, open));
        line.append(el('span', { className: 'tvd-plan-quiet', textContent: `checked ${ago(state.checked_at)}` }));
      } else {
        line.append(el('span', { className: 'tvd-plan-quiet',
                                 textContent: `Up to date, no changes scheduled — checked ${ago(state.checked_at)}` }));
      }
      body.append(line);

      const actions = el('div', { className: 'tvd-rule-actions' });
      const editButton = el('button', { type: 'button', className: 'tvd-small', textContent: 'Edit' });
      editButton.addEventListener('click', () => editRule(rule));
      actions.append(editButton);
      if (isChecking(rule.id)) {
        [...actions.children].forEach((button) => { button.disabled = true; });
        card.classList.add('tvd-busy-row');
      }
      body.append(actions);
      card.append(body);
      container.append(card);
    });
  }

  $('tvd-search').addEventListener('input', renderRules);
  $('tvd-filter').addEventListener('change', renderRules);
  $('tvd-sort').addEventListener('change', renderRules);

  // Acts on what the filter is showing, and says how many, because "all" on a filtered
  // list of nine out of thirty-six is not what anybody means.
  function setAllShown(wanted) {
    return guarded('', async () => {
      const shown = visibleRules();
      const changing = shown.filter((rule) => !!rule.enabled !== wanted);
      if (!changing.length) throw new Error(`Every series shown is already ${wanted ? 'enabled' : 'disabled'}.`);
      if (!window.confirm(`${wanted ? 'Enable' : 'Disable'} ${plural(changing.length, 'series')}?`)) return;
      changing.forEach((rule) => {
        const target = (settings.rules || []).find((other) => other.id === rule.id);
        if (target) target.enabled = wanted;
      });
      await saveSettings(`${plural(changing.length, 'series')} ${wanted ? 'enabled' : 'disabled'}.`);
    });
  }

  $('tvd-enable-all').addEventListener('click', () => setAllShown(true));
  $('tvd-disable-all').addEventListener('click', () => setAllShown(false));

  // -- alerts ------------------------------------------------------------
  // One card per series, not one per problem. The series is the thing you act on, so it
  // owns the card; each problem inside it is a short labelled line. Severity is carried by
  // the card frame and the badge, and nowhere else — a card tinted end to end says nothing
  // a coloured edge does not.
  const ALERT_TAG = {
    'unmatched': 'No Sonarr match',
    'folder-missing': 'Folder missing',
    'path-changed': 'Series moved',
    'monitored-outside-frame': 'Monitored outside window',
    'unmonitored-inside-frame': 'Unmonitored inside window',
    'unknown-files': 'Unknown files',
    'ended-expired': 'Series ended',
    'sonarr-unreachable': 'Sonarr unreachable',
    'run-aborted': 'Run stopped',
  };
  const ACTION_LABEL = {
    'monitor-in-frame': 'Monitor inside the window',
    'unmonitor-out-frame': 'Unmonitor outside the window',
    'rematch': 'Re-check against Sonarr',
    'accept-path': 'Accept the new folder',
    'remove-rule': 'Remove from TV Delete',
    'open-instance': 'Open Sonarr settings',
    'test-instance': 'Test the connection',
    'enable-recycle-bin': 'Give Sonarr a recycle bin',
  };
  let severityFilter = 'all';

  // A small mark before each problem, sized and coloured by rule. Geometric characters
  // rather than emoji: these have no colour-emoji presentation to fall back to, so they
  // render at the size the stylesheet asks for on every platform.
  function alertItem(alert) {
    const item = el('div', { className: 'tvd-alert-item' });
    const line = el('div', { className: 'tvd-alert-line' }, [
      el('span', { className: `tvd-sev ${alert.severity}`, title: alert.severity }),
      el('span', { className: 'tvd-alert-kind', textContent: ALERT_TAG[alert.kind] || alert.title }),
      el('span', { className: 'tvd-alert-detail', textContent: alert.detail }),
    ]);
    if (alert.blocking) line.append(el('span', { className: 'tvd-tag blocking', textContent: 'blocks runs' }));
    line.append(el('span', { className: 'tvd-alert-age', textContent: ago(alert.first_seen) }));
    item.append(line);
    // The explanation is the point of a notice — hiding it behind a link left one saying
    // nothing at all. It is short, so it stays on the page.
    item.append(el('p', { className: 'tvd-alert-help', textContent: alert.help }));

    const foot = el('div', { className: 'tvd-alert-foot' });
    if (alert.action) {
      const button = el('button', { type: 'button', className: 'tvd-action',
                                    textContent: `Quick action: ${ACTION_LABEL[alert.action] || 'Fix'}` });
      button.addEventListener('click', () => runAlertAction(alert));
      foot.append(button);
    }
    if ((alert.data || {}).files) {
      const files = el('div', { className: 'tvd-alert-files', hidden: true });
      ((alert.data || {}).files || []).slice(0, 10).forEach((path) =>
        files.append(el('div', { className: 'tvd-mono', textContent: path })));
      const show = el('button', { type: 'button', className: 'tvd-link', textContent: `List ${alert.count} file(s)` });
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
    const card = el('div', { className: `tvd-alert-card ${severity}` });
    const head = el('div', { className: 'tvd-alert-card-head' }, [
      el('span', { className: 'tvd-rule-title', textContent: rule.series_title || rule.path }),
      el('span', { className: 'tvd-alert-count', textContent: plural(list.length, 'issue') }),
    ]);
    if (blocked) head.append(el('span', { className: 'tvd-tag blocking', textContent: 'blocked' }));
    const state = monitoring[rule.id];
    head.append(el('span', { className: 'tvd-alert-age', textContent: state ? `checked ${ago(state.checked_at)}` : 'not checked' }));
    card.append(head);
    list.forEach((alert) => card.append(alertItem(alert)));
    const foot = el('div', { className: 'tvd-alert-foot' });
    if (!options.hideOpen) {
      const open = el('button', { type: 'button', className: 'tvd-small', textContent: 'Show in Series' });
      open.addEventListener('click', () => {
        document.querySelector('.tvd-tabs button[data-tab="series"]').click();
        $('tvd-search').value = rule.series_title || rule.path;
        renderRules();
      });
      foot.append(open);
    }
    const recheck = el('button', { type: 'button', className: 'tvd-small', textContent: 'Re-check now' });
    recheck.addEventListener('click', () => { $('tvd-dialog').close('cancel'); queueChecks([rule.id]); });
    foot.append(recheck);
    card.append(foot);
    return card;
  }

  function systemAlertCard(instanceName, list) {
    const severity = worstSeverity(list);
    const card = el('div', { className: `tvd-alert-card ${severity}` });
    card.append(el('div', { className: 'tvd-alert-card-head' }, [
      el('span', { className: 'tvd-rule-title', textContent: instanceName }),
      el('span', { className: 'tvd-alert-count', textContent: plural(list.length, 'issue') }),
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
            + 'Sonarr deletes — not only to TV Delete.' }));
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
        document.querySelector('.tvd-tabs button[data-tab="settings"]').click();
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
      body.append(el('div', { className: `tvd-status ${blocked ? 'bad' : 'ok'}` }, [
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
    const counts = { error: 0, warning: 0, notice: 0 };
    system.forEach((alert) => { counts[alert.severity] = (counts[alert.severity] || 0) + 1; });
    $('tvd-count-all').textContent = system.length;
    ['error', 'warning', 'notice'].forEach((severity) => {
      $(`tvd-count-${severity}`).textContent = counts[severity] || 0;
    });

    const rollup = $('tvd-alert-rollup');
    const seriesList = (snapshot.alerts || []).filter((alert) => alert.scope !== 'system');
    rollup.hidden = seriesList.length === 0;
    if (seriesList.length) {
      const affected = new Set(seriesList.map((alert) => alert.rule_id)).size;
      const bySeverity = { error: 0, warning: 0, notice: 0 };
      seriesList.forEach((alert) => { bySeverity[alert.severity] += 1; });
      rollup.replaceChildren(
        el('strong', { textContent: `${plural(seriesList.length, 'alert')} need to be addressed` }),
        el('span', { textContent: ` across ${plural(affected, 'series')} — ${bySeverity.error} critical, `
          + `${bySeverity.warning} warning, ${bySeverity.notice} notice. Open the Series tab to act on them.` }));
    }

    const matches = (alert) => severityFilter === 'all' || alert.severity === severityFilter;
    const systemBox = $('tvd-alerts-system');
    systemBox.replaceChildren();
    const shown = system.filter(matches);
    $('tvd-alerts-system-empty').hidden = shown.length > 0;
    const byInstance = new Map();
    shown.forEach((alert) => {
      const instance = (settings.instances || []).find((i) => i.id === alert.instance_id);
      const name = instance ? instance.name : 'TV Delete';
      byInstance.set(name, (byInstance.get(name) || []).concat([alert]));
    });
    byInstance.forEach((list, name) => systemBox.append(systemAlertCard(name, list)));
  }

  $('tvd-alert-rollup').addEventListener('click', () => {
    document.querySelector('.tvd-tabs button[data-tab="series"]').click();
    $('tvd-filter').value = 'attention';
    $('tvd-search').value = '';
    renderRules();
  });

  document.querySelectorAll('.tvd-seg button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.tvd-seg button').forEach((other) => other.classList.toggle('active', other === button));
      severityFilter = button.dataset.severity;
      renderAlerts();
    });
  });

  $('tvd-recheck-all').addEventListener('click', () => guarded('', async () => {
    const ids = (settings.rules || []).filter((rule) => rule.enabled).map((rule) => rule.id);
    if (!ids.length) throw new Error('There are no enabled series to check.');
    queueChecks(ids);
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
    const container = $('tvd-presets');
    const presets = settings.profiles || [];
    container.replaceChildren();
    $('tvd-presets-empty').hidden = presets.length > 0;
    presets.forEach((preset) => {
      const users = (settings.rules || []).filter((rule) => rule.profile_id === preset.id);
      const card = el('div', { className: 'tvd-rule ok' });
      card.append(el('div', { className: 'tvd-rule-head' }, [
        el('span', { className: 'tvd-rule-title', textContent: preset.name }),
        el('span', { className: 'tvd-chip', textContent: `used by ${plural(users.length, 'series')}` }),
      ]));
      const body = el('div', { className: 'tvd-rule-body' });
      presetSummary(preset).forEach((label) => body.append(el('span', { className: 'tvd-chip on', textContent: label })));
      body.append(el('span', { className: 'tvd-chip', textContent: `combine: ${preset.combine}` }));
      const actions = el('div', { className: 'tvd-rule-actions' });
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
      el('div', { className: 'tvd-row' }, [field('Keep days', days), field('Keep episodes', episodes),
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
        const remove = el('button', { type: 'button', className: 'tvd-danger tvd-small', textContent: 'Remove preset' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          guarded('', async () => {
            if (users.length) throw new Error(`${plural(users.length, 'series')} still use "${preset.name}".`);
            if (!window.confirm(`Remove the preset "${preset.name}"?`)) return;
            $('tvd-dialog').close('cancel');
            settings.profiles = settings.profiles.filter((other) => other.id !== preset.id);
            await saveSettings('Preset removed.');
          });
        });
        $('tvd-dialog-extra').replaceChildren(remove);
      }
      if (users.length) {
        body.append(el('p', { textContent: `${plural(users.length, 'series')} use this preset and will change with it:` }));
        users.forEach((rule) => body.append(el('div', { className: 'tvd-mono', textContent: rule.series_title || rule.path })));
        if ((settings.retention || {}).auto_monitor) {
          body.append(el('div', { className: 'tvd-banner', textContent:
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

  $('tvd-add-preset').addEventListener('click', () => editPreset(null));

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

  function editRule(existing) {
    const rule = Object.assign({
      id: '', enabled: true, instance_id: (settings.instances[0] || {}).id || '',
      series_id: null, series_title: '', tvdb_id: null, path: '',
      profile_id: existing ? '' : ((settings.profiles || [])[0] || {}).id || '',
      keep_days: '', keep_episodes: '', keep_seasons: '', combine: 'earliest',
      include_specials: null,
    }, existing || {});

    if (!settings.instances.length) {
      notice('Add a Sonarr instance first — every series must be bound to a Sonarr record.', 'bad');
      return;
    }

    dialog(existing ? 'Edit series' : 'Add series', (body) => {
      const instanceSelect = options(el('select'),
        settings.instances.map((instance) => [instance.id, instance.name]), rule.instance_id);
      const hint = el('small', { textContent: 'Loading series from Sonarr…' });
      let picker = null;
      const holder = el('div', {});
      const refreshButton = el('button', { type: 'button', textContent: 'Refresh list' });
      const seriesField = el('label', { className: 'tvd-field' },
        [el('span', { textContent: 'Series' }), holder, hint]);

      const load = (force) => guarded('', async () => {
        const catalogue = await seriesFor(instanceSelect.value, rule.id, force);
        picker = comboBox(catalogue, {
          placeholder: 'Type a few letters of the series name…',
          label: (entry) => `${entry.title}${entry.year ? ` (${entry.year})` : ''}`,
          usable: (entry) => entry.selectable,
          note: (entry) => (entry.selectable
            ? (entry.awaiting ? 'awaiting first episode — no folder yet' : entry.path)
            : entry.reason),
        });
        holder.replaceChildren(picker.node);
        const current = catalogue.find((entry) => String(entry.series_id) === String(rule.series_id));
        if (current) picker.set(current);
        const blocked = catalogue.filter((entry) => !entry.selectable).length;
        hint.textContent = blocked
          ? `${catalogue.length - blocked} of ${catalogue.length} series can be used; the rest show why not.`
          : `${catalogue.length} series available.`;
      });
      refreshButton.addEventListener('click', (event) => { event.preventDefault(); load(true); });
      instanceSelect.addEventListener('change', () => { seriesCache = {}; load(false); });
      load(false);

      const presetSelect = el('select');
      (settings.profiles || []).forEach((preset) => presetSelect.append(
        el('option', { value: preset.id, textContent: `${preset.name} — ${presetSummary(preset).join(', ')}` })));
      presetSelect.append(el('option', { value: '', textContent: 'Custom — values for this series only' }));
      presetSelect.value = rule.profile_id || '';
      const conditions = conditionFields(rule);
      const applyPreset = () => { conditions.node.hidden = !!presetSelect.value; };
      presetSelect.addEventListener('change', applyPreset);
      applyPreset();

      const specials = options(el('select'), [['', 'Use the global setting'], ['no', 'Exclude specials'],
                                              ['yes', 'Include specials']],
        rule.include_specials === true ? 'yes' : (rule.include_specials === false ? 'no' : ''));
      const monitorMissing = options(el('select'), [['no', 'Leave them unmonitored'],
                                                   ['yes', 'Set them to monitored']],
                                     rule.monitor_missing ? 'yes' : 'no');
      const enabled = toggle(rule.enabled ? 'Enabled' : 'Disabled', rule.enabled, null,
                             { className: 'tvd-card-switch' });

      body.append(
        el('div', { className: 'tvd-card-head' },
           [el('span', { className: 'tvd-card-title', textContent: existing ? 'Series' : 'New series' }),
            enabled.node]),
        field('Sonarr instance', instanceSelect),
        seriesField,
        el('div', { className: 'tvd-row' }, [refreshButton]),
        field('Retention', presetSelect, (settings.profiles || []).length
          ? 'Presets are managed on the Presets tab.' : 'No presets yet — create one to reuse values.'),
        conditions.node,
        el('div', { className: 'tvd-row' }, [
          field('Season 0 / specials', specials),
          field('Missing episodes inside the window', monitorMissing,
                'Monitoring these asks Sonarr to fetch them.'),
        ]),
      );
      if (existing) {
        // Bottom left, in the row with Cancel and Save, away from the primary action.
        const remove = el('button', { type: 'button', className: 'tvd-danger tvd-small',
                                      textContent: 'Delete…' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          $('tvd-dialog').close('cancel');
          deleteSeries(rule);
        });
        $('tvd-dialog-extra').replaceChildren(remove);
      }
      return { instanceSelect, getSeries: () => picker && picker.value, presetSelect, conditions,
               specials, monitorMissing, enabled };
    }, async (context) => {
      const chosen = context.getSeries();
      const draft = {
        id: rule.id || undefined,
        enabled: context.enabled.input.checked,
        instance_id: context.instanceSelect.value,
        profile_id: context.presetSelect.value || '',
        keep_days: context.presetSelect.value ? null : (context.conditions.days.value || null),
        keep_episodes: context.presetSelect.value ? null : (context.conditions.episodes.value || null),
        keep_seasons: context.presetSelect.value ? null : (context.conditions.seasons.value || null),
        combine: context.conditions.combine.value,
        include_specials: context.specials.value,
        monitor_missing: context.monitorMissing.value === 'yes',
      };
      if (chosen) {
        if (!chosen.selectable) throw new Error(`${chosen.title} cannot be used: ${chosen.reason}.`);
        Object.assign(draft, { series_id: chosen.series_id, series_title: chosen.title,
                               tvdb_id: chosen.tvdb_id, path: chosen.path });
      } else if (existing) {
        Object.assign(draft, { series_id: rule.series_id, series_title: rule.series_title,
                               tvdb_id: rule.tvdb_id, path: rule.path });
      } else {
        throw new Error('Choose a series from the list.');
      }
      settings.rules = (settings.rules || []).filter((other) => other.id !== rule.id).concat([draft]);
      await saveSettings(null);
      const matched = await api('match', {}, 'Matching against Sonarr…');
      settings = matched.settings;
      snapshot.settings = settings;
      render();
      const saved = settings.rules[settings.rules.length - 1];
      if (saved) queueChecks([saved.id]);
      notice('Series saved.', 'ok');
    });
  }

  $('tvd-add').addEventListener('click', () => editRule(null));

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
    const card = el('div', { className: 'tvd-rule queued' });
    card.append(el('div', { className: 'tvd-rule-head' }, [
      el('span', { className: 'tvd-queued-mark', textContent: '×' }),
      el('span', { className: 'tvd-rule-title', textContent: rule.series_title || rule.path }),
      el('span', { className: 'tvd-alert-age', textContent: `queued ${ago(queued.created_at)}` }),
    ]));
    const lines = el('div', { className: 'tvd-queued-lines' });
    lines.append(el('div', { textContent: 'Queued for removal from TV Delete at the next run.' }));
    lines.append(el('div', { textContent: REMOVAL_SONARR[queued.action] || '' }));
    lines.append(el('div', { className: REMOVAL_FILES[queued.action] ? 'tvd-queued-danger' : '',
                             textContent: REMOVAL_FILES[queued.action] || 'No files will be removed' }));
    card.append(lines);
    const undo = el('button', { type: 'button', className: 'tvd-action', textContent: 'Undo' });
    undo.addEventListener('click', () => guarded('', async () => {
      const target = (settings.rules || []).find((other) => other.id === rule.id);
      target.queue = Object.assign({}, target.queue, { removal: null });
      await saveSettings('Removal cancelled.');
    }));
    card.append(el('div', { className: 'tvd-alert-foot' }, [undo]));
    return card;
  }

  // Removing a series is an intent, not an act: it queues, and the two destructive Sonarr
  // options each demand their own word before they can be queued at all.
  function deleteSeries(rule) {
    dialog(`Remove ${rule.series_title || rule.path}`, (body) => {
      body.append(el('p', { textContent:
        'This queues the series for removal from TV Delete. Nothing happens until the next '
        + 'run, and it can be undone from the series card until then.' }));
      const action = options(el('select'), REMOVAL_ACTIONS, 'remove');
      const warning = el('div', { className: 'tvd-danger-box', hidden: true });
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
        $('tvd-dialog-ok').disabled = !!word && confirm.value.trim() !== word;
        if (!word) return;
        warning.replaceChildren(
          el('strong', { textContent: 'Sonarr will delete this series.' }),
          el('span', { textContent: action.value === 'delete-series-files'
            ? ' Its episode files go too, and only Sonarr’s own recycle bin will hold them. '
              + 'TV Delete does not remove the series itself — it asks Sonarr to.'
            : ' The files stay on disk; only Sonarr’s record of the series is removed.' }));
      };
      action.addEventListener('change', review);
      confirm.addEventListener('input', review);
      body.append(field('Sonarr action', action,
                        'What Sonarr should do as the series leaves TV Delete.'), warning, confirmField);
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
    $('tvd-schedule-enabled').checked = !!schedule.enabled;
    $('tvd-test-mode').checked = schedule.test_mode !== false;
    options($('tvd-weekday'), WEEKDAYS, schedule.weekday ?? 0);
    options($('tvd-monthly-day'), range(1, 28, true), schedule.monthly_day ?? 1);
    options($('tvd-monthly-weekday'), [['', 'Day of the month']].concat(WEEKDAYS),
            schedule.monthly_weekday === '' ? '' : (schedule.monthly_weekday ?? ''));
    options($('tvd-hour'), range(0, 23, true), schedule.hour ?? 4);
    options($('tvd-minute'), range(0, 59, true), schedule.minute ?? 0);
    $('tvd-freq').value = schedule.frequency || 'daily';
    $('tvd-monthly-mode').value = schedule.monthly_mode || 'day';
    $('tvd-cron').value = schedule.cron || '0 4 * * *';
    $('tvd-schedule-summary').textContent = snapshot.schedule_text || 'Off';

    const match = (settings.health || {}).series_match || {};
    $('tvd-match-freq').value = ['hourly', 'daily', 'weekly'].includes(match.frequency) ? match.frequency : 'daily';
    options($('tvd-match-hour'), range(0, 23, true), match.hour ?? 5);
    options($('tvd-match-minute'), range(0, 59, true), match.minute ?? 0);
    $('tvd-match-summary').textContent = snapshot.series_match_text || '';
    $('tvd-connectivity').value = String((settings.health || {}).connectivity_seconds || 300);
    applyScheduleVisibility();
  }

  // Only the fields that mean something for the chosen frequency are shown, so the form
  // never asks for a weekday that will be ignored.
  function applyScheduleVisibility() {
    const frequency = $('tvd-freq').value;
    const monthlyMode = $('tvd-monthly-mode').value;
    const show = (id, on) => { $(id).hidden = !on; };
    show('tvd-field-weekday', frequency === 'weekly');
    show('tvd-field-monthly-mode', frequency === 'monthly');
    show('tvd-field-monthly-day', frequency === 'monthly' && monthlyMode === 'day');
    show('tvd-field-monthly-weekday', frequency === 'monthly' && monthlyMode !== 'day');
    show('tvd-field-hour', frequency !== 'hourly' && frequency !== 'custom');
    show('tvd-field-minute', frequency !== 'custom');
    show('tvd-field-cron', frequency === 'custom');
  }
  ['tvd-freq', 'tvd-monthly-mode'].forEach((id) => $(id).addEventListener('change', applyScheduleVisibility));

  function collectSchedule() {
    return {
      enabled: $('tvd-schedule-enabled').checked,
      test_mode: $('tvd-test-mode').checked,
      frequency: $('tvd-freq').value,
      minute: $('tvd-minute').value,
      hour: $('tvd-hour').value,
      weekday: $('tvd-weekday').value,
      monthly_mode: $('tvd-monthly-mode').value,
      monthly_day: $('tvd-monthly-day').value,
      monthly_weekday: $('tvd-monthly-weekday').value,
      cron: $('tvd-cron').value.trim(),
    };
  }

  $('tvd-save-schedule').addEventListener('click', () => guarded('', async () => {
    settings.schedule = collectSchedule();
    settings.health = Object.assign({}, settings.health, {
      series_match: Object.assign({}, (settings.health || {}).series_match, {
        enabled: true,
        frequency: $('tvd-match-freq').value,
        hour: $('tvd-match-hour').value,
        minute: $('tvd-match-minute').value,
      }),
      connectivity_seconds: $('tvd-connectivity').value,
    });
    await saveSettings('Schedule saved.');
  }));

  // -- Sonarr instances --------------------------------------------------
  function renderInstances() {
    const container = $('tvd-instances');
    container.replaceChildren();
    (settings.instances || []).forEach((instance) => {
      const health = ((snapshot.health || {}).instances || {})[instance.id] || {};
      const reachable = health.reachable !== false;
      const card = el('div', { className: `tvd-instance ${reachable ? 'ok' : 'bad'}` });
      const line = el('div', { className: 'tvd-instance-line' });
      line.append(el('span', { className: `tvd-dot ${reachable ? 'ok' : 'bad'}`,
                               title: reachable ? 'Answering' : (health.error || 'Not answering') }));
      line.append(el('span', { className: 'tvd-rule-title', textContent: instance.name }));
      line.append(el('span', { className: 'tvd-chip tvd-mono', textContent: instance.url }));
      if (health.sonarr_version) line.append(el('span', { className: 'tvd-chip', textContent: `Sonarr ${health.sonarr_version}` }));
      if (!reachable) line.append(el('span', { className: 'tvd-tag blocking', textContent: 'unreachable' }));
      const edit = el('button', { type: 'button', className: 'tvd-small', textContent: 'Edit' });
      edit.addEventListener('click', () => editInstance(instance));
      line.append(el('span', { className: 'tvd-spacer' }));
      line.append(edit);
      // Enabling an instance is a switch on the card, like enabling a series.
      const control = toggle(instance.enabled ? 'Enabled' : 'Disabled', instance.enabled, null,
                             { className: 'tvd-card-switch', label: `${instance.name} enabled` });
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
                             { className: 'tvd-card-switch' });
      const verify = toggle('Verify the TLS certificate', instance.verify_tls, null, { className: 'tvd-row-switch' });
      const testButton = el('button', { type: 'button', className: 'tvd-primary', textContent: 'Test connection' });
      const testResult = el('span', { className: 'tvd-result' });

      const gate = () => {
        $('tvd-dialog-ok').disabled = !verified;
        $('tvd-dialog-ok').textContent = verified ? 'Save' : 'Test first';
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
          testResult.className = 'tvd-result ok';
          gate();
        });
      });

      body.append(
        el('div', { className: 'tvd-card-head' },
           [el('span', { className: 'tvd-card-title', textContent: 'Connection' }), enabled.node]),
        field('Name', name),
        field('URL', url, 'Include the port, and any base URL Sonarr is configured with.'),
        field('API key', key, existing ? 'Leave the masked value to keep the stored key.' : 'Sonarr: Settings → General → API Key.'),
        verify.node,
        el('div', { className: 'tvd-row tvd-inline' }, [testButton, testResult]),
      );
      if (existing) {
        const remove = el('button', { type: 'button', className: 'tvd-danger tvd-small',
                                      textContent: 'Remove this instance' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          guarded('', async () => {
            const used = (settings.rules || []).filter((rule) => rule.instance_id === instance.id);
            if (used.length) throw new Error(`${plural(used.length, 'series')} still use ${instance.name}.`);
            if (!window.confirm(`Remove the Sonarr instance ${instance.name}?`)) return;
            $('tvd-dialog').close('cancel');
            settings.instances = settings.instances.filter((other) => other.id !== instance.id);
            await saveSettings('Instance removed.');
          });
        });
        body.append(el('div', { className: 'tvd-editor-foot' }, [remove]));
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

  $('tvd-add-instance').addEventListener('click', () => editInstance(null));

  const NOTIFICATIONS = [
    ['run_started', 'A run has started'],
    ['run_completed', 'A run has finished'],
    ['series_removed', 'A series was removed from Sonarr'],
    ['series_ended', 'Sonarr reports a series has ended'],
    ['health_problems', 'A check found something wrong'],
    ['health_ok', 'A check found nothing wrong'],
    ['errors', 'Any error'],
  ];
  const notifyInputs = {};

  function renderSettings() {
    const retention = settings.retention || {};
    $('tvd-include-specials').checked = !!retention.include_specials;
    $('tvd-estimated-dates').checked = retention.allow_estimated_dates !== false;
    $('tvd-search-after').checked = !!retention.search_after_monitor;
    $('tvd-tmdb-key').value = (settings.tmdb || {}).api_key || '';
    $('tvd-state-dir').value = settings.state_dir || '';
    $('tvd-history-size').value = settings.log_retention_runs;
    $('tvd-log-level').value = (settings.logging || {}).level || 'warning';

    const box = $('tvd-notifications');
    box.replaceChildren();
    NOTIFICATIONS.forEach(([name, label]) => {
      const control = toggle(label, (settings.notifications || {})[name], null, { className: 'tvd-notify-row' });
      notifyInputs[name] = control.input;
      box.append(control.node);
    });
  }

  function collectSettings() {
    const notifications = {};
    NOTIFICATIONS.forEach(([name]) => { notifications[name] = notifyInputs[name].checked; });
    return Object.assign({}, settings, {
      retention: {
        include_specials: $('tvd-include-specials').checked,
        allow_estimated_dates: $('tvd-estimated-dates').checked,
        search_after_monitor: $('tvd-search-after').checked,
      },
      tmdb: { api_key: $('tvd-tmdb-key').value },
      state_dir: $('tvd-state-dir').value.trim(),
      log_retention_runs: $('tvd-history-size').value,
      logging: Object.assign({}, settings.logging, { level: $('tvd-log-level').value }),
      notifications,
    });
  }

  async function saveSettings(message, quiet) {
    const data = await api('settings', { settings: collectSettings() }, 'Saving…', quiet);
    settings = data.settings;
    snapshot.settings = settings;
    snapshot.schedule_text = data.schedule_text || snapshot.schedule_text;
    snapshot.series_match_text = data.series_match_text || snapshot.series_match_text;
    render();
    if (message) notice(message, 'ok');
  }

  $('tvd-save').addEventListener('click', () => guarded('', () => saveSettings('Settings saved.')));
  $('tvd-browse-state').addEventListener('click', () => {
    browseFolder($('tvd-state-dir').value || '/mnt/user/appdata', (picked) => { $('tvd-state-dir').value = picked; });
  });
  $('tvd-tmdb-test').addEventListener('click', () => guarded('', async () => {
    const result = $('tvd-tmdb-result');
    try {
      await api('test-tmdb', { tmdb: { api_key: $('tvd-tmdb-key').value } }, 'Contacting TMDB…');
      result.textContent = 'Key accepted';
      result.className = 'tvd-result ok';
    } catch (error) {
      result.textContent = 'Key rejected';
      result.className = 'tvd-result bad';
      throw error;
    }
  }));

  // -- run results and history -------------------------------------------
  function showResult(result, title) {
    dialog(`${title}: ${result.dry_run ? 'nothing was changed' : `${result.deleted} files deleted`}`, (body) => {
      if (result.aborted) body.append(el('div', { className: 'tvd-warning', textContent: result.aborted }));
      (result.blocked || []).forEach((message) => body.append(el('div', { className: 'tvd-warning', textContent: message })));
      body.append(el('p', { textContent:
        `${result.planned} file(s) selected across ${result.rules.length} series in ${result.duration_seconds}s.`
        + (result.dry_run ? ' Preview: nothing was changed.' : ` ${bytes(result.freed_bytes)} reclaimed.`)
        + (result.remonitored ? ` ${result.remonitored} episode(s) re-monitored.` : '') }));
      result.rules.forEach((rule) => {
        const card = el('div', { className: `tvd-rule ${rule.ok ? 'ok' : 'blocked'}` });
        card.append(el('div', { className: 'tvd-rule-head' }, [
          el('span', { className: 'tvd-rule-title', textContent: rule.series_title }),
          el('span', { className: 'tvd-chip', textContent: `${rule.deleted.length} selected` }),
          el('span', { className: 'tvd-chip', textContent: `${rule.kept} kept` }),
          el('span', { className: 'tvd-chip', textContent: `${rule.protected} protected` }),
        ]));
        if (rule.preset) card.append(el('small', { textContent: `Preset: ${rule.preset}` }));
        if (rule.note) card.append(el('small', { textContent: rule.note }));
        if (rule.error) card.append(el('div', { className: 'tvd-error', textContent: rule.error }));
        if (rule.blocked) card.append(el('div', { className: 'tvd-warning', textContent: rule.blocked }));
        if ((rule.remonitored || []).length) {
          card.append(el('p', { textContent: `${rule.remonitored.length} previously removed episode(s) `
            + `${result.dry_run ? 'would be' : 'were'} re-monitored.` }));
        }
        if (rule.deleted.length) {
          const table = el('table', { className: 'tvd-table' });
          table.append(el('thead', { innerHTML: '<tr><th>Episode</th><th>Aired</th><th>Size</th><th>Why</th></tr>' }));
          const tbody = el('tbody');
          rule.deleted.slice(0, 200).forEach((item) => {
            const tr = el('tr');
            tr.append(el('td', {}, [
              el('div', { textContent: `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')} — ${item.title || ''}` }),
              el('div', { className: 'tvd-mono', textContent: item.path })]));
            tr.append(el('td', { textContent: item.air_date ? `${item.air_date} (${item.air_source})` : 'unknown' }));
            tr.append(el('td', { textContent: bytes(item.size) }));
            tr.append(el('td', { textContent: (item.error ? `FAILED: ${item.error} — ` : '') + (item.reason || '') }));
            tbody.append(tr);
          });
          table.append(tbody);
          card.append(el('div', { className: 'tvd-scroll' }, [table]));
          if (rule.deleted.length > 200) {
            card.append(el('small', { textContent: `…and ${rule.deleted.length - 200} more. The full list is in the journal.` }));
          }
        }
        body.append(card);
      });
      return {};
    }, null, 'Close');
  }

  function renderHistory() {
    const runs = snapshot.runs || [];
    const tbody = $('tvd-history-table').querySelector('tbody');
    tbody.replaceChildren();
    $('tvd-history-empty').hidden = runs.length > 0;
    $('tvd-history-table').hidden = runs.length === 0;
    runs.forEach((run) => {
      const tr = el('tr');
      [when(run.started), run.scheduled ? 'schedule' : 'manual', run.dry_run ? 'preview' : 'live',
       run.planned, run.deleted, bytes(run.freed_bytes), run.aborted || (run.errors || []).join('; ') || '']
        .forEach((value) => tr.append(el('td', { textContent: String(value) })));
      tbody.append(tr);
    });
    const last = $('tvd-last-run');
    last.replaceChildren();
    if (snapshot.last_run) {
      const button = el('button', { type: 'button', className: 'tvd-secondary', textContent: 'Show the last run report' });
      button.addEventListener('click', () => showResult(snapshot.last_run, 'Last run'));
      last.append(button);
    }
  }

  $('tvd-clear-history').addEventListener('click', () => guarded('', async () => {
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
    $('tvd-log').textContent = '';
    const tick = async () => {
      try {
        const data = await api('log', { offset: logOffset }, '', true);
        logOffset = data.offset;
        if (data.text) {
          const pane = $('tvd-log');
          pane.append(text(data.text));
          // Keep the pane bounded; the file itself is the record, this is just the view.
          if (pane.textContent.length > 400000) pane.textContent = pane.textContent.slice(-200000);
          if ($('tvd-log-follow').checked) pane.scrollTop = pane.scrollHeight;
        }
        $('tvd-log-status').textContent = `${(settings.logging || {}).level || 'warning'} · ${bytes(data.size)}`;
      } catch (error) {
        $('tvd-log-status').textContent = 'log unavailable';
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

  $('tvd-log-clear').addEventListener('click', () => { $('tvd-log').textContent = ''; });

  // -- start -------------------------------------------------------------
  // Whatever happens, the page must end up interactive with a readable message.
  refresh().catch((error) => {
    busyDepth = 0;
    $('tvd-busy').hidden = true;
    notice(`TV Delete could not load: ${error.message}`, 'bad');
  });
  window.addEventListener('error', () => { busyDepth = 0; $('tvd-busy').hidden = true; });
  window.addEventListener('unhandledrejection', () => { busyDepth = 0; $('tvd-busy').hidden = true; });
})();
