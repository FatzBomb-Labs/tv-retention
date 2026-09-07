/* TV Delete web UI.
 *
 * The browser holds no authority: it renders whatever the worker reports and posts
 * whole settings documents back for re-validation. API keys are never sent to the page;
 * a mask is shown instead, and echoing the mask back means "keep the stored key".
 */
(function () {
  'use strict';

  const root = document.getElementById('tv-delete');
  if (!root) return;
  const API = root.dataset.api;
  const CSRF = root.dataset.csrf;

  let snapshot = null;      // last payload from the worker
  let settings = null;      // working copy, saved as a whole document
  let seriesCache = {};     // instance id -> Sonarr series list

  const $ = (id) => document.getElementById(id);
  const el = (tag, props, children) => {
    const node = Object.assign(document.createElement(tag), props || {});
    (children || []).forEach((child) => node.append(child));
    return node;
  };

  // -- transport ---------------------------------------------------------
  let busyDepth = 0;
  function busy(on, text) {
    busyDepth = Math.max(0, busyDepth + (on ? 1 : -1));
    $('tvd-busy').hidden = busyDepth === 0;
    if (on && text) $('tvd-busy-text').textContent = text;
  }

  // A request must always settle. Without this, one stalled call leaves the busy overlay
  // covering the page with no way to dismiss it and nothing on screen explaining why.
  const TIMEOUTS = { run: 3600000, preview: 900000, series: 120000, 'test-instance': 90000,
                     'detect-mappings': 90000, match: 300000, 'test-tmdb': 60000 };
  const DEFAULT_TIMEOUT = 60000;

  async function api(action, payload, label) {
    busy(true, label);
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
      busy(false);
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

  // -- tabs --------------------------------------------------------------
  document.querySelectorAll('.tvd-tabs button').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('.tvd-tabs button').forEach((other) => other.classList.toggle('active', other === button));
      ['shows', 'presets', 'sonarr', 'settings', 'history', 'help'].forEach((name) => {
        $(`tvd-panel-${name}`).hidden = name !== button.dataset.tab;
      });
    });
  });

  // -- dialog ------------------------------------------------------------
  function dialog(title, buildBody, onOk, okLabel) {
    const box = $('tvd-dialog');
    const body = $('tvd-dialog-body');
    body.replaceChildren(el('h3', { textContent: title }));
    const context = buildBody(body);
    $('tvd-dialog-ok').textContent = okLabel || 'Save';
    $('tvd-dialog-ok').hidden = !onOk;
    const handler = async (event) => {
      box.removeEventListener('close', handler);
      if (box.returnValue !== 'ok' || !onOk) return;
      await guarded('', () => onOk(context));
    };
    box.addEventListener('close', handler);
    box.showModal();
  }

  function field(label, control, hint) {
    const wrapper = el('label', { className: 'tvd-field' }, [el('span', { textContent: label }), control]);
    if (hint) wrapper.append(el('small', { textContent: hint }));
    return wrapper;
  }

  function checkbox(label, checked, hint) {
    const input = el('input', { type: 'checkbox', checked: !!checked });
    const wrapper = el('label', { className: 'tvd-check' }, [input, document.createTextNode(' ' + label)]);
    if (hint) wrapper.append(el('small', { textContent: hint }));
    return { input, node: wrapper };
  }

  // -- snapshot ----------------------------------------------------------
  async function refresh() {
    snapshot = await api('snapshot', {}, 'Loading…');
    settings = snapshot.settings;
    render();
  }

  function render() {
    $('tvd-version').textContent = snapshot.version || '';
    $('tvd-array').hidden = !!snapshot.array_ready;
    $('tvd-dry').hidden = !settings.dry_run;
    renderStats();
    renderRules();
    renderPresets();
    renderInstances();
    renderSettings();
    renderHistory();
  }

  function renderStats() {
    const rules = settings.rules || [];
    const unmatched = rules.filter((rule) => rule.match_status !== 'matched');
    $('tvd-stat-rules').textContent = rules.length;
    $('tvd-stat-rules-sub').textContent = `${rules.filter((r) => r.enabled).length} enabled`;
    $('tvd-stat-unmatched').textContent = unmatched.length;
    const last = (snapshot.runs || [])[0];
    $('tvd-stat-last').textContent = last ? when(last.started) : 'Never';
    $('tvd-stat-last-sub').textContent = last
      ? `${last.dry_run ? 'would delete' : 'deleted'} ${last.dry_run ? last.planned : last.deleted} files`
      : ' ';
    const schedule = settings.schedule || {};
    $('tvd-stat-schedule').textContent = schedule.enabled ? 'On' : 'Off';
    $('tvd-stat-schedule-sub').textContent = schedule.enabled ? schedule.cron : 'Manual runs only';
  }

  // -- rules -------------------------------------------------------------
  function presetFor(rule) {
    return (settings.profiles || []).find((preset) => preset.id === rule.profile_id) || null;
  }

  function ruleSummary(rule) {
    const preset = presetFor(rule);
    const source = preset || rule;
    const parts = [];
    if (source.keep_days) parts.push(`keep ${plural(source.keep_days, 'day')}`);
    if (source.keep_episodes) parts.push(`keep ${plural(source.keep_episodes, 'episode')}`);
    if (source.keep_seasons) parts.push(`keep ${plural(source.keep_seasons, 'season')}`);
    return parts;
  }

  function renderRules() {
    const container = $('tvd-rules');
    const term = ($('tvd-search').value || '').toLowerCase();
    const filter = $('tvd-filter').value;
    const rules = (settings.rules || []).filter((rule) => {
      if (term && !(`${rule.series_title} ${rule.path}`.toLowerCase().includes(term))) return false;
      if (filter === 'enabled') return rule.enabled;
      if (filter === 'disabled') return !rule.enabled;
      if (filter === 'unmatched') return rule.match_status !== 'matched';
      return true;
    });
    container.replaceChildren();
    $('tvd-rules-empty').hidden = (settings.rules || []).length > 0;
    rules.forEach((rule) => {
      const matched = rule.match_status === 'matched';
      const card = el('div', { className: `tvd-rule ${matched ? 'matched' : 'unmatched'}${rule.enabled ? '' : ' disabled'}` });
      const instance = (settings.instances || []).find((i) => i.id === rule.instance_id);
      card.append(el('div', { className: 'tvd-rule-head' }, [
        el('span', { className: 'tvd-rule-title', textContent: rule.series_title || '(unmatched folder)' }),
        el('span', { className: `tvd-badge ${matched ? 'ok' : 'bad'}`, textContent: matched ? 'matched' : 'not matched' }),
        rule.enabled ? el('span', { className: 'tvd-badge off', textContent: 'enabled' })
                     : el('span', { className: 'tvd-badge off', textContent: 'disabled' }),
        el('span', { className: 'tvd-chip', textContent: instance ? instance.name : 'unknown instance' }),
      ]));
      card.append(el('div', { className: 'tvd-rule-path', textContent: rule.path }));
      const body = el('div', { className: 'tvd-rule-body' });
      ruleSummary(rule).forEach((text) => body.append(el('span', { className: 'tvd-chip on', textContent: text })));
      const preset = presetFor(rule);
      if (preset) body.prepend(el('span', { className: 'tvd-chip on', textContent: `preset: ${preset.name}` }));
      body.append(el('span', { className: 'tvd-chip', textContent: `combine: ${(preset || rule).combine}` }));
      if (rule.unmonitor) body.append(el('span', { className: 'tvd-chip', textContent: 'unmonitor' }));

      const actions = el('div', { className: 'tvd-rule-actions' });
      const previewButton = el('button', { type: 'button', textContent: 'Preview' });
      previewButton.addEventListener('click', () => guarded('', async () => {
        const data = await api('preview', { rule_ids: [rule.id] }, 'Previewing…');
        showResult(data.result, 'Preview');
      }));
      const editButton = el('button', { type: 'button', textContent: 'Edit' });
      editButton.addEventListener('click', () => editRule(rule));
      const removeButton = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Remove' });
      removeButton.addEventListener('click', () => guarded('', async () => {
        if (!window.confirm(`Remove the rule for ${rule.series_title || rule.path}? No files are deleted.`)) return;
        settings.rules = settings.rules.filter((other) => other.id !== rule.id);
        await saveSettings('Rule removed.');
      }));
      actions.append(previewButton, editButton, removeButton);
      body.append(actions);
      card.append(body);
      if (!matched && rule.match_error) card.append(el('div', { className: 'tvd-error', textContent: rule.match_error }));
      container.append(card);
    });
  }

  $('tvd-search').addEventListener('input', renderRules);
  $('tvd-filter').addEventListener('change', renderRules);

  // -- retention presets -------------------------------------------------
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
      const card = el('div', { className: 'tvd-rule matched' });
      card.append(el('div', { className: 'tvd-rule-head' }, [
        el('span', { className: 'tvd-rule-title', textContent: preset.name }),
        el('span', { className: 'tvd-chip', textContent: `used by ${plural(users.length, 'show')}` }),
      ]));
      const body = el('div', { className: 'tvd-rule-body' });
      presetSummary(preset).forEach((text) => body.append(el('span', { className: 'tvd-chip on', textContent: text })));
      body.append(el('span', { className: 'tvd-chip', textContent: `combine: ${preset.combine}` }));
      const actions = el('div', { className: 'tvd-rule-actions' });
      const editButton = el('button', { type: 'button', textContent: 'Edit' });
      editButton.addEventListener('click', () => editPreset(preset));
      const removeButton = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Remove' });
      removeButton.addEventListener('click', () => guarded('', async () => {
        if (users.length) throw new Error(`${plural(users.length, 'show')} still use "${preset.name}". Point them elsewhere first.`);
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
    const combine = el('select');
    [['earliest', 'Earliest — keep if any condition keeps it (safest)'],
     ['latest', 'Latest — delete only if every condition agrees'],
     ['any', 'Any — delete if any condition says so (most aggressive)']]
      .forEach(([value, label]) => combine.append(el('option', { value, textContent: label })));
    combine.value = source.combine || 'earliest';
    const node = el('div', {}, [
      el('div', { className: 'tvd-row' }, [field('Keep days', days), field('Keep episodes', episodes), field('Keep seasons', seasons)]),
      el('small', { textContent: 'Leave a box empty to switch that condition off. At least one is required.' }),
      field('Combine conditions', combine),
    ]);
    return { days, episodes, seasons, combine, node };
  }

  function editPreset(existing) {
    const preset = Object.assign({ id: '', name: '', keep_days: '', keep_episodes: '', keep_seasons: '', combine: 'earliest' }, existing || {});
    dialog(existing ? 'Edit preset' : 'Add preset', (body) => {
      const name = el('input', { type: 'text', value: preset.name, placeholder: 'Keep 30 days' });
      const conditions = conditionFields(preset);
      const users = (settings.rules || []).filter((rule) => rule.profile_id === preset.id);
      body.append(field('Preset name', name), conditions.node);
      if (users.length) {
        body.append(el('p', { textContent: `${plural(users.length, 'show')} use this preset and will change with it:` }));
        users.forEach((rule) => body.append(el('div', { className: 'tvd-mono', textContent: rule.series_title || rule.path })));
        if ((settings.retention || {}).remonitor_widened) {
          body.append(el('div', { className: 'tvd-banner', textContent: 'Re-monitoring is on: widening this preset will put previously removed episodes back on Sonarr’s wanted list at the next run.' }));
        }
      }
      return { name, conditions };
    }, async (context) => {
      const draft = {
        id: preset.id || undefined,
        name: context.name.value,
        keep_days: context.conditions.days.value || null,
        keep_episodes: context.conditions.episodes.value || null,
        keep_seasons: context.conditions.seasons.value || null,
        combine: context.conditions.combine.value,
      };
      settings.profiles = (settings.profiles || []).filter((other) => other.id !== preset.id).concat([draft]);
      await saveSettings('Preset saved.');
    });
  }

  $('tvd-add-preset').addEventListener('click', () => editPreset(null));

  // -- rule editor -------------------------------------------------------
  async function seriesFor(instanceId) {
    if (!seriesCache[instanceId]) {
      const data = await api('series', { instance_id: instanceId }, 'Loading series from Sonarr…');
      seriesCache[instanceId] = data.series;
    }
    return seriesCache[instanceId];
  }

  function browseFolder(startPath, onPick) {
    let current = startPath || '/mnt/user';
    dialog('Choose a folder', (body) => {
      const crumb = el('div', { className: 'tvd-mono' });
      const list = el('div', { className: 'tvd-instances' });
      const useButton = el('button', { type: 'button', className: 'tvd-primary', textContent: 'Use this folder' });
      useButton.addEventListener('click', (event) => { event.preventDefault(); $('tvd-dialog').close('cancel'); onPick(current); });
      body.append(crumb, list, useButton);
      const load = (path) => guarded('', async () => {
        const data = await api('browse', { path }, 'Reading folder…');
        current = data.path;
        crumb.textContent = data.path;
        list.replaceChildren();
        if (data.parent) {
          const up = el('button', { type: 'button', textContent: '⬑ up one level' });
          up.addEventListener('click', (event) => { event.preventDefault(); load(data.parent); });
          list.append(up);
        }
        data.entries.forEach((entry) => {
          const button = el('button', { type: 'button', textContent: `📁 ${entry.name}` });
          button.addEventListener('click', (event) => { event.preventDefault(); load(entry.path); });
          list.append(button);
        });
        if (!data.entries.length) list.append(el('p', { className: 'tvd-empty', textContent: 'No sub-folders here.' }));
      });
      load(current);
      return {};
    }, null, 'Close');
  }

  function editRule(existing) {
    const rule = Object.assign({
      id: '', enabled: true, instance_id: (settings.instances[0] || {}).id || '',
      series_id: null, series_title: '', tvdb_id: null, path: '',
      profile_id: existing ? '' : ((settings.profiles || [])[0] || {}).id || '',
      keep_days: '', keep_episodes: '', keep_seasons: '', combine: 'earliest', unmonitor: true,
    }, existing || {});

    if (!settings.instances.length) {
      notice('Add a Sonarr instance first — every rule must be bound to a Sonarr series.', 'bad');
      return;
    }

    dialog(existing ? 'Edit rule' : 'Add show', (body) => {
      const instanceSelect = el('select');
      settings.instances.forEach((instance) => instanceSelect.append(el('option', { value: instance.id, textContent: instance.name })));
      instanceSelect.value = rule.instance_id;

      const modeSelect = el('select');
      modeSelect.append(el('option', { value: 'series', textContent: 'Pick the series from Sonarr' }));
      modeSelect.append(el('option', { value: 'folder', textContent: 'Pick a folder, then match it to Sonarr' }));

      const seriesSelect = el('select');
      const seriesField = field('Sonarr series', seriesSelect, 'Only series this Sonarr instance manages.');
      const pathInput = el('input', { type: 'text', value: rule.path, spellcheck: false, placeholder: '/mnt/user/media/TV/…' });
      const browseButton = el('button', { type: 'button', textContent: 'Browse…' });
      browseButton.addEventListener('click', (event) => {
        event.preventDefault();
        browseFolder(pathInput.value || '/mnt/user', (picked) => { pathInput.value = picked; });
      });
      const pathRow = el('div', { className: 'tvd-row' }, [field('Series folder', pathInput), browseButton]);
      const pathField = el('div', {}, [pathRow, el('small', { textContent: 'The folder must belong to a Sonarr series on the selected instance, or the rule is saved unmatched and skipped.' })]);

      const loadSeries = () => guarded('', async () => {
        const catalogue = await seriesFor(instanceSelect.value);
        seriesSelect.replaceChildren(el('option', { value: '', textContent: '— choose a series —' }));
        catalogue.forEach((entry) => {
          const suffix = entry.exists ? '' : '  (folder not found on this server)';
          seriesSelect.append(el('option', {
            value: String(entry.series_id),
            textContent: `${entry.title}${entry.year ? ` (${entry.year})` : ''}${suffix}`,
          }));
        });
        if (rule.series_id) seriesSelect.value = String(rule.series_id);
      });

      const applyMode = () => {
        const mode = modeSelect.value;
        seriesField.hidden = mode !== 'series';
        pathField.hidden = mode !== 'folder';
        if (mode === 'series') loadSeries();
      };
      modeSelect.addEventListener('change', applyMode);
      instanceSelect.addEventListener('change', () => { seriesCache = {}; applyMode(); });

      const presetSelect = el('select');
      (settings.profiles || []).forEach((preset) => {
        presetSelect.append(el('option', { value: preset.id, textContent: `${preset.name} — ${presetSummary(preset).join(', ')}` }));
      });
      presetSelect.append(el('option', { value: '', textContent: 'Custom — set the values on this show only' }));
      presetSelect.value = rule.profile_id || '';
      const conditions = conditionFields(rule);
      const applyPreset = () => { conditions.node.hidden = !!presetSelect.value; };
      presetSelect.addEventListener('change', applyPreset);
      applyPreset();

      const enabled = checkbox('Rule is enabled', rule.enabled);
      const unmonitor = checkbox('Unmonitor these episodes in Sonarr after deleting', rule.unmonitor);

      body.append(
        field('Sonarr instance', instanceSelect),
        field('How to identify the show', modeSelect),
        seriesField, pathField,
        field('Retention', presetSelect, (settings.profiles || []).length
          ? 'Presets are managed on the Retention presets tab.'
          : 'No presets yet — create one there to reuse the same values across shows.'),
        conditions.node,
        enabled.node, unmonitor.node,
      );
      modeSelect.value = existing && existing.series_id ? 'series' : (existing ? 'folder' : 'series');
      applyMode();

      return { instanceSelect, modeSelect, seriesSelect, pathInput, presetSelect, conditions, enabled, unmonitor };
    }, async (context) => {
      const draft = {
        id: rule.id || undefined,
        enabled: context.enabled.input.checked,
        instance_id: context.instanceSelect.value,
        profile_id: context.presetSelect.value || '',
        keep_days: context.presetSelect.value ? null : (context.conditions.days.value || null),
        keep_episodes: context.presetSelect.value ? null : (context.conditions.episodes.value || null),
        keep_seasons: context.presetSelect.value ? null : (context.conditions.seasons.value || null),
        combine: context.conditions.combine.value,
        unmonitor: context.unmonitor.input.checked,
      };
      if (context.modeSelect.value === 'series') {
        const catalogue = await seriesFor(context.instanceSelect.value);
        const chosen = catalogue.find((entry) => String(entry.series_id) === context.seriesSelect.value);
        if (!chosen) throw new Error('Choose a series from the list.');
        if (!chosen.path) throw new Error(`${chosen.title} has no folder in Sonarr.`);
        Object.assign(draft, { series_id: chosen.series_id, series_title: chosen.title, tvdb_id: chosen.tvdb_id, path: chosen.path });
      } else {
        if (!context.pathInput.value.trim()) throw new Error('Choose a folder.');
        Object.assign(draft, { series_id: null, series_title: '', tvdb_id: null, path: context.pathInput.value.trim() });
      }
      settings.rules = (settings.rules || []).filter((other) => other.id !== rule.id).concat([draft]);
      await saveSettings(null);
      const matchReport = await api('match', {}, 'Matching against Sonarr…');
      settings = matchReport.settings;
      snapshot.settings = settings;
      render();
      const saved = settings.rules[settings.rules.length - 1];
      if (saved && saved.match_status !== 'matched') notice(`Saved, but not matched: ${saved.match_error}`, 'bad');
      else notice('Rule saved and matched to Sonarr.', 'ok');
    });
  }

  $('tvd-add').addEventListener('click', () => editRule(null));
  $('tvd-rematch').addEventListener('click', () => guarded('', async () => {
    const data = await api('match', {}, 'Re-checking Sonarr…');
    settings = data.settings;
    snapshot.settings = settings;
    render();
    const failed = data.report.filter((entry) => !entry.ok).length;
    notice(failed ? `${failed} rule(s) could not be matched; they will be skipped.` : 'All rules matched.', failed ? 'bad' : 'ok');
  }));

  // -- instances ---------------------------------------------------------
  function renderInstances() {
    const container = $('tvd-instances');
    container.replaceChildren();
    (settings.instances || []).forEach((instance) => {
      const card = el('div', { className: 'tvd-rule matched' });
      card.append(el('div', { className: 'tvd-rule-head' }, [
        el('span', { className: 'tvd-rule-title', textContent: instance.name }),
        el('span', { className: `tvd-badge ${instance.enabled ? 'ok' : 'off'}`, textContent: instance.enabled ? 'enabled' : 'disabled' }),
        el('span', { className: 'tvd-chip', textContent: instance.url }),
      ]));
      const mappings = (instance.path_maps || []).map((entry) => `${entry.from} → ${entry.to}`).join('   ');
      card.append(el('div', { className: 'tvd-rule-path', textContent: mappings || 'No path mapping (Sonarr paths are used as-is)' }));
      const body = el('div', { className: 'tvd-rule-body' });
      const actions = el('div', { className: 'tvd-rule-actions' });
      const testButton = el('button', { type: 'button', textContent: 'Test' });
      testButton.addEventListener('click', () => guarded('', async () => {
        const data = await api('test-instance', { instance }, 'Contacting Sonarr…');
        showInstanceTest(instance, data);
      }));
      const editButton = el('button', { type: 'button', textContent: 'Edit' });
      editButton.addEventListener('click', () => editInstance(instance));
      const removeButton = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Remove' });
      removeButton.addEventListener('click', () => guarded('', async () => {
        const used = (settings.rules || []).filter((rule) => rule.instance_id === instance.id);
        if (used.length) throw new Error(`${used.length} rule(s) still use ${instance.name}. Remove or move them first.`);
        if (!window.confirm(`Remove the Sonarr instance ${instance.name}?`)) return;
        settings.instances = settings.instances.filter((other) => other.id !== instance.id);
        await saveSettings('Instance removed.');
      }));
      actions.append(testButton, editButton, removeButton);
      body.append(actions);
      card.append(body);
      container.append(card);
    });
  }

  function showInstanceTest(instance, data) {
    dialog(`${instance.name}: connection test`, (body) => {
      body.append(el('p', { textContent: `Sonarr ${data.sonarr_version} answered, managing ${data.series_count} series.` }));
      body.append(el('p', { textContent: `${data.folders_found} of ${data.series_count} series folders were found on this server.` }));
      if (data.folders_missing.length) {
        body.append(el('p', { className: 'tvd-error', textContent: 'These mapped folders do not exist here — the path mapping is probably wrong:' }));
        data.folders_missing.forEach((path) => body.append(el('div', { className: 'tvd-mono', textContent: path })));
      }
      body.append(el('p', { textContent: data.sonarr_recycle_bin
        ? `Sonarr's recycle bin: ${data.sonarr_recycle_bin}`
        : 'Sonarr has no recycle bin configured, so deletions through Sonarr are permanent.' }));
      const table = el('table', { className: 'tvd-table' });
      table.append(el('thead', { innerHTML: '<tr><th>Series</th><th>Sonarr path</th><th>Unraid path</th><th>Found</th></tr>' }));
      const tbody = el('tbody');
      (data.sample || []).forEach((row) => {
        const tr = el('tr');
        [row.title, row.sonarr_path, row.path, row.exists ? 'yes' : 'no'].forEach((value, index) => {
          tr.append(el('td', { className: index ? 'tvd-mono' : '', textContent: String(value) }));
        });
        tbody.append(tr);
      });
      table.append(tbody);
      body.append(el('div', { className: 'tvd-scroll' }, [table]));
      return {};
    }, null, 'Close');
  }

  function editInstance(existing) {
    const instance = Object.assign({ id: '', name: '', url: '', api_key: '', enabled: true, verify_tls: true, path_maps: [] }, existing || {});
    dialog(existing ? 'Edit Sonarr instance' : 'Add Sonarr instance', (body) => {
      const name = el('input', { type: 'text', value: instance.name, placeholder: 'Sonarr — Series' });
      const url = el('input', { type: 'text', value: instance.url, placeholder: 'http://192.168.1.10:8989', spellcheck: false });
      const key = el('input', { type: 'password', value: instance.api_key || '', autocomplete: 'off', placeholder: 'Sonarr API key' });
      const enabled = checkbox('Instance is enabled', instance.enabled);
      const verify = checkbox('Verify the TLS certificate', instance.verify_tls, 'Turn off only for a self-signed certificate on your own network.');
      // Sonarr's mounted paths will not match Unraid's. Each row translates one root.
      const mapRows = el('div', { className: 'tvd-rules' });
      const addRow = (entry) => {
        const from = el('input', { type: 'text', value: (entry || {}).from || '', placeholder: '/tv', spellcheck: false });
        const to = el('input', { type: 'text', value: (entry || {}).to || '', placeholder: '/mnt/user/media/TV', spellcheck: false });
        const drop = el('button', { type: 'button', className: 'tvd-danger', textContent: 'Remove' });
        const row = el('div', { className: 'tvd-row' }, [field('Path in Sonarr', from), field('Path on Unraid', to), drop]);
        drop.addEventListener('click', (event) => { event.preventDefault(); row.remove(); });
        row.dataset.mapping = '1';
        row._pair = { from, to };
        mapRows.append(row);
        return row;
      };
      ((instance.path_maps || []).length ? instance.path_maps : [{}]).forEach(addRow);
      const readMappings = () => [...mapRows.children]
        .map((row) => ({ from: row._pair.from.value.trim(), to: row._pair.to.value.trim() }))
        .filter((entry) => entry.from || entry.to);

      const addMapButton = el('button', { type: 'button', textContent: 'Add another root' });
      addMapButton.addEventListener('click', (event) => { event.preventDefault(); addRow(null); });

      const draftInstance = () => ({
        id: instance.id, name: name.value, url: url.value, api_key: key.value,
        enabled: enabled.input.checked, verify_tls: verify.input.checked, path_maps: readMappings(),
      });

      const detectButton = el('button', { type: 'button', textContent: 'Detect roots' });
      detectButton.addEventListener('click', (event) => {
        event.preventDefault();
        guarded('', async () => {
          const data = await api('detect-mappings', { instance: draftInstance() }, 'Reading Sonarr root folders…');
          if (data.mappings.length) {
            mapRows.replaceChildren();
            data.mappings.forEach(addRow);
            const missing = data.mappings.filter((entry) => !entry.exists);
            notice(missing.length
              ? `Filled in ${data.mappings.length} mapping(s), but ${missing.length} target folder(s) do not exist here. Check them before saving.`
              : `Filled in ${data.mappings.length} mapping(s) from Sonarr's root folders and the container's mounts.`,
              missing.length ? 'bad' : 'ok');
          } else {
            notice(`${data.note} Sonarr root folders: ${(data.root_folders || []).join(', ') || 'none reported'}`, 'bad');
          }
        });
      });

      const testButton = el('button', { type: 'button', textContent: 'Test connection' });
      testButton.addEventListener('click', (event) => {
        event.preventDefault();
        guarded('', async () => {
          const data = await api('test-instance', { instance: draftInstance() }, 'Contacting Sonarr…');
          notice(`Sonarr ${data.sonarr_version}: ${data.series_count} series, ${data.folders_found} folders found on this server.`, 'ok');
        });
      });

      body.append(
        field('Name', name),
        field('URL', url, 'Include the port, and any base URL Sonarr is configured with.'),
        field('API key', key, existing ? 'Leave the masked value to keep the stored key.' : 'Sonarr: Settings → General → API Key.'),
        el('h3', { textContent: 'Root path mapping' }),
        el('small', { textContent: 'Sonarr reports the paths it sees inside its container; they will not match Unraid’s. Map each Sonarr root to the share it really lives on, and every path is translated in both directions. "Detect roots" fills this in from Sonarr’s root folders and the container’s mounts.' }),
        mapRows,
        el('div', { className: 'tvd-row' }, [addMapButton, detectButton]),
        enabled.node, verify.node, testButton,
      );
      return { name, url, key, enabled, verify, readMappings };
    }, async (context) => {
      const draft = {
        id: instance.id || undefined,
        name: context.name.value,
        url: context.url.value,
        api_key: context.key.value,
        enabled: context.enabled.input.checked,
        verify_tls: context.verify.input.checked,
        path_maps: context.readMappings(),
      };
      settings.instances = (settings.instances || []).filter((other) => other.id !== instance.id).concat([draft]);
      seriesCache = {};
      await saveSettings('Sonarr instance saved.');
    });
  }

  $('tvd-add-instance').addEventListener('click', () => editInstance(null));

  // -- settings form -----------------------------------------------------
  function renderSettings() {
    const schedule = settings.schedule || {};
    const guards = settings.guards || {};
    const retention = settings.retention || {};
    const recycle = settings.recycle || {};
    const sidecars = settings.sidecars || {};
    $('tvd-schedule-enabled').checked = !!schedule.enabled;
    $('tvd-schedule-cron').value = schedule.cron || '0 4 * * *';
    const preset = $('tvd-schedule-preset');
    preset.value = [...preset.options].some((option) => option.value === schedule.cron) ? schedule.cron : 'custom';
    $('tvd-dry-run').checked = !!settings.dry_run;
    $('tvd-max-deletes').value = guards.max_deletes_per_run;
    $('tvd-max-percent').value = guards.max_percent_per_rule;
    $('tvd-min-age').value = guards.min_file_age_hours;
    $('tvd-include-specials').checked = !!retention.include_specials;
    $('tvd-mtime-fallback').checked = !!retention.allow_mtime_fallback;
    $('tvd-unmonitor').checked = !!retention.unmonitor_deleted;
    $('tvd-remonitor').checked = !!retention.remonitor_widened;
    $('tvd-recycle-mode').value = recycle.mode || 'sonarr';
    $('tvd-recycle-path').value = recycle.path || '';
    $('tvd-recycle-days').value = recycle.retention_days;
    $('tvd-sidecars').checked = !!sidecars.enabled;
    $('tvd-sidecar-ext').value = (sidecars.extensions || []).join(', ');
    $('tvd-empty-dirs').checked = !!settings.delete_empty_dirs;
    $('tvd-state-dir').value = settings.state_dir || '';
    $('tvd-history-size').value = settings.log_retention_runs;
    $('tvd-notify').checked = !!settings.notify;
    $('tvd-tmdb-enabled').checked = !!(settings.tmdb || {}).enabled;
    $('tvd-tmdb-key').value = (settings.tmdb || {}).api_key || '';
    applyRecycleVisibility();
  }

  function applyRecycleVisibility() {
    const usesFolder = $('tvd-recycle-mode').value === 'plugin';
    $('tvd-recycle-path-field').hidden = !usesFolder;
    $('tvd-recycle-days-field').hidden = !usesFolder;
  }
  $('tvd-recycle-mode').addEventListener('change', applyRecycleVisibility);
  $('tvd-schedule-preset').addEventListener('change', () => {
    if ($('tvd-schedule-preset').value !== 'custom') $('tvd-schedule-cron').value = $('tvd-schedule-preset').value;
  });

  function collectSettings() {
    return Object.assign({}, settings, {
      dry_run: $('tvd-dry-run').checked,
      schedule: { enabled: $('tvd-schedule-enabled').checked, cron: $('tvd-schedule-cron').value.trim() },
      guards: {
        max_deletes_per_run: $('tvd-max-deletes').value,
        max_percent_per_rule: $('tvd-max-percent').value,
        min_file_age_hours: $('tvd-min-age').value,
      },
      retention: {
        include_specials: $('tvd-include-specials').checked,
        allow_mtime_fallback: $('tvd-mtime-fallback').checked,
        unmonitor_deleted: $('tvd-unmonitor').checked,
        remonitor_widened: $('tvd-remonitor').checked,
      },
      recycle: { mode: $('tvd-recycle-mode').value, path: $('tvd-recycle-path').value.trim(), retention_days: $('tvd-recycle-days').value },
      sidecars: { enabled: $('tvd-sidecars').checked, extensions: $('tvd-sidecar-ext').value.split(',').map((value) => value.trim()).filter(Boolean) },
      delete_empty_dirs: $('tvd-empty-dirs').checked,
      state_dir: $('tvd-state-dir').value.trim(),
      log_retention_runs: $('tvd-history-size').value,
      notify: $('tvd-notify').checked,
      tmdb: { enabled: $('tvd-tmdb-enabled').checked, api_key: $('tvd-tmdb-key').value },
    });
  }

  async function saveSettings(message) {
    const data = await api('settings', { settings: collectSettings() }, 'Saving…');
    settings = data.settings;
    snapshot.settings = settings;
    snapshot.schedule_active = data.schedule_active;
    render();
    if (message) notice(message, 'ok');
  }

  $('tvd-save').addEventListener('click', () => guarded('', () => saveSettings('Settings saved.')));
  $('tvd-tmdb-test').addEventListener('click', () => guarded('', async () => {
    const data = await api('test-tmdb', { tmdb: { api_key: $('tvd-tmdb-key').value } }, 'Contacting TMDB…');
    notice(data.ok_message, 'ok');
  }));

  // -- runs and results --------------------------------------------------
  function showResult(result, title) {
    dialog(`${title}: ${result.dry_run ? 'nothing was deleted' : `${result.deleted} files deleted`}`, (body) => {
      if (result.aborted) body.append(el('div', { className: 'tvd-warning', textContent: result.aborted }));
      (result.blocked || []).forEach((message) => body.append(el('div', { className: 'tvd-warning', textContent: message })));
      body.append(el('p', {
        textContent: `${result.planned} file(s) selected across ${result.rules.length} rule(s) in ${result.duration_seconds}s.`
                     + (result.dry_run ? ' Dry run: nothing was changed.' : ` ${bytes(result.freed_bytes)} reclaimed.`)
                     + (result.remonitored ? ` ${result.remonitored} episode(s) re-monitored.` : ''),
      }));
      result.rules.forEach((rule) => {
        const card = el('div', { className: `tvd-rule ${rule.ok ? 'matched' : 'unmatched'}` });
        card.append(el('div', { className: 'tvd-rule-head' }, [
          el('span', { className: 'tvd-rule-title', textContent: rule.series_title }),
          el('span', { className: 'tvd-chip', textContent: `${rule.deleted.length} selected` }),
          el('span', { className: 'tvd-chip', textContent: `${rule.kept} kept` }),
          el('span', { className: 'tvd-chip', textContent: `${rule.protected} protected` }),
        ]));
        if (rule.preset) card.append(el('small', { textContent: `Retention preset: ${rule.preset}` }));
        if (rule.error) card.append(el('div', { className: 'tvd-error', textContent: rule.error }));
        if ((rule.remonitored || []).length) {
          card.append(el('p', { textContent: `${rule.remonitored.length} previously removed episode(s) ${result.dry_run ? 'would be' : 'were'} re-monitored because this rule now covers them again:` }));
          rule.remonitored.slice(0, 20).forEach((item) => card.append(el('div', {
            className: 'tvd-mono',
            textContent: `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')} ${item.title || ''} (aired ${item.air_date || 'unknown'})`,
          })));
        }
        if (rule.blocked) card.append(el('div', { className: 'tvd-warning', textContent: rule.blocked }));
        if (rule.deleted.length) {
          const table = el('table', { className: 'tvd-table' });
          table.append(el('thead', { innerHTML: '<tr><th>Episode</th><th>Aired</th><th>Size</th><th>Why</th></tr>' }));
          const tbody = el('tbody');
          rule.deleted.slice(0, 200).forEach((item) => {
            const label = `S${String(item.season).padStart(2, '0')}E${String(item.episode).padStart(2, '0')} — ${item.title || ''}`;
            const aired = item.air_date ? `${item.air_date} (${item.air_source})` : 'unknown';
            const tr = el('tr');
            tr.append(el('td', {}, [el('div', { textContent: label }), el('div', { className: 'tvd-mono', textContent: item.path })]));
            tr.append(el('td', { textContent: aired }));
            tr.append(el('td', { textContent: bytes(item.size) }));
            tr.append(el('td', { textContent: (item.error ? `FAILED: ${item.error} — ` : '') + (item.reason || '') }));
            tbody.append(tr);
          });
          table.append(tbody);
          card.append(el('div', { className: 'tvd-scroll' }, [table]));
          if (rule.deleted.length > 200) card.append(el('small', { textContent: `…and ${rule.deleted.length - 200} more. The full list is in the run journal.` }));
        }
        if (rule.unknown_files.length) {
          card.append(el('p', { className: 'tvd-error', textContent: `${rule.unknown_files.length} media file(s) here are unknown to Sonarr and were left alone:` }));
          rule.unknown_files.slice(0, 20).forEach((path) => card.append(el('div', { className: 'tvd-mono', textContent: path })));
        }
        if (rule.emptied_dirs.length) card.append(el('small', { textContent: `${rule.emptied_dirs.length} empty folder(s) ${result.dry_run ? 'would be' : 'were'} removed.` }));
        body.append(card);
      });
      return {};
    }, null, 'Close');
  }

  $('tvd-preview').addEventListener('click', () => guarded('', async () => {
    const data = await api('preview', {}, 'Previewing all rules…');
    showResult(data.result, 'Preview');
  }));

  $('tvd-run').addEventListener('click', () => guarded('', async () => {
    const enabled = (settings.rules || []).filter((rule) => rule.enabled && rule.match_status === 'matched').length;
    if (!enabled) throw new Error('There are no enabled, matched rules to run.');
    const warning = settings.dry_run
      ? `Run ${plural(enabled, 'rule')} now? Dry run is on, so nothing will be deleted.`
      : `Run ${plural(enabled, 'rule')} now? This will DELETE episode files through Sonarr.`;
    if (!window.confirm(warning)) return;
    const data = await api('run', {}, 'Running…');
    await refresh();
    showResult(data.result, 'Run');
  }));

  // -- history -----------------------------------------------------------
  function renderHistory() {
    const runs = snapshot.runs || [];
    const tbody = $('tvd-history-table').querySelector('tbody');
    tbody.replaceChildren();
    $('tvd-history-empty').hidden = runs.length > 0;
    $('tvd-history-table').hidden = runs.length === 0;
    runs.forEach((run) => {
      const tr = el('tr');
      const notes = run.aborted || (run.errors || []).join('; ') || '';
      [when(run.started), run.scheduled ? 'schedule' : 'manual', run.dry_run ? 'dry run' : 'live',
       run.planned, run.deleted, bytes(run.freed_bytes), notes]
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
