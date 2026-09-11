// Presets: a named keep window that any number of series can point at.
//
// The point of a preset is that widening it moves every series bound to it at once, so
// both the list and the editor say who is affected before anything is saved — and say it
// again, louder, when auto monitor is on and widening would put deleted episodes back on
// Sonarr's wanted list.
//
// `conditionFields` and `presetSummary` are exported because the series editor asks the
// same two questions of a one-off rule that this asks of a preset: what boxes does a keep
// window need, and how do you say one in a phrase. They are the reason a rule and a preset
// cannot drift into describing themselves differently.
//
// `getSettings` is an accessor rather than a value because the entry replaces the whole
// settings document on every save. The two writes here mutate `profiles` on whatever
// document is current, which is the same thing every other panel does.
import { $, el, field, options } from './dom.js';
import { plural } from './format.js';
import { dialog, guarded } from './feedback.js';

export function createPresets({ getSettings, saveSettings }) {
  function presetSummary(preset) {
    const parts = [];
    if (preset.keep_days) parts.push(`keep ${plural(preset.keep_days, 'day')}`);
    if (preset.keep_episodes) parts.push(`keep ${plural(preset.keep_episodes, 'episode')}`);
    if (preset.keep_seasons) parts.push(`keep ${plural(preset.keep_seasons, 'season')}`);
    return parts;
  }

  function renderPresets() {
    const container = $('tvr-presets');
    const presets = getSettings().profiles || [];
    container.replaceChildren();
    $('tvr-presets-empty').hidden = presets.length > 0;
    presets.forEach((preset) => {
      const users = (getSettings().rules || []).filter((rule) => rule.profile_id === preset.id);
      const card = el('div', { className: 'tvr-rule ok' });
      card.append(el('div', { className: 'tvr-rule-head' }, [
        el('span', { className: 'tvr-rule-title', textContent: preset.name }),
        el('span', { className: 'tvr-chip', textContent: `used by ${plural(users.length, 'series')}` }),
      ]));
      const body = el('div', { className: 'tvr-rule-body' });
      presetSummary(preset).forEach((label) => body.append(el('span', { className: 'tvr-chip on', textContent: label })));
      body.append(el('span', { className: 'tvr-chip', textContent: `combine: ${preset.combine}` }));
      const actions = el('div', { className: 'tvr-rule-actions' });
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
      el('div', { className: 'tvr-row' }, [field('Keep days', days), field('Keep episodes', episodes),
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
      const users = (getSettings().rules || []).filter((rule) => rule.profile_id === preset.id);
      body.append(field('Preset name', name), conditions.node);
      if (existing) {
        const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small', textContent: 'Remove preset' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          guarded('', async () => {
            if (users.length) throw new Error(`${plural(users.length, 'series')} still use "${preset.name}".`);
            if (!window.confirm(`Remove the preset "${preset.name}"?`)) return;
            $('tvr-dialog').close('cancel');
            getSettings().profiles = getSettings().profiles.filter((other) => other.id !== preset.id);
            await saveSettings('Preset removed.');
          });
        });
        $('tvr-dialog-extra').replaceChildren(remove);
      }
      if (users.length) {
        body.append(el('p', { textContent: `${plural(users.length, 'series')} use this preset and will change with it:` }));
        users.forEach((rule) => body.append(el('div', { className: 'tvr-mono', textContent: rule.series_title || rule.path })));
        if ((getSettings().retention || {}).auto_monitor) {
          body.append(el('div', { className: 'tvr-banner', textContent:
            'Auto monitor is on: widening this preset will put previously removed episodes back on '
            + 'Sonarr’s wanted list at the next run.' }));
        }
      }
      return { name, conditions };
    }, async (context) => {
      getSettings().profiles = (getSettings().profiles || []).filter((other) => other.id !== preset.id).concat([{
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

  function wire() {
    $('tvr-add-preset').addEventListener('click', () => editPreset(null));
  }

  return { renderPresets, presetSummary, conditionFields, wire };
}
