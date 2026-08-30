/* =====================================================================
 * agentops/ui/dropdown.js
 * Dropdown menu primitive. ARIA menu, keyboard navigation.
 * ===================================================================== */

import { h } from '../lib/dom.js';

export function createDropdown(opts = {}) {
  const { trigger, items = [], placement = 'bottom-end', onSelect } = opts;
  // items: [{ id, label, icon, danger, divider, disabled }] or { divider: true }
  const root = h('div', { class: 'dropdown' });
  const trig = h('button', {
    type: 'button', class: 'dropdown-trigger', 'aria-haspopup': 'menu', 'aria-expanded': 'false',
  }, trigger);
  const menu = h('div', { class: `dropdown-menu dropdown-${placement}`, role: 'menu' });

  function close() {
    menu.classList.remove('is-open');
    trig.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onDoc);
  }
  function open() {
    menu.classList.add('is-open');
    trig.setAttribute('aria-expanded', 'true');
    setTimeout(() => document.addEventListener('click', onDoc), 0);
  }
  function onDoc(e) { if (!root.contains(e.target)) close(); }
  function toggle() { menu.classList.contains('is-open') ? close() : open(); }

  trig.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });

  for (const it of items) {
    if (it.divider) { menu.append(h('div', { class: 'dropdown-divider', role: 'separator' })); continue; }
    const btn = h('button', {
      type: 'button', class: `dropdown-item ${it.danger ? 'is-danger' : ''}`.trim(),
      role: 'menuitem', tabIndex: -1,
      disabled: it.disabled || undefined,
    });
    if (it.icon) btn.append(h('span', { class: 'dropdown-icon', 'aria-hidden': 'true', html: it.icon }));
    btn.append(h('span', { class: 'dropdown-label' }, it.label));
    btn.addEventListener('click', () => { if (it.disabled) return; if (onSelect) onSelect(it); close(); });
    menu.append(btn);
  }

  // keyboard
  trig.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault(); open();
      const f = menu.querySelector('.dropdown-item:not([disabled])');
      if (f) f.focus();
    }
  });
  menu.addEventListener('keydown', (e) => {
    const items = Array.from(menu.querySelectorAll('.dropdown-item:not([disabled])'));
    const idx = items.indexOf(document.activeElement);
    if (e.key === 'ArrowDown') { e.preventDefault(); items[(idx + 1) % items.length]?.focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); items[(idx - 1 + items.length) % items.length]?.focus(); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); trig.focus(); }
    else if (e.key === 'Home') { e.preventDefault(); items[0]?.focus(); }
    else if (e.key === 'End') { e.preventDefault(); items[items.length - 1]?.focus(); }
  });

  root.append(trig, menu);
  root.open = open; root.close = close;
  return root;
}
