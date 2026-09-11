/* Building elements, and the controls that are built the same way everywhere.
 *
 * Every function here touches the DOM when it is *called*, and none of them touch it
 * while this module is imported — which is the rule for every module but the entry, and
 * the reason `$` is a function rather than a lookup performed up front.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, props, children) => {
  const node = Object.assign(document.createElement(tag), props || {});
  (children || []).forEach((child) => node.append(child));
  return node;
};
const text = (value) => document.createTextNode(value);

// One switch implementation, so a toggle looks and behaves the same everywhere it
// appears: on a series card, in a dialog, or in the page header.
function toggle(label, checked, onChange, options) {
  const config = options || {};
  const input = el('input', { type: 'checkbox', checked: !!checked, disabled: !!config.disabled });
  if (config.label) input.setAttribute('aria-label', config.label);
  const caption = el('span', { className: 'tvr-switch-text', textContent: label });
  const node = el('label', { className: `tvr-switch${config.className ? ' ' + config.className : ''}`,
                             title: config.title || '' },
                  [input, el('span', { className: 'tvr-slider' }), caption]);
  if (onChange) input.addEventListener('change', () => onChange(input.checked, input, caption));
  return { node, input, caption };
}


function field(label, control, note) {
  const wrapper = el('label', { className: 'tvr-field' }, [el('span', { textContent: label }), control]);
  if (note) wrapper.append(el('small', { textContent: note }));
  return wrapper;
}

function options(select, values, selected) {
  select.replaceChildren();
  values.forEach(([value, label]) => select.append(el('option', { value: String(value), textContent: label })));
  if (selected !== undefined && selected !== null) select.value = String(selected);
  return select;
}

export { $, el, text, toggle, field, options };
