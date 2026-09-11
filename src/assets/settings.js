// The settings views: schedule, notifications, automation, air-date providers, the
// alert preferences and the About panel. Everything reached from the Settings section
// that is not a Sonarr connection or a preset.
//
// Two contracts run through this file and must survive any later change. A save posts
// the *whole* settings document rather than the panel that was edited, so saving from
// one view keeps what another holds. And an API key arrives masked: echoing the mask
// back means "keep the stored key", so an unedited field must never be rewritten.
//
// `settings` and `snapshot` live in the entry and are replaced wholesale whenever a save
// or a refresh returns, so they are reached through accessors here. `saveSettings` also
// *writes* them, which a getter cannot do -- hence `applySaved`, the one hook the entry
// gives this module for putting a saved document back.

import { $, el, toggle, options } from './dom.js';
import { ago, plural, range } from './format.js';
import { notice, guarded } from './feedback.js';

function createSettings({ api, render, testMode, getSettings, getSnapshot, applySaved }) {
  // Read through a getter rather than captured once: every save replaces the document.
  const settings = () => getSettings();
  const snapshot = () => getSnapshot();
  // -- schedule ----------------------------------------------------------
  const WEEKDAYS = [[0, 'Sunday'], [1, 'Monday'], [2, 'Tuesday'], [3, 'Wednesday'],
                    [4, 'Thursday'], [5, 'Friday'], [6, 'Saturday']];

  function renderSchedule() {
    const schedule = settings().schedule || {};
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
    $('tvr-schedule-summary').textContent = snapshot().schedule_text || 'Off';

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
    // The schedule rides along as an override rather than being written into the local
    // document first: `collectSettings` spreads whatever `settings()` holds, so this is
    // the same posted payload without mutating state a failed save would leave changed.
    await saveSettings(null, true, { schedule: collectSchedule() });
    $('tvr-schedule-summary').textContent = snapshot().schedule_text || 'Off';
  }

  const NOTIFICATIONS = [
    ['run_started', 'A run has started'],
    ['run_completed', 'A run has finished'],
    ['series_removed', 'A series was removed from Sonarr'],
    ['series_ended', 'Sonarr reports a series has ended'],
    ['series_added', 'Sonarr has a new series TV Retention does not manage'],
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
  const airReady = (name) => name === 'tmdb' && !!((settings().tmdb || {}).api_key || '').trim();

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
    const retention = settings().retention || {};
    $('tvr-monitoring').value = retention.monitoring || 'unmonitor-only';
    const air = settings().air_dates || {};
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
    const automation = settings().automation || {};
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
    $('tvr-tmdb-key').value = (settings().tmdb || {}).api_key || '';
    $('tvr-history-size').value = settings().log_retention_runs;
    $('tvr-log-level').value = (settings().logging || {}).level || 'info';

    const box = $('tvr-notifications');
    box.replaceChildren();
    NOTIFICATIONS.forEach(([name, label]) => {
      const control = toggle(label, (settings().notifications || {})[name], null, { className: 'tvr-notify-row' });
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
    return Object.assign({}, settings(), {
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
      logging: Object.assign({}, settings().logging, { level: $('tvr-log-level').value }),
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
      health: settings().health,
      notifications,
    });
  }

  async function saveSettings(message, quiet, overrides) {
    const document_ = Object.assign(collectSettings(), overrides || {});
    const data = await api('settings', { settings: document_ }, 'Saving…', quiet);
    // The entry owns `settings` and `snapshot`; this is the one hook that writes them.
    applySaved(data);
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
  function renderAlertSettings() {
    const alerts = (settings() || {}).alerts || {};
    $('tvr-alert-header').value = alerts.header || 'all';
    $('tvr-alert-ack').checked = alerts.acknowledge !== false;
    $('tvr-test-banner-mode').value = alerts.test_banner || 'full';
    const box = $('tvr-alert-muted');
    box.replaceChildren(el('p', { className: 'tvr-lede',
                                  textContent: 'A kind hidden here is never shown and never notified '
                                               + 'about — including ones you have not seen yet. It still '
                                               + 'blocks a series if that is what it does.' }));
    MUTABLE_KINDS.forEach(([kind, label]) => {
      const control = toggle(label, (alerts.muted || []).includes(kind), null,
                             { className: 'tvr-row-switch' });
      control.input.dataset.kind = kind;
      mutedInputs[kind] = control.input;
      box.append(control.node);
    });
  }

  function renderAbout() {
    const box = $('tvr-about-state');
    if (!box) return;
    const sync = (snapshot().sync || {});
    const built = snapshot().build_date;
    const buildDate = built
      ? new Date(built).toLocaleDateString([], { dateStyle: 'medium' })
      : 'not recorded';
    const rows = [
      ['Version', snapshot().version || 'unknown'],
      ['Build', snapshot().build_number || 'not recorded'],
      ['Build date', buildDate],
      ['Series with a rule', plural((settings().rules || []).length, 'series')],
      ['Sonarr last read', sync.synced_at ? ago(sync.synced_at) : 'not yet'],
      ['Schedule', (settings().schedule || {}).enabled ? (snapshot().schedule_text || 'on') : 'off'],
      ['Test Mode', testMode() ? 'on — nothing writes, scheduled or manual' : 'off'],
      ['Storage', settings().state_dir || ''],
    ];
    box.replaceChildren(...rows.map(([name, value]) => el('div', { className: 'tvr-inline-row' }, [
      el('span', { className: 'tvr-inline-label', textContent: name }),
      el('span', { textContent: String(value) }),
    ])));
  }

  // Every listener this section owns, held back behind a call: a module that is not the
  // entry may not touch the document while it is being imported.
  function wire() {
    ['tvr-freq', 'tvr-monthly-mode'].forEach((id) => $(id).addEventListener('change', applyScheduleVisibility));

    ['tvr-schedule-enabled', 'tvr-test-mode', 'tvr-freq', 'tvr-minute', 'tvr-hour', 'tvr-weekday',
     'tvr-monthly-mode', 'tvr-monthly-day', 'tvr-monthly-weekday', 'tvr-cron'
    ].forEach((id) => $(id).addEventListener('change', () => guarded('', saveScheduleNow)));

    document.querySelectorAll('.tvr-view:has(.tvr-save)').forEach((view) => {
      view.addEventListener('change', (event) => {
        if (event.target.closest('#tvr-instances')) return;   // instance cards save themselves
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
  }

  return {
    renderSchedule, renderSettings, renderAlertSettings, renderAbout,
    renderAirProviders, saveSettings, collectSettings, wire,
  };
}

export { createSettings };
