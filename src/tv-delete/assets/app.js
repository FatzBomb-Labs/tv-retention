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
    $('tvd-preview').checked = !!settings.preview;
    $('tvd-preview-banner').hidden = !settings.preview;
    $('tvd-preview-label').classList.toggle('on', !!settings.preview);
    $('tvd-run').textContent = settings.preview ? 'Preview run' : 'Run now';
    renderStats();
    renderRules();
    renderAlerts();
    renderPresets();
    renderInstances();
    renderSchedule();
    renderSettings();
    renderHistory();
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

  // -- preview mode ------------------------------------------------------
  $('tvd-preview').addEventListener('change', () => guarded('', async () => {
    const wanted = $('tvd-preview').checked;
    // Turning Preview off arms every destructive action on the page, so it asks. Turning
    // it on is always safe and never interrupts.
    if (!wanted && !window.confirm('Turn Preview off? Runs and fixes will make real changes '
                                   + 'to your files and to Sonarr.')) {
      $('tvd-preview').checked = true;
      return;
    }
    settings.preview = wanted;
    await saveSettings(wanted ? 'Preview is on. Nothing will be changed.' : 'Preview is off. Actions are live.');
  }));

  $('tvd-run').addEventListener('click', () => guarded('', async () => {
    const runnable = (settings.rules || []).filter((rule) => rule.enabled && !isBlocked(rule.id));
    if (!runnable.length) throw new Error('There are no enabled series ready to run.');
    const warning = settings.preview
      ? `Preview ${plural(runnable.length, 'series')} now? Nothing will be changed.`
      : `Run ${plural(runnable.length, 'series')} now? This will DELETE episode files through Sonarr.`;
    if (!window.confirm(warning)) return;
    const data = await api('run', {}, settings.preview ? 'Previewing…' : 'Running…');
    await refresh();
    showResult(data.result, settings.preview ? 'Preview' : 'Run');
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

  function renderRules() {
    const container = $('tvd-rules');
    const term = ($('tvd-search').value || '').toLowerCase();
    const filter = $('tvd-filter').value;
    const rules = sortRules((settings.rules || []).filter((rule) => {
      if (term && !(`${rule.series_title} ${rule.path}`.toLowerCase().includes(term))) return false;
      if (filter === 'enabled') return rule.enabled;
      if (filter === 'disabled') return !rule.enabled;
      if (filter === 'attention') return seriesAlerts(rule.id).length > 0;
      if (filter === 'blocked') return isBlocked(rule.id);
      return true;
    }));
    container.replaceChildren();
    $('tvd-rules-empty').hidden = (settings.rules || []).length > 0;
    rules.forEach((rule) => {
      const blocked = isBlocked(rule.id);
      const card = el('div', { className: `tvd-rule ${blocked ? 'blocked' : 'ok'}${rule.enabled ? '' : ' disabled'}` });
      const instance = (settings.instances || []).find((i) => i.id === rule.instance_id);
      const head = el('div', { className: 'tvd-rule-head' }, [
        alertBadge(rule),
        el('span', { className: 'tvd-rule-title', textContent: rule.series_title || '(unmatched)' }),
        el('span', { className: 'tvd-chip', textContent: instance ? instance.name : 'unknown instance' }),
      ]);
      head.append(enableToggle(rule));
      card.append(head);
      card.append(el('div', { className: 'tvd-rule-path', textContent: rule.path }));

      const body = el('div', { className: 'tvd-rule-body' });
      const preset = presetFor(rule);
      if (preset) body.append(el('span', { className: 'tvd-chip on', textContent: `preset: ${preset.name}` }));
      ruleSummary(rule).forEach((label) => body.append(el('span', { className: 'tvd-chip on', textContent: label })));
      body.append(el('span', { className: 'tvd-chip', textContent: `combine: ${(preset || rule).combine}` }));
      if (rule.include_specials === true) body.append(el('span', { className: 'tvd-chip on', textContent: 'specials included' }));
      if (rule.include_specials === false) body.append(el('span', { className: 'tvd-chip', textContent: 'specials excluded' }));

      const actions = el('div', { className: 'tvd-rule-actions' });
      const previewButton = el('button', { type: 'button', textContent: 'Preview' });
      previewButton.addEventListener('click', () => guarded('', async () => {
        const data = await api('preview', { rule_ids: [rule.id] }, 'Previewing…');
        showResult(data.result, 'Preview');
      }));
      const runButton = el('button', { type: 'button', textContent: settings.preview ? 'Run (preview)' : 'Run' });
      runButton.addEventListener('click', () => guarded('', async () => {
        if (blocked) throw new Error('This series is blocked by an error. Open its badge to see why.');
        const warning = settings.preview
          ? `Preview ${rule.series_title}? Nothing will be changed.`
          : `Run ${rule.series_title} now? This will DELETE episode files through Sonarr.`;
        if (!window.confirm(warning)) return;
        const data = await api('run', { rule_ids: [rule.id] }, 'Running…');
        await refresh();
        showResult(data.result, 'Run');
      }));
      const editButton = el('button', { type: 'button', textContent: 'Edit' });
      editButton.addEventListener('click', () => editRule(rule));
      const deleteButton = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Delete…' });
      deleteButton.addEventListener('click', () => deleteSeries(rule));
      actions.append(previewButton, runButton, editButton, deleteButton);
      if (isChecking(rule.id)) {
        // Editing mid-read would save against a frame the incoming result no longer describes.
        [previewButton, runButton, editButton, deleteButton].forEach((button) => { button.disabled = true; });
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
    'mapping-broken': 'Root folder missing',
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
  };
  let severityFilter = 'all';

  function alertItem(alert) {
    const item = el('div', { className: 'tvd-alert-item' });
    const line = el('div', { className: 'tvd-alert-line' }, [
      el('span', { className: `tvd-tag ${alert.severity}`, textContent: ALERT_TAG[alert.kind] || alert.title }),
      el('span', { className: 'tvd-alert-detail', textContent: alert.detail }),
    ]);
    if (alert.blocking) line.append(el('span', { className: 'tvd-tag blocking', textContent: 'blocks runs' }));
    line.append(el('span', { className: 'tvd-alert-age', textContent: ago(alert.first_seen) }));
    item.append(line);

    const help = el('p', { className: 'tvd-alert-help', textContent: alert.help, hidden: true });
    const files = el('div', { className: 'tvd-alert-files', hidden: true });
    ((alert.data || {}).files || []).slice(0, 10).forEach((path) =>
      files.append(el('div', { className: 'tvd-mono', textContent: path })));

    const foot = el('div', { className: 'tvd-alert-foot' });
    if (alert.action) {
      const button = el('button', { type: 'button', className: 'tvd-primary tvd-small',
                                    textContent: ACTION_LABEL[alert.action] || 'Fix' });
      button.addEventListener('click', () => runAlertAction(alert));
      foot.append(button);
    }
    const why = el('button', { type: 'button', className: 'tvd-link', textContent: 'What does this mean?' });
    why.addEventListener('click', () => {
      help.hidden = !help.hidden;
      why.textContent = help.hidden ? 'What does this mean?' : 'Hide explanation';
    });
    foot.append(why);
    if ((alert.data || {}).files) {
      const show = el('button', { type: 'button', className: 'tvd-link', textContent: `List ${alert.count} file(s)` });
      show.addEventListener('click', () => { files.hidden = !files.hidden; });
      foot.append(show);
    }
    item.append(foot, help, files);
    return item;
  }

  function seriesAlertCard(rule, list, config) {
    const options = config || {};
    const severity = worstSeverity(list);
    const blocked = list.some((alert) => alert.blocking);
    const card = el('div', { className: `tvd-alert-card ${severity}` });
    const head = el('div', { className: 'tvd-alert-card-head' }, [
      el('span', { className: `tvd-dot-badge ${severity}`, textContent: String(list.length) }),
      el('span', { className: 'tvd-rule-title', textContent: rule.series_title || rule.path }),
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
      el('span', { className: `tvd-dot-badge ${severity}`, textContent: String(list.length) }),
      el('span', { className: 'tvd-rule-title', textContent: instanceName }),
    ]));
    list.forEach((alert) => card.append(alertItem(alert)));
    return card;
  }

  function runAlertAction(alert) {
    return guarded('', async () => {
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
    const summary = snapshot.alert_summary || { error: 0, warning: 0, notice: 0, total: 0 };
    $('tvd-count-all').textContent = summary.total || 0;
    ['error', 'warning', 'notice'].forEach((severity) => {
      $(`tvd-count-${severity}`).textContent = summary[severity] || 0;
    });
    const matches = (alert) => severityFilter === 'all' || alert.severity === severityFilter;

    const systemBox = $('tvd-alerts-system');
    systemBox.replaceChildren();
    const system = systemAlerts.filter(matches);
    $('tvd-alerts-system-empty').hidden = system.length > 0;
    // Grouped the same way series are: one card per thing, however many problems it has.
    const byInstance = new Map();
    system.forEach((alert) => {
      const instance = (settings.instances || []).find((i) => i.id === alert.instance_id);
      const name = instance ? instance.name : 'TV Delete';
      byInstance.set(name, (byInstance.get(name) || []).concat([alert]));
    });
    byInstance.forEach((list, name) => systemBox.append(systemAlertCard(name, list)));

    const seriesBox = $('tvd-alerts-series');
    seriesBox.replaceChildren();
    const grouped = (settings.rules || [])
      .map((rule) => [rule, seriesAlerts(rule.id).filter(matches)])
      .filter(([, list]) => list.length)
      .sort((a, b) => ATTENTION_RANK[worstSeverity(a[1])] - ATTENTION_RANK[worstSeverity(b[1])]
        || (a[0].series_title || '').localeCompare(b[0].series_title || ''));
    $('tvd-alerts-series-empty').hidden = grouped.length > 0;
    grouped.forEach(([rule, list]) => seriesBox.append(seriesAlertCard(rule, list)));
  }

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
      const removeButton = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Remove' });
      removeButton.addEventListener('click', () => guarded('', async () => {
        if (users.length) throw new Error(`${plural(users.length, 'series')} still use "${preset.name}".`);
        if (!window.confirm(`Remove the preset "${preset.name}"?`)) return;
        settings.profiles = settings.profiles.filter((other) => other.id !== preset.id);
        await saveSettings('Preset removed.');
      }));
      actions.append(editButton, removeButton);
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
      const enabled = toggle('Series is enabled', rule.enabled, null, { className: 'tvd-row-switch' });

      body.append(
        field('Sonarr instance', instanceSelect),
        seriesField,
        el('div', { className: 'tvd-row' }, [refreshButton]),
        field('Retention', presetSelect, (settings.profiles || []).length
          ? 'Presets are managed on the Presets tab.' : 'No presets yet — create one to reuse values.'),
        conditions.node,
        field('Season 0 / specials', specials, 'Overrides the global setting for this series only.'),
        enabled.node,
      );
      return { instanceSelect, getSeries: () => picker && picker.value, presetSelect, conditions,
               specials, enabled };
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
        unmonitor: true,
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

  // The only action that can destroy a whole series, and the only one that ignores
  // Preview. That is deliberate — tidying up is normally done with Preview on, and a
  // removal that silently did nothing would be worse — so the dialog says it outright.
  function deleteSeries(rule) {
    dialog(`Remove ${rule.series_title || rule.path}`, (body) => {
      const state = monitoring[rule.id] || {};
      body.append(el('p', { textContent:
        'Removing this series from TV Delete stops it being managed here and touches nothing '
        + 'else. The two options below go further.' }));
      const fromSonarr = toggle('Also remove the series from Sonarr', false, null, { className: 'tvd-row-switch' });
      const fromDisk = toggle('Also delete its episode files from disk', false, null, { className: 'tvd-row-switch' });
      body.append(fromSonarr.node, fromDisk.node);
      if (state.files_total) {
        body.append(el('small', { textContent: `Sonarr reports ${plural(state.files_total, 'file')} for this series.` }));
      }
      const warning = el('div', { className: 'tvd-danger-box', hidden: true });
      const confirm = el('input', { type: 'text', autocomplete: 'off', spellcheck: false, placeholder: 'DELETE' });
      const confirmField = field('Type DELETE to confirm', confirm);
      confirmField.hidden = true;
      const review = () => {
        const destructive = fromSonarr.input.checked || fromDisk.input.checked;
        warning.hidden = !destructive;
        confirmField.hidden = !destructive;
        warning.replaceChildren();
        if (!destructive) return;
        const parts = [];
        if (fromDisk.input.checked) parts.push('every episode file WILL be deleted from disk');
        if (fromSonarr.input.checked) parts.push('the series WILL be removed from Sonarr');
        warning.append(el('strong', { textContent: 'This ignores Preview.' }));
        warning.append(el('span', { textContent: ` Even with Preview on, ${parts.join(' and ')}. `
          + 'It is not covered by the run guards and cannot be undone from here — only Sonarr’s '
          + 'own recycle bin, if you have one, will hold anything.' }));
      };
      fromSonarr.input.addEventListener('change', review);
      fromDisk.input.addEventListener('change', review);
      body.append(warning, confirmField);
      return { fromSonarr, fromDisk, confirm };
    }, async (context) => {
      const destructive = context.fromSonarr.input.checked || context.fromDisk.input.checked;
      if (destructive && context.confirm.value.trim() !== 'DELETE') {
        throw new Error('Type DELETE to confirm. Nothing was removed.');
      }
      if (!destructive) {
        const data = await api('alert-action', { kind: 'remove-rule', rule_id: rule.id }, 'Removing…');
        settings = data.settings;
        snapshot.settings = settings;
        await refresh();
        notice('Series removed from TV Delete. No files were touched.', 'ok');
        return;
      }
      const data = await api('remove-series', {
        rule_id: rule.id, confirm_title: rule.series_title,
        delete_files: context.fromDisk.input.checked,
        remove_from_sonarr: context.fromSonarr.input.checked,
      }, 'Removing the series…');
      await refresh();
      notice(`${data.removed.series_title} removed`
             + (data.removed.deleted_files ? ` (${plural(data.removed.files, 'file')}, ${bytes(data.removed.bytes)})` : '')
             + '.', 'ok');
    }, 'Remove');
  }

  // -- schedule ----------------------------------------------------------
  const WEEKDAYS = [[0, 'Sunday'], [1, 'Monday'], [2, 'Tuesday'], [3, 'Wednesday'],
                    [4, 'Thursday'], [5, 'Friday'], [6, 'Saturday']];

  function renderSchedule() {
    const schedule = settings.schedule || {};
    $('tvd-schedule-enabled').checked = !!schedule.enabled;
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
      const roots = (instance.roots || []).filter((root) => root.enabled);
      const card = el('div', { className: `tvd-instance ${reachable ? 'ok' : 'bad'}` });
      const line = el('div', { className: 'tvd-instance-line' });
      line.append(el('span', { className: `tvd-dot ${reachable ? 'ok' : 'bad'}`,
                               title: reachable ? 'Answering' : (health.error || 'Not answering') }));
      line.append(el('span', { className: 'tvd-rule-title', textContent: instance.name }));
      line.append(el('span', { className: 'tvd-chip tvd-mono', textContent: instance.url }));
      line.append(el('span', { className: 'tvd-chip', textContent: `${plural(roots.length, 'root')} mapped` }));
      if (!instance.enabled) line.append(el('span', { className: 'tvd-badge quiet', textContent: 'disabled' }));
      if (!reachable) line.append(el('span', { className: 'tvd-badge error', textContent: 'unreachable' }));
      const actions = el('div', { className: 'tvd-rule-actions' });
      const editButton = el('button', { type: 'button', textContent: 'Edit' });
      editButton.addEventListener('click', () => editInstance(instance));
      const removeButton = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Remove' });
      removeButton.addEventListener('click', () => guarded('', async () => {
        const used = (settings.rules || []).filter((rule) => rule.instance_id === instance.id);
        if (used.length) throw new Error(`${plural(used.length, 'series')} still use ${instance.name}.`);
        if (!window.confirm(`Remove the Sonarr instance ${instance.name}?`)) return;
        settings.instances = settings.instances.filter((other) => other.id !== instance.id);
        await saveSettings('Instance removed.');
      }));
      actions.append(editButton, removeButton);
      line.append(actions);
      card.append(line);
      container.append(card);
    });
  }

  // The editor gates everything below the connection on a passing test. Roots cannot be
  // mapped sensibly until Sonarr has told us what its roots are, so asking for them first
  // would only invite guesses.
  function editInstance(existing) {
    const instance = Object.assign({ id: '', name: '', url: '', api_key: '', enabled: true,
                                     verify_tls: true, roots: [] }, existing || {});
    dialog(existing ? `Edit ${instance.name}` : 'Add Sonarr instance', (body) => {
      let verified = !!(existing && (instance.roots || []).length);
      let sonarrRoots = (instance.roots || []).map((root) => root.sonarr_path);
      const rows = [];

      const name = el('input', { type: 'text', value: instance.name, placeholder: 'Sonarr — Series' });
      const url = el('input', { type: 'text', value: instance.url, placeholder: 'http://192.168.1.10:8989', spellcheck: false });
      const key = el('input', { type: 'password', value: instance.api_key || '', autocomplete: 'off',
                                placeholder: 'Sonarr API key' });
      const enabled = toggle('Instance is enabled', instance.enabled, null, { className: 'tvd-head-switch' });
      const verify = toggle('Verify the TLS certificate', instance.verify_tls, null, { className: 'tvd-row-switch' });
      const testButton = el('button', { type: 'button', className: 'tvd-primary', textContent: 'Test connection' });
      const testResult = el('span', { className: 'tvd-result' });

      const rootsBox = el('div', { className: 'tvd-roots' });
      const rootsSection = el('div', {}, [
        el('h3', { className: 'tvd-card-title', textContent: 'Root folders' }),
        el('small', { textContent: 'Every root folder Sonarr has, and where each one lives on this '
                                   + 'server. A root left disabled is remembered but ignored.' }),
        el('div', { className: 'tvd-roots-head' }, [
          el('span', { textContent: 'Sonarr path' }), el('span', { textContent: 'Unraid path' }),
          el('span', { textContent: 'Enable' })]),
        rootsBox,
      ]);
      const autoFill = el('button', { type: 'button', textContent: 'Auto-fill from a common root…' });
      const gated = [rootsSection, autoFill, verify.node];

      const applyGate = () => {
        gated.forEach((node) => { node.hidden = !verified; });
        $('tvd-dialog-ok').disabled = !verified;
        $('tvd-dialog-ok').textContent = verified ? 'Save' : 'Test first';
      };

      const drawRoots = () => {
        rootsBox.replaceChildren();
        rows.length = 0;
        sonarrRoots.forEach((sonarrPath) => {
          const stored = (instance.roots || []).find((root) => root.sonarr_path === sonarrPath) || {};
          const unraid = el('input', { type: 'text', value: stored.unraid_path || '',
                                       spellcheck: false, placeholder: '/mnt/user/media/TV' });
          const browse = el('button', { type: 'button', className: 'tvd-small', textContent: 'Browse' });
          browse.addEventListener('click', (event) => {
            event.preventDefault();
            browseFolder(unraid.value || '/mnt/user', (picked) => { unraid.value = picked; });
          });
          const on = toggle('', stored.enabled !== false, null, { label: `${sonarrPath} enabled` });
          const row = el('div', { className: 'tvd-root-row' }, [
            el('span', { className: 'tvd-mono', textContent: sonarrPath }),
            el('span', { className: 'tvd-root-path' }, [unraid, browse]),
            on.node,
          ]);
          rows.push({ sonarr_path: sonarrPath, unraid, enabled: on.input });
          rootsBox.append(row);
        });
        if (!sonarrRoots.length) {
          rootsBox.append(el('p', { className: 'tvd-empty', textContent: 'Sonarr reported no root folders.' }));
        }
      };

      autoFill.addEventListener('click', (event) => {
        event.preventDefault();
        // One common Sonarr prefix plus one Unraid folder fills every root beneath it,
        // which is the difference between one entry and twenty-six on a sharded library.
        const prefixes = [...new Set(sonarrRoots.map((path) => {
          const parts = path.split('/').filter(Boolean);
          return parts.length ? '/' + parts[0] : path;
        }))];
        dialog('Auto-fill root folders', (inner) => {
          const prefix = options(el('select'), prefixes.map((value) => [value, value]), prefixes[0]);
          const target = el('input', { type: 'text', spellcheck: false, placeholder: '/mnt/user/media/TV' });
          const browse = el('button', { type: 'button', textContent: 'Browse…' });
          browse.addEventListener('click', (event2) => {
            event2.preventDefault();
            browseFolder(target.value || '/mnt/user', (picked) => { target.value = picked; });
          });
          inner.append(el('p', { textContent: 'Pick a Sonarr prefix and the folder it corresponds to here. '
                                              + 'Every root under that prefix is filled in from it.' }),
                       field('Sonarr prefix', prefix),
                       el('div', { className: 'tvd-row tvd-inline' }, [field('Unraid folder', target), browse]));
          return { prefix, target };
        }, (inner) => {
          const from = inner.prefix.value;
          const to = inner.target.value.trim().replace(/\/+$/, '');
          if (!to) throw new Error('Choose the Unraid folder first.');
          rows.forEach((row) => {
            if (row.sonarr_path === from || row.sonarr_path.startsWith(from + '/')) {
              row.unraid.value = to + row.sonarr_path.slice(from.length);
            }
          });
        }, 'Fill');
      });

      testButton.addEventListener('click', (event) => {
        event.preventDefault();
        guarded('', async () => {
          const draft = { id: instance.id, name: name.value, url: url.value, api_key: key.value,
                          enabled: enabled.input.checked, verify_tls: verify.input.checked, roots: [] };
          const data = await api('test-instance', { instance: draft }, 'Contacting Sonarr…');
          verified = true;
          sonarrRoots = data.root_folders || [];
          testResult.textContent = `Connected — Sonarr ${data.sonarr_version}, ${data.series_count} series`;
          testResult.className = 'tvd-result ok';
          drawRoots();
          applyGate();
        });
      });

      body.append(
        el('div', { className: 'tvd-card-head' }, [el('span', { className: 'tvd-card-title', textContent: 'Connection' }), enabled.node]),
        field('Name', name),
        field('URL', url, 'Include the port, and any base URL Sonarr is configured with.'),
        field('API key', key, existing ? 'Leave the masked value to keep the stored key.' : 'Sonarr: Settings → General → API Key.'),
        verify.node,
        el('div', { className: 'tvd-row tvd-inline' }, [testButton, testResult]),
        rootsSection, autoFill,
      );
      drawRoots();
      applyGate();
      return { name, url, key, enabled, verify, rows: () => rows, verified: () => verified };
    }, async (context) => {
      if (!context.verified()) throw new Error('Test the connection before saving.');
      const roots = context.rows().map((row) => ({
        sonarr_path: row.sonarr_path,
        unraid_path: row.unraid.value.trim(),
        enabled: row.enabled.checked,
      })).filter((root) => root.unraid_path || !root.enabled);
      const draft = {
        id: instance.id || undefined,
        name: context.name.value,
        url: context.url.value,
        api_key: context.key.value,
        enabled: context.enabled.input.checked,
        verify_tls: context.verify.input.checked,
        verified_at: new Date().toISOString(),
        roots,
      };
      settings.instances = (settings.instances || []).filter((other) => other.id !== instance.id).concat([draft]);
      seriesCache = {};
      await saveSettings('Sonarr instance saved.');
    }, 'Test first');
  }

  $('tvd-add-instance').addEventListener('click', () => editInstance(null));

  // -- settings ----------------------------------------------------------
  // Each guard is a switch, an explanation and a number on one line. The switch is the
  // difference between "no limit" and "a limit I have chosen", which a bare number cannot
  // express.
  const GUARDS = [
    ['max_deletes_per_run', 'Stop a run above this many deletions',
     'Abandons the whole run if the plan is larger. This is what catches a broken mapping '
     + 'or a mistyped rule before it does damage.', 1, 100000],
    ['max_percent_per_rule', 'Stop a series above this share of its episodes (%)',
     'Skips one series if it would remove more than this proportion of what it holds.', 1, 100],
    ['min_file_age_hours', 'Never touch files modified within (hours)',
     'Protects imports and copies that are still in progress.', 0, 8760],
  ];

  const NOTIFICATIONS = [
    ['run_started', 'A run has started'],
    ['run_completed', 'A run has finished'],
    ['series_removed', 'A series was removed from Sonarr'],
    ['series_ended', 'Sonarr reports a series has ended'],
    ['health_problems', 'A check found something wrong'],
    ['health_ok', 'A check found nothing wrong'],
    ['errors', 'Any error'],
  ];

  const guardInputs = {};
  const notifyInputs = {};

  function renderSettings() {
    const safety = $('tvd-safety');
    safety.replaceChildren();
    const row = (control, label, explanation, value) => el('div', { className: 'tvd-three' }, [
      control,
      el('div', { className: 'tvd-three-label' }, [el('span', { textContent: label }), hint(explanation)]),
      value || el('span'),
    ]);

    GUARDS.forEach(([name, label, help, low, high]) => {
      const stored = (settings.guards || {})[name] || { enabled: true, value: low };
      const number = el('input', { type: 'number', min: String(low), max: String(high), value: stored.value });
      const control = toggle('', stored.enabled, (on) => { number.disabled = !on; },
                             { label, className: 'tvd-cell-switch' });
      number.disabled = !stored.enabled;
      guardInputs[name] = { enabled: control.input, value: number };
      safety.append(row(control.node, label, help, number));
    });

    const sidecars = settings.sidecars || {};
    const extensions = el('input', { type: 'text', spellcheck: false,
                                     value: (sidecars.extensions || []).join(', '),
                                     placeholder: 'all matching files' });
    const sidecarSwitch = toggle('', sidecars.enabled, (on) => { extensions.disabled = !on; },
                                 { label: 'Remove matching sidecar files', className: 'tvd-cell-switch' });
    extensions.disabled = !sidecars.enabled;
    guardInputs.sidecars = { enabled: sidecarSwitch.input, value: extensions };
    safety.append(row(sidecarSwitch.node, 'Remove matching sidecar files on media deletion',
      'Subtitles, artwork and .nfo files sharing the episode name. Leave the list empty to remove '
      + 'all of them, or name extensions to restrict it. Another video file is never treated as a '
      + 'sidecar.', extensions));

    const emptySwitch = toggle('', settings.delete_empty_dirs, null,
                               { label: 'Remove empty season folders', className: 'tvd-cell-switch' });
    guardInputs.empty = { enabled: emptySwitch.input };
    safety.append(row(emptySwitch.node, 'Remove empty season folders',
      'Season folders left empty by a deletion. The series folder itself is never removed.'));

    const retention = settings.retention || {};
    $('tvd-include-specials').checked = !!retention.include_specials;
    $('tvd-auto-unmonitor').checked = !!retention.auto_unmonitor;
    $('tvd-auto-monitor').checked = !!retention.auto_monitor;
    $('tvd-mtime-fallback').checked = !!retention.allow_mtime_fallback;
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
    const guards = {};
    GUARDS.forEach(([name]) => {
      guards[name] = { enabled: guardInputs[name].enabled.checked, value: guardInputs[name].value.value };
    });
    const notifications = {};
    NOTIFICATIONS.forEach(([name]) => { notifications[name] = notifyInputs[name].checked; });
    return Object.assign({}, settings, {
      preview: $('tvd-preview').checked,
      guards,
      retention: {
        include_specials: $('tvd-include-specials').checked,
        allow_mtime_fallback: $('tvd-mtime-fallback').checked,
        auto_unmonitor: $('tvd-auto-unmonitor').checked,
        auto_monitor: $('tvd-auto-monitor').checked,
      },
      sidecars: {
        enabled: guardInputs.sidecars.enabled.checked,
        extensions: guardInputs.sidecars.value.value.split(',').map((value) => value.trim()).filter(Boolean),
      },
      delete_empty_dirs: guardInputs.empty.enabled.checked,
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
