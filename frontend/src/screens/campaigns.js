/* =====================================================================
 * agentops/screens/campaigns.js
 * Campaign list + campaign detail with the Phase C additions:
 *   - Test mode (issue #24) — "Test" button → modal with N + distribution
 *     picker → POST /api/campaigns/{id}/test → bar chart of outcomes.
 *   - Compliance (issue #25) — DNC + time-of-day preflight via
 *     GET /api/compliance/preview. Red badge when any skip > 0.
 *
 * The screen is a v1 Phase C implementation; it is wired into
 * the router at /campaigns. The legacy monolithic PWA still serves
 * the full campaign management UI; this view covers the new pieces.
 * ===================================================================== */

import { h, formatDate, formatDuration } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { renderBarChart } from '../ui/chart.js';
import { toastError, toastSuccess } from '../ui/toast.js';

const STATUS_VARIANT = {
  draft:     'neutral',
  scheduled: 'info',
  running:   'accent',
  paused:    'warning',
  completed: 'success',
  failed:    'danger',
};

const OUTCOME_VARIANT = {
  answer:    'success',
  voicemail: 'accent',
  no_answer: 'warning',
  busy:      'warning',
  failed:    'danger',
};

let _state = { campaigns: [], selected: null, busy: false };

export async function mountCampaignsScreen(root) {
  root.innerHTML = '';
  _state = { campaigns: [], selected: null, busy: false };

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Campaigns'),
      h('p', { class: 'page-sub' },
        'Test mode simulates calls without hitting Telnyx. Compliance pre-flights DNC + time-of-day before launch.')
    ),
    h('div', { class: 'page-actions' },
      createButton({
        variant: 'ghost', size: 'sm', children: 'Refresh',
        onClick: () => loadCampaigns(root),
      }),
    )
  ));

  // Two-column layout: list on the left, detail on the right.
  const layout = h('div', { class: 'campaigns-layout', style: 'display: grid; grid-template-columns: 380px 1fr; gap: var(--space-4); align-items: start;' });
  const listCard = h('div', { class: 'card' });
  listCard.append(h('div', { class: 'card-head' },
    h('div', {},
      h('h3', {}, 'Your campaigns'),
      h('p', { class: 'sub', style: 'margin: 4px 0 0; font-size: var(--text-sm); color: var(--color-fg-3);' },
        `${_state.campaigns.length} total`)
    ),
  ));
  const listBody = h('div', { class: 'card-body', id: 'cmp-list-body', style: 'padding: 0;' });
  listBody.append(h('div', { style: 'padding: var(--space-4); color: var(--color-fg-3);' }, 'Loading…'));
  listCard.append(listBody);
  layout.append(listCard);

  const detailCard = h('div', { id: 'cmp-detail', class: 'card' });
  detailCard.append(h('div', { class: 'card-body' },
    h('div', { class: 'empty-state' },
      h('div', { class: 'empty-icon' }, '⊞'),
      h('div', { class: 'empty-title' }, 'Select a campaign'),
      h('div', { class: 'empty-body' }, 'Choose a campaign from the list to view test mode + compliance pre-flight.'),
    )
  ));
  layout.append(detailCard);

  root.append(layout);
  await loadCampaigns(root);
}

async function loadCampaigns(root) {
  let list = [];
  try {
    const data = await api.get('/campaigns');
    list = data.campaigns || data || [];
  } catch (e) {
    toastError('Failed to load campaigns: ' + e.message);
    list = [];
  }
  _state.campaigns = list;
  const listBody = root.querySelector('#cmp-list-body');
  if (!listBody) return;
  listBody.innerHTML = '';
  if (!list.length) {
    listBody.append(h('div', { class: 'empty-state', style: 'padding: var(--space-5);' },
      h('div', { class: 'empty-title' }, 'No campaigns yet'),
      h('div', { class: 'empty-body' }, 'Create a campaign to begin testing.'),
    ));
    return;
  }
  for (const c of list) listBody.append(renderCampaignRow(c, root));
}

