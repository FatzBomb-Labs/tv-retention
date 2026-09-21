/* The three ways the page speaks to the reader: the notice bar, the error guard that
 * writes to it, and the modal dialog.
 *
 * `guarded` is why almost nothing else here catches: it clears the bar, runs the work,
 * and turns whatever escaped into a message. The dialog routes its confirm handler
 * through it for the same reason — a save that throws inside a modal would otherwise
 * close it silently, and the reader would be left assuming it worked.
 */
'use strict';

import { $, el } from './dom.js';

function notice(message, kind) {
  const box = $('tvr-notice');
  box.textContent = message;
  box.className = kind || '';
  box.hidden = !message;
  if (message) box.scrollIntoView({ block: 'nearest' });
}

async function guarded(label, work) {
  try { notice(''); await work(); } catch (error) { notice(error.message, 'bad'); }
}

function dialog(title, buildBody, onOk, okLabel) {
  const box = $('tvr-dialog');
  const body = $('tvr-dialog-body');
  const cancel = box.querySelector('button[value="cancel"]');
  // The dialog's aria-labelledby points here, so every dialog is announced by its
  // own title rather than as an unnamed modal.
  body.replaceChildren(el('h3', { id: 'tvr-dialog-title', textContent: title }));
  $('tvr-dialog-extra').replaceChildren();
  $('tvr-dialog-ok').disabled = false;
  const context = buildBody(body);
  $('tvr-dialog-ok').textContent = okLabel || 'Save';
  $('tvr-dialog-ok').hidden = !onOk;
  if (cancel) cancel.textContent = onOk ? 'Cancel' : (okLabel || 'Cancel');
  const handler = async () => {
    box.removeEventListener('close', handler);
    if (box.returnValue !== 'ok' || !onOk) return;
    await guarded('', () => onOk(context));
  };
  box.addEventListener('close', handler);
  box.showModal();
}

export { notice, guarded, dialog };
