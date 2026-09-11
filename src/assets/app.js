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

import { $ } from './dom.js';
import { createApi, resetBusy } from './transport.js';
import { notice } from './feedback.js';
import { createActivity } from './activity.js';
import { createSettings } from './settings.js';
import { createChecks } from './checks.js';
import { createRemoval } from './series-removal.js';
import { createSeriesEditor } from './series-editor.js';
import { createAlerts } from './alerts.js';
import { createNavigation } from './navigation.js';
import { createTopBar } from './topbar.js';
import { createPresets } from './presets.js';
import { createConnections } from './connections.js';
import { createLibrary } from './library.js';

function start(root) {
  const api = createApi(root.dataset.api, root.dataset.csrf);

  let snapshot = null;      // last payload from the worker
  let settings = null;      // working copy, saved as a whole document
  let monitoring = {};      // rule id -> cached check result
  let alertsByRule = {};    // rule id -> that series' alerts
  let systemAlerts = [];

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
    renderLibrary: () => libraryView.renderLibrary(),
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
    getLibrary: () => libraryView.getLibrary(),
    applySaved,
    saveSettings,
    applyAlerts,
    conditionFields: (rule) => conditionFields(rule),
    presetSummary: (preset) => presetSummary(preset),
    posterNode: (series, className) => libraryView.posterNode(series, className),
    sonarrLink: (rule) => libraryView.sonarrLink(rule),
    seriesAlertCard: (rule, alerts, opts) => seriesAlertCard(rule, alerts, opts),
    seriesAlerts: (id) => seriesAlerts(id),
    queuedBanner: (rule) => queuedBanner(rule),
    queueChecks: (ids) => queueChecks(ids),
    deleteSeries: (rule) => deleteSeries(rule),
    forgetLibrary: () => libraryView.forgetLibrary(),
    render: () => render(),
    renderLibrary: () => libraryView.renderLibrary(),
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
      renderLibrary();
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
    renderLibrary: () => libraryView.renderLibrary(),
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
    getLibrary: () => libraryView.getLibrary(),
    applySaved,
    applyHealth,
    applyAlerts,
    forgetLibrary: () => libraryView.forgetLibrary(),
    refresh: () => refresh(),
    render: () => render(),
    showResult: (result, title) => showResult(result, title),
    testMode: () => testMode(),
    ruleFor: (series) => libraryView.ruleFor(series),
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
    forgetSeriesCache: () => libraryView.forgetSeriesCache(),
  });
  const { renderInstances, editInstance } = connections;

  // -- library -----------------------------------------------------------
  // The editor, navigation, alerts and top bar above all reach this module through
  // deferred callbacks. None invokes one while its factory is being constructed, so the
  // entry can assemble the honest two-way relationships without sibling imports.
  const libraryView = createLibrary({
    api,
    getSettings: () => settings,
    getMonitoring: () => monitoring,
    bulkChecking: () => checks.bulkChecking(),
    seriesAlerts: (id) => seriesAlerts(id),
    isChecking: (id) => isChecking(id),
    worstSeverity: (list) => worstSeverity(list),
    isBlocked: (id) => isBlocked(id),
    showSeriesAlerts: (rule) => showSeriesAlerts(rule),
    syncedAgo: () => syncedAgo(),
    queuedRemoval: (rule) => queuedRemoval(rule),
    getLibraryFilter: () => getLibraryFilter(),
    isLibraryView: () => isLibraryView(),
    isOpen: (rule, series) => isOpen(rule, series),
    closeEditor: () => closeEditor(),
    renderDetails: () => renderDetails(),
    openEditor: (rule, series) => openEditor(rule, series),
    presetSummary: (preset) => presetSummary(preset),
  });
  const { renderLibrary } = libraryView;

  function render() {
    $('tvr-version').textContent = snapshot.version ? `v${snapshot.version}` : '';
    const banner = ((settings || {}).alerts || {}).test_banner || 'full';
    $('tvr-test-banner').hidden = !testMode() || banner === 'chip';
    $('tvr-test-chip').hidden = !testMode() || banner !== 'chip';
    renderCounts();
    renderTopBar();
    renderLibrary();
    renderAlerts();
    renderPresets();
    renderInstances();
    renderSchedule();
    renderSettings();
    renderAlertSettings();
    renderHistory();
    renderAbout();
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
  libraryView.wire();

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
