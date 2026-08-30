/* =====================================================================
 * agentops/ui/toast.js
 * Toast primitive. Stack, auto-dismiss, ARIA live region.
 * ===================================================================== */

import { h } from '../lib/dom.js';

let region = null;
function getRegion() {
  if (region) return region;
  region = h('div', {
    class: 'toast-region',
    role: 'region',
    'aria-label': 'Notifications',
    'aria-live': 'polite',
  });
  document.body.append(region);
  return region;
}

const ICONS = {
  success: '✓',
  error:   '!',
  info:    'i',
  warn:    '!',
};

export function toast({ title, message, kind = 'info', durationMs = 5000 } = {}) {
  const r = getRegion();
  const t = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('div', { class: 'toast-icon', 'aria-hidden': 'true' }, ICONS[kind] || 'i'),
    h('div', { class: 'toast-body' },
      title ? h('div', { class: 'toast-title' }, title) : null,
      message ? h('div', { class: 'toast-message' }, message) : null
    ),
    h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss', onClick: () => dismiss(t) }, '×')
  );
  r.append(t);
  requestAnimationFrame(() => t.classList.add('is-visible'));
  const timer = setTimeout(() => dismiss(t), durationMs);
  t.addEventListener('mouseenter', () => clearTimeout(timer));
  return t;
}

function dismiss(t) {
  t.classList.remove('is-visible');
  setTimeout(() => t.remove(), 200);
}

export const toastSuccess = (msg, title) => toast({ title, message: msg, kind: 'success' });
export const toastError   = (msg, title) => toast({ title, message: msg, kind: 'error', durationMs: 8000 });
export const toastInfo    = (msg, title) => toast({ title, message: msg, kind: 'info' });
export const toastWarn    = (msg, title) => toast({ title, message: msg, kind: 'warn' });
