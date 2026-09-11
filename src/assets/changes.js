/* What a planned change is called, and how one is drawn.
 *
 * One description of a change, used everywhere a change is shown: the header, a series
 * card, the detail view and the report after a run. Adding a kind of change means adding
 * it here once, rather than in four places that then drift apart.
 */
'use strict';

import { el } from './dom.js';
import { bytes, plural } from './format.js';

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

// Each line opens exactly what it names. The total replaces the old button: a count you
// can read is more use than a button that only promises one.
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

export { REMOVAL_SUMMARY, changeSummary, changeLines, changeRows };
