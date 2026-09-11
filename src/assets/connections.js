// The Sonarr instances a rule can bind to, and the dialog that adds one.
//
// Saving is gated on a successful test rather than trusted, because an instance that
// does not answer is a rule that cannot be processed, and the failure would otherwise
// surface as an alert hours later instead of in the dialog that caused it. Removing one
// is refused while any rule still points at it, for the same reason.
//
// `forgetSeriesCache` is a named intent rather than a binding: changing where a Sonarr
// lives invalidates every series list read from the old one, and the entry owns that
// cache.
import { $, el, field, toggle } from './dom.js';
import { plural } from './format.js';
import { dialog, guarded } from './feedback.js';

export function createConnections({ api, getSettings, getSnapshot, saveSettings,
                                    forgetSeriesCache }) {
  function renderInstances() {
    const container = $('tvr-instances');
    container.replaceChildren();
    (getSettings().instances || []).forEach((instance) => {
      const health = ((getSnapshot().health || {}).instances || {})[instance.id] || {};
      const reachable = health.reachable !== false;
      const card = el('div', { className: `tvr-instance ${reachable ? 'ok' : 'bad'}` });
      const line = el('div', { className: 'tvr-instance-line' });
      line.append(el('span', { className: `tvr-dot ${reachable ? 'ok' : 'bad'}`,
                               title: reachable ? 'Answering' : (health.error || 'Not answering') }));
      line.append(el('span', { className: 'tvr-rule-title', textContent: instance.name }));
      line.append(el('span', { className: 'tvr-chip tvr-mono', textContent: instance.url }));
      if (health.sonarr_version) line.append(el('span', { className: 'tvr-chip', textContent: `Sonarr ${health.sonarr_version}` }));
      if (!reachable) line.append(el('span', { className: 'tvr-tag blocking', textContent: 'unreachable' }));
      const edit = el('button', { type: 'button', className: 'tvr-small', textContent: 'Edit' });
      edit.addEventListener('click', () => editInstance(instance));
      line.append(el('span', { className: 'tvr-spacer' }));
      line.append(edit);
      // Enabling an instance is a switch on the card, like enabling a series.
      const control = toggle(instance.enabled ? 'Enabled' : 'Disabled', instance.enabled, null,
                             { className: 'tvr-card-switch', label: `${instance.name} enabled` });
      control.input.addEventListener('change', () => {
        const wanted = control.input.checked;
        control.input.disabled = true;
        guarded('', async () => {
          const target = (getSettings().instances || []).find((other) => other.id === instance.id);
          target.enabled = wanted;
          try {
            await saveSettings(null, true);
          } catch (error) {
            target.enabled = !wanted;
            renderInstances();
            throw error;
          }
        });
      });
      line.append(control.node);
      card.append(line);
      container.append(card);
    });
  }

  // Sonarr owns the filesystem, so the editor is only a connection: address, key, and
  // whether it answers. There is nothing left to map.
  function editInstance(existing) {
    const instance = Object.assign({ id: '', name: '', url: '', api_key: '', enabled: true,
                                     verify_tls: true }, existing || {});
    dialog(existing ? `Edit ${instance.name}` : 'Add Sonarr instance', (body) => {
      let verified = !!existing;
      const name = el('input', { type: 'text', value: instance.name, placeholder: 'Sonarr — Series' });
      const url = el('input', { type: 'text', value: instance.url, placeholder: 'http://192.168.1.10:8989', spellcheck: false });
      const key = el('input', { type: 'password', value: instance.api_key || '', autocomplete: 'off',
                                placeholder: 'Sonarr API key' });
      const enabled = toggle(instance.enabled ? 'Enabled' : 'Disabled', instance.enabled, null,
                             { className: 'tvr-card-switch' });
      const verify = toggle('Verify the TLS certificate', instance.verify_tls, null, { className: 'tvr-row-switch' });
      const testButton = el('button', { type: 'button', className: 'tvr-primary', textContent: 'Test connection' });
      const testResult = el('span', { className: 'tvr-result' });

      const gate = () => {
        $('tvr-dialog-ok').disabled = !verified;
        $('tvr-dialog-ok').textContent = verified ? 'Save' : 'Test first';
      };
      testButton.addEventListener('click', (event) => {
        event.preventDefault();
        guarded('', async () => {
          const data = await api('test-instance', {
            instance: { id: instance.id, name: name.value, url: url.value, api_key: key.value,
                        enabled: enabled.input.checked, verify_tls: verify.input.checked },
          }, 'Contacting Sonarr…');
          verified = true;
          testResult.textContent = `Connected — Sonarr ${data.sonarr_version}, ${data.series_count} series`
            + (data.recycle_bin ? '' : ' · no recycle bin');
          testResult.className = 'tvr-result ok';
          gate();
        });
      });

      body.append(
        el('div', { className: 'tvr-card-head' },
           [el('span', { className: 'tvr-card-title', textContent: 'Connection' }), enabled.node]),
        field('Name', name),
        field('URL', url, 'Include the port, and any base URL Sonarr is configured with.'),
        field('API key', key, existing ? 'Leave the masked value to keep the stored key.' : 'Sonarr: Settings → General → API Key.'),
        verify.node,
        el('div', { className: 'tvr-row tvr-inline' }, [testButton, testResult]),
      );
      if (existing) {
        const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small',
                                      textContent: 'Remove this instance' });
        remove.addEventListener('click', (event) => {
          event.preventDefault();
          guarded('', async () => {
            const used = (getSettings().rules || []).filter((rule) => rule.instance_id === instance.id);
            if (used.length) throw new Error(`${plural(used.length, 'series')} still use ${instance.name}.`);
            if (!window.confirm(`Remove the Sonarr instance ${instance.name}?`)) return;
            $('tvr-dialog').close('cancel');
            getSettings().instances = getSettings().instances.filter((other) => other.id !== instance.id);
            await saveSettings('Instance removed.');
          });
        });
        body.append(el('div', { className: 'tvr-editor-foot' }, [remove]));
      }
      gate();
      return { name, url, key, enabled, verify, verified: () => verified };
    }, async (context) => {
      if (!context.verified()) throw new Error('Test the connection before saving.');
      getSettings().instances = (getSettings().instances || []).filter((other) => other.id !== instance.id).concat([{
        id: instance.id || undefined,
        name: context.name.value,
        url: context.url.value,
        api_key: context.key.value,
        enabled: context.enabled.input.checked,
        verify_tls: context.verify.input.checked,
        verified_at: new Date().toISOString(),
      }]);
      forgetSeriesCache();
      await saveSettings('Sonarr instance saved.');
    }, 'Test first');
  }

  function wire() {
    $('tvr-add-instance').addEventListener('click', () => editInstance(null));
  }

  return { renderInstances, editInstance, wire };
}