function renderCampaignRow(c, root) {
  const variant = STATUS_VARIANT[c.status] || 'neutral';
  const isTest = !!c.test_mode;
  const cid = c.id;
  return h('div', {
    class: 'rec-row', id: `cmp-row-${cid}`,
    style: 'border-radius: 0; border-left: 0; border-right: 0; margin: 0; cursor: pointer;',
    onClick: () => selectCampaign(c, root),
  },
    h('div', { class: 'rec-row-meta' },
      h('div', { class: 'rec-row-name' },
        c.name,
        isTest ? h('span', { class: 'badge badge-accent', style: 'margin-left: 6px; font-size: 10px;' }, 'TEST') : null
      ),
      h('div', { class: 'rec-row-num' },
        `${(c.contact_ids || []).length} contacts · ${c.type}`)
    ),
    h('div', {}, createBadge({ variant, dot: true, children: c.status })),
  );
}

async function selectCampaign(c, root) {
  _state.selected = c;
  // Re-render only the detail card
  const slot = root.querySelector('#cmp-detail');
  if (!slot) return;
  slot.innerHTML = '';
  slot.append(buildDetail(c, root));
  await Promise.all([
    refreshCompliance(c.id, slot),
    refreshTestSummary(c.id, slot),
  ]);
}

function buildDetail(c, root) {
  const wrap = h('div', {});
  // Header
  const head = h('div', { class: 'card-head' },
    h('div', {},
      h('h3', {}, c.name),
      h('p', { class: 'sub', style: 'margin: 4px 0 0; font-size: var(--text-sm); color: var(--color-fg-3);' },
        `${c.type} · ${(c.contact_ids || []).length} contacts · ${c.from_number || '—'}`)
    ),
    h('div', { style: 'display: flex; gap: var(--space-2); align-items: center;' },
      createBadge({ variant: STATUS_VARIANT[c.status] || 'neutral', dot: true, children: c.status }),
      !!c.test_mode && createBadge({ variant: 'accent', children: 'TEST MODE' }),
    ),
  );
  wrap.append(head);

  const body = h('div', { class: 'card-body' });
  body.style.padding = 'var(--space-4)';

  // ───── Test mode section (issue #24) ─────
  body.append(buildTestSection(c, root));
  // ───── Compliance section (issue #25) ─────
  body.append(buildComplianceSection(c, root));

  wrap.append(body);
  return wrap;
}

// ─────────────────────── Test mode (issue #24) ────────────────────────────

