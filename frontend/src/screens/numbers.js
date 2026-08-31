/* =====================================================================
 * agentops/screens/numbers.js
 * Number provisioning UI (issue #15). Search + buy + own + assign.
 * ===================================================================== */

import { h, formatDate } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createInput } from '../ui/input.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createSelect } from '../ui/select.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toast, toastError, toastSuccess } from '../ui/toast.js';

let _state = {
  area_code: '',
  country: 'US',
  has_voice: true,
  has_sms: false,
  has_mms: false,
  available: [],
  owned: [],
  workflows: [],
  assistants: [],
  loading: { search: false, owned: false },
};

export async function mountNumbersScreen(root) {
  root.innerHTML = '';

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Numbers'),
      h('p', { class: 'page-sub' }, 'Search Telnyx inventory, buy numbers, and assign them to workflows or assistants.')
    )
  ));

  const grid = h('div', { class: 'numbers-grid' });

  // Left: search
  const search = h('div', { class: 'card' });
  const sb = h('div', { class: 'card-body' });
  search.append(h('div', { class: 'card-head' }, h('h3', {}, 'Search inventory')), sb);
  grid.append(search);

  // Middle: available
  const avail = h('div', { class: 'card' });
  const avb = h('div', { class: 'card-body', id: 'numbers-avail-body' });
  avail.append(h('div', { class: 'card-head' }, h('h3', {}, 'Available')), avb);
  grid.append(avail);

  // Right: owned
  const own = h('div', { class: 'card' });
  const ob = h('div', { class: 'card-body', id: 'numbers-owned-body' });
  own.append(h('div', { class: 'card-head' }, h('h3', {}, 'Your numbers')), ob);
  grid.append(own);

  root.append(grid);

  renderSearchForm(sb);
  renderAvailableEmpty();
  await loadOwned();
  await loadWorkflowsAndAssistants();
  renderOwned();
}

function renderSearchForm(body) {
  body.innerHTML = '';
  const ac = createInput({ label: 'Area code', placeholder: '512', value: _state.area_code });
  ac.input.addEventListener('input', e => { _state.area_code = e.target.value; });
  const country = createSelect({
    label: 'Country',
    value: _state.country,
    options: [
      { value: 'US', label: 'United States' },
      { value: 'CA', label: 'Canada' },
      { value: 'GB', label: 'United Kingdom' },
    ],
    onChange: e => { _state.country = e.target.value; },
  });

  const featureBox = h('div', { style: 'display: flex; gap: var(--space-3); margin-top: var(--space-2);' });
  for (const f of ['voice', 'sms', 'mms']) {
    const cb = h('label', { style: 'display: flex; align-items: center; gap: 4px;' },
      h('input', { type: 'checkbox',
        checked: _state[`has_${f}`] || undefined,
        onChange: e => { _state[`has_${f}`] = e.target.checked; },
      }),
      h('span', {}, f),
    );
    featureBox.append(cb);
  }

  body.append(ac, country, featureBox);
  body.append(h('div', { style: 'margin-top: var(--space-3);' },
    createButton({
      variant: 'primary',
      onClick: () => doSearch(),
      children: 'Search',
    })
  ));
}

async function doSearch() {
  const body = document.getElementById('numbers-avail-body');
  if (!body) return;
  _state.loading.search = true;
  body.innerHTML = '';
  body.append(createSkeleton({ lines: 4, height: 36 }));
  const params = new URLSearchParams();
  if (_state.area_code) params.set('area_code', _state.area_code);
  params.set('country', _state.country);
  if (_state.has_voice) params.set('has_voice', '1');
  if (_state.has_sms) params.set('has_sms', '1');
  if (_state.has_mms) params.set('has_mms', '1');
  try {
    const res = await api.get('/numbers/available?' + params.toString());
    _state.available = res.available || [];
    _state.loading.search = false;
    renderAvailable(body);
  } catch (e) {
    body.innerHTML = '';
    body.append(h('p', { style: 'color: var(--color-fg-3);' },
      'Search failed: ' + e.message + ' (Telnyx API key may not be configured)'));
  }
}

function renderAvailableEmpty() {
  const body = document.getElementById('numbers-avail-body');
  if (!body) return;
  body.innerHTML = '';
  body.append(createEmptyState({
    icon: '⌕',
    title: 'Search for numbers',
    body: 'Enter an area code and click Search to see Telnyx inventory.',
  }));
}

function renderAvailable(body) {
  body.innerHTML = '';
  if (!_state.available.length) {
    body.append(createEmptyState({
      icon: '∅',
      title: 'No numbers matched',
      body: 'Try a different area code or relax the feature filters.',
    }));
    return;
  }
  for (const n of _state.available) {
    const row = h('div', { class: 'rec-row', style: 'border-radius: 6px; margin-bottom: 6px;' },
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name mono' }, n.phone_number || '—'),
        h('div', { class: 'rec-row-num' },
          (n.locality || '') + (n.region ? `, ${n.region}` : '') + ` · ${n.country_code}` +
          (n.monthly_cost ? ` · $${n.monthly_cost}/mo` : ''))
      ),
      h('div', { style: 'display: flex; gap: 4px;' },
        ...(n.features || []).slice(0, 3).map(f => createBadge({ variant: 'neutral', children: f })),
      ),
      h('div', {},
        createButton({ variant: 'primary', size: 'sm',
          onClick: () => buyNumber(n),
          children: 'Buy' }),
      )
    );
    body.append(row);
  }
}

