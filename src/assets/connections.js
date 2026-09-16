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
import { dialog, guarded, notice } from './feedback.js';

export function createConnections({ api, getSettings, getSnapshot, saveSettings,
                                    forgetSeriesCache }) {
  const OPTIONAL = [
    ['plex', 'Plex', 'token', 'https://plex.example:32400', 'Plex server URL and token. Used only for future air-date enrichment.'],
    ['jellyfin', 'Jellyfin', 'api_key', 'http://jellyfin:8096', 'Jellyfin server URL and API key. Used only for future air-date enrichment.'],
  ];

  function renderProviders() {
    const box = $('tvr-provider-connections');
    if (!box) return;
    box.replaceChildren(...OPTIONAL.map(([kind, label, credential, placeholder, help]) => {
      const current = Object.assign({ url: '', enabled: false, verify_tls: true, verified_at: '' },
                                    (getSettings().connections || {})[kind] || {});
      const url = el('input', { type: 'text', value: current.url || '', spellcheck: false,
                                placeholder });
      const secret = el('input', { type: 'password', value: current[credential] || '', autocomplete: 'off',
                                   placeholder: current[credential] ? 'Leave masked to keep it' : 'Not set' });
      const enabled = toggle(current.enabled ? 'Enabled' : 'Disabled', !!current.enabled, null,
                             { className: 'tvr-row-switch' });
      const verify = toggle('Verify the TLS certificate', current.verify_tls !== false, null,
                            { className: 'tvr-row-switch' });
      const markRole = (node, value) => {
        if (node.dataset) node.dataset.connectionRole = value;
        else if (node.setAttribute) node.setAttribute('data-connection-role', value);
      };
      markRole(enabled.input, 'enabled');
      markRole(verify.input, 'verify-tls');
      const result = el('span', { className: 'tvr-result' });
      const test = el('button', { type: 'button', className: 'tvr-small', textContent: 'Test' });
      test.addEventListener('click', () => guarded('', async () => {
        try {
          await api('test-connection', { kind, connection: { url: url.value,
            [credential]: secret.value, enabled: enabled.input.checked,
            verify_tls: verify.input.checked } }, 'Testing connection…');
          result.textContent = 'Connected'; result.className = 'tvr-result ok';
        } catch (error) {
          result.textContent = 'Unavailable'; result.className = 'tvr-result bad'; throw error;
        }
      }));
      const card = el('div', { className: 'tvr-card tvr-optional-connection' }, [
        el('div', { className: 'tvr-card-head' }, [el('h3', { className: 'tvr-card-title', textContent: label }), enabled.node]),
        field('URL', url), field(credential === 'token' ? 'Token' : 'API key', secret,
          'Leave the masked value unchanged to keep the stored credential.'),
        verify.node,
        el('small', { textContent: help }),
        el('div', { className: 'tvr-inline-row' }, [test, result]),
      ]);
      if (card.dataset) card.dataset.connectionKind = kind;
      else card.setAttribute('data-connection-kind', kind);
      return card;
    }));
  }

  function renderApiKey() {
    const state = $('tvr-api-key-state');
    const actions = $('tvr-api-key-actions');
    if (!state || !actions) return;
    const metadata = getSettings().api_key || { status: 'not_created' };
    state.replaceChildren(el('span', { className: 'tvr-inline-label', textContent: 'State' }),
      el('span', { textContent: metadata.status === 'created'
        ? `Created ${metadata.prefix ? `(${metadata.prefix}…)` : ''}` : (metadata.status || 'not_created') }));
    actions.replaceChildren();
    const add = (label, operation, className='tvr-secondary') => {
      const button = el('button', { type: 'button', className, textContent: label });
      button.addEventListener('click', () => guarded('', async () => {
        const data = await api('api-key', { operation }, 'Updating API key…');
        if (data.key) {
          const reveal = el('div', { className: 'tvr-inline-row' });
          const input = el('input', { type: 'text', value: data.key, readonly: true, className: 'tvr-mono' });
          const copy = el('button', { type: 'button', className: 'tvr-small', textContent: 'Copy' });
          copy.addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(data.key); notice('API key copied.', 'ok'); }
            catch (_) { input.select(); notice('Select and copy the key manually.', 'ok'); }
          });
          reveal.append(input, copy); actions.append(reveal);
          notice('API key created — copy it now; it will not be shown again.', 'ok');
          getSettings().api_key = data.api_key || getSettings().api_key;
          state.replaceChildren(el('span', { className: 'tvr-inline-label', textContent: 'State' }),
            el('span', { textContent: `Created (${data.api_key?.prefix || ''}…)` }));
          return;
        } else notice(`API key ${operation}d.`, 'ok');
        getSettings().api_key = data.api_key || getSettings().api_key;
        renderApiKey();
      }));
      actions.append(button);
    };
    if (metadata.status === 'created') { add('Regenerate', 'regenerate'); add('Revoke', 'revoke', 'tvr-danger'); }
    else add('Create key', 'create', 'tvr-primary');
  }

  function renderInstances() {
    const container = $('tvr-instances');
    container.replaceChildren();
    renderProviders();
    renderApiKey();
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
          // The card can outlive the instance it names if a concurrent refresh removed
          // it between the click and this line; writing to it anyway would silently
          // invent the instance back into the settings just saved.
          if (!target) { renderInstances(); throw new Error('That Sonarr instance is no longer here.'); }
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
