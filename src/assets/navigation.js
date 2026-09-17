import { $ } from './dom.js';
import { guarded } from './feedback.js';
import { remember, remembered } from './storage.js';

// Navigation: which view is on screen, which sidebar section is open, and the two things
// that have to happen on the way in and out of one.
//
// The view list comes from the markup rather than from a constant here, so the two cannot
// disagree -- disagreeing is the failure that blanked four tabs. A remembered view name is
// checked against that list before it is honoured, because a name outlives the view it
// named. Rehomed views are resolved through an explicit legacy map so an upgrade never
// makes a section heading appear dead.
//
// The library's three sidebar items are one panel with a different filter, which is why
// `isLibraryView` and `getLibraryFilter` are exported: the editor asks the first whether a
// library view is already up, and the library list asks the second what to show. Leaving
// the library discards drafts, so the editor's three calls arrive as callbacks rather than
// as its state.
export function createNavigation({ getSettings, forgetDrafts, closeEditor, renderDetails,
                                   renderLibrary, renderStatsView, renderStatusView, renderBackupView,
                                   startLog, stopLog }) {
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

  // View names are persisted in the browser, so a renamed or rehomed page needs an
  // explicit landing place rather than silently falling back to the library.
  const LEGACY_VIEWS = {
    'media-stats': 'system-stats',
    'media-presets': 'series-presets',
    'media-automation': 'series-exclusions',
    'media-schedule': 'settings-schedule',
    'general-alerts': 'system-status',
    'general-connections': 'settings-connections',
    'general-air-dates': 'settings-air-dates',
    'general-safety': 'system-status',
    'general-logging': 'settings-general',
    'general-backup': 'system-backup',
  };

  // -- sections ----------------------------------------------------------
  // One open at a time. Purpose-built groups keep policy, configuration, and runtime
  // state separate without making every tab a top-level destination.
  const sectionOf = (view) => {
    const button = document.querySelector(`.tvr-side [data-view="${view}"]`);
    return button ? button.closest('[data-section]').dataset.section : null;
  };

  // Where a section opens when you have never been in it. Series is the exception: with
  // nothing connected yet, "All" is the only list with anything in it.
  function sectionDefault(section) {
    if (section === 'series') {
      return (getSettings().rules || []).length ? 'series-connected' : 'series-all';
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

  function showView(name) {
    name = LEGACY_VIEWS[name] || name;
    if (!VIEWS.includes(name)) name = 'series-all';
    // Unsaved edits belong to the library. Leaving it closes the pane, and a draft kept
    // past that would be a second copy of the settings, invisible until it reappeared
    // over whatever the rule had become in the meantime.
    if (!LIBRARY[name] && LIBRARY[currentView]) { forgetDrafts(); closeEditor(); renderDetails(); }
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
    if (name === 'system-stats') guarded('', renderStatsView);
    if (name === 'system-status') guarded('', renderStatusView);
    if (name === 'system-backup') guarded('', renderBackupView);
    if (name === 'system-logs') startLog(); else stopLog();
  }

  // Both listeners bind to sidebar nodes the markup already holds, so they cannot run at
  // import. The entry calls this at start-up.
  function wire() {
    document.querySelectorAll('.tvr-side [data-section-head]').forEach((head) => {
      head.addEventListener('click', () => {
        const section = head.dataset.sectionHead;
        // Help is an entry point rather than a workspace: its heading always opens the
        // overview, while the other sections return to the tool the operator was using.
        if (section === 'help') { showView('help-about'); return; }
        // Clicking the section you are already in collapses nothing: there would be no open
        // section and no view to show. It just returns you to where you were.
        //
        // Where you were is in the browser, and it outlives the view it names. The legacy map
        // resolves rehomed pages before the current view list is checked.
        const last = remembered(`last.${section}`, '');
        showView(VIEWS.includes(last) ? last : sectionDefault(section));
      });
    });
    document.querySelectorAll('.tvr-side [data-view]').forEach((button) => {
      button.addEventListener('click', () => showView(button.dataset.view));
    });
  }

  return { showView, isLibraryView: () => !!LIBRARY[currentView],
           getLibraryFilter: () => libraryFilter, wire };
}
