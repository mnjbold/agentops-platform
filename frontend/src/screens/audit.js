/* =====================================================================
 * agentops/screens/audit.js
 * Audit log screen (issue #20).
 * Filter bar + table + detail panel + CSV/JSON export.
 * ===================================================================== */

import { h, formatDate, debounce } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError } from '../ui/toast.js';

let _state = { items: [], loading: true, filters: { from: '', to: '', action: '', user_id: '' } };

export async function mountAuditScreen(root) {
  root.innerHTML = '';

  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', { class: 'page-title' }, 'Audit log'),
        h('p', { class: 'page-sub' }, 'Append-only record of every /api/* request')
      ),
      h('div', { class: 'page-actions' },
        createButton({ variant: 'secondary', size: 'sm', children: 'Export CSV', onClick: () => exportFile('csv') }),
        createButton({ variant: 'secondary', size: 'sm', children: 'Export JSON', onClick: () => exportFile('json') })
      )
    )
  );

  // Filter bar
  const filters = h('div', { class: 'card', style: 'padding: 12px 16px; margin-bottom: 16px; display: grid; grid-template-columns: 1fr 1fr 1fr 1fr auto; gap: 8px; align-items: end;' });
  filters.append(
    filterInput('From', 'date', _state.filters.from, (v) => { _state.filters.from = v; debouncedLoad(root); }),
    filterInput('To', 'date', _state.filters.to, (v) => { _state.filters.to = v; debouncedLoad(root); }),
    filterInput('Action', 'text', _state.filters.action, (v) => { _state.filters.action = v; debouncedLoad(root); }),
    filterInput('User id', 'text', _state.filters.user_id, (v) => { _state.filters.user_id = v; debouncedLoad(root); }),
    createButton({ variant: 'primary', size: 'sm', children: 'Apply', onClick: () => load(root) })
  );
  root.append(filters);

  // Table
  const tableWrap = h('div', { class: 'card' });
  const head = h('div', { style: 'padding: 12px 16px; border-bottom: 1px solid var(--color-line); display: flex; justify-content: space-between; align-items: center;' },
    h('div', { style: 'font-weight: 600;' }, 'Events'),
    h('div', { id: 'audit-count', style: 'color: var(--color-fg-3); font-size: 12px;' })
  );
  const body = h('div', { id: 'audit-rows', style: 'padding: 0;' });
  for (let i = 0; i < 6; i++) body.append(createSkeleton({ lines: 1, height: 32 }));
  tableWrap.append(head, body);
  root.append(tableWrap);

  // Detail panel
  const detail = h('div', { class: 'card', id: 'audit-detail', style: 'padding: 16px; margin-top: 16px; display: none;' });
  root.append(detail);

  await load(root);
}

function filterInput(label, type, value, onChange) {
  const i = h('input', { type, value, 'aria-label': label, style: 'background: var(--color-bg-1); border: 1px solid var(--color-line); color: var(--color-fg-1); padding: 6px 8px; border-radius: var(--radius-sm); width: 100%;' });
  i.addEventListener('input', debounce(() => onChange(i.value), 250));
  return h('div', {}, h('div', { style: 'color: var(--color-fg-3); font-size: 11px; margin-bottom: 2px;' }, label), i);
}

const debouncedLoad = debounce((root) => load(root), 250);

async function load(root) {
  _state.loading = true;
  const body = root.querySelector('#audit-rows');
  if (body) {
    body.innerHTML = '';
    for (let i = 0; i < 6; i++) body.append(createSkeleton({ lines: 1, height: 32 }));
  }
  try {
    const params = new URLSearchParams();
    if (_state.filters.from) params.set('from', _state.filters.from);
    if (_state.filters.to) params.set('to', _state.filters.to);
    if (_state.filters.action) params.set('action', _state.filters.action);
    if (_state.filters.user_id) params.set('user_id', _state.filters.user_id);
    params.set('limit', '100');
    const data = await api.get('/v1/audit?' + params.toString());
    _state.items = data.items || [];
    _state.loading = false;
    render(root);
  } catch (e) {
    _state.loading = false;
    if (body) {
      body.innerHTML = '';
      body.append(createEmptyState({ icon: '!', title: 'Could not load audit log', body: e.message }));
    }
  }
}

