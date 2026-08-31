/* =====================================================================
 * agentops/screens/analytics.js
 * Analytics dashboard (issue #16).
 * KPI cards + tiny SVG charts + per-assistant breakdown.
 * No Chart.js / D3 — ~200 lines of vanilla SVG.
 * ===================================================================== */

import { h, formatDate, debounce } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError } from '../ui/toast.js';

let _state = {
  preset: '7d',
  from: '',
  to: '',
  compare: false,
  data: null,
  loading: true,
};

const PRESETS = [
  { id: 'today',       label: 'Today' },
  { id: '7d',          label: '7d' },
  { id: '30d',         label: '30d' },
  { id: 'this-month',  label: 'This month' },
  { id: 'last-month',  label: 'Last month' },
];

export async function mountAnalyticsScreen(root) {
  root.innerHTML = '';

  // Page head + actions
  const actions = h('div', { class: 'page-actions' });
  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', { class: 'page-title' }, 'Analytics'),
        h('p', { class: 'page-sub' }, 'Call volume, transfer rates, and spend')
      ),
      actions
    )
  );

  // Preset bar
  const presetBar = h('div', { class: 'card', style: 'padding: 12px; margin-bottom: 16px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;' });
  for (const p of PRESETS) {
    presetBar.append(createButton({
      variant: _state.preset === p.id ? 'primary' : 'secondary',
      size: 'sm',
      onClick: () => { _state.preset = p.id; _state.from = ''; _state.to = ''; load(root); render(root); },
      children: p.label,
    }));
  }
  const compareBtn = createButton({
    variant: _state.compare ? 'primary' : 'ghost',
    size: 'sm',
    onClick: () => { _state.compare = !_state.compare; load(root); },
    children: _state.compare ? 'Compare on' : 'Compare to prev',
  });
  presetBar.append(h('div', { style: 'flex: 1;' }));
  presetBar.append(compareBtn);

  // Custom range pickers (small text inputs)
  const fromIn = h('input', { type: 'date', value: _state.from, 'aria-label': 'From date', style: 'background: var(--color-bg-1); border: 1px solid var(--color-line); color: var(--color-fg-1); padding: 6px 8px; border-radius: var(--radius-sm);' });
  const toIn   = h('input', { type: 'date', value: _state.to,   'aria-label': 'To date',   style: 'background: var(--color-bg-1); border: 1px solid var(--color-line); color: var(--color-fg-1); padding: 6px 8px; border-radius: var(--radius-sm);' });
  fromIn.addEventListener('change', debounce(() => { _state.from = fromIn.value; _state.preset = 'custom'; load(root); render(root); }, 250));
  toIn.addEventListener('change', debounce(() => { _state.to = toIn.value; _state.preset = 'custom'; load(root); render(root); }, 250));
  presetBar.append(h('span', { style: 'color: var(--color-fg-3); font-size: 12px;' }, 'Custom:'));
  presetBar.append(fromIn, toIn);

  // Export CSV
  const exportBtn = createButton({
    variant: 'secondary', size: 'sm', icon: '⬇',
    onClick: async () => {
      const params = new URLSearchParams();
      if (_state.from) params.set('from', _state.from);
      if (_state.to) params.set('to', _state.to);
      if (_state.preset) params.set('preset', _state.preset);
      try {
        const res = await fetch((api.base()) + '/v1/analytics/export.csv?' + params.toString(), {
          headers: { 'X-Tenant-Id': 'default' },
        });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'agentops-analytics.csv';
        a.click();
        URL.revokeObjectURL(a.href);
      } catch (e) { toastError('Export failed: ' + e.message); }
    },
    children: 'Export CSV',
  });
  presetBar.append(exportBtn);
  root.append(presetBar);

  // KPI card row
  const cards = h('div', { id: 'analytics-kpis', style: 'display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px;' });
  for (let i = 0; i < 4; i++) cards.append(createSkeleton({ lines: 1, height: 88 }));
  root.append(cards);

  // Two-column body: line chart (call volume) + bar chart (transfer rate)
  const row = h('div', { style: 'display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 16px;' });
  const lineCard = h('div', { class: 'card' }, h('div', { style: 'padding: 12px 16px; border-bottom: 1px solid var(--color-line); font-weight: 600;' }, 'Call volume by hour'));
  const lineBody = h('div', { id: 'analytics-line', style: 'padding: 12px 16px; height: 220px; position: relative;' });
  lineCard.append(lineBody);

  const barCard = h('div', { class: 'card' }, h('div', { style: 'padding: 12px 16px; border-bottom: 1px solid var(--color-line); font-weight: 600;' }, 'Transfer rate per assistant'));
  const barBody = h('div', { id: 'analytics-bars', style: 'padding: 12px 16px; min-height: 220px;' });
  barCard.append(barBody);
  row.append(lineCard, barCard);
  root.append(row);

  // Per-assistant table
  const tableCard = h('div', { class: 'card' });
  const tableHead = h('div', { style: 'padding: 12px 16px; border-bottom: 1px solid var(--color-line); display: flex; justify-content: space-between; align-items: center;' },
    h('div', { style: 'font-weight: 600;' }, 'Per-assistant breakdown'),
    h('div', { id: 'analytics-window', style: 'color: var(--color-fg-3); font-size: 12px;' })
  );
  const tableBody = h('div', { id: 'analytics-assistants', style: 'padding: 8px 0;' });
  tableCard.append(tableHead, tableBody);
  root.append(tableCard);

  await load(root);
}

