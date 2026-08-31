/* =====================================================================
 * agentops/screens/power-dialer.js
 * Power Dialer screen. Pick list, preview, launch, live progress.
 * ===================================================================== */

import { h, formatDate, formatDuration } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createInput } from '../ui/input.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError, toastSuccess } from '../ui/toast.js';

const STATUS_VARIANT = {
  queued:     'neutral',
  dialing:    'info',
  connected:  'success',
  ringing:    'warning',
  voicemail:  'accent',
  'no-answer':'warning',
  busy:       'warning',
  failed:     'danger',
  completed:  'success',
};

let _state = { contacts: [], preview: [], running: false, runId: null, items: [], throttle: 5, inFlight: 0 };

export async function mountPowerDialerScreen(root) {
  root.innerHTML = '';

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Power Dialer'),
      h('p', { class: 'page-sub' }, 'Launch a 50-contact campaign. Live progress. Retry failed.')
    ),
    h('div', { class: 'page-actions' })
  ));

  // Setup card
  const setup = h('div', { class: 'card', style: 'margin-bottom: var(--space-4);' });
  const setupBody = h('div', { class: 'card-body', style: 'display:grid; grid-template-columns: 1fr 200px 160px auto; gap: var(--space-3); align-items:end;' });

  const listInput = createInput({
    label: 'Contact list', placeholder: 'Paste numbers, one per line (E.164)',
    onInput: (e) => {
      _state.contacts = e.target.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean).map(parseContact);
      updatePreview();
    }
  });
  listInput.input.rows = 4;
  listInput.input.style.height = 'auto';
  listInput.input.style.minHeight = '80px';
  listInput.input.style.fontFamily = 'var(--font-mono)';

  const throttle = createInput({
    label: 'Throttle (max concurrent)', type: 'number', value: '5',
    onInput: (e) => { _state.throttle = Math.max(1, Math.min(20, parseInt(e.target.value, 10) || 5)); }
  });

  const callerNum = createInput({
    label: 'From number', placeholder: '+15078731084', value: '+15078731084'
  });

  const launchBtn = createButton({
    variant: 'primary', size: 'lg', children: 'Launch campaign',
    onClick: () => launchCampaign(root)
  });

  setupBody.append(listInput, throttle, callerNum, launchBtn);
  setup.append(h('div', { class: 'card-head' }, h('h3', {}, 'Campaign setup')), setupBody);
  root.append(setup);

  // Progress card
  const progress = h('div', { class: 'card' });
  const progressHead = h('div', { class: 'card-head' },
    h('div', {},
      h('h3', {}, 'Progress'),
      h('p', { class: 'sub', style: 'margin:4px 0 0; font-size:var(--text-sm); color:var(--color-fg-3);' }, 'Live updates as calls connect')
    ),
    h('div', { id: 'pd-summary' }, createBadge({ variant: 'neutral', children: 'Idle' }))
  );
  const progressBody = h('div', { class: 'card-body' });
  progress.append(progressHead, progressBody);
  root.append(progress);

  // Initial state
  const meter = h('div', { class: 'pd-progress' },
    h('span', { class: 'mono', id: 'pd-meter-label' }, '0/0 in flight'),
    h('div', { class: 'pd-meter' }, h('div', { class: 'pd-meter-fill', id: 'pd-meter-fill', style: 'width: 0%;' })),
    h('button', { type: 'button', class: 'btn btn-ghost btn-sm', onClick: abortCampaign, id: 'pd-abort' }, 'Abort')
  );
  progressBody.append(meter);
  const list = h('div', { id: 'pd-list' });
  progressBody.append(list);
  list.append(createEmptyState({
    icon: '▶',
    title: 'No active campaign',
    body: 'Add contacts above and click Launch to start dialing.',
  }));
}

function parseContact(s) {
  // very tolerant: "+1 555 123 4567" or "foo@bar" or "Name <num>"
  const m = s.match(/^(.*?)\s*<?([+\d][\d\s\-().]{6,})>?$/);
  if (!m) return { name: '', number: s };
  return { name: (m[1] || '').trim(), number: m[2].replace(/[^\d+]/g, '') };
}

function updatePreview() {
  // could re-render preview; for now just log
}

