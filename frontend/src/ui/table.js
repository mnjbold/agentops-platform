/* =====================================================================
 * agentops/ui/table.js
 * Table primitive. Sortable columns, sticky header, row actions, empty state.
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { createEmptyState } from './empty-state.js';

export function createTable(opts = {}) {
  const {
    columns = [],       // [{ key, label, sortable, render, width, align }]
    rows = [],
    emptyTitle = 'Nothing to show',
    emptyBody = '',
    emptyAction = null,
    loading = false,
    sticky = true,
    onRowClick,
    sortBy,
    sortDir = 'asc',
    onSort,
  } = opts;

  const wrap = h('div', { class: `table-wrap ${sticky ? 'is-sticky' : ''}`.trim(), role: 'region', 'aria-label': 'Data table' });

  if (loading) {
    wrap.append(h('div', { class: 'table-loading', 'aria-busy': 'true' }, 'Loading…'));
    return wrap;
  }

  if (!rows.length) {
    wrap.append(createEmptyState({ title: emptyTitle, body: emptyBody, action: emptyAction, compact: true }));
    return wrap;
  }

  const tbl = h('table', { class: 'table' });
  const thead = h('thead');
  const trh = h('tr');
  columns.forEach(c => {
    const th = h('th', {
      style: c.width ? `width:${c.width}` : '',
      class: `th-${c.align || 'left'}`,
      scope: 'col',
      'aria-sort':
        c.sortable && sortBy === c.key
          ? (sortDir === 'desc' ? 'descending' : 'ascending')
          : (c.sortable ? 'none' : undefined),
    });
    if (c.sortable) {
      th.append(h('button', {
        type: 'button', class: 'th-sort',
        'aria-label': `Sort by ${c.label}`,
        onClick: () => onSort && onSort(c.key),
      }, c.label, h('span', { class: 'th-sort-icon', 'aria-hidden': 'true' }, sortBy === c.key ? (sortDir === 'desc' ? '↓' : '↑') : '↕')));
    } else {
      th.textContent = c.label;
    }
    trh.append(th);
  });
  thead.append(trh);
  tbl.append(thead);

  const tbody = h('tbody');
  rows.forEach((r, i) => {
    const tr = h('tr', { tabIndex: 0, dataset: { row: i } });
    columns.forEach(c => {
      const td = h('td', { class: `td-${c.align || 'left'}` });
      if (c.render) {
        const out = c.render(r, i);
        if (out instanceof Node) td.append(out);
        else if (out != null) td.textContent = out;
      } else {
        td.textContent = r[c.key] ?? '';
      }
      tr.append(td);
    });
    if (onRowClick) {
      tr.addEventListener('click', () => onRowClick(r, i, tr));
      tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRowClick(r, i, tr); } });
    }
    tbody.append(tr);
  });
  tbl.append(tbody);
  wrap.append(tbl);
  return wrap;
}
