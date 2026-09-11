/* Turning values into the words the page says.
 *
 * Pure: no DOM, no clock beyond `Date.now()` inside `ago`, nothing to configure. These
 * were the first things to move out of the entry because nothing about them depends on
 * the page existing, which is also why they are the easiest to be sure about.
 */
'use strict';

const bytes = (value) => {
  if (!value) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let index = 0, size = Number(value);
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
};
const when = (iso) => (iso ? new Date(iso).toLocaleString() : '—');
// "series" is already plural; nothing ending in s takes another one.
const plural = (count, word) => `${count} ${word}${count === 1 || word.endsWith('s') ? '' : 's'}`;
function ago(stamp) {
  if (!stamp) return 'never checked';
  const seconds = Math.max(0, (Date.now() - new Date(stamp).getTime()) / 1000);
  if (seconds < 90) return 'just now';
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

const range = (from, to, pad) => {
  const out = [];
  for (let index = from; index <= to; index += 1) {
    out.push([index, pad ? String(index).padStart(2, '0') : String(index)]);
  }
  return out;
};

export { bytes, when, plural, ago, range };
