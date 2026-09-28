import { $, el } from './dom.js';
import { ago } from './format.js';
import { notice } from './feedback.js';
import { remember, remembered } from './storage.js';

const dateKey = (date) => date.toISOString().slice(0, 10);
const today = () => dateKey(new Date());
const dateOf = (key) => new Date(`${key}T12:00:00Z`);
const labelDay = (key) => dateOf(key).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', timeZone: 'UTC' });

export function createCalendar({ api }) {
  let events = [];
  let syncedAt = '';
  let loaded = false;
  let pending = false;
  let mode = remembered('calendar.layout', 'list') === 'calendar' ? 'calendar' : 'list';
  let month = today().slice(0, 7);

  function grouped() {
    const result = new Map();
    for (const event of events) {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(event.date)) continue;
      if (!result.has(event.date)) result.set(event.date, []);
      result.get(event.date).push(event);
    }
    return result;
  }

  function eventNode(event) {
    const caption = event.kind === 'airing' ? 'Airing'
      : event.kind === 'queued' ? 'Queued removal' : 'Estimated deletion';
    const episode = event.episode;
    const number = episode && episode.season != null && episode.number != null
      ? `S${String(episode.season).padStart(2, '0')}E${String(episode.number).padStart(2, '0')}` : '';
    const episodeText = episode ? [number, episode.title].filter(Boolean).join(' · ') : '';
    const row = el('div', { className: `tvr-calendar-event ${event.kind}` }, [
      el('span', { className: 'tvr-calendar-kind', textContent: caption }),
      el('span', { textContent: [event.title, episodeText].filter(Boolean).join(' · ') }),
    ]);
    if (event.detail) row.append(el('small', { textContent: event.detail }));
    return row;
  }

  function renderCalendar() {
    $('tvr-calendar-list').classList.toggle('active', mode === 'list');
    $('tvr-calendar-grid').classList.toggle('active', mode === 'calendar');
    $('tvr-calendar-list').setAttribute('aria-pressed', String(mode === 'list'));
    $('tvr-calendar-grid').setAttribute('aria-pressed', String(mode === 'calendar'));
    $('tvr-calendar-month-nav').hidden = mode !== 'calendar';
    $('tvr-calendar-reading').textContent = pending ? 'Reading cached calendar…'
      : !loaded ? 'Calendar has not loaded yet.'
      : `From cached Sonarr data${syncedAt ? ` · synced ${ago(syncedAt)}` : ' · sync age unknown'}. Predictions can change; only a fresh run decides what to delete.`;
    const box = $('tvr-calendar-events');
    box.replaceChildren();
    if (mode === 'list') {
      box.className = 'tvr-calendar-list';
      for (const [day, items] of [...grouped()].sort(([a], [b]) => a.localeCompare(b))) {
        const section = el('section', { className: 'tvr-calendar-day' }, [
          el('h3', { textContent: labelDay(day) }),
          ...items.map(eventNode),
        ]);
        box.append(section);
      }
    } else {
      box.className = 'tvr-calendar-grid';
      const first = dateOf(`${month}-01`);
      const last = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0, 12));
      $('tvr-calendar-month').textContent = first.toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' });
      for (const weekday of ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']) {
        box.append(el('span', { className: 'tvr-calendar-weekday', textContent: weekday }));
      }
      for (let blank = 0; blank < first.getUTCDay(); blank += 1) box.append(el('div', { className: 'tvr-calendar-blank' }));
      const byDay = grouped();
      for (let day = 1; day <= last.getUTCDate(); day += 1) {
        const key = `${month}-${String(day).padStart(2, '0')}`;
        box.append(el('div', { className: `tvr-calendar-cell${key === today() ? ' today' : ''}` }, [
          el('strong', { textContent: String(day) }),
          ...(byDay.get(key) || []).map(eventNode),
        ]));
      }
    }
    if (loaded && !events.length) box.append(el('p', { className: 'tvr-empty', textContent: 'No upcoming airings or projected deletions in the next six weeks.' }));
  }

  async function load() {
    if (pending) return;
    pending = true;
    renderCalendar();
    try {
      const data = await api('calendar', {}, '', true);
      events = data.events || [];
      syncedAt = data.synced_at || '';
      loaded = true;
    } catch (error) {
      notice(`Could not read calendar: ${error.message}`, 'bad');
    } finally {
      pending = false;
      renderCalendar();
    }
  }

  function wire() {
    for (const [id, layout] of [['tvr-calendar-list', 'list'], ['tvr-calendar-grid', 'calendar']]) {
      $(id).addEventListener('click', () => { mode = layout; remember('calendar.layout', mode); renderCalendar(); });
    }
    $('tvr-calendar-refresh').addEventListener('click', load);
    for (const [id, direction] of [['tvr-calendar-prev', -1], ['tvr-calendar-next', 1]]) {
      $(id).addEventListener('click', () => {
        const current = dateOf(`${month}-01`);
        month = dateKey(new Date(Date.UTC(current.getUTCFullYear(), current.getUTCMonth() + direction, 1, 12))).slice(0, 7);
        renderCalendar();
      });
    }
    renderCalendar();
  }

  return { wire, load };
}
