import { $, el } from './dom.js';
import { ago, plural } from './format.js';
import { changeSummary, changeList } from './changes.js';
import { dialog, guarded, notice } from './feedback.js';
import { remember, remembered } from './storage.js';

// The top bar and the sidebar counts: one number that opens what it counts, the age of
// the reading everything else is answered from, the Run button and its three states, and
// the one overview of everything wrong.
//
// The three renders are separate on purpose rather than folded into one. `renderRunButton`
// works out its own "nothing to do" from the plan, so alerts arriving on their own
// schedule can call it without going through `renderTopBar`; `renderCounts` answers to the
// check queue, which finishes per series. Folding them together would make every one of
// those a full redraw of the bar.
//
// `seriesAlertCard` and `systemAlertCard` arrive as callbacks because they live in
// `alerts.js`, and sibling modules cannot import each other here. That is also why the
// entry has to build `createAlerts` before this.
export function createTopBar({ api, getSettings, getSnapshot, getSystemAlerts, getLibrary,
                               applySaved, applyHealth, applyAlerts, applySuppressed, forgetLibrary,
                               refresh, render, showResult, testMode, ruleFor, isBlocked,
                               worstSeverity, seriesAlertList, seriesAlertCard,
                               systemAlertCard, getStatus }) {
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

  // Everything on screen is answered from one reading, so its age is said once, here,
  // rather than repeated against every series.
  function syncedAgo() {
    const stamp = (getSnapshot().sync || {}).synced_at;
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
    if (!(getSettings().instances || []).length) return 'blocked';
    const statusAlerts = (getStatus && getStatus().alerts) || [];
    const seen = new Set();
    const system = getSystemAlerts().concat(statusAlerts)
      .filter((alert) => !seen.has(alert.key) && seen.add(alert.key));
    if (system.some((alert) => alert.blocking)) return 'blocked';
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
    const plan = getSnapshot().plan || { actionable: 0, trustworthy: false };
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
    const plan = getSnapshot().plan || { actionable: 0, trustworthy: false, unknown: 0 };
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

  function setBadge(badge, list) {
    if (!badge) return;
    const live = (list || []).filter((alert) => !alert.acknowledged);
    badge.hidden = live.length === 0;
    badge.textContent = live.length || '';
    badge.className = `tvr-tab-badge ${worstSeverity(live) || 'notice'}`;
  }

  // The sidebar carries the counts. A badge sits beside the thing it is about: a series
  // problem belongs to Series, and an installation problem belongs to Connections. There
  // is deliberately no second global total in the top bar.
  function renderCounts() {
    const rules = getSettings().rules || [];
    const connectedAlerts = seriesAlertList();
    const seen = new Set();
    const instances = getSystemAlerts().concat((getStatus && getStatus().alerts) || [])
      .filter((alert) => alert.kind !== 'run-aborted')
      .filter((alert) => !seen.has(alert.key) && seen.add(alert.key));

    $('tvr-count-connected').textContent = rules.length;
    $('tvr-count-all-series').textContent = getLibrary() ? getLibrary().length : rules.length;
    $('tvr-count-unconnected').textContent = getLibrary()
      ? getLibrary().filter((series) => !ruleFor(series)).length : 0;
    $('tvr-count-presets').textContent = (getSettings().profiles || []).length;

    setBadge($('tvr-badge-series-all'), connectedAlerts);
    setBadge($('tvr-badge-series-connected'), connectedAlerts);
    setBadge($('tvr-badge-series-unconnected'), []);
    setBadge($('tvr-badge-settings-connections'), instances);
    setBadge($('tvr-badge-system-status'), instances);
    const failed = (getSnapshot().runs || []).slice(0, 1).filter((run) => (run.errors || []).length);
    setBadge($('tvr-badge-system-history'), failed.map(() => ({ severity: 'warning' })));

  }

  // The one overview left: what is wrong, and where to go and fix it. Two things open it —
  // the count, and the Run button when something is stopping every run — because "why can
  // I not run?" and "what is wrong?" are the same question.
  function showEverythingNeedingAttention() {
    const everything = seriesAlertList().concat(getSystemAlerts(), (getStatus && getStatus().alerts) || []);
    dialog('Everything needing attention', (body) => {
      if (!everything.length) { body.append(el('p', { textContent: 'Nothing.' })); return {}; }
      const byRule = new Map();
      everything.forEach((alert) => {
        const key = alert.rule_id || 'system';
        byRule.set(key, (byRule.get(key) || []).concat([alert]));
      });
      byRule.forEach((list, key) => {
        const rule = (getSettings().rules || []).find((candidate) => candidate.id === key);
        body.append(rule ? seriesAlertCard(rule, list, { hideOpen: false })
                         : systemAlertCard('TV Retention', list));
      });
      return {};
    }, null, 'Close');
  }

  // Every one of these binds to a node `interface.html` already holds, so none of them can
  // run at import and none belong in the factory body. The entry calls this at start-up.
  function wire() {
    applyTheme(remembered('theme', 'auto'));
    $('tvr-theme').addEventListener('click', () => {
      const at = THEMES.findIndex(([value]) => value === remembered('theme', 'auto'));
      applyTheme(THEMES[(at + 1) % THEMES.length][0]);
    });

    $('tvr-changes-button').addEventListener('click', (event) => {
      event.stopPropagation();
      const menu = $('tvr-changes-menu');
      menu.hidden = !menu.hidden;
      $('tvr-changes-button').setAttribute('aria-expanded', String(!menu.hidden));
    });

    document.addEventListener('click', (event) => {
      if (!event.target.closest('#tvr-changes')) $('tvr-changes-menu').hidden = true;
    });

    // The one control that waits on Sonarr, and it says so. Everything else on this page is
    // answered from the stored reading, which is why nothing else makes you wait.
    $('tvr-refresh-all').addEventListener('click', () => guarded('Reading Sonarr…', async () => {
      const data = await api('sync', { reason: 'manual', force: true }, 'Reading Sonarr…');
      if (data.busy) {
        notice('A Sonarr read or retention run is already in progress.', 'ok');
        return;
      }
      applySaved(data);
      applyHealth(data.health);
      applyAlerts(data.alerts);
      applySuppressed(data.suppressed_alerts || []);
      getSnapshot().plan = data.plan;
      getSnapshot().sync = data.sync;
      getSnapshot().sync_due = !!data.sync_due;
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
      const runnable = (getSettings().rules || []).filter((rule) => rule.enabled && !isBlocked(rule.id));
      if (!runnable.length) throw new Error('There are no enabled series ready to run.');
      const plan = getSnapshot().plan || {};
      // The confirmation states the actual plan rather than describing runs in general.
      let warning = `Run ${plural(runnable.length, 'series')} now?\n\n`;
      const changes = changeSummary(plan).map((row) => `• ${row.text}`).join('\n');
      warning += plan.actionable ? `Scheduled changes:\n${changes}\n\n` : 'No changes are currently expected.\n\n';
      if (testMode()) {
        warning += 'Test mode is on, so this changes nothing: it reports exactly what it would '
          + 'have done and writes neither to your files nor to Sonarr.';
      } else {
        warning += 'This deletes episode files through Sonarr and cannot be undone from here.';
      }
      if (!window.confirm(warning)) return;
      const data = await api('run', {}, 'Running…');
      await refresh(true);
      showResult(data.result, 'Run');
    }));
  }

  return { renderTopBar, renderCounts, renderRunButton, syncedAgo,
           showEverythingNeedingAttention, wire };
}
