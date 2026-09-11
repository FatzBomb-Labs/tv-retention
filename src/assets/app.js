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

/* The entry module. It is loaded as type="module" from the release namespace, so the
 * graph behind it is one version of the interface. Module evaluation defines things and
 * nothing else — no DOM, timers, storage or network until start() runs — because
 * imports evaluate before this body could guard any of it. The root lookup at the
 * bottom is the one exception, and it belongs to this file alone.
 */
'use strict';

import { $, el, text, toggle, field, options } from './dom.js';
import { bytes, when, plural, ago } from './format.js';
import { exclusionTree, monitorTree } from './episode-trees.js';
import { changeSummary, changeLines, changeRows, changeList } from './changes.js';
import { createApi, resetBusy } from './transport.js';
import { notice, guarded, dialog } from './feedback.js';
import { createActivity } from './activity.js';
import { createSettings } from './settings.js';
import { createChecks } from './checks.js';
import { createRemoval } from './series-removal.js';
import { createSeriesEditor } from './series-editor.js';
import { createAlerts } from './alerts.js';
import { createNavigation } from './navigation.js';
import { createTopBar } from './topbar.js';
import { remember, remembered } from './storage.js';
import { createPresets } from './presets.js';
import { createConnections } from './connections.js';

function start(root) {
  const api = createApi(root.dataset.api, root.dataset.csrf);

  let snapshot = null;      // last payload from the worker
  let settings = null;      // working copy, saved as a whole document
  let monitoring = {};      // rule id -> cached check result
  let alertsByRule = {};    // rule id -> that series' alerts
  let systemAlerts = [];
  let seriesCache = {};

  // The System section's panes. `snapshot` and `settings` are handed over as accessors:
  // both are replaced wholesale whenever a refresh or a save returns, so a value captured
  // here would pin the panes to whichever document was current when the page loaded.
  const activity = createActivity({
    api,
    refresh: () => refresh(),
    getSnapshot: () => snapshot,
    getSettings: () => settings,
  });
  const { renderStatsView, showResult, renderHistory, startLog, stopLog } = activity;

  // The settings views. Same accessor reasoning, plus a setter: a save returns a fresh
  // document and the module has to be able to put it back, which a getter cannot do.
  const settingsView = createSettings({
    api,
    render: () => render(),
    testMode: () => testMode(),
    getSnapshot: () => snapshot,
    getSettings: () => settings,
    applySaved,
  });
  const { renderSchedule, renderSettings, renderAlertSettings, renderAbout,
          saveSettings } = settingsView;

  // -- snapshot and background checking ----------------------------------
  // The queue, the poll and the heartbeat. `monitoring` is passed as an accessor for the
  // same reason as `snapshot`: `applyHealth` replaces it outright on every reading, so a
  // captured value would leave the checks writing into an object nothing else can see.
  const checks = createChecks({
    api,
    getSnapshot: () => snapshot,
    getMonitoring: () => monitoring,
    applyHealth,
    applyAlerts,
    render: () => render(),
    renderRules: () => renderRules(),
    renderAlerts: () => renderAlerts(),
    renderCounts: () => renderCounts(),
  });
  const { isChecking, queueChecks, startPolling } = checks;

  // -- queued removal ----------------------------------------------------
  // `settings` is reassigned wholesale every time a document comes back, so the module
  // reads it through the accessor rather than holding the object it was built with.
  const { queuedRemoval, queuedBanner, deleteSeries } = createRemoval({
    api,
    getSettings: () => settings,
    saveSettings,
    renderDetails: () => renderDetails(),
  });

  // -- the series editor -------------------------------------------------
  // Navigation is mutual: the pane needs a library view on screen, and leaving the library
  // discards drafts. Rather than hand the editor `showView` and the view tables, the entry
  // brokers the one intent behind them, which is why the two modules have no edge.
  const editor = createSeriesEditor({
    api,
    getSettings: () => settings,
    getSnapshot: () => snapshot,
    getMonitoring: () => monitoring,
    getLibrary: () => library,
    applySaved,
    saveSettings,
    applyAlerts,
    conditionFields: (rule) => conditionFields(rule),
    presetSummary: (preset) => presetSummary(preset),
    posterNode: (series, className) => posterNode(series, className),
    sonarrLink: (rule) => sonarrLink(rule),
    seriesAlertCard: (rule, alerts, opts) => seriesAlertCard(rule, alerts, opts),
    seriesAlerts: (id) => seriesAlerts(id),
    queuedBanner: (rule) => queuedBanner(rule),
    queueChecks: (ids) => queueChecks(ids),
    deleteSeries: (rule) => deleteSeries(rule),
    forgetLibrary: () => forgetLibrary(),
    render: () => render(),
    renderRules: () => renderRules(),
    renderLibrary: () => renderLibrary(),
    openLibraryView: () => { if (!isLibraryView()) showView('series-all'); },
  });
  const { isOpen, openEditor, renderDetails, forgetDrafts } = editor;
  // Closing the pane is the only thing outside the editor does to its state.
  const closeEditor = () => editor.setEditing(null);

  // -- alerts ------------------------------------------------------------
  // The two navigation callbacks are intents, not views: a card wants this series found in
  // the library, or this instance's settings open. Naming them that way keeps the alerts
  // module off `showView` entirely, so the navigation module has nothing here to honour.
  //
  // This has to be built before `createTopBar`: `showEverythingNeedingAttention` calls both
  // alert cards, and sibling modules cannot import each other, so the entry brokers them —
  // which only works if the cards exist by then.
  const alerts = createAlerts({
    api,
    getSettings: () => settings,
    getSnapshot: () => snapshot,
    getMonitoring: () => monitoring,
    getSystemAlerts: () => systemAlerts,
    applySaved,
    applyAlerts,
    seriesAlerts: (id) => seriesAlerts(id),
    isBlocked: (id) => isBlocked(id),
    worstSeverity: (list) => worstSeverity(list),
    queueChecks: (ids, force) => queueChecks(ids, force),
    render: () => render(),
    showSeriesInLibrary: (rule) => {
      showView('series-all');
      $('tvr-search').value = rule.series_title || rule.path;
      renderRules();
    },
    openInstance: (instanceId) => {
      showView('settings-connections');
      const instance = (settings.instances || []).find((i) => i.id === instanceId);
      if (instance) editInstance(instance);
    },
  });
  const { seriesAlertCard, systemAlertCard, showSeriesAlerts, renderAlerts } = alerts;

  // -- navigation --------------------------------------------------------
  // Built after the editor and the alerts, because both reach navigation and neither is
  // reachable from it: the editor's `openLibraryView` and the alerts' two intents are
  // callbacks the entry brokers, so nothing above has to exist when this is constructed.
  const navigation = createNavigation({
    getSettings: () => settings,
    forgetDrafts: () => forgetDrafts(),
    closeEditor: () => closeEditor(),
    renderDetails: () => renderDetails(),
    renderLibrary: () => renderLibrary(),
    renderStatsView: () => renderStatsView(),
    startLog: () => startLog(),
    stopLog: () => stopLog(),
  });
  const { showView, isLibraryView, getLibraryFilter } = navigation;

  // The one way a saved document gets back into the entry's state. `settings.js` posts the
  // whole document and needs to write both bindings; handing it a setter keeps the entry
  // the only place they are assigned.
  function applySaved(data) {
    settings = data.settings;
    snapshot.settings = settings;
    snapshot.schedule_text = data.schedule_text || snapshot.schedule_text;
  }

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

  // -- top bar -----------------------------------------------------------
  // Built after the alerts, which own the two cards `showEverythingNeedingAttention`
  // stacks, and after the severity helpers just above, which it reads for the counts.
  const topbar = createTopBar({
    api,
    getSettings: () => settings,
    getSnapshot: () => snapshot,
    getSystemAlerts: () => systemAlerts,
    getLibrary: () => library,
    applySaved,
    applyHealth,
    applyAlerts,
    forgetLibrary: () => forgetLibrary(),
    refresh: () => refresh(),
    render: () => render(),
    showResult: (result, title) => showResult(result, title),
    testMode: () => testMode(),
    ruleFor: (id) => ruleFor(id),
    isBlocked: (id) => isBlocked(id),
    worstSeverity: (list) => worstSeverity(list),
    seriesAlertList: () => seriesAlertList(),
    seriesAlertCard: (rule, list, options) => seriesAlertCard(rule, list, options),
    systemAlertCard: (title, list) => systemAlertCard(title, list),
  });
  const { renderTopBar, renderCounts, renderRunButton, syncedAgo,
          showEverythingNeedingAttention } = topbar;

  // -- presets -----------------------------------------------------------
  // Built here rather than beside the other panels because the editor above already
  // brokers two of its exports, and a preset knows nothing about a series.
  const presets = createPresets({ getSettings: () => settings, saveSettings });
  const { renderPresets, presetSummary, conditionFields } = presets;

  // -- Sonarr instances --------------------------------------------------
  // The alerts' `openInstance` intent reaches `editInstance`, but only from inside a
  // callback, so it is bound long before anything can call it.
  const connections = createConnections({
    api,
    getSettings: () => settings,
    getSnapshot: () => snapshot,
    saveSettings,
    forgetSeriesCache: () => { seriesCache = {}; },
  });
  const { renderInstances, editInstance } = connections;

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
    if (isLibraryView()) loadLibrary().catch(() => { libraryLoading = false; });
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
        if (getLibraryFilter() === 'connected' && !rule) return;
        if (getLibraryFilter() === 'unconnected' && rule) return;
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
    if (checks.bulkChecking()) {
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
      if (isOpen(rule)) { closeEditor(); renderLibrary(); renderDetails(); return; }
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


  // -- start -------------------------------------------------------------
  activity.wire();
  settingsView.wire();
  checks.wire();
  alerts.wire();
  navigation.wire();
  topbar.wire();
  presets.wire();
  connections.wire();

  // Whatever happens, the page must end up interactive with a readable message.
  refresh().catch((error) => {
    resetBusy();
    notice(`TV Retention could not load: ${error.message}`, 'bad');
  });
  window.addEventListener('error', resetBusy);
  window.addEventListener('unhandledrejection', resetBusy);
}

// The entry point: find the page this module belongs to, and only then run it. On any
// other page — or under a test harness with no page at all — importing stays silent.
const root = document.getElementById('tv-retention');
if (root) start(root);
