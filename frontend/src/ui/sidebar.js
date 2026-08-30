/* =====================================================================
 * agentops/ui/sidebar.js
 * Sidebar primitive. Collapsible, with badges, active highlight.
 * ===================================================================== */

import { h, delegate } from '../lib/dom.js';

export function createSidebar(opts = {}) {
  const { items = [], active, onSelect, collapsible = true } = opts;
  const root = h('aside', { class: 'sidebar', 'aria-label': 'Primary' });
  const nav = h('nav', { class: 'sidebar-nav' });

  for (const group of items) {
    if (group.heading) nav.append(h('div', { class: 'sidebar-group-label' }, group.heading));
    for (const it of group.items || []) {
      const a = h('a', {
        href: it.href || '#',
        class: `sidebar-link ${active === it.id ? 'is-active' : ''}`.trim(),
        'aria-current': active === it.id ? 'page' : undefined,
        dataset: { navLink: it.href?.replace(/^#/, '') || it.id },
        role: 'link',
      });
      if (it.icon) a.append(h('span', { class: 'sidebar-icon', 'aria-hidden': 'true', html: it.icon }));
      const lbl = h('span', { class: 'sidebar-label' }, it.label);
      a.append(lbl);
      if (it.badge != null) a.append(h('span', { class: 'sidebar-badge' }, it.badge));
      nav.append(a);
    }
  }

  delegate(nav, 'click', '.sidebar-link', (e, link) => {
    if (onSelect) { e.preventDefault(); onSelect(link.dataset.navLink, link); }
    nav.querySelectorAll('.sidebar-link').forEach(l => l.classList.toggle('is-active', l === link));
  });

  root.append(nav);
  if (collapsible) {
    const toggle = h('button', { type: 'button', class: 'sidebar-toggle', 'aria-label': 'Collapse sidebar' }, '‹');
    toggle.addEventListener('click', () => root.classList.toggle('is-collapsed'));
    root.append(toggle);
  }
  return root;
}
