/* The two episode pickers.
 *
 * Excluding is not monitoring, so these are two trees rather than one with a mode. A tick
 * in the exclusion tree means "never touch this" and is stored by season and episode
 * *number*; a tick in the monitoring tree means "Sonarr should have this" and is read
 * back as episode *ids*. The day those share a code path is the day one of them is wrong.
 */
'use strict';

import { el } from './dom.js';

// Every reason core can give, said in the picker's own words. A reason with no entry
// here falls back to naming Automation rather than to a template with a hole in it.
const EXCLUDED_WHY = {
  specials: () => 'specials',
  season: (episode) => `excluded season ${episode.excluded_by}`,
  folder: (episode) => `folder matches “${episode.excluded_by}”`,
  episode: (episode) => `matches “${episode.excluded_by}”`,
};

// Excluding is not monitoring, so this is its own tree rather than a mode of the other
// one. A tick here means "never touch this", a tick there means "Sonarr should have
// this", and the day those two share a code path is the day one of them is wrong.
//
// A season's own box means the whole season, including episodes that do not exist yet —
// which is the only thing ticking every episode individually cannot say, and the reason
// the stored list has an entry shaped that way.
function exclusionTree(seasons, current) {
  const node = el('div', { className: 'tvr-tree' });
  const wholeSeasons = new Set((current || [])
    .filter((entry) => entry.episode === null || entry.episode === undefined)
    .map((entry) => entry.season));
  const picked = new Set((current || [])
    .filter((entry) => entry.episode !== null && entry.episode !== undefined)
    .map((entry) => `${entry.season}:${entry.episode}`));
  const seasonRows = [];

  (seasons || []).forEach((season) => {
    const rows = season.episodes || [];
    if (!rows.length) return;
    // An episode excluded by Automation is shown and not offered. The rule that put it
    // there is global, so unticking it here would be an override with nowhere to live —
    // and saying that out loud is more use than a box that springs back.
    const auto = rows.filter((episode) => episode.excluded && episode.excluded !== 'manual');
    const locked = auto.length === rows.length;
    const box = el('input', { type: 'checkbox', className: 'tvr-pick',
                              checked: wholeSeasons.has(season.season), disabled: locked });
    const count = el('span', { className: 'tvr-tree-count' });
    const caret = el('button', { type: 'button', className: 'tvr-tree-caret' },
                     [el('i', { className: 'fa fa-caret-right' })]);
    const list = el('div', { className: 'tvr-tree-episodes', hidden: true });
    caret.addEventListener('click', (event) => {
      event.preventDefault();
      list.hidden = !list.hidden;
      caret.firstChild.className = `fa fa-caret-${list.hidden ? 'right' : 'down'}`;
    });
    const header = el('label', { className: 'tvr-tree-row tvr-tree-season',
                                 title: locked ? 'Every episode of this season is excluded by Automation'
                                               : 'Exclude the whole season, including episodes not yet aired' }, [
      box, el('span', { textContent: season.season === 0 ? 'Specials' : `Season ${season.season}` }), count,
    ]);
    const ticks = [];
    rows.forEach((episode) => {
      const isAuto = episode.excluded && episode.excluded !== 'manual';
      const tick = el('input', { type: 'checkbox', className: 'tvr-pick',
                                 checked: isAuto || picked.has(`${episode.season}:${episode.episode}`),
                                 disabled: isAuto });
      // Named, not inferred. "Anything that is not a season is a pattern" renders a
      // reason nobody taught this about as `matches “undefined”`, which is worse than
      // saying less.
      const why = !isAuto ? '' : (EXCLUDED_WHY[episode.excluded] || (() => 'excluded by Automation'))(episode);
      list.append(el('label', { className: `tvr-tree-row tvr-tree-episode${isAuto ? ' tvr-tree-auto' : ''}` }, [
        tick,
        el('span', { textContent: `E${String(episode.episode).padStart(2, '0')} · ${episode.title || ''}` }),
        el('span', { className: 'tvr-tree-note tvr-tree-why', textContent: why }),
      ]));
      if (!isAuto) ticks.push({ tick, episode });
      tick.addEventListener('change', refresh);
    });
    box.addEventListener('change', () => {
      ticks.forEach((entry) => { entry.tick.disabled = box.checked;
                                 if (box.checked) entry.tick.checked = true; });
      refresh();
    });
    ticks.forEach((entry) => { entry.tick.disabled = box.checked; });
    seasonRows.push({ season: season.season, box, ticks, count, total: rows.length, auto: auto.length });
    node.append(el('div', { className: 'tvr-tree-season-wrap' },
                   [el('div', { className: 'tvr-tree-head' }, [caret, header]), list]));
  });

  function refresh() {
    seasonRows.forEach((row) => {
      const on = row.box.checked ? row.total
        : row.auto + row.ticks.filter((entry) => entry.tick.checked).length;
      row.count.textContent = `${on}/${row.total} excluded`;
    });
  }
  refresh();

  return {
    node,
    empty: !seasonRows.length,
    // Only the hand-picked half. What Automation excludes is not stored per series, so
    // reading the ticked boxes back wholesale would bake a global rule into this one
    // series and leave it there after the rule changed.
    picked: () => {
      const found = [];
      seasonRows.forEach((row) => {
        if (row.box.checked) { found.push({ season: row.season, episode: null }); return; }
        row.ticks.forEach((entry) => {
          if (entry.tick.checked) found.push({ season: row.season, episode: entry.episode.episode });
        });
      });
      return found;
    },
  };
}