function render(root) {
  // Re-render the preset bar's active button
  const bar = root.querySelector('.card');
  if (!bar) return;
  for (const btn of bar.querySelectorAll('button')) {
    const match = PRESETS.find(p => p.label === btn.textContent.trim());
    if (match) {
      btn.classList.toggle('btn-primary', _state.preset === match.id);
      btn.classList.toggle('btn-secondary', _state.preset !== match.id);
    }
  }
}

async function load(root) {
  _state.loading = true;
  const kpis = root.querySelector('#analytics-kpis');
  if (kpis) {
    kpis.innerHTML = '';
    for (let i = 0; i < 4; i++) kpis.append(createSkeleton({ lines: 1, height: 88 }));
  }

  try {
    const params = new URLSearchParams();
    if (_state.from) params.set('from', _state.from);
    if (_state.to) params.set('to', _state.to);
    if (_state.preset) params.set('preset', _state.preset);
    if (_state.compare) params.set('compare', '1');
    const [overview, assistants] = await Promise.all([
      api.get('/v1/analytics/overview?' + params.toString()).catch(() => null),
      api.get('/v1/analytics/assistants?' + params.toString()).catch(() => null),
    ]);
    _state.data = { overview, assistants };
    _state.loading = false;
    paint(root);
  } catch (e) {
    _state.loading = false;
    if (kpis) {
      kpis.innerHTML = '';
      kpis.append(createEmptyState({ icon: '!', title: 'Could not load analytics', body: e.message }));
    }
  }
}

function paint(root) {
  const { overview, assistants } = _state.data || {};
  if (!overview) return;
  const cur = overview.current;
  const prev = overview.previous;
  const delta = overview.delta;

  // KPIs
  const kpis = root.querySelector('#analytics-kpis');
  kpis.innerHTML = '';
  const items = [
    { label: 'Total calls', value: cur.total_calls, delta: delta ? delta.total_calls : null },
    { label: 'Avg handle time', value: cur.top_agents && cur.top_agents[0] ? '—' : '—', delta: null, suffix: '' },
    { label: 'Transfer rate', value: computeTransferRate(assistants), delta: null, suffix: '%' },
    { label: 'Spend', value: formatCents(cur.spend_cents), delta: delta ? delta.spend_cents : null, suffix: '' },
  ];
  for (const it of items) {
    kpis.append(renderKpi(it));
  }

  // Window caption
  const win = root.querySelector('#analytics-window');
  if (win) win.textContent = `${overview.window.from} → ${overview.window.to}`;

  // Line chart (call volume by hour)
  drawLineChart(root.querySelector('#analytics-line'), cur.busiest_hours || []);

  // Bar chart (transfer rate per assistant)
  drawBarChart(root.querySelector('#analytics-bars'), (assistants && assistants.assistants) || []);

  // Per-assistant table
  renderAssistantTable(root, (assistants && assistants.assistants) || []);
}

