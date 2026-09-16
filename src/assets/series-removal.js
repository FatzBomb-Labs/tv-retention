import { $, el, toggle, field, options } from './dom.js';
import { plural, ago } from './format.js';
import { monitorTree } from './episode-trees.js';
import { guarded, dialog } from './feedback.js';

/* Queued removal: the banner a queued series wears, and the dialog that queues it.
 *
 * Nothing destructive here happens now. Removing a series is an intent that a run
 * applies, so the banner keeps undo one click away for as long as that is true —
 * which is the whole reason removal queues rather than acts.
 *
 * The two Sonarr-destructive options each demand their own typed word before they
 * can even be queued. That guard is a property of the action, not of Test Mode:
 * Test Mode governs what a run writes, and this writes nothing either way.
 *
 * The one thing that is applied immediately is monitoring, and deliberately: it is
 * reversible in a click, deletes nothing, and the point of it is that Sonarr behaves
 * as you want between now and the run that removes the series.
 */
export function createRemoval({ api, getSettings, saveSettings, renderDetails }) {
const REMOVAL_ACTIONS = [
  ['remove', 'Leave the series untouched in Sonarr'],
  ['monitor-all', 'Set the entire series to monitored'],
  ['unmonitor-all', 'Set the entire series to unmonitored'],
  ['monitor-in-frame', 'Set only episodes inside the keep window to monitored'],
  ['delete-series', 'Ask Sonarr to delete the series, keeping the files'],
  ['delete-series-files', 'Ask Sonarr to delete the series and its files'],
];
const REMOVAL_CONFIRM = { 'delete-series': 'DELETE', 'delete-series-files': 'DELETE ALL' };
const REMOVAL_SONARR = {
  'remove': 'Sonarr will not be touched',
  'monitor-all': 'Sonarr will monitor the whole series',
  'unmonitor-all': 'Sonarr will unmonitor the whole series',
  'monitor-in-frame': 'Sonarr will monitor the episodes inside the keep window',
  'delete-series': 'Sonarr will delete the series record',
  'delete-series-files': 'Sonarr will delete the series record',
};
const REMOVAL_FILES = {
  'delete-series-files': 'Sonarr will delete its files',
};

const queuedRemoval = (rule) => (rule.queue || {}).removal || null;

// A queued series states its intent on the card and can be taken back until a run
// applies it. Nothing has happened yet, so the card says exactly what will.
// Queued removal, said where the rest of this series' state is said. It used to replace
// the whole card in the list, which meant a series about to be removed was the one thing
// in the library you could not see the poster of — and undo has to stay one click away,
// because nothing has happened yet and that is the whole point of queueing.
function queuedBanner(rule) {
  const queued = queuedRemoval(rule);
  if (!queued) return null;
  const box = el('div', { className: 'tvr-queued-box' });
  box.append(el('div', { className: 'tvr-queued-lines' }, [
    el('div', { textContent: `Queued for removal at the next run, ${ago(queued.created_at)}.` }),
    el('div', { textContent: REMOVAL_SONARR[queued.action] || '' }),
    el('div', { className: REMOVAL_FILES[queued.action] ? 'tvr-queued-danger' : '',
                textContent: REMOVAL_FILES[queued.action] || 'No files will be removed' }),
  ]));
  const undo = el('button', { type: 'button', className: 'tvr-action', textContent: 'Undo' });
  undo.addEventListener('click', () => guarded('', async () => {
    const target = (getSettings().rules || []).find((other) => other.id === rule.id);
    // A concurrent refresh can replace the whole settings document while this waits on
    // the operator, and silently doing nothing would look identical to success.
    if (!target) throw new Error('That series is no longer in the library. Nothing was changed.');
    target.queue = Object.assign({}, target.queue, { removal: null });
    await saveSettings('Removal cancelled.');
    renderDetails();
  }));
  box.append(undo);
  return box;
}

// Removing a series is an intent, not an act: it queues, and the two destructive Sonarr
// options each demand their own word before they can be queued at all.
function deleteSeries(rule) {
  dialog(`Remove ${rule.series_title || rule.path}`, (body) => {
    body.append(el('p', { textContent:
      'This queues the series for removal from TV Retention. Nothing happens until the next '
      + 'run, and it can be undone from the series card until then.' }));
    const action = options(el('select'), REMOVAL_ACTIONS, 'remove');
    const warning = el('div', { className: 'tvr-danger-box', hidden: true });
    const confirm = el('input', { type: 'text', autocomplete: 'off', spellcheck: false });
    const confirmField = field('Confirm', confirm);
    confirmField.hidden = true;
    const review = () => {
      const word = REMOVAL_CONFIRM[action.value];
      warning.hidden = !word;
      confirmField.hidden = !word;
      confirm.placeholder = word || '';
      confirmField.querySelector('span').textContent = word ? `Type ${word} to confirm` : 'Confirm';
      // The button itself is unavailable until the word matches; throwing after a click
      // tells you the same thing later and less kindly.
      $('tvr-dialog-ok').disabled = !!word && confirm.value.trim() !== word;
      if (!word) return;
      warning.replaceChildren(
        el('strong', { textContent: 'Sonarr will delete this series.' }),
        el('span', { textContent: action.value === 'delete-series-files'
          ? ' Its episode files go too, and only Sonarr’s own recycle bin will hold them. '
            + 'TV Retention does not remove the series itself — it asks Sonarr to.'
          : ' The files stay on disk; only Sonarr’s record of the series is removed.' }));
    };
    action.addEventListener('change', review);
    confirm.addEventListener('input', review);
    body.append(field('Sonarr action', action,
                      'What Sonarr should do as the series leaves TV Retention.'), warning, confirmField);

    // Leaving is the moment to put monitoring back the way you want it, because after
    // this the plugin stops having an opinion about this series at all. Every season,
    // not only the window's — the window is about to stop mattering.
    const restore = toggle('Set monitoring in Sonarr before it goes', false, null,
                           { className: 'tvr-row-switch' });
    const treeBox = el('div', { className: 'tvr-tree-box', hidden: true });
    let tree = null;
    restore.input.addEventListener('change', () => guarded('', async () => {
      treeBox.hidden = !restore.input.checked;
      if (!restore.input.checked) { tree = null; treeBox.replaceChildren(); return; }
      const data = await api('episodes', { rule_id: rule.id }, 'Reading episodes…', true);
      tree = monitorTree(data.seasons, {});
      treeBox.replaceChildren(tree.node);
    }));
    body.append(restore.node, treeBox);
    review();
    return { action, confirm, tree: () => (restore.input.checked ? tree : null) };
  }, async (context) => {
    const word = REMOVAL_CONFIRM[context.action.value];
    if (word && context.confirm.value.trim() !== word) {
      throw new Error(`Type ${word} to confirm. Nothing was queued.`);
    }
    // Monitoring is set now rather than queued: it is reversible in a click, and the
    // point of it is that Sonarr behaves as you want between now and the run.
    const wanted = context.tree && context.tree();
    const changes = wanted ? wanted.changes() : { monitor: [], unmonitor: [] };
    let done = '';
    if (changes.monitor.length || changes.unmonitor.length) {
      const applied = await api('set-monitored', Object.assign({ rule_id: rule.id }, changes),
                                'Setting monitoring in Sonarr…');
      const parts = [];
      if (applied.monitored) parts.push(`${plural(applied.monitored, 'episode')} monitored`);
      if (applied.unmonitored) parts.push(`${plural(applied.unmonitored, 'episode')} unmonitored`);
      done = parts.length ? ` ${parts.join(', ')} in Sonarr.` : '';
    }
    const target = (getSettings().rules || []).find((other) => other.id === rule.id);
    // The Sonarr write above can take up to ninety seconds; a concurrent refresh can
    // have replaced the settings document by the time it returns. Silently doing nothing
    // here would tell the operator the removal queued when it did not — worse, after a
    // monitoring change that really did happen.
    if (!target) throw new Error(`That series is no longer in the library. Nothing was queued.${done}`);
    target.queue = Object.assign({}, target.queue,
                                 { removal: { action: context.action.value, created_at: new Date().toISOString() } });
    await saveSettings('Queued. It will be applied at the next run, and can be undone until then.' + done);
  }, 'Queue removal');
}

  return { queuedRemoval, queuedBanner, deleteSeries };
}