// Seasons and episodes with a checkbox each, checked where Sonarr monitors them now.
// The series and season boxes are three-state, because "some of this" is a real answer
// and a box that can only say yes or no would have to lie about it.
function monitorTree(seasons, options) {
  const only = (options || {}).only;                // a filter over which episodes show
  const state = new Map();                          // episode id -> wanted, as displayed
  const episodeBoxes = new Map();
  const seasonBoxes = [];
  const node = el('div', { className: 'tvr-tree' });

  const seriesBox = el('input', { type: 'checkbox', className: 'tvr-pick' });
  const seriesRow = el('label', { className: 'tvr-tree-row tvr-tree-series' }, [
    seriesBox, el('span', { textContent: 'All of it' }),
    el('span', { className: 'tvr-tree-count' }),
  ]);
  node.append(seriesRow);

  const setThree = (box, on, off) => {
    box.checked = on > 0 && off === 0;
    box.indeterminate = on > 0 && off > 0;
  };
  const refresh = () => {
    let allOn = 0, allOff = 0;
    seasonBoxes.forEach((entry) => {
      let on = 0, off = 0;
      entry.ids.forEach((id) => { if (state.get(id)) on += 1; else off += 1; });
      setThree(entry.box, on, off);
      entry.count.textContent = `${on}/${entry.ids.length} monitored`;
      allOn += on; allOff += off;
    });
    setThree(seriesBox, allOn, allOff);
    seriesRow.querySelector('.tvr-tree-count').textContent = `${allOn}/${allOn + allOff} monitored`;
    if (options && options.onChange) options.onChange();
  };

  (seasons || []).forEach((season) => {
    const rows = (season.episodes || []).filter((episode) => !only || only(episode));
    if (!rows.length) return;
    const ids = rows.map((episode) => episode.episode_id);
    ids.forEach((id, index) => state.set(id, !!rows[index].monitored));

    const box = el('input', { type: 'checkbox', className: 'tvr-pick' });
    const count = el('span', { className: 'tvr-tree-count' });
    const caret = el('button', { type: 'button', className: 'tvr-tree-caret' },
                     [el('i', { className: 'fa fa-caret-right' })]);
    const list = el('div', { className: 'tvr-tree-episodes', hidden: true });
    const header = el('label', { className: 'tvr-tree-row tvr-tree-season' }, [
      box, el('span', { textContent: season.season === 0 ? 'Specials' : `Season ${season.season}` }), count,
    ]);
    caret.addEventListener('click', (event) => {
      event.preventDefault();
      list.hidden = !list.hidden;
      caret.firstChild.className = `fa fa-caret-${list.hidden ? 'right' : 'down'}`;
    });
    const wrap = el('div', { className: 'tvr-tree-season-wrap' },
                    [el('div', { className: 'tvr-tree-head' }, [caret, header]), list]);

    rows.forEach((episode) => {
      const tick = el('input', { type: 'checkbox', className: 'tvr-pick',
                                 checked: !!episode.monitored });
      tick.addEventListener('change', () => { state.set(episode.episode_id, tick.checked); refresh(); });
      episodeBoxes.set(episode.episode_id, tick);
      list.append(el('label', { className: 'tvr-tree-row tvr-tree-episode' }, [
        tick,
        el('span', { textContent: `E${String(episode.episode).padStart(2, '0')} · ${episode.title || ''}` }),
        el('span', { className: 'tvr-tree-note',
                     textContent: episode.has_file ? '' : 'no file' }),
      ]));
    });

    box.addEventListener('change', () => {
      ids.forEach((id) => {
        state.set(id, box.checked);
        episodeBoxes.get(id).checked = box.checked;
      });
      refresh();
    });
    seasonBoxes.push({ box, ids, count });
    node.append(wrap);
  });

  seriesBox.addEventListener('change', () => {
    state.forEach((value, id) => {
      state.set(id, seriesBox.checked);
      episodeBoxes.get(id).checked = seriesBox.checked;
    });
    refresh();
  });
  refresh();

  return {
    node,
    empty: state.size === 0,
    // Only what differs from what Sonarr already has: pressing save without touching a
    // box should ask Sonarr for nothing.
    changes: () => {
      const monitor = [], unmonitor = [];
      (seasons || []).forEach((season) => (season.episodes || []).forEach((episode) => {
        if (!state.has(episode.episode_id)) return;
        const wanted = state.get(episode.episode_id);
        if (wanted === !!episode.monitored) return;
        (wanted ? monitor : unmonitor).push(episode.episode_id);
      }));
      return { monitor, unmonitor };
    },
  };
}

export { EXCLUDED_WHY, exclusionTree, monitorTree };
