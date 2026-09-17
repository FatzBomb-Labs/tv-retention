/* The series editor: the details pane and the form inside it.
 *
 * One series at a time, and only ever the one being edited. There is no selection to
 * build, because the bulk mechanisms are a preset and a default — each one edit with a
 * blast radius you can name.
 *
 * The pane and the form are one module rather than two because they are one closure:
 * `renderDetails` drives `editing.build`, and the form reaches back into the draft map
 * that the pane clears. Splitting them would hand a fragment of a closure across a
 * module boundary, which is not a boundary.
 *
 * Reassigned entry bindings arrive as accessors — `getSettings`, `getSnapshot`,
 * `getMonitoring`, `getLibrary` — because each is replaced wholesale when a document or
 * a reading arrives, and a captured object goes stale on the first successful save.
 * Saving writes entry state, so it takes `applySaved` rather than assigning.
 */
'use strict';

import { $, el, text, toggle, field, options } from './dom.js';
import { bytes, plural, ago } from './format.js';
import { exclusionTree, monitorTree } from './episode-trees.js';
import { changeLines, changeList } from './changes.js';
import { notice, guarded, dialog } from './feedback.js';

export function createSeriesEditor({
  api, getSettings, getSnapshot, getMonitoring, getLibrary, applySaved, saveSettings,
  applyAlerts, conditionFields, presetSummary, posterNode, sonarrLink,
  seriesAlertCard, seriesAlerts, queuedBanner, queueChecks, deleteSeries,
  forgetLibrary, render, renderLibrary, openLibraryView,
}) {
    function ruleForm(existing, preselect) {
      const rule = Object.assign({
        id: '', enabled: true,
        instance_id: (preselect && preselect.instance_id) || (getSettings().instances[0] || {}).id || '',
        series_id: null, series_title: '', tvdb_id: null, path: '',
        profile_id: '',
        keep_days: '', keep_episodes: '', keep_seasons: '', combine: 'any',
        include_specials: null,
        auto_reenable: false,
      }, existing || {});

      if (!getSettings().instances.length) {
        notice('Add a Sonarr instance first — every series must be bound to a Sonarr record.', 'bad');
        return null;
      }
      // Sonarr's own record for this series: the one being added, or the one the rule is
      // already bound to. Everything the panel shows about the series comes from here.
      const series = preselect
        || (getLibrary() || []).find((entry) => entry.series_id === rule.series_id
                                           && entry.instance_id === rule.instance_id)
        || { series_id: rule.series_id, instance_id: rule.instance_id, title: rule.series_title,
             path: rule.path };
      if (!existing) {
        Object.assign(rule, { instance_id: series.instance_id, series_id: series.series_id,
                              series_title: series.title, tvdb_id: series.tvdb_id,
                              path: series.path });
      }

      const draftKey = existing ? `rule:${rule.id}`
                                : `add:${series.instance_id}:${series.series_id}`;
      return {
        title: existing ? 'Edit series' : 'Add series',
        rule,
        series,
        existing: !!existing,
        draftKey,
        build: (body, top) => {
        // The navigator already said which series this is, so there is no picker and no
        // instance to choose: the series carries its own. A rule *is* its binding to one
        // series, so pointing it at another is delete and add, not an edit.
        // One fact per line rather than a single run of dots: they answer different
        // questions, and a reader looking for the episode count should not have to find it
        // among the network and the certificate.
        const originLine = el('div', { className: 'tvr-identity-line' });
        const countLine = el('div', { className: 'tvr-identity-line' });
        const nextLine = el('div', { className: 'tvr-identity-line tvr-identity-next' });
        // One number on the line, coloured by the thing anyone actually wants to know: not
        // how many episodes are monitored, but whether the ones the rule keeps are. Green
        // is the state a run leaves the series in; anything else is a difference from it.
        // Everything behind the colour is in the tooltip rather than on the line.
        const monitorCountText = el('span', { className: 'tvr-monitor-count' });
        const sayCounts = (counts) => {
          const parts = [];
          if (series.season_count) parts.push(plural(series.season_count, 'season'));
          const total = (counts && counts.episodes) || series.total_episode_count || 0;
          countLine.replaceChildren();
          countLine.hidden = !total && !parts.length;
          if (parts.length) countLine.append(text(`${parts.join(' · ')}${total ? ' · ' : ''}`));
          if (!total) return;
          const held = counts && counts.episodes_on_disk != null
            ? counts.episodes_on_disk : series.episode_file_count;
          const disk = [`${held} of ${total} episodes are on disk`];
          if (series.size_on_disk) disk.push(`${bytes(series.size_on_disk)} on disk`);
          if (!existing) {
            const plain = el('span', { textContent: plural(total, 'episode'),
                                       title: disk.join('\n') });
            countLine.append(plain);
            return;
          }
          countLine.append(text(`${plural(total, 'episode')} (`), monitorCountText, text(')'));
          const monitored = counts && counts.episodes_monitored != null
            ? counts.episodes_monitored : null;
          monitorCountText.textContent = `${monitored == null ? '?' : monitored} monitored`;
          // Scope, not the whole series: a finished show with two seasons kept and six
          // unmonitored is exactly right, and colouring it orange would say otherwise.
          const scope = counts && counts.known ? counts.in_scope : null;
          const inScopeMonitored = scope == null ? null : scope - counts.in_scope_unmonitored;
          monitorCountText.className = 'tvr-monitor-count'
            + (inScopeMonitored == null || !scope ? ''
               : (counts.in_scope_unmonitored === 0 ? ' all'
                  : (inScopeMonitored > 0 ? ' some' : ' none')));
          const lines = disk.slice();
          if (scope != null) {
            lines.push(`${inScopeMonitored} of ${scope} episodes inside the keep window are monitored`);
            lines.push(counts.out_scope_monitored
              ? `${counts.out_scope_monitored} of ${counts.out_scope} outside it are still monitored`
                + ' — a run will unmonitor them'
              : `none of the ${counts.out_scope} outside it are monitored`);
          }
          monitorCountText.title = lines.join('\n');
        };
        // Three answers, and only one of them is a date: a series that has ended is not
        // "nothing scheduled", it is finished, and the difference decides what retention on
        // it even means.
        const stamp = (iso) => new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
        const sayNext = (counts) => {
          const next = counts && counts.next_episode;
          const at = series.next_airing
            ? stamp(series.next_airing)
            : (next ? new Date(`${next.air_date}T00:00:00`).toLocaleDateString() : '');
          if (at) {
            nextLine.textContent = `Next episode: ${at}`;
            // Which episode it is belongs to whoever asks for it: the line is about when.
            nextLine.title = next
              ? `S${String(next.season).padStart(2, '0')}E${String(next.episode).padStart(2, '0')}`
                + `${next.title ? `: ${next.title}` : ''}${next.estimated ? '\nDate estimated, not from Sonarr' : ''}`
              : '';
          } else if (series.ended) {
            nextLine.textContent = 'Series ended';
            nextLine.title = '';
          } else {
            nextLine.textContent = 'Nothing scheduled';
            nextLine.title = '';
          }
        };
        originLine.textContent = [series.year, series.network, series.certification]
          .filter(Boolean).join(' · ');
        originLine.hidden = !originLine.textContent;
        sayCounts(null);
        sayNext(null);
        // Not a field on a form: switching a series off is a thing you do, not a change you
        // save, so it takes effect where it is clicked and the list redraws behind it. No
        // caption either — the tooltip says which way it is, and one word beside the name
        // was a word that never changed.
        const enabled = toggle('', rule.enabled, null, { className: 'tvr-identity-switch' });
        let autoReenable = null;
        const showAutoReenable = () => {
          if (!autoReenable) return;
          autoReenable.node.hidden = !(existing && series.ended && !enabled.input.checked);
        };
        const sayState = () => {
          const on = enabled.input.checked;
          enabled.node.title = existing
            ? (on ? 'Enabled — every run includes this series. Click to disable it.'
                  : 'Disabled — every run skips this series. Click to enable it.')
            : (on ? 'Will be added enabled — every run will include this series.'
                  : 'Will be added disabled — no run will touch this series until it is enabled.');
          enabled.input.setAttribute('aria-label', on ? 'Enabled' : 'Disabled');
        };
        sayState();
        enabled.node.hidden = !existing;
        if (existing) {
          enabled.input.addEventListener('change', () => {
            const on = enabled.input.checked;
            const target = (getSettings().rules || []).find((other) => other.id === rule.id);
            if (!target) return;
            const previousAutoReenable = {
              target: target.auto_reenable,
              rule: rule.auto_reenable,
              checked: autoReenable.input.checked,
            };
            enabled.input.disabled = true;
            guarded('', async () => {
              try {
                target.enabled = rule.enabled = on;
                if (on) {
                  target.auto_reenable = rule.auto_reenable = false;
                  autoReenable.input.checked = false;
                }
                await saveSettings(null, true);
                sayState();
                showAutoReenable();
              } catch (error) {
                // Nothing was written, so nothing should look as though it was.
                target.enabled = rule.enabled = !on;
                target.auto_reenable = previousAutoReenable.target;
                rule.auto_reenable = previousAutoReenable.rule;
                enabled.input.checked = !on;
                autoReenable.input.checked = previousAutoReenable.checked;
                sayState();
                showAutoReenable();
                throw error;
              } finally {
                enabled.input.disabled = false;
              }
            });
          });
        }
        // What the next run would do, with the facts rather than in a box of its own below
        // them: it is a fact about this series, and it was the thing being scrolled past.
        const planLines = el('div', { className: 'tvr-identity-plan', hidden: true });
        const readLine = el('div', { className: 'tvr-identity-read' });
        const sayPlan = (plan) => {
          if (!existing) return;
          planLines.hidden = false;
          planLines.replaceChildren(
            plan && (plan.delete || plan.monitor || plan.unmonitor)
              ? changeLines(plan, (kind) => guarded('', async () => {
                  const data = await api('preview', { rule_ids: [rule.id] },
                                         'Working out what would change…');
                  changeList(data.result, `${rule.series_title}: scheduled changes`, kind);
                }), true)
              : el('div', { className: 'tvr-plan-quiet',
                            textContent: plan ? 'Nothing scheduled for the next run'
                                              : 'Not read from Sonarr yet' }));
        };
        // How old everything above it is, with the button that renews it on the same line —
        // the age is the reason anyone presses it. A series being added has no reading of
        // its own, so it is dated by the catalogue reading it was drawn from.
        const readText = el('span');
        // A series with no rule is dated by the catalogue reading it was drawn from, until
        // someone refreshes it on its own — after which it is dated by that.
        let readAt = null;
        const sayRead = () => {
          const reading = existing ? (getMonitoring()[rule.id] || {}) : {};
          const stamp = existing ? (reading.read_at || reading.checked_at)
                                 : (readAt || (getSnapshot().sync || {}).synced_at);
          readText.textContent = `Last refreshed: ${stamp ? ago(stamp) : 'never'}`;
        };
        readLine.append(readText);
        sayRead();
        if (existing) sayPlan((getMonitoring()[rule.id] || {}).plan);

        // Offered while adding too. There it has no rule to check, but the counts and the
        // episode list behind them come straight from Sonarr, so it is the same question
        // asked of the same series — and the one being added is the one most likely to have
        // changed since the catalogue was read. The button lives in the pane's own bar; this
        // is only what pressing it does.
        const reread = (button) => guarded('', async () => {
          if (button) button.disabled = true;
          try {
            if (existing) {
              const data = await api('check-rule', { rule_id: rule.id, force: true },
                                     'Reading from Sonarr…', true);
              if (data.busy) throw new Error('A run is in progress. Try again when it finishes.');
              if (data.state) getMonitoring()[data.rule_id] = data.state;
              applyAlerts(data.alerts);
              renderLibrary();
              sayRead();
              sayPlan((getMonitoring()[rule.id] || {}).plan);
            } else {
              // No rule to check, so the catalogue entry itself is what goes stale: the
              // title, the season count, the next airing and the artwork all come from it.
              const data = await api('refresh-series', { instance_id: series.instance_id,
                                                         series_id: series.series_id },
                                     'Reading from Sonarr…', true);
              if (data.series) {
                Object.assign(series, data.series);
                const row = (getLibrary() || []).find(
                  (entry) => entry.series_id === series.series_id
                             && entry.instance_id === series.instance_id);
                if (row) Object.assign(row, data.series);
                renderLibrary();
              }
              readAt = data.read_at || new Date().toISOString();
              sayRead();
            }
            refreshCounts();
          } finally {
            if (button) button.disabled = false;
          }
        });
        const controls = el('div', { className: 'tvr-identity-controls' }, [enabled.node]);
        const identity = el('div', { className: 'tvr-identity' }, [
          controls,
          posterNode(series, 'tvr-poster tvr-poster-panel'),
          el('div', { className: 'tvr-identity-body' }, [
            el('div', { className: 'tvr-identity-title' }, [
              el('span', { className: 'tvr-identity-name',
                           textContent: series.title || rule.series_title }),
              // The one place it belongs: beside the name, where you are already looking at
              // this series. On every card in a list it was three thousand links to nowhere
              // anyone was going.
              sonarrLink(Object.assign({}, rule, { slug: series.slug || rule.slug,
                                                   instance_id: series.instance_id })) || text(''),
            ]),
            // The path is Sonarr's business. Nothing here is decided by it, nothing here
            // reads it, and it was the one line long enough to wrap the panel.
            originLine, countLine, nextLine, planLines,
          ]),
        ]);

        // Custom first, and what a new series starts on. A preset is a decision to share
        // values with other series, which is not what adding one usually is; offering the
        // first preset by default made that decision for you and quietly.
        const presetSelect = el('select');
        presetSelect.append(el('option', { value: '', textContent: 'Custom — values for this series only' }));
        (getSettings().profiles || []).forEach((preset) => presetSelect.append(
          el('option', { value: preset.id, textContent: `${preset.name} — ${presetSummary(preset).join(', ')}` })));
        presetSelect.value = rule.profile_id || '';
        const conditions = conditionFields(rule);
        const applyPreset = () => { conditions.node.hidden = !!presetSelect.value; };
        presetSelect.addEventListener('change', applyPreset);
        applyPreset();

        // Inheriting says what it will inherit. "Use the global setting" made you go and
        // look; naming the value means the row already answers the question.
        const globalSpecials = (getSettings().retention || {}).include_specials ? 'Include specials' : 'Exclude specials';
        const specials = options(el('select'), [['', `[Default] ${globalSpecials}`], ['no', 'Exclude specials'],
                                                ['yes', 'Include specials']],
          rule.include_specials === true ? 'yes' : (rule.include_specials === false ? 'no' : ''));
        autoReenable = toggle(
          'Switch back on if the series resumes or a newer episode appears',
          rule.auto_reenable, null, { className: 'tvr-row-switch' });
        showAutoReenable();

        // Two one-time actions, not getSettings(). They happen when you save and never again,
        // which is why each says so and says what it will ask Sonarr to do.
        const before = existing ? scopeOf(rule) : null;
        const monitorNew = toggle('Change monitor status for episodes within scope',
                                  false, null, { className: 'tvr-row-switch' });
        // What each action would actually touch, against the window as it stands in the
        // form. Without it the two toggles are a decision made blind.
        const monitorCount = el('div', { className: 'tvr-once-count' });
        const unmonitorCount = el('div', { className: 'tvr-once-count' });
        const empty = el('div', { className: 'tvr-plan-quiet', hidden: true,
                                  textContent: 'Nothing to monitor — the keep window holds no episodes.' });
        // Turning it on opens the window's own seasons and episodes, checked where Sonarr
        // monitors them now, so the choice is which ones rather than all or nothing.
        const treeBox = el('div', { className: 'tvr-tree-box', hidden: true });
        let tree = null;
        const scopeRow = el('div', { className: 'tvr-once' }, [
          el('div', { className: 'tvr-once-title', textContent: 'Monitoring in Sonarr' }),
          monitorNew.node,
          monitorCount,
          treeBox,
          empty,
        ]);
        const loadTree = () => guarded('', async () => {
          const data = await api('episodes', {
            rule_id: rule.id || '', instance_id: series.instance_id, series_id: series.series_id,
            draft: Object.assign({}, draftScope(), { include_specials: specials.value }),
          }, 'Reading episodes…', true);
          tree = monitorTree(data.seasons, { only: (episode) => episode.in_scope });
          treeBox.replaceChildren(tree.node);
        });
        monitorNew.input.addEventListener('change', () => {
          treeBox.hidden = !monitorNew.input.checked;
          if (monitorNew.input.checked) loadTree(); else { tree = null; treeBox.replaceChildren(); }
        });
        // Not a toggle. A run unmonitors everything outside the window whatever anyone
        // chooses, so offering the choice here only decided whether it happened now or
        // within a day — and off by default meant Sonarr spent that day fetching episodes
        // the next run would delete. It is stated instead, because it still happens.
        // Shown only when there is something to say. "Nothing outside the scope is
        // monitored" is a heading and a sentence to report that nothing will happen.
        const unmonitorNote = el('div', { className: 'tvr-once', hidden: true }, [
          el('div', { className: 'tvr-once-title', textContent: 'On save' }),
          unmonitorCount,
        ]);

        // Everything being done to this series that nobody asked for on this screen. The
        // getMonitoring() mode and the specials setting have controls below, and the exclusions
        // come from Exclusion Rules and from this series' own list — which is exactly why they
        // are worth stating together. "Why is this episode never deleted" should be
        // answerable here rather than by opening another page and matching in your head.
        const autoLines = el('div', { className: 'tvr-auto-lines' });
        const autoEdit = el('button', { type: 'button', className: 'tvr-action tvr-small',
                                        textContent: 'Edit…' });
        const autoRow = el('div', { className: 'tvr-once', hidden: true }, [
          el('div', { className: 'tvr-once-title' },
             [text('Exclusion Rules'), el('span', { className: 'tvr-spacer' }), autoEdit]),
          autoLines,
        ]);
        // Kept here rather than read back off `rule` each time, so the pane shows what was
        // just saved without waiting for the getSettings() to come round again.
        let manualExclusions = (rule.exclusions || []).slice();
        const sayAutomation = (data) => {
          autoRow.hidden = false;
          const found = Object.assign({ seasons: [], folders: [], episode_patterns: [],
                                        manual: 0, specials: 0, total: 0 },
                                      (data && data.exclusions) || {});
          const line = (className, label) => el('div', { className: `tvr-auto-line ${className}`,
                                                         textContent: label });
          const from = (n) => `${plural(n, 'episode')}, from Exclusion Rules`;
          const lines = [];
          lines.push(line(data.specials_default ? 'tvr-auto-inherited' : 'tvr-auto-plain',
            `Specials: ${data.specials ? 'kept' : 'excluded'} — `
            + (data.specials_default ? 'inherited from global Exclusion Rules' : 'set on this series')));
          if (found.specials) lines.push(line('tvr-auto-rule', `Specials excluded — ${from(found.specials)}`));
          found.seasons.forEach((entry) => lines.push(line('tvr-auto-rule',
            `Season ${entry.season} excluded — ${from(entry.episodes)}`)));
          found.folders.forEach((entry) => lines.push(line('tvr-auto-rule',
            `Folder matches “${entry.pattern}” — ${from(entry.episodes)}`)));
          found.episode_patterns.forEach((entry) => lines.push(line('tvr-auto-rule',
            `Matches “${entry.pattern}” — ${from(entry.episodes)}`)));
          if (found.manual) lines.push(line('tvr-auto-manual',
            `${plural(found.manual, 'episode')} excluded on this series`));
          if (!found.total) lines.push(line('tvr-auto-plain', 'Nothing is excluded from this series'));
          autoLines.replaceChildren(...lines);
        };
        const loadAutomation = () => {
          if (!existing) {
            const global = (getSettings().retention || {}).include_specials;
            sayAutomation({ specials: !!global, specials_default: true, exclusions: {} });
            return;
          }
          guarded('', async () => sayAutomation(await api('exclusions', { rule_id: rule.id }, '', true)));
        };
        // The same episode list the getMonitoring() tree reads, so the picker and the pane agree
        // about which episodes exist and which are already spoken for.
        autoEdit.addEventListener('click', () => guarded('', async () => {
          const data = await api('episodes', {
            rule_id: rule.id, instance_id: series.instance_id, series_id: series.series_id,
            draft: Object.assign({}, draftScope(), { include_specials: specials.value }),
          }, 'Reading episodes…', true);
          const tree = exclusionTree(data.seasons, manualExclusions);
          dialog(`Exclusions — ${title}`, (box) => {
            box.append(el('p', { textContent:
              'An excluded episode is never deleted and never unmonitored, whatever this '
              + 'series’ rule says. Nothing is restored or removed by saving: an exclusion '
              + 'decides what a run may touch, and a run is still the only thing that acts.' }));
            box.append(el('p', { className: 'tvr-lede', textContent:
              'Greyed episodes are excluded by Exclusion Rules, which apply to every series and '
              + 'changes there. A season’s own box excludes the whole season, including '
              + 'episodes that have not aired yet.' }));
            box.append(el('p', { className: 'tvr-lede', textContent:
              'The second column is Sonarr’s monitored flag, as Sonarr has it now. Nothing '
              + 'here ever changes it on an excluded episode, so this is the place to set '
              + 'it: whatever you leave it on stays on.' }));
            box.append(el('p', { className: 'tvr-lede tvr-tree-legend' }, [
              el('span', { className: 'tvr-tree-legend-key' }),
              text('Shaded episodes are inside the keep window as the rule stands now — '
                   + 'a run would keep them whether or not they are excluded.'),
            ]));
            box.append(tree.empty
              ? el('p', { className: 'tvr-empty', textContent: 'Sonarr has no episodes for this series.' })
              : el('div', { className: 'tvr-tree-box' }, [tree.node]));
            return tree;
          }, async (picked) => {
            const target = (getSettings().rules || []).find((other) => other.id === rule.id);
            if (!target) return;
            manualExclusions = picked.picked();
            target.exclusions = rule.exclusions = manualExclusions;
            await saveSettings('Exclusions saved.');
            const moved = picked.monitoring();
            if (moved.monitor.length || moved.unmonitor.length) {
              await api('set-monitored', Object.assign({ rule_id: rule.id }, moved),
                        'Setting monitoring in Sonarr…');
            }
            loadAutomation();
            refreshCounts();
          });
        }));

        // Counted by the worker from the episodes it already holds, and re-counted when the
        // window moves. Debounced because typing a keep value changes it on every keystroke.
        const title = series.title || rule.series_title || 'this series';
        let countTimer = null;
        const refreshCounts = () => {
          clearTimeout(countTimer);
          countTimer = setTimeout(() => guarded('', async () => {
            const scope = draftScope();
            const counts = await api('scope-counts', {
              rule_id: rule.id || '', instance_id: series.instance_id, series_id: series.series_id,
              draft: Object.assign({}, scope, { include_specials: specials.value,
                                                previous_scope: existing ? before : null }),
            }, '', true);
            sayCounts(counts.known ? counts : null);
            sayNext(counts.known ? counts : null);
            if (counts.plan) sayPlan(counts.plan);
            if (!counts.known) {
              monitorCount.textContent = unmonitorCount.textContent = '';
              return;
            }
            const monitored = counts.in_scope - counts.in_scope_unmonitored;
            monitorCount.textContent =
              `${monitored} of ${counts.in_scope} episodes in ${title}'s keep scope are monitored`;
            // Nothing in the window means nothing to decide, so the toggle goes and says why.
            scopeRow.hidden = false;
            monitorNew.node.hidden = counts.in_scope === 0;
            monitorCount.hidden = counts.in_scope === 0;
            empty.hidden = counts.in_scope > 0;
            if (counts.in_scope === 0) {
              monitorNew.input.checked = false;
              treeBox.hidden = true;
            }
            unmonitorNote.hidden = !counts.out_scope_monitored;
            unmonitorCount.textContent = counts.out_scope_monitored
              ? `${counts.out_scope_monitored} of ${counts.out_scope} episodes outside ${title}'s keep scope are `
                + 'monitored, and will be unmonitored so Sonarr stops fetching them'
              : '';
          }), 250);
        };
        const draftScope = () => ({
          profile_id: presetSelect.value || '',
          keep_days: presetSelect.value ? null : (conditions.days.value || null),
          keep_episodes: presetSelect.value ? null : (conditions.episodes.value || null),
          keep_seasons: presetSelect.value ? null : (conditions.seasons.value || null),
          combine: conditions.combine.value,
        });
        // The window's own contents are always worth showing; whether they are worth
        // changing is the operator's business, not a rule about widening.
        const updateScopeRow = () => { if (monitorNew.input.checked && tree) loadTree(); };
        [presetSelect, conditions.days, conditions.episodes, conditions.seasons,
         conditions.combine, specials].forEach((input) => {
          input.addEventListener('change', () => { updateScopeRow(); refreshCounts(); });
          input.addEventListener('input', refreshCounts);
        });
        setTimeout(() => { updateScopeRow(); refreshCounts(); loadAutomation(); }, 0);

        top.append(identity);
        // The banner names what this form does, under the series it does it to. Enabling a
        // series is not one of the things it does, which is why the switch sits above it.
        const formBanner = el('div', { className: 'tvr-form-banner',
                                       textContent: existing ? 'Edit series' : 'Add series' });
        top.append(formBanner);
        if (existing) {
          const queued = queuedBanner(rule);
          if (queued) body.append(queued);
          const alertsHere = seriesAlerts(rule.id);
          if (alertsHere.length) body.append(seriesAlertCard(rule, alertsHere, { compact: true }));
        }
        const automationBox = el('div', { className: 'tvr-automation-box' }, [
          el('div', { className: 'tvr-automation-title', textContent: 'Exclusion Rules' }),
          el('p', { className: 'tvr-lede', textContent:
            'Global exclusions apply to every series. The value below is this series’ override; '
            + 'episode exclusions are shown for context and edited in their own picker.' }),
          field('Season 0 / specials', specials), autoRow,
        ]);
        body.append(
          field('Retention', presetSelect, (getSettings().profiles || []).length
            ? 'Presets are managed under Series.' : 'No presets yet — create one to reuse values.'),
          conditions.node,
          autoReenable.node,
          automationBox, scopeRow, unmonitorNote);
        const formState = () => JSON.stringify({ profile_id: presetSelect.value,
                                          keep_days: conditions.days.value,
                                          keep_episodes: conditions.episodes.value,
                                          keep_seasons: conditions.seasons.value,
                                          combine: conditions.combine.value,
                                          include_specials: specials.value,
                                          auto_reenable: autoReenable.input.checked,
                                          once: monitorNew.input.checked });
        // What Update compares against: the rule as saved, captured before any half-typed
        // draft is put back. Taken after the restore it would call the draft the baseline,
        // and Update would sit disabled over changes nobody had saved.
        const saved = formState();
        const held = drafts.get(draftKey);
        if (held && held !== saved) {
          try {
            const values = JSON.parse(held);
            presetSelect.value = values.profile_id || '';
            conditions.days.value = values.keep_days || '';
            conditions.episodes.value = values.keep_episodes || '';
            conditions.seasons.value = values.keep_seasons || '';
            conditions.combine.value = values.combine || 'any';
            specials.value = values.include_specials || '';
            applyPreset();
            sayState();
          } catch (error) {
            drafts.delete(draftKey);      // unreadable is not worth carrying
          }
        }
        return { presetSelect, conditions, specials, monitorNew,
                 autoReenable, before, enabled, draftKey, saved, reread, readLine, formBanner,
                 tree: () => (monitorNew.input.checked ? tree : null),
                 // A rule needs somewhere to keep from: a preset, or at least one value.
                 valid: () => conditions.valid() && !!(presetSelect.value || conditions.days.value
                                                        || conditions.episodes.value || conditions.seasons.value),
                 state: formState };
      },
        save: async (context, startEnabled) => {
        const draft = {
          id: rule.id || undefined,
          enabled: existing ? context.enabled.input.checked : !!startEnabled,
          instance_id: series.instance_id,
          profile_id: context.presetSelect.value || '',
          keep_days: context.presetSelect.value ? null : (context.conditions.days.value || null),
          keep_episodes: context.presetSelect.value ? null : (context.conditions.episodes.value || null),
          keep_seasons: context.presetSelect.value ? null : (context.conditions.seasons.value || null),
          combine: context.conditions.combine.value,
          include_specials: context.specials.value,
          auto_reenable: context.autoReenable.input.checked,
          queue: rule.queue || undefined,
        };
        if (!existing && series.selectable === false) {
          throw new Error(`${series.title} cannot be used: ${series.reason}.`);
        }
        Object.assign(draft, { series_id: series.series_id, series_title: series.title || rule.series_title,
                               tvdb_id: series.tvdb_id, path: series.path || rule.path });
        getSettings().rules = (getSettings().rules || []).filter((other) => other.id !== rule.id).concat([draft]);
        await saveSettings(null);
        const matched = await api('match', {}, 'Matching against Sonarr…');
        applySaved(matched.settings);

        render();
        forgetLibrary();           // one more series with a rule
        const saved = getSettings().rules[getSettings().rules.length - 1];
        let done = '';
        // Applied now, against the saved rule, because the unmonitor half exists to stop
        // downloads that would otherwise happen before the next run.
        if (saved) {
          // Outside the window is not a choice: a run unmonitors it regardless, so waiting
          // only gives Sonarr a day to fetch what that run would delete.
          const pass = await api('scope-pass', {
            rule_id: saved.id, monitor_new: false, unmonitor_outside: true,
          }, 'Setting getMonitoring() in Sonarr…');
          const parts = [];
          if (pass.unmonitored) parts.push(`${plural(pass.unmonitored, 'episode')} unmonitored`);
          // Inside the window is entirely the operator's: exactly what the tree was left
          // showing, and only where it differs from what Sonarr already has.
          const wanted = context.tree && context.tree();
          const changes = wanted ? wanted.changes() : { monitor: [], unmonitor: [] };
          if (changes.monitor.length || changes.unmonitor.length) {
            const applied = await api('set-monitored', Object.assign({ rule_id: saved.id }, changes),
                                      'Setting getMonitoring() in Sonarr…');
            if (applied.monitored) parts.push(`${plural(applied.monitored, 'episode')} monitored`);
            if (applied.unmonitored) parts.push(`${plural(applied.unmonitored, 'episode')} unmonitored`);
          }
          done = parts.length ? ` ${parts.join(', ')} in Sonarr.` : '';
        }
        if (saved) queueChecks([saved.id]);
        notice(`Series saved.${done}`, 'ok');
        renderDetails();
      },
      };
    }

    // -- the details pane --------------------------------------------------
    // Editing happens here rather than in a dialog: the list stays visible beside it, so
    // what you are changing is never the only thing on screen.
    // One series at a time, and only ever the one being edited. There is no selection to
    // build: the bulk mechanisms are a preset, which moves every series pointing at it, and
    // a default, which moves every series inheriting it — each one edit with a blast radius
    // you can name. Thirty ticked boxes and a forgotten one is not a third, and this app
    // deletes things.
    let editing = null;          // the form currently open in the pane, if any
    const isOpen = (rule, series) => !!(editing && (
      (rule && editing.rule && editing.rule.id === rule.id)
      || (!rule && series && editing.series
          && editing.series.instance_id === series.instance_id
          && editing.series.series_id === series.series_id)
    ));

    // Half-typed edits, kept while the getLibrary() is on screen. Clicking a second poster to
    // check something and clicking back is browsing, not abandoning: nothing was saved, so
    // nothing should be lost. Leaving the getLibrary() is leaving, and clears them — held any
    // longer they would be a second, invisible copy of the getSettings().
    const drafts = new Map();
    const forgetDrafts = () => drafts.clear();

    function openEditor(existing, preselect) {
      const form = ruleForm(existing, preselect);
      if (!form) return;
      editing = form;
      openLibraryView();
      renderDetails();
      // A different series starts at its own top. The pane is the scroller, and replacing
      // its contents leaves the scroll position where the last series had put it.
      $('tvr-details').scrollTop = 0;
      $('tvr-details').scrollIntoView({ block: 'nearest' });
    }

    function renderDetails() {
      const pane = $('tvr-details');
      const shell = $('tvr-series-shell');
      // Always open. It used to appear on selection, which narrowed the list beside it and
      // reflowed the grid — sliding the card you had just clicked out from under the
      // pointer. An empty pane costs a column and removes that entirely.
      shell.classList.add('open');
      pane.replaceChildren();
      if (!editing) {
        pane.append(el('div', { className: 'tvr-details-head' },
                       [el('h3', { textContent: 'Series details' })]));
        pane.append(el('div', { className: 'tvr-details-idle' },
                       [el('p', { textContent: "Select a series to view or modify its details." })]));
        return;
      }

      const head = el('div', { className: 'tvr-details-head' });
      const close = el('button', { type: 'button', className: 'tvr-icon-button', title: 'Close' },
                      [el('i', { className: 'fa fa-times' })]);
      close.addEventListener('click', () => { editing = null; renderLibrary(); renderDetails(); });

      {
        // The pane's own title, not the form's: what is below it is this series, whether it
        // is being added or edited, and both buttons belong to the pane rather than to the
        // form under it.
        const refresh = el('button', { type: 'button', className: 'tvr-icon-button',
                                       title: 'Re-read this series from Sonarr' },
                           [el('i', { className: 'fa fa-refresh' })]);
        // Two lines in the space the buttons already take: the second is set small enough
        // that the bar is still as tall as the icons beside it and no taller.
        const headMain = el('div', { className: 'tvr-details-head-main' },
                            [el('h3', { textContent: 'Series details' })]);
        head.append(headMain, el('span', { className: 'tvr-spacer' }), refresh, close);
        pane.append(head);
        // Fixed: the series, and what the next run would do to it. Only the getSettings() below
        // scroll, so neither can be scrolled out from under the other.
        const top = el('div', { className: 'tvr-details-top' });
        const body = el('div', { className: 'tvr-details-body' });
        pane.append(top, body);
        const context = editing.build(body, top);
        editing.context = context;
        refresh.addEventListener('click', () => context.reread(refresh));
        headMain.append(context.readLine);
        if (!editing.existing && !editing.expanded) {
          context.formBanner.hidden = true;
          body.hidden = true;
          const add = el('button', { type: 'button', className: 'tvr-primary',
                                     textContent: 'Add to Retention' });
          add.addEventListener('click', () => {
            editing.expanded = true;
            renderDetails();
          });
          pane.append(el('div', { className: 'tvr-details-add' }, [add]));
          return;
        }
        const actions = el('div', { className: 'tvr-actions' });
        const commit = (startEnabled) => guarded('', async () => {
          await editing.save(context, startEnabled);
          drafts.delete(context.draftKey);
          editing = null;
          renderDetails();
        });
        // Adding a series and switching it on are two decisions, so they are two buttons
        // rather than a switch that has to be found first. Nothing runs against a series
        // saved with the plain one until someone says so.
        const primary = el('button', { type: 'button', className: 'tvr-primary',
                                       textContent: editing.existing ? 'Update' : 'Save and enable' });
        primary.addEventListener('click', () => commit(true));
        const buttons = [primary];
        actions.append(primary);
        if (!editing.existing) {
          const quiet = el('button', { type: 'button', className: 'tvr-secondary', textContent: 'Save' });
          quiet.dataset.hint = 'Added switched off. No run touches it until it is enabled.';
          quiet.addEventListener('click', () => commit(false));
          buttons.push(quiet);
          actions.append(quiet);
        }
        // Adding needs a keep window to be worth anything; updating needs something to have
        // changed as well, so the button says whether pressing it would do something.
        const check = () => {
          const valid = context.valid();
          const now = context.state();
          // Kept as it stands, so clicking away and back returns to what was typed.
          if (now === context.saved) drafts.delete(context.draftKey);
          else drafts.set(context.draftKey, now);
          buttons.forEach((button) => {
            button.disabled = !valid || (editing.existing && now === context.saved);
            button.title = !valid ? 'Set a preset, or at least one keep value'
              : (button.disabled ? 'Nothing has changed' : (button.dataset.hint || ''));
          });
        };
        body.addEventListener('input', check);
        body.addEventListener('change', check);
        if (editing.existing) {
          const remove = el('button', { type: 'button', className: 'tvr-danger tvr-small', textContent: 'Delete…' });
          remove.addEventListener('click', () => deleteSeries(editing.rule));
          actions.append(el('span', { className: 'tvr-spacer' }), remove);
        }
        pane.append(actions);
        check();
      }
    }

    // A rule's keep window, as the one-time pass needs to remember it.
    function scopeOf(rule) {
      return { keep_days: rule.keep_days ?? null, keep_episodes: rule.keep_episodes ?? null,
               keep_seasons: rule.keep_seasons ?? null, combine: rule.combine || 'any',
               profile_id: rule.profile_id || '' };
    }

  return { editing: () => editing, setEditing: (v) => { editing = v; },
           isOpen, openEditor, renderDetails, forgetDrafts };
}