function buildTestSection(c, root) {
  const sec = h('div', { class: 'card', style: 'margin-bottom: var(--space-4);' });
  sec.append(h('div', { class: 'card-head' },
    h('div', {},
      h('h3', {}, 'Test mode'),
      h('p', { class: 'sub', style: 'margin: 4px 0 0; font-size: var(--text-sm); color: var(--color-fg-3);' },
        'Simulate the campaign — no Telnyx calls, no real spend.'),
    ),
    h('div', { style: 'display: flex; gap: var(--space-2); align-items: center;' },
      h('label', { style: 'font-size: var(--text-sm); color: var(--color-fg-2); display: flex; align-items: center; gap: 6px;' },
        h('input', {
          type: 'checkbox',
          checked: !!c.test_mode,
          onChange: (e) => toggleTestMode(c, !!e.target.checked, root),
        }),
        'Test mode on',
      ),
    ),
  ));
  const body = h('div', { class: 'card-body' });
  body.style.display = 'grid';
  body.style.gridTemplateColumns = '1fr 320px';
  body.style.gap = 'var(--space-4)';

  // Left: "Run test" controls + summary
  const left = h('div', {});
  const picker = h('div', { style: 'display: grid; grid-template-columns: 100px 1fr auto; gap: var(--space-2); align-items: end; margin-bottom: var(--space-3);' });
  const nInput = h('input', {
    type: 'number', value: '100', min: '1', max: '1000',
    style: 'padding: 6px 8px; background: var(--color-bg-1); color: var(--color-fg-0); border: 1px solid var(--color-line); border-radius: 6px; width: 100px;',
  });
  nInput.id = `test-n-${c.id}`;
  const distSelect = h('select', {
    style: 'padding: 6px 8px; background: var(--color-bg-1); color: var(--color-fg-0); border: 1px solid var(--color-line); border-radius: 6px;',
  },
    h('option', { value: 'mixed' }, 'Mixed (60/20/10/5/5)'),
    h('option', { value: 'all_answer' }, 'All answer (100%)'),
    h('option', { value: 'all_voicemail' }, 'All voicemail (100%)'),
    h('option', { value: 'all_no_answer' }, 'All no-answer (100%)'),
    h('option', { value: 'all_busy' }, 'All busy (100%)'),
    h('option', { value: 'all_failed' }, 'All failed (100%)'),
  );
  distSelect.id = `test-dist-${c.id}`;
  const runBtn = createButton({
    variant: 'primary', size: 'md', children: 'Run test',
    onClick: () => runTest(c, root),
  });
  picker.append(
    h('div', {}, h('label', { for: nInput.id, style: 'font-size: var(--text-sm); color: var(--color-fg-2);' }, 'N'), nInput),
    h('div', {}, h('label', { for: distSelect.id, style: 'font-size: var(--text-sm); color: var(--color-fg-2);' }, 'Distribution'), distSelect),
    runBtn,
  );
  left.append(picker);
  // Result summary
  const summary = h('div', { id: `test-summary-${c.id}`, style: 'color: var(--color-fg-3); font-size: var(--text-sm);' },
    'No test run yet. Click "Run test" to generate synthetic calls.');
  left.append(summary);
  body.append(left);

  // Right: bar chart canvas
  const right = h('div', {});
  right.append(h('div', { style: 'font-size: var(--text-sm); color: var(--color-fg-2); margin-bottom: 4px;' }, 'Outcome distribution'));
  const canvas = h('canvas', {
    id: `test-chart-${c.id}`,
    style: 'width: 100%; height: auto;',
  });
  right.append(canvas);
  body.append(right);

  // Synthetic log (collapsed by default; expand button)
  const logToggle = createButton({
    variant: 'ghost', size: 'sm', children: 'View test log (last 50)',
    onClick: () => toggleTestLog(c, root),
  });
  body.append(h('div', { style: 'margin-top: var(--space-3);' }, logToggle));
  const logSlot = h('div', { id: `test-log-${c.id}`, style: 'display: none; margin-top: var(--space-2);' });
  body.append(logSlot);

  sec.append(body);
  return sec;
}

async function toggleTestMode(c, on, root) {
  try {
    const res = await api.patch(`/campaigns/${c.id}`, { test_mode: on ? 1 : 0 });
    if (res && res.campaign) {
      _state.selected = res.campaign;
      const slot = root.querySelector('#cmp-detail');
      if (slot) {
        slot.innerHTML = '';
        slot.append(buildDetail(res.campaign, root));
        await Promise.all([
          refreshCompliance(res.campaign.id, slot),
          refreshTestSummary(res.campaign.id, slot),
        ]);
      }
      // Update the list row's badge
      const row = root.querySelector(`#cmp-row-${c.id} .rec-row-name`);
      if (row) {
        const existing = row.querySelector('.badge');
        if (on && !existing) row.appendChild(h('span', { class: 'badge badge-accent', style: 'margin-left: 6px; font-size: 10px;' }, 'TEST'));
        if (!on && existing) existing.remove();
      }
    }
    toastSuccess(on ? 'Test mode ON' : 'Test mode OFF');
  } catch (e) {
    toastError('Failed to update test mode: ' + e.message);
  }
}