function render(root) {
  const body = root.querySelector('#audit-rows');
  const count = root.querySelector('#audit-count');
  if (!body) return;
  body.innerHTML = '';
  if (count) count.textContent = `${_state.items.length} events`;
  if (!_state.items.length) {
    body.append(createEmptyState({ icon: '📜', title: 'No audit events', body: 'Try a wider date range, or check the filters above.' }));
    return;
  }
  const table = h('table', { style: 'width: 100%; border-collapse: collapse; font-size: 13px;' });
  const thead = h('thead', {}, h('tr', { style: 'text-align: left; color: var(--color-fg-3);' },
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'Time'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'User'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'Action'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'Target'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'Method'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'Path'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'Status'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'IP'),
    h('th', { style: 'padding: 8px 12px; border-bottom: 1px solid var(--color-line);' }, 'RT'),
  ));
  table.append(thead);
  const tbody = h('tbody');
  for (const r of _state.items) {
    const tr = h('tr', {
      style: 'border-bottom: 1px solid var(--color-line); cursor: pointer;',
      onClick: () => showDetail(root, r.id),
    });
    tr.append(
      h('td', { style: 'padding: 8px 12px; color: var(--color-fg-2);' }, formatDate(r.timestamp) + ' ' + (r.timestamp || '').split('T')[1]?.slice(0, 8) || ''),
      h('td', { style: 'padding: 8px 12px;' }, r.user_id || '—'),
      h('td', { style: 'padding: 8px 12px;' }, h('code', {}, r.action || '')),
      h('td', { style: 'padding: 8px 12px; color: var(--color-fg-2);' }, r.target || '—'),
      h('td', { style: 'padding: 8px 12px;' }, createBadge({ variant: r.method === 'GET' ? 'info' : r.method === 'POST' ? 'success' : r.method === 'DELETE' ? 'danger' : 'neutral', children: r.method })),
      h('td', { style: 'padding: 8px 12px; font-family: var(--font-mono); font-size: 12px;' }, r.path),
      h('td', { style: 'padding: 8px 12px;' }, createBadge({
        variant: (r.response_status || 0) < 300 ? 'success' : (r.response_status || 0) < 400 ? 'warning' : 'danger',
        children: String(r.response_status || '?'),
      })),
      h('td', { style: 'padding: 8px 12px; color: var(--color-fg-3); font-family: var(--font-mono); font-size: 11px;' }, r.ip || '—'),
      h('td', { style: 'padding: 8px 12px; color: var(--color-fg-3); font-size: 12px;' }, `${r.response_time_ms || 0}ms`),
    );
    tbody.append(tr);
  }
  table.append(tbody);
  body.append(table);
}

async function showDetail(root, id) {
  const panel = root.querySelector('#audit-detail');
  if (!panel) return;
  panel.style.display = 'block';
  panel.innerHTML = '<div style="color: var(--color-fg-3);">Loading…</div>';
  try {
    const data = await api.get('/v1/audit/' + id);
    const r = data.item;
    panel.innerHTML = '';
    panel.append(
      h('div', { style: 'display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;' },
        h('div', { style: 'font-weight: 600;' }, 'Audit row ' + r.id),
        createButton({ variant: 'ghost', size: 'sm', children: 'Close', onClick: () => { panel.style.display = 'none'; } })
      ),
      kvTable([
        ['Timestamp', r.timestamp],
        ['Tenant', r.tenant_id],
        ['User', r.user_id || '—'],
        ['Action', r.action],
        ['Target', r.target || '—'],
        ['Method', r.method],
        ['Path', r.path],
        ['Status', String(r.response_status || '?')],
        ['RT (ms)', String(r.response_time_ms || '?')],
        ['IP', r.ip || '—'],
        ['UA', r.user_agent || '—'],
        ['Request ID', r.request_id || '—'],
      ]),
    );
    if (r.request_body) {
      panel.append(h('div', { style: 'margin-top: 12px; font-weight: 600;' }, 'Request body'));
      panel.append(h('pre', { style: 'background: var(--color-bg-2); padding: 8px; border-radius: 4px; overflow: auto; font-size: 12px;' }, r.request_body));
    }
    if (r.response_body) {
      panel.append(h('div', { style: 'margin-top: 12px; font-weight: 600;' }, 'Response body'));
      panel.append(h('pre', { style: 'background: var(--color-bg-2); padding: 8px; border-radius: 4px; overflow: auto; font-size: 12px;' }, r.response_body));
    }
  } catch (e) {
    panel.innerHTML = '';
    panel.append(createEmptyState({ icon: '!', title: 'Could not load detail', body: e.message }));
  }
}

function kvTable(rows) {
  const t = h('table', { style: 'width: 100%; border-collapse: collapse; font-size: 13px;' });
  for (const [k, v] of rows) {
    t.append(h('tr', {},
      h('td', { style: 'padding: 4px 12px 4px 0; color: var(--color-fg-3); width: 140px;' }, k),
      h('td', { style: 'padding: 4px 0;' }, v),
    ));
  }
  return t;
}

async function exportFile(format) {
  try {
    const params = new URLSearchParams({ format });
    if (_state.filters.from) params.set('from', _state.filters.from);
    if (_state.filters.to) params.set('to', _state.filters.to);
    if (_state.filters.action) params.set('action', _state.filters.action);
    const res = await fetch((api.base()) + '/v1/audit/export?' + params.toString(), {
      headers: { 'X-Tenant-Id': 'default' },
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'agentops-audit.' + format;
    a.click();
    URL.revokeObjectURL(a.href);
  } catch (e) { toastError('Export failed: ' + e.message); }
}
