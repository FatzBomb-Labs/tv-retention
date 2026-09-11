import { $, el, field } from './dom.js';
import { ago, plural } from './format.js';
import { dialog, guarded, notice } from './feedback.js';

// Alerts: the card that shows what is wrong with a series or with the installation, the
// quick actions that fix it, and the tab that lists the installation's own problems.
//
// Cut as one module because a card is the only thing here anyone looks at: `alertItem`
// draws a problem, the two card functions frame a list of them, `runAlertAction` is what
// the button on one does, and `renderAlerts` and `showSeriesAlerts` are the two places a
// card is put on screen. The first three are private -- nothing outside ever drew one.
//
// `settings`, `snapshot`, `monitoring` and `systemAlerts` arrive as accessors: the entry
// replaces all four wholesale, so a captured value would go stale the first time a
// document came back or a reading landed. Navigation arrives as two intents rather than
// as `showView` -- "find this series in the library" and "open this instance's settings"
// -- so the navigation module has no edge into this one to honour.
export function createAlerts({ api, getSettings, getSnapshot, getMonitoring, getSystemAlerts,
                               applySaved, applyAlerts, seriesAlerts, isBlocked, worstSeverity,
                               queueChecks, render, showSeriesInLibrary, openInstance }) {
  // One card per series, not one per problem. The series is the thing you act on, so it
  // owns the card; each problem inside it is a short labelled line. Severity is carried by
  // the card frame and the badge, and nowhere else — a card tinted end to end says nothing
  // a coloured edge does not.
  const ALERT_TAG = {
    'unmatched': 'No Sonarr match',
    'folder-missing': 'Folder missing',
    'unknown-files': 'Unknown files',
    'ended': 'Series ended',
    'ended-expired': 'Series ended',
    'sonarr-unreachable': 'Sonarr unreachable',
  };
  const ACTION_LABEL = {
    'rematch': 'Re-check against Sonarr',
    'remove-rule': 'Remove from TV Retention',
    'open-instance': 'Open Sonarr settings',
    'test-instance': 'Test the connection',
    'enable-recycle-bin': 'Give Sonarr a recycle bin',
  };

  // A small mark before each problem, sized and coloured by rule. Geometric characters
  // rather than emoji: these have no colour-emoji presentation to fall back to, so they
  // render at the size the stylesheet asks for on every platform.
  function alertItem(alert) {
    const item = el('div', { className: 'tvr-alert-item' });
    const line = el('div', { className: 'tvr-alert-line' }, [
      el('span', { className: `tvr-sev ${alert.severity}`, title: alert.severity }),
      el('span', { className: 'tvr-alert-kind', textContent: ALERT_TAG[alert.kind] || alert.title }),
      el('span', { className: 'tvr-alert-detail', textContent: alert.detail }),
    ]);
    if (alert.blocking) line.append(el('span', { className: 'tvr-tag blocking', textContent: 'blocks runs' }));
    line.append(el('span', { className: 'tvr-alert-age', textContent: ago(alert.first_seen) }));
    item.append(line);
    // The explanation is the point of a notice — hiding it behind a link left one saying
    // nothing at all. It is short, so it stays on the page.
    item.append(el('p', { className: 'tvr-alert-help', textContent: alert.help }));

    const foot = el('div', { className: 'tvr-alert-foot' });
    if (alert.action) {
      const button = el('button', { type: 'button', className: 'tvr-action',
                                    textContent: `Quick action: ${ACTION_LABEL[alert.action] || 'Fix'}` });
      button.addEventListener('click', () => runAlertAction(alert));
      foot.append(button);
    }
    // Acknowledging is not dismissing: it hides this alert as it stands, and the alert
    // comes back if what it says changes. An error is never offered it.
    const ackable = alert.severity !== 'error' && !alert.blocking
                    && ((getSettings() || {}).alerts || {}).acknowledge !== false;
    if (ackable) {
      const ack = el('button', { type: 'button', className: 'tvr-small',
                                 textContent: alert.acknowledged ? 'Show again' : 'Acknowledge' });
      ack.addEventListener('click', () => guarded('', async () => {
        const data = await api('acknowledge', { key: alert.key, undo: !!alert.acknowledged },
                               'Saving…', true);
        applyAlerts(data.alerts);
        render();
        notice(alert.acknowledged ? 'Shown again.' : 'Acknowledged — it will return if it changes.', 'ok');
      }));
      foot.append(ack);
    }
    if (alert.acknowledged) item.classList.add('acknowledged');
    if ((alert.data || {}).files) {
      const files = el('div', { className: 'tvr-alert-files', hidden: true });
      ((alert.data || {}).files || []).slice(0, 10).forEach((path) =>
        files.append(el('div', { className: 'tvr-mono', textContent: path })));
      const show = el('button', { type: 'button', className: 'tvr-link', textContent: `List ${alert.count} file(s)` });
      show.addEventListener('click', () => { files.hidden = !files.hidden; });
      foot.append(show);
      item.append(files);
    }
    if (foot.children.length) item.append(foot);
    return item;
  }

  function seriesAlertCard(rule, list, config) {
    const options = config || {};
    const severity = worstSeverity(list);
    const blocked = list.some((alert) => alert.blocking);
    // Compact is the series editor, where the panel above has already named the series,
    // counted its issues, dated the reading and offered a re-read. Repeating all four
    // under a heading made an ended series with no episodes say the same thing four ways.
    const compact = !!options.compact;
    const card = el('div', { className: `tvr-alert-card ${severity}${compact ? ' compact' : ''}` });
    if (!compact) {
      const head = el('div', { className: 'tvr-alert-card-head' }, [
        el('span', { className: 'tvr-rule-title', textContent: rule.series_title || rule.path }),
        el('span', { className: 'tvr-alert-count', textContent: plural(list.length, 'issue') }),
      ]);
      if (blocked) head.append(el('span', { className: 'tvr-tag blocking', textContent: 'blocked' }));
      const state = getMonitoring()[rule.id];
      head.append(el('span', { className: 'tvr-alert-age',
                               textContent: state ? `Sonarr read ${ago(state.read_at || state.checked_at)}` : 'not checked' }));
      card.append(head);
    }
    list.forEach((alert) => card.append(alertItem(alert)));
    if (compact) return card;
    const foot = el('div', { className: 'tvr-alert-foot' });
    if (!options.hideOpen) {
      const open = el('button', { type: 'button', className: 'tvr-small', textContent: 'Show in Series' });
      open.addEventListener('click', () => {
        showSeriesInLibrary(rule);
      });
      foot.append(open);
    }
    const recheck = el('button', { type: 'button', className: 'tvr-small', textContent: 'Re-check now' });
    recheck.addEventListener('click', () => { $('tvr-dialog').close('cancel'); queueChecks([rule.id], true); });
    foot.append(recheck);
    card.append(foot);
    return card;
  }

  function systemAlertCard(instanceName, list) {
    const severity = worstSeverity(list);
    const card = el('div', { className: `tvr-alert-card ${severity}` });
    card.append(el('div', { className: 'tvr-alert-card-head' }, [
      el('span', { className: 'tvr-rule-title', textContent: instanceName }),
      el('span', { className: 'tvr-alert-count', textContent: plural(list.length, 'issue') }),
    ]));
    list.forEach((alert) => card.append(alertItem(alert)));
    return card;
  }

  function runAlertAction(alert) {
    return guarded('', async () => {
      if (alert.action === 'enable-recycle-bin') {
        // Writes to Sonarr's own configuration, so it asks for the path and says plainly
        // that the change applies to everything Sonarr deletes.
        const instance = (getSettings().instances || []).find((i) => i.id === alert.instance_id);
        dialog('Give Sonarr a recycle bin', (body) => {
          body.append(el('p', { textContent:
            'Sonarr will move deleted files here instead of removing them, and clean the folder '
            + 'out after a while. This changes Sonarr’s own setting, so it applies to everything '
            + 'Sonarr deletes — not only to TV Retention.' }));
          const path = el('input', { type: 'text', spellcheck: false, placeholder: '/tv/.recycle',
                                     value: '' });
          body.append(field('Recycle bin path, as Sonarr sees it', path,
                            'A path inside Sonarr, on the same filesystem as your library so moves '
                            + 'are instant. Sonarr creates it if it does not exist.'));
          return { path };
        }, async (inner) => {
          const data = await api('enable-recycle-bin',
                                 { instance_id: alert.instance_id, path: inner.path.value.trim() },
                                 'Updating Sonarr…');
          queueChecks((getSettings().rules || []).filter((r) => r.instance_id === alert.instance_id)
            .map((r) => r.id).slice(0, 1));
          notice(data.ok_message, 'ok');
        }, 'Set it');
        return;
      }
      if (alert.action === 'open-instance' || alert.action === 'test-instance') {
        openInstance(alert.instance_id);
        return;
      }
      const data = await api('alert-action', { kind: alert.action, rule_id: alert.rule_id },
                             'Applying the fix…');
      if (data.settings) applySaved(data);
      if (data.monitoring) getMonitoring()[alert.rule_id] = data.monitoring;
      const fresh = await api('alerts', {}, '', true);
      applyAlerts(fresh.alerts);
      getSnapshot().alert_summary = fresh.summary;
      render();
      notice(data.ok_message || 'Done.', 'ok');
    });
  }

  function showSeriesAlerts(rule) {
    dialog(rule.series_title || rule.path, (body) => {
      const list = seriesAlerts(rule.id);
      const blocked = isBlocked(rule.id);
      body.append(el('div', { className: `tvr-status ${blocked ? 'bad' : 'ok'}` }, [
        el('strong', { textContent: blocked ? 'Blocked' : 'Runs normally' }),
        el('span', { textContent: blocked
          ? 'Skipped by every run until the errors below are resolved.'
          : 'The items below are advisory and do not stop this series.' }),
      ]));
      body.append(seriesAlertCard(rule, list, { hideOpen: true }));
      return {};
    }, null, 'Close');
  }

  function renderAlerts() {
    // The counts describe what this tab shows — system problems — with series problems
    // summarised by the roll-up, because they are acted on from the series card.
    const system = getSystemAlerts().slice();
    const matches = () => true;
    const systemBox = $('tvr-alerts-system');
    systemBox.replaceChildren();
    const shown = system.filter(matches);
    $('tvr-alerts-system-empty').hidden = shown.length > 0;
    const byInstance = new Map();
    shown.forEach((alert) => {
      const instance = (getSettings().instances || []).find((i) => i.id === alert.instance_id);
      const name = instance ? instance.name : 'TV Retention';
      byInstance.set(name, (byInstance.get(name) || []).concat([alert]));
    });
    byInstance.forEach((list, name) => systemBox.append(systemAlertCard(name, list)));
  }

  // The one listener on a node the module did not make. It cannot run at import, so the
  // entry calls this at start-up beside the other modules' wiring.
  function wire() {
    $('tvr-recheck-all').addEventListener('click', () => guarded('', async () => {
      const ids = (getSettings().rules || []).filter((rule) => rule.enabled).map((rule) => rule.id);
      if (!ids.length) throw new Error('There are no enabled series to check.');
      queueChecks(ids, true);
      notice(`Re-reading ${plural(ids.length, 'series')} in the background.`, 'ok');
    }));
  }

  return { seriesAlertCard, systemAlertCard, showSeriesAlerts, renderAlerts, wire };
}