async function runTest(c, root) {
  if (_state.busy) return;
  _state.busy = true;
  const n = parseInt((root.querySelector(`#test-n-${c.id}`) || {}).value || '100', 10);
  const dist = (root.querySelector(`#test-dist-${c.id}`) || {}).value || 'mixed';
  const summary = root.querySelector(`#test-summary-${c.id}`);
  const canvas = root.querySelector(`#test-chart-${c.id}`);
  if (summary) summary.textContent = `Running ${n} synthetic calls…`;
  try {
    const res = await api.post(`/campaigns/${c.id}/test`, { n, distribution: dist });
    const distObj = res.distribution || {};
    const total = Object.values(distObj).reduce((s, v) => s + v, 0);
    if (summary) {
      summary.innerHTML = '';
      summary.append(h('div', { style: 'color: var(--color-fg-1);' },
        h('strong', {}, `${total} calls`),
        ` · ${res.elapsed_ms ?? 0}ms · inserted ${res.inserted}`
      ));
      summary.append(h('div', { style: 'margin-top: 4px; font-size: var(--text-xs); color: var(--color-fg-3);' },
        `Distribution: ${Object.entries(distObj).map(([k, v]) => `${k}=${v}`).join(', ') || '—'}`));
    }
    if (canvas) renderBarChart(canvas, distObj, { title: `${n} synthetic calls` });
    toastSuccess(`Test complete · ${total} calls in ${res.elapsed_ms ?? 0}ms`);
  } catch (e) {
    toastError('Test failed: ' + e.message);
    if (summary) summary.textContent = 'Test failed: ' + e.message;
  } finally {
    _state.busy = false;
  }
}

async function refreshTestSummary(campaignId, slot) {
  try {
    const res = await api.get(`/campaigns/${campaignId}/test-summary`);
    const canvas = slot.querySelector(`#test-chart-${campaignId}`);
    if (canvas) renderBarChart(canvas, res.distribution || {}, { title: `${res.total || 0} synthetic calls total` });
    const summary = slot.querySelector(`#test-summary-${campaignId}`);
    if (summary && res.total) {
      summary.innerHTML = '';
      summary.append(h('div', { style: 'color: var(--color-fg-1);' },
        h('strong', {}, `${res.total} calls`),
        ' generated so far'));
    }
  } catch (e) {
    // Non-fatal; just leave the chart empty.
  }
}

async function toggleTestLog(c, root) {
  const slot = root.querySelector(`#test-log-${c.id}`);
  if (!slot) return;
  if (slot.style.display === 'block') {
    slot.style.display = 'none';
    return;
  }
  slot.style.display = 'block';
  slot.innerHTML = '';
  slot.append(h('div', { style: 'color: var(--color-fg-3);' }, 'Loading…'));
  try {
    const res = await api.get(`/campaigns/${c.id}/test-log?limit=50`);
    const calls = res.synthetic_calls || [];
    slot.innerHTML = '';
    if (!calls.length) {
      slot.append(h('div', { style: 'color: var(--color-fg-3);' }, 'No synthetic calls yet.'));
      return;
    }
    for (const sc of calls) slot.append(renderTestLogRow(sc));
  } catch (e) {
    slot.innerHTML = '';
    slot.append(h('div', { style: 'color: var(--color-danger);' }, 'Failed to load test log: ' + e.message));
  }
}

function renderTestLogRow(sc) {
  return h('div', { class: 'rec-row', style: 'border-radius: 0; border-left: 0; border-right: 0; margin: 0; align-items: center;' },
    h('div', {},
      createBadge({ variant: 'accent', children: 'TEST' }),
    ),
    h('div', { class: 'rec-row-meta' },
      h('div', { class: 'rec-row-name mono', style: 'font-size: 12px;' }, sc.id),
      h('div', { class: 'rec-row-num' },
        sc.contact_id ? `contact=${sc.contact_id} · ` : '',
        formatDate(sc.started_at || ''),
      ),
    ),
    h('div', {},
      createBadge({ variant: OUTCOME_VARIANT[sc.outcome] || 'neutral', children: sc.outcome }),
    ),
  );
}