async function launchCampaign(root) {
  if (!_state.contacts.length) { toastError('Add at least one contact to launch.'); return; }
  if (_state.running) { toastError('A campaign is already running.'); return; }

  const list = root.querySelector('#pd-list');
  list.innerHTML = '';
  const items = _state.contacts.map((c, i) => ({ id: 'pd-' + i, ...c, status: 'queued' }));
  _state.items = items;
  _state.running = true;

  for (const it of items) list.append(renderRow(it));

  try {
    const res = await api.post('/calls/power-dialer/start', {
      contacts: items.map(i => ({ name: i.name, number: i.number })),
      throttle: _state.throttle,
    });
    _state.runId = res.run_id || res.id;
    toastSuccess(`Campaign started · ${items.length} contacts`);
    // simulate progress (the backend will push WS events; this is the local optimistic loop)
    simulateProgress(root);
  } catch (e) {
    _state.running = false;
    toastError('Failed to start campaign: ' + e.message);
  }
}

function simulateProgress(root) {
  // If the backend WS isn't wired yet, we tick a local simulation so the UI is testable.
  const tick = () => {
    if (!_state.running) return;
    const inFlight = _state.items.filter(i => i.status === 'dialing' || i.status === 'ringing').length;
    _state.inFlight = inFlight;
    updateMeter(root);
    updateSummary(root);

    // move one from queued to dialing if under throttle
    const queued = _state.items.find(i => i.status === 'queued');
    if (queued && inFlight < _state.throttle) {
      queued.status = 'dialing';
      queued.ai_mode = 'ai';   // AI is greeting while the dial connects
      refreshRow(root, queued);
      setTimeout(() => {
        const r = Math.random();
        queued.status = r < 0.6 ? 'connected' : r < 0.75 ? 'voicemail' : r < 0.9 ? 'no-answer' : 'failed';
        // After connection the human takes over (whisper mode).
        queued.ai_mode = queued.status === 'voicemail' ? 'voicemail' :
                         queued.status === 'no-answer' ? 'ai' : 'human';
        refreshRow(root, queued);
        updateSummary(root);
      }, 800 + Math.random() * 1200);
    }
    if (_state.items.some(i => i.status === 'queued' || i.status === 'dialing' || i.status === 'ringing')) {
      setTimeout(tick, 600);
    } else {
      _state.running = false;
      updateSummary(root);
    }
  };
  tick();
}

function abortCampaign() {
  if (!_state.running) return;
  _state.running = false;
  toastSuccess('Campaign aborted');
  const label = document.getElementById('pd-meter-label');
  if (label) label.textContent = 'Aborted';
}

function updateMeter(root) {
  const total = _state.items.length;
  const done = _state.items.filter(i => ['connected', 'voicemail', 'no-answer', 'failed', 'busy', 'completed'].includes(i.status)).length;
  const label = document.getElementById('pd-meter-label');
  const fill = document.getElementById('pd-meter-fill');
  if (label) label.textContent = `${_state.inFlight}/${_state.throttle} in flight · ${done}/${total} done`;
  if (fill) fill.style.width = (total ? (done / total) * 100 : 0) + '%';
}

function updateSummary(root) {
  const el = document.getElementById('pd-summary');
  if (!el) return;
  el.innerHTML = '';
  const counts = {};
  for (const it of _state.items) counts[it.status] = (counts[it.status] || 0) + 1;
  if (!_state.running && _state.items.every(i => i.status !== 'queued' && i.status !== 'dialing' && i.status !== 'ringing')) {
    el.append(createBadge({ variant: 'success', children: 'Complete' }));
  } else {
    el.append(createBadge({ variant: 'info', dot: true, children: 'Running' }));
  }
}

function renderRow(it) {
  return h('div', { class: 'rec-row', id: `pd-row-${it.id}` },
    h('div', { class: 'rec-row-meta' },
      h('div', { class: 'rec-row-name' }, it.name || it.number),
      h('div', { class: 'rec-row-num mono' }, it.number)
    ),
    h('div', {},
      createBadge({ variant: STATUS_VARIANT[it.status] || 'neutral', dot: true, children: it.status })),
    h('div', {},
      createBadge({ variant: AI_VARIANT[it.ai_mode] || 'neutral', children: it.ai_mode || '—' })),
    h('div', {},
      h('button', {
        type: 'button', class: 'btn btn-ghost btn-sm', onClick: () => retry(it),
      }, 'Retry')
    )
  );
}

const AI_VARIANT = {
  ai:        'accent',
  human:     'info',
  muted:     'warning',
  transfer:  'warning',
  voicemail: 'neutral',
};

function refreshRow(root, it) {
  const old = document.getElementById(`pd-row-${it.id}`);
  const fresh = renderRow(it);
  if (old) old.replaceWith(fresh);
}

function retry(it) {
  it.status = 'queued';
  toastSuccess(`Re-queued ${it.name || it.number}`);
  // optimistic restart
  const root = document.getElementById('pd-list')?.closest('.app-main');
  if (root) {
    if (!_state.running) { _state.running = true; simulateProgress(root); }
  }
}
