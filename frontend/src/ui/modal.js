/* =====================================================================
 * agentops/ui/modal.js
 * Modal primitive. Focus trap, ESC close, scroll lock, ARIA dialog.
 * ===================================================================== */

import { h } from '../lib/dom.js';

const openStack = [];

function focusable(root) {
  return Array.from(root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  ));
}

export function createModal(opts = {}) {
  const {
    title,
    body,
    footer,
    size = 'md',        // sm | md | lg | full
    closeOnBackdrop = true,
    onClose,
  } = opts;

  const overlay = h('div', {
    class: `modal-overlay modal-size-${size}`,
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': title || 'Dialog',
    tabIndex: -1,
  });

  const dialog = h('div', { class: 'modal-dialog' });

  const head = h('header', { class: 'modal-head' },
    title ? h('h2', { class: 'modal-title' }, title) : null,
    h('button', {
      type: 'button', class: 'modal-close', 'aria-label': 'Close',
      onClick: () => close(),
    }, '×')
  );

  const bodyEl = h('div', { class: 'modal-body' });
  if (body) {
    if (typeof body === 'string') bodyEl.textContent = body;
    else if (body instanceof Node) bodyEl.append(body);
  }

  const footerEl = footer ? h('footer', { class: 'modal-foot' }, footer instanceof Node ? footer : null) : null;
  if (footerEl && typeof footer === 'string') footerEl.textContent = footer;

  dialog.append(head, bodyEl);
  if (footerEl) dialog.append(footerEl);
  overlay.append(dialog);

  let prevFocus = null;
  function onKey(e) {
    if (e.key === 'Escape') { e.stopPropagation(); close(); return; }
    if (e.key !== 'Tab') return;
    const f = focusable(dialog);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function onBackdropClick(e) {
    if (e.target === overlay && closeOnBackdrop) close();
  }

  function open() {
    prevFocus = document.activeElement;
    document.body.append(overlay);
    document.body.classList.add('modal-open');
    openStack.push(overlay);
    overlay.addEventListener('keydown', onKey);
    overlay.addEventListener('mousedown', onBackdropClick);
    requestAnimationFrame(() => {
      overlay.classList.add('is-open');
      const f = focusable(dialog);
      (f[0] || dialog).focus();
    });
  }

  function close() {
    overlay.classList.remove('is-open');
    overlay.removeEventListener('keydown', onKey);
    overlay.removeEventListener('mousedown', onBackdropClick);
    setTimeout(() => {
      overlay.remove();
      const i = openStack.indexOf(overlay);
      if (i >= 0) openStack.splice(i, 1);
      if (!openStack.length) document.body.classList.remove('modal-open');
      if (prevFocus && prevFocus.focus) prevFocus.focus();
      if (onClose) onClose();
    }, 200);
  }

  // Public surface
  overlay.open = open;
  overlay.close = close;
  overlay.body = bodyEl;
  overlay.dialog = dialog;
  overlay.setTitle = (t) => { const h2 = head.querySelector('.modal-title'); if (h2) h2.textContent = t; };

  return overlay;
}