function computeTransferRate(assistants) {
  if (!assistants || !assistants.assistants || !assistants.assistants.length) return '0.0';
  const totals = assistants.assistants.reduce((a, r) => ({
    calls: a.calls + (r.call_count || 0),
    transfers: a.transfers + (r.transfer_count || 0),
  }), { calls: 0, transfers: 0 });
  if (!totals.calls) return '0.0';
  return ((totals.transfers / totals.calls) * 100).toFixed(1);
}

function formatCents(c) {
  if (!c) return '$0.00';
  return '$' + (c / 100).toFixed(2);
}

function formatDelta(d) {
  if (d == null) return '';
  if (d === 0) return '  ±0';
  const sign = d > 0 ? '↑' : '↓';
  const cls = d > 0 ? 'kpi-delta-up' : 'kpi-delta-down';
  return `  ${sign}${Math.abs(d)}`;
}

function renderKpi({ label, value, delta, suffix }) {
  const card = h('div', { class: 'card', style: 'padding: 14px 16px;' });
  card.append(h('div', { style: 'color: var(--color-fg-3); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em;' }, label));
  const valRow = h('div', { style: 'display: flex; align-items: baseline; gap: 6px; margin-top: 4px;' });
  valRow.append(h('div', { style: 'font-size: 28px; font-weight: 700; color: var(--color-fg-1);' }, String(value) + (suffix || '')));
  if (delta != null) {
    valRow.append(h('span', {
      style: `font-size: 12px; color: ${delta > 0 ? '#48c78e' : delta < 0 ? '#ff6363' : 'var(--color-fg-3)'};`,
    }, formatDelta(delta)));
  }
  card.append(valRow);
  return card;
}

function drawLineChart(host, hours) {
  if (!host) return;
  host.innerHTML = '';
  const W = host.clientWidth || 480;
  const H = host.clientHeight - 16 || 200;
  const PAD = 24;
  const data = (hours && hours.length === 24) ? hours.map(h => h.calls || 0) : new Array(24).fill(0);
  const max = Math.max(1, ...data);
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('width', String(W));
  svg.setAttribute('height', String(H));
  svg.style.display = 'block';

  // X axis baseline + ticks every 6h
  const axis = document.createElementNS(svgNS, 'line');
  axis.setAttribute('x1', String(PAD));
  axis.setAttribute('x2', String(W - PAD));
  axis.setAttribute('y1', String(H - PAD));
  axis.setAttribute('y2', String(H - PAD));
  axis.setAttribute('stroke', 'var(--color-line)');
  svg.appendChild(axis);
  for (let i = 0; i <= 24; i += 6) {
    const x = PAD + (i / 24) * (W - 2 * PAD);
    const t = document.createElementNS(svgNS, 'text');
    t.setAttribute('x', String(x));
    t.setAttribute('y', String(H - PAD + 14));
    t.setAttribute('fill', 'var(--color-fg-3)');
    t.setAttribute('font-size', '10');
    t.setAttribute('text-anchor', 'middle');
    t.textContent = String(i).padStart(2, '0') + ':00';
    svg.appendChild(t);
  }

  // Area + line
  if (data.some(v => v > 0)) {
    const step = (W - 2 * PAD) / 23;
    let lineD = '';
    let areaD = `M ${PAD} ${H - PAD} `;
    for (let i = 0; i < 24; i++) {
      const x = PAD + i * step;
      const y = H - PAD - (data[i] / max) * (H - 2 * PAD);
      lineD += (i === 0 ? 'M' : 'L') + x + ' ' + y + ' ';
      areaD += 'L' + x + ' ' + y + ' ';
    }
    areaD += `L ${PAD + 23 * step} ${H - PAD} Z`;

    const area = document.createElementNS(svgNS, 'path');
    area.setAttribute('d', areaD);
    area.setAttribute('fill', 'rgba(91,108,255,0.18)');
    svg.appendChild(area);

    const line = document.createElementNS(svgNS, 'path');
    line.setAttribute('d', lineD.trim());
    line.setAttribute('fill', 'none');
    line.setAttribute('stroke', '#5b6cff');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(line);
  } else {
    const empty = document.createElementNS(svgNS, 'text');
    empty.setAttribute('x', String(W / 2));
    empty.setAttribute('y', String(H / 2));
    empty.setAttribute('fill', 'var(--color-fg-3)');
    empty.setAttribute('text-anchor', 'middle');
    empty.textContent = 'No calls in this window';
    svg.appendChild(empty);
  }
  host.appendChild(svg);
}