// ─────────────────────── Compliance (issue #25) ────────────────────────────

function buildComplianceSection(c, root) {
  const sec = h('div', { class: 'card' });
  sec.append(h('div', { class: 'card-head' },
    h('div', {},
      h('h3', {}, 'Compliance pre-flight'),
      h('p', { class: 'sub', style: 'margin: 4px 0 0; font-size: var(--text-sm); color: var(--color-fg-3);' },
        'DNC + time-of-day checks before launch. Toggle to skip.'),
    ),
  ));
  const body = h('div', { class: 'card-body' });
  body.style.display = 'grid';
  body.style.gridTemplateColumns = '1fr 1fr';
  body.style.gap = 'var(--space-3)';

  // Toggles
  const dncChecked = c.dnc_check_enabled !== 0;
  const twChecked = c.time_window_enabled !== 0;
  const twStart = c.time_window_start ?? 8;
  const twEnd = c.time_window_end ?? 21;

  const toggles = h('div', { style: 'display: flex; flex-direction: column; gap: var(--space-2);' });
  const dncRow = h('label', { style: 'display: flex; gap: 8px; align-items: center; font-size: var(--text-sm);' },
    h('input', { type: 'checkbox', checked: dncChecked,
      onChange: (e) => saveCompliance(c, { dnc_check_enabled: e.target.checked ? 1 : 0 }, root) }),
    'DNC check (US DNC list)',
  );
  const twRow = h('label', { style: 'display: flex; gap: 8px; align-items: center; font-size: var(--text-sm);' },
    h('input', { type: 'checkbox', checked: twChecked,
      onChange: (e) => saveCompliance(c, { time_window_enabled: e.target.checked ? 1 : 0 }, root) }),
    'Time-of-day window',
  );
  const twInputs = h('div', { style: 'display: flex; gap: 6px; align-items: center; font-size: var(--text-sm); margin-left: 22px; color: var(--color-fg-2);' },
    'Window:',
    h('input', { type: 'number', value: String(twStart), min: '0', max: '23', id: `tw-start-${c.id}`,
      style: 'width: 60px; padding: 4px 6px; background: var(--color-bg-1); color: var(--color-fg-0); border: 1px solid var(--color-line); border-radius: 4px;',
      onChange: () => {} }),
    'to',
    h('input', { type: 'number', value: String(twEnd), min: '1', max: '24', id: `tw-end-${c.id}`,
      style: 'width: 60px; padding: 4px 6px; background: var(--color-bg-1); color: var(--color-fg-0); border: 1px solid var(--color-line); border-radius: 4px;',
      onChange: () => {} }),
    h('button', {
      class: 'btn btn-ghost btn-sm',
      onClick: () => {
        const s = parseInt((document.getElementById(`tw-start-${c.id}`) || {}).value || '8', 10);
        const e = parseInt((document.getElementById(`tw-end-${c.id}`) || {}).value || '21', 10);
        saveCompliance(c, { time_window_start: s, time_window_end: e }, root);
      },
    }, 'Save window'),
  );
  toggles.append(dncRow, twRow, twInputs);
  body.append(toggles);

  // Preview pane
  const preview = h('div', { id: `cmp-preview-${c.id}`, style: 'background: var(--color-bg-1); border-radius: 8px; padding: var(--space-3);' });
  preview.append(h('div', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' }, 'Loading pre-flight…'));
  body.append(preview);

  sec.append(body);
  return sec;
}

async function saveCompliance(c, fields, root) {
  try {
    const res = await api.patch(`/campaigns/${c.id}/compliance`, fields);
    if (res && res.campaign) {
      _state.selected = res.campaign;
      // Re-render detail to keep toggles in sync
      const slot = root.querySelector('#cmp-detail');
      if (slot) {
        slot.innerHTML = '';
        slot.append(buildDetail(res.campaign, root));
        await Promise.all([
          refreshCompliance(res.campaign.id, slot),
          refreshTestSummary(res.campaign.id, slot),
        ]);
      }
      toastSuccess('Compliance settings saved');
    }
  } catch (e) {
    toastError('Failed to save compliance: ' + e.message);
  }
}

async function refreshCompliance(campaignId, slot) {
  const pane = slot.querySelector(`#cmp-preview-${campaignId}`);
  if (!pane) return;
  pane.innerHTML = '';
  pane.append(h('div', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' }, 'Loading pre-flight…'));
  try {
    const res = await api.get(`/compliance/preview?campaign_id=${campaignId}`);
    renderPreview(pane, res);
  } catch (e) {
    pane.innerHTML = '';
    pane.append(h('div', { style: 'color: var(--color-danger); font-size: var(--text-sm);' }, 'Failed to load pre-flight: ' + e.message));
  }
}

function renderPreview(pane, p) {
  pane.innerHTML = '';
  const hasSkips = (p.skipped_dnc || 0) > 0 || (p.skipped_time || 0) > 0;
  const head = h('div', { style: 'display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;' },
    h('strong', {}, 'Pre-flight'),
    hasSkips
      ? createBadge({ variant: 'danger', dot: true, children: `${p.skipped_total} skipped` })
      : createBadge({ variant: 'success', dot: true, children: 'All clear' }),
  );
  pane.append(head);

  // Stat tiles
  const tiles = h('div', { style: 'display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;' });
  for (const [label, value, accent] of [
    ['Total', p.total || 0, false],
    ['Will dial', p.will_dial || 0, true],
    ['Skip DNC', p.skipped_dnc || 0, (p.skipped_dnc || 0) > 0],
    ['Skip time', p.skipped_time || 0, (p.skipped_time || 0) > 0],
  ]) {
    const tile = h('div', { style: 'background: var(--color-bg-2); border-radius: 6px; padding: 8px; text-align: center;' },
      h('div', { style: 'font-size: var(--text-xs); color: var(--color-fg-3); text-transform: uppercase; letter-spacing: 0.04em;' }, label),
      h('div', { style: `font-size: 20px; font-weight: 600; color: ${accent ? 'var(--color-danger)' : 'var(--color-fg-0)'};` }, String(value)),
    );
    tiles.append(tile);
  }
  pane.append(tiles);

  // Toggles summary
  const summary = h('div', { style: 'margin-top: 8px; font-size: var(--text-xs); color: var(--color-fg-3);' });
  summary.append(
    p.dnc_enabled ? 'DNC on · ' : 'DNC off · ',
    p.time_window_enabled
      ? `Window ${p.time_window[0]}–${p.time_window[1]} local · `
      : 'Time window off · ',
    `${p.will_dial || 0} of ${p.total || 0} will be dialed.`
  );
  pane.append(summary);

  // Sample skipped
  if (p.sample_skipped && p.sample_skipped.length) {
    const list = h('div', { style: 'margin-top: 8px; max-height: 160px; overflow: auto; border: 1px solid var(--color-line); border-radius: 6px;' });
    for (const s of p.sample_skipped) {
      list.append(h('div', { style: 'display: flex; gap: 8px; align-items: center; padding: 4px 8px; border-bottom: 1px solid var(--color-line); font-size: 12px;' },
        createBadge({ variant: s.reason === 'dnc' ? 'danger' : 'warning', children: s.reason }),
        h('span', { class: 'mono', style: 'color: var(--color-fg-2);' }, s.phone || s.contact_id || '—'),
      ));
    }
    pane.append(list);
  }
}
