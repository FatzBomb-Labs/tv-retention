// Stats, run reports, history and the live log — everything the System section shows
// about what the worker has been doing, as opposed to what it is configured to do.
//
// These read the snapshot rather than owning any state of their own, except the log's
// poll timer and its read offset, which belong to the pane and nothing else. The entry
// still holds `snapshot` and `settings`: both are reassigned wholesale when a save or a
// refresh returns, so they arrive here as accessors rather than as values, which would
// freeze at whichever document happened to be current when this module was built.

import { $, el, text } from './dom.js';
import { ago, bytes, when, plural } from './format.js';
import { changeRows } from './changes.js';
import { dialog, guarded, notice } from './feedback.js';

function createActivity({ api, refresh, getSnapshot, getSettings }) {
  let status = null;

  function renderStatus(data) {
    status = data || getSnapshot().status || null;
    if (!status) return;
    const summary = $('tvr-status-summary');
    if (!summary) return;
    const sync = status.sync || {};
    const schedule = status.schedule || {};
    summary.replaceChildren(...[
      ['HEALTH', status.alerts && status.alerts.length ? `${status.alerts.length} issue(s)` : 'Healthy',
       status.alerts && status.alerts.length ? 'See details below' : 'No status errors or warnings'],
      ['UPTIME', `${Math.floor((status.uptime_seconds || 0) / 3600)}h ${Math.floor(((status.uptime_seconds || 0) % 3600) / 60)}m`, 'Worker process'],
      ['SONARR SYNC', sync.synced_at ? ago(sync.synced_at) : 'Not yet', sync.running ? 'Read in progress' : 'Cached reading'],
      ['SCHEDULE', schedule.enabled ? 'Enabled' : 'Off', status.test_mode ? 'Test Mode on' : 'Live mode'],
      ['BUILD', `${status.version || 'unknown'} · ${status.build_number || 'dev'}`,
       status.build_date ? `built ${new Date(status.build_date).toLocaleDateString()}` : 'build date not recorded'],
    ].map(([name, value, note]) => el('div', {}, [
      el('span', { textContent: name }), el('strong', { textContent: value }),
      el('small', { textContent: note }),
    ])));

    const alerts = $('tvr-status-alerts');
    alerts.replaceChildren(...(status.alerts || []).map((alert) =>
      el('div', { className: `tvr-status ${alert.severity === 'error' ? 'bad' : 'ok'}` }, [
        el('strong', { textContent: alert.title }),
        el('span', { textContent: alert.detail || alert.help || '' }),
      ])));
    const recent = $('tvr-status-recent');
    if (recent) recent.replaceChildren(...(status.recent || []).map((alert) =>
      el('div', { className: 'tvr-status-recent' }, [
        el('span', { className: `tvr-sev ${alert.severity || 'notice'}` }),
        el('strong', { textContent: alert.title }),
        el('span', { textContent: alert.detail || '' }),
        el('span', { className: 'tvr-alert-age', textContent: alert.last_seen ? ago(alert.last_seen) : '' }),
      ])));
    const instances = $('tvr-status-instances');
    instances.replaceChildren(...Object.values(status.instances || {}).map((instance) =>
      el('div', { className: `tvr-instance ${instance.reachable === false ? 'bad' : 'ok'}` }, [
        el('span', { className: `tvr-dot ${instance.reachable === false ? 'bad' : 'ok'}` }),
        el('strong', { textContent: instance.name || instance.instance_id }),
        el('span', { textContent: instance.reachable === false ? (instance.error || 'Unreachable')
          : `Sonarr ${instance.sonarr_version || 'connected'} · ${instance.series_count || 0} series` }),
        el('span', { className: 'tvr-alert-age', textContent: instance.checked_at ? ago(instance.checked_at) : 'not checked' }),
      ])));
    const storage = $('tvr-status-storage');
    const shape = status.storage || {};
    storage.replaceChildren(...[
      ['Config readable', shape.config_readable ? 'yes' : 'no'],
      ['State writable', shape.state_writable ? 'yes' : 'no'],
      ['Backup destination', shape.backup_path ? (shape.backup_valid ? 'available' : 'unavailable') : 'not configured'],
      ['API key', (status.api_key || {}).status || 'not_created'],
    ].map(([name, value]) => el('div', { className: 'tvr-inline-row' }, [
      el('span', { className: 'tvr-inline-label', textContent: name }), el('span', { textContent: value }),
    ])));
    const run = $('tvr-status-run');
    const last = status.last_run;
    run.replaceChildren(el('div', { className: 'tvr-inline-row' }, [
      el('span', { className: 'tvr-inline-label', textContent: 'Last run' }),
      el('span', { textContent: last ? `${when(last.finished || last.started)} · ${last.deleted || 0} deleted` : 'No runs yet' }),
    ]));
  }

  async function renderStatusView() {
    const data = await api('status', {}, 'Refreshing status…', true);
    getSnapshot().status = data;
    renderStatus(data);
  }
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

  // -- run results and history -------------------------------------------
  function showResult(result, title) {
    // Removals are a separate list from the retention pass's own deleted/freed_bytes
    // counters, and the two never spoke to each other: a run that only removed queued
    // series reported "0 files deleted" as its headline even though whole series, files
    // and all, had just been asked of Sonarr — the removal cards further down said so,
    // but the headline read as if nothing had happened.
    const removed = (result.removals || []).filter((record) => record.ok && !record.dry_run).length;
    const parts = [];
    if (result.deleted) parts.push(`${plural(result.deleted, 'file')} deleted`);
    if (removed) parts.push(`${plural(removed, 'series')} removed`);
    const headline = result.dry_run ? 'nothing was changed' : (parts.join(', ') || 'nothing was deleted');
    dialog(`${title}: ${headline}`, (body) => {
      (result.blocked || []).forEach((message) => body.append(el('div', { className: 'tvr-warning', textContent: message })));
      body.append(el('p', { textContent:
        `${result.planned} file(s) across ${result.rules.length} series in ${result.duration_seconds}s.`
        + (result.dry_run ? ' Nothing was changed.'
           : ` ${bytes(result.freed_bytes)} reclaimed.` + (removed ? ` ${plural(removed, 'series')} removed.` : '')) }));
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
    const snapshot = getSnapshot();
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
        $('tvr-log-status').textContent = `${(getSettings().logging || {}).level || 'warning'} · ${bytes(data.size)}`;
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

  // The listeners these panes own. Held back behind a call rather than run at import,
  // because a module that is not the entry may not touch the document while it loads.
  function wire() {
    $('tvr-clear-history').addEventListener('click', () => guarded('', async () => {
      if (!window.confirm('Clear the run history? The on-disk journal is kept.')) return;
      await api('clear-history', {}, 'Clearing…');
      await refresh();
      notice('History cleared.', 'ok');
    }));

    $('tvr-log-clear').addEventListener('click', () => { $('tvr-log').textContent = ''; });
    $('tvr-status-refresh').addEventListener('click', () => guarded('', renderStatusView));
  }

  return { renderStatsView, renderStatusView, renderStatus, showResult, renderHistory, startLog, stopLog, wire };
}

export { createActivity };