function drawBarChart(host, rows) {
  if (!host) return;
  host.innerHTML = '';
  if (!rows.length) {
    host.append(createEmptyState({ icon: '📊', title: 'No assistant data yet' }));
    return;
  }
  const max = Math.max(0.01, ...rows.map(r => r.transfer_rate || 0));
  for (const r of rows) {
    const row = h('div', { style: 'display: flex; align-items: center; gap: 8px; margin-bottom: 8px;' });
    row.append(h('div', { style: 'flex: 0 0 120px; font-size: 13px; color: var(--color-fg-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;' }, r.name || r.assistant_id));
    const bar = h('div', { style: 'flex: 1; background: var(--color-bg-2); height: 14px; border-radius: 4px; overflow: hidden;' });
    const fill = h('div', { style: `width: ${(r.transfer_rate / max) * 100 || 1}%; height: 100%; background: linear-gradient(90deg, #5b6cff, #8a5bff);` });
    bar.append(fill);
    row.append(bar);
    row.append(h('div', { style: 'flex: 0 0 70px; text-align: right; font-size: 12px; color: var(--color-fg-3);' },
      `${((r.transfer_rate || 0) * 100).toFixed(1)}% (${r.transfer_count || 0})`));
    host.appendChild(row);
  }
}

function renderAssistantTable(root, rows) {
  const host = root.querySelector('#analytics-assistants');
  if (!host) return;
  host.innerHTML = '';
  if (!rows.length) {
    host.append(createEmptyState({ icon: '🤖', title: 'No assistants yet', body: 'Create an assistant to start tracking analytics.' }));
    return;
  }
  const table = h('table', { class: 'table', style: 'width: 100%; border-collapse: collapse;' });
  const thead = h('thead', {}, h('tr', {},
    h('th', {}, 'Assistant'),
    h('th', {}, 'Calls'),
    h('th', {}, 'Transfer rate'),
    h('th', {}, 'Avg handle time'),
    h('th', {}, 'Outcomes'),
  ));
  table.append(thead);
  const tbody = h('tbody');
  for (const r of rows) {
    const tr = h('tr', { style: 'border-top: 1px solid var(--color-line);' });
    tr.append(h('td', { style: 'padding: 8px 12px;' }, r.name || r.assistant_id));
    tr.append(h('td', { style: 'padding: 8px 12px;' }, String(r.call_count || 0)));
    tr.append(h('td', { style: 'padding: 8px 12px;' }, `${((r.transfer_rate || 0) * 100).toFixed(1)}%`));
    tr.append(h('td', { style: 'padding: 8px 12px;' }, r.avg_handle_time ? formatDuration(r.avg_handle_time) : '—'));
    const outcomes = r.outcomes || {};
    const tag = h('td', { style: 'padding: 8px 12px; display: flex; gap: 4px; flex-wrap: wrap;' });
    for (const [k, v] of Object.entries(outcomes)) {
      if (v > 0) tag.append(createBadge({ variant: 'neutral', children: `${k} ${v}` }));
    }
    tr.append(tag);
    tbody.append(tr);
  }
  table.append(tbody);
  host.append(table);
}

function formatDuration(s) {
  s = Math.max(0, Math.floor(s));
  const m = Math.floor(s / 60), ss = s % 60;
  return `${m}:${String(ss).padStart(2, '0')}`;
}