async function buyNumber(n) {
  if (!confirm(`Buy ${n.phone_number} for $${n.monthly_cost || '?'}/mo?`)) return;
  try {
    await api.post('/numbers/buy', { phone_number: n.phone_number });
    toastSuccess('Number purchased');
    await loadOwned();
    renderOwned();
  } catch (e) {
    toastError('Buy failed: ' + (e?.data?.detail || e.message));
  }
}

async function loadOwned() {
  _state.loading.owned = true;
  const body = document.getElementById('numbers-owned-body');
  if (body) {
    body.innerHTML = '';
    body.append(createSkeleton({ lines: 3, height: 36 }));
  }
  try {
    const res = await api.get('/numbers');
    _state.owned = res.numbers || [];
    _state.loading.owned = false;
  } catch (e) {
    _state.owned = [];
  }
}

async function loadWorkflowsAndAssistants() {
  try {
    const w = await api.get('/workflows');
    _state.workflows = w.workflows || [];
  } catch (e) { _state.workflows = []; }
  try {
    const a = await api.get('/assistants');
    _state.assistants = a.assistants || [];
  } catch (e) { _state.assistants = []; }
}

function renderOwned() {
  const body = document.getElementById('numbers-owned-body');
  if (!body) return;
  body.innerHTML = '';
  if (!_state.owned.length) {
    body.append(createEmptyState({
      icon: '☎',
      title: 'No numbers yet',
      body: 'Buy one on the left.',
    }));
    return;
  }
  for (const n of _state.owned) {
    const row = h('div', { class: 'rec-row', style: 'border-radius: 6px; margin-bottom: 6px; flex-direction: column; align-items: stretch;' },
      h('div', { style: 'display: flex; justify-content: space-between; align-items: center;' },
        h('div', { class: 'rec-row-meta' },
          h('div', { class: 'rec-row-name mono' }, n.phone_number),
          h('div', { class: 'rec-row-num' },
            n.country_code + (n.monthly_cost ? ` · $${n.monthly_cost}/mo` : '') +
            (n.per_minute_rate ? ` · $${n.per_minute_rate}/min` : ''))
        ),
        h('div', { style: 'display: flex; gap: 4px;' },
          createButton({ variant: 'danger', size: 'sm',
            onClick: () => releaseNumber(n),
            children: 'Release' }),
        )
      ),
      h('div', { style: 'display: flex; gap: 8px; align-items: center; margin-top: 6px;' },
        h('span', { style: 'font-size: var(--text-sm); color: var(--color-fg-2);' }, 'Assign:'),
        createSelect({
          value: n.assignment_kind || '',
          options: [
            { value: '', label: '— none —' },
            { value: 'workflow', label: 'Workflow' },
            { value: 'assistant', label: 'AI Assistant' },
            { value: 'direct', label: 'Direct (inbox)' },
          ],
          onChange: (e) => updateAssignmentKind(n, e.target.value, body),
        }),
        n.assignment_kind && (n.assignment_kind === 'workflow' || n.assignment_kind === 'assistant')
          ? createTargetSelect(n, body) : null,
      )
    );
    body.append(row);
  }
}

function createTargetSelect(n, body) {
  const targets = n.assignment_kind === 'workflow' ? _state.workflows : _state.assistants;
  return createSelect({
    value: n.assignment_target || '',
    options: [{ value: '', label: '— pick one —' }, ...targets.map(t => ({ value: t.id, label: t.name }))],
    onChange: (e) => updateAssignmentTarget(n, e.target.value, body),
  });
}

async function updateAssignmentKind(n, kind, body) {
  try {
    await api.patch('/numbers/' + n.id + '/assignment', { kind: kind || null, target_id: null });
    toastSuccess('Assignment updated');
    await loadOwned();
    renderOwned();
  } catch (e) {
    toastError('Update failed: ' + (e?.data?.detail || e.message));
  }
}

async function updateAssignmentTarget(n, targetId, body) {
  try {
    await api.patch('/numbers/' + n.id + '/assignment',
      { kind: n.assignment_kind, target_id: targetId || null });
    toastSuccess('Assignment updated');
    await loadOwned();
    renderOwned();
  } catch (e) {
    toastError('Update failed: ' + (e?.data?.detail || e.message));
  }
}

async function releaseNumber(n) {
  if (!confirm(`Release ${n.phone_number}? This removes it from your account.`)) return;
  try {
    await api.del('/numbers/' + n.id);
    toastSuccess('Number released');
    await loadOwned();
    renderOwned();
  } catch (e) {
    toastError('Release failed: ' + (e?.data?.detail || e.message));
  }
}
