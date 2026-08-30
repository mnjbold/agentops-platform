/* =====================================================================
 * agentops/ui/tabs.js
 * Tabs primitive. Horizontal or vertical. Arrow key navigation.
 * ===================================================================== */

import { h, delegate } from '../lib/dom.js';

export function createTabs(opts = {}) {
  const {
    tabs = [],          // [{ id, label, badge, content }]
    active,
    orientation = 'horizontal',
    onChange,
  } = opts;

  const root = h('div', {
    class: `tabs tabs-${orientation}`,
    role: 'tablist',
    'aria-orientation': orientation,
  });

  const list = h('div', { class: 'tabs-list' });
  const panels = h('div', { class: 'tabs-panels' });

  let current = active || tabs[0]?.id;

  function activate(id, { focus = false } = {}) {
    if (current === id && !focus) return;
    current = id;
    list.querySelectorAll('[role="tab"]').forEach(t => {
      const sel = t.dataset.tabId === id;
      t.classList.toggle('is-active', sel);
      t.setAttribute('aria-selected', sel ? 'true' : 'false');
      t.tabIndex = sel ? 0 : -1;
      if (sel && focus) t.focus();
    });
    panels.querySelectorAll('[role="tabpanel"]').forEach(p => {
      p.classList.toggle('hidden', p.dataset.panelId !== id);
    });
    if (onChange) onChange(id);
  }

  for (const t of tabs) {
    const tab = h('button', {
      type: 'button',
      role: 'tab',
      class: 'tabs-tab',
      'aria-selected': t.id === current ? 'true' : 'false',
      'aria-controls': `panel-${t.id}`,
      id: `tab-${t.id}`,
      tabIndex: t.id === current ? 0 : -1,
      dataset: { tabId: t.id },
    }, t.label);
    if (t.badge != null) tab.append(h('span', { class: 'tabs-badge' }, t.badge));
    list.append(tab);

    const panel = h('div', {
      role: 'tabpanel',
      id: `panel-${t.id}`,
      class: `tabs-panel ${t.id === current ? '' : 'hidden'}`.trim(),
      'aria-labelledby': `tab-${t.id}`,
      dataset: { panelId: t.id },
    });
    if (t.content instanceof Node) panel.append(t.content);
    else if (t.content) panel.textContent = t.content;
    panels.append(panel);
  }

  // Arrow keys
  list.addEventListener('keydown', (e) => {
    const tabsArr = Array.from(list.querySelectorAll('[role="tab"]'));
    const idx = tabsArr.findIndex(t => t.dataset.tabId === current);
    let next = idx;
    const vertical = orientation === 'vertical';
    if ((vertical && e.key === 'ArrowDown') || (!vertical && e.key === 'ArrowRight')) next = (idx + 1) % tabsArr.length;
    else if ((vertical && e.key === 'ArrowUp') || (!vertical && e.key === 'ArrowLeft')) next = (idx - 1 + tabsArr.length) % tabsArr.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = tabsArr.length - 1;
    else return;
    e.preventDefault();
    activate(tabsArr[next].dataset.tabId, { focus: true });
  });

  delegate(list, 'click', '[role="tab"]', (e, t) => activate(t.dataset.tabId));

  root.append(list, panels);
  root.activate = activate;
  root.getActive = () => current;
  return root;
}
