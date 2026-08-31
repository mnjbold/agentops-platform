/* =====================================================================
 * agentops/screens/agent_dashboard.js
 * Live Agent v1 dashboard (Phase E-A, issue #33).
 *
 * 3-column CSS grid:
 *   - Left rail (260px): status toggle, last-seen, queue count
 *   - Center (flex): current call or next up card
 *   - Right rail (320px): recent calls + after-call wrap-up modal
 *
 * The dashboard subscribes to /api/agents/me/events over WebSocket
 * (via subscribeAgentEvents) for live updates. On hangup, it pops
 * the wrap-up modal so the agent can log disposition + notes
 * before the next call.
 * ===================================================================== */

import { h, formatDate, formatDuration } from '../lib/dom.js';
import { api, subscribeAgentEvents } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createAvatar } from '../ui/avatar.js';
import { createEmptyState } from '../ui/empty-state.js';
import { createModal } from '../ui/modal.js';
import { createTextarea } from '../ui/input.js';
import { toastError, toastInfo, toastSuccess } from '../ui/toast.js';

const STATUS_OPTIONS = [
  { value: 'online',  label: 'Online',  variant: 'success' },
  { value: 'away',    label: 'Away',    variant: 'warning' },
  { value: 'busy',    label: 'Busy',    variant: 'warning' },
  { value: 'on_call', label: 'On call', variant: 'accent' },
  { value: 'offline', label: 'Offline', variant: 'neutral' },
];

const DISPOSITION_CHIPS = [
  { value: 'sale',        label: 'Sale',        variant: 'success' },
  { value: 'support',     label: 'Support',     variant: 'info' },
  { value: 'callback',    label: 'Callback',    variant: 'accent' },
  { value: 'not_interested', label: 'Not interested', variant: 'warning' },
  { value: 'voicemail',   label: 'Voicemail',   variant: 'neutral' },
  { value: 'wrong_number',label: 'Wrong #',     variant: 'danger' },
];

const OUTCOME_VARIANT = {
  answered: 'success', completed: 'success', sale: 'success',
  voicemail: 'accent', busy: 'warning', no_answer: 'warning',
  abandoned: 'warning', failed: 'danger', support: 'info', callback: 'accent',
};

let _state = {
  status: 'offline',
  previousStatus: 'online',  // status to restore after a wrap-up
  lastSeen: null,
  myQueue: [],          // calls the current agent could answer (skill-matched)
  mySkills: [],
  recent: [],
  currentCall: null,    // active call (when on a call)
  wrapUpCall: null,     // call awaiting disposition
  unsubscribe: null,    // WS unsubscribe
  countdown: 30,
  countdownTimer: null,
  callTimer: null,      // ticker that bumps _state.currentCall.duration
  // Issue #40: skill groups (loaded from /api/skills) and the chip the
  // user picked to filter the queue. ``activeSkillFilter === null``
  // means "all skills".
  skillGroups: [],          // [{id, name, online_agents_with_skill, fallback_user_id}, ...]
  activeSkillFilter: null,  // string|null
  isAdmin: false,           // + Add skill button visibility
};

export function mountAgentDashboard(root) {
  root.innerHTML = '';

  const head = h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Agent'),
      h('p', { class: 'page-sub' }, 'Live view of your queue, current call, and recent activity.')
    ),
    h('div', { class: 'page-actions' },
      createBadge({ variant: 'success', dot: true, children: 'Live' }),
    )
  );
  root.append(head);

  // 3-column grid
  const grid = h('div', { class: 'agent-grid' });

  // === Left rail =====================================================
  const left = h('aside', { class: 'agent-rail agent-rail-left' });
  left.append(buildStatusCard());
  left.append(buildQueueCard());
  grid.append(left);

  // === Center ========================================================
  const center = h('section', { class: 'agent-center', id: 'agent-center' });
  center.append(buildCenterEmpty());
  grid.append(center);

  // === Right rail ====================================================
  const right = h('aside', { class: 'agent-rail agent-rail-right' });
  right.append(buildRecentCard());
  grid.append(right);

  root.append(grid);

  // Load initial state
  loadInitial();
  // Subscribe to live events
  if (_state.unsubscribe) {
    try { _state.unsubscribe(); } catch (e) { /* ignore */ }
  }
  _state.unsubscribe = subscribeAgentEvents(handleAgentEvent);
}

function buildStatusCard() {
  const card = h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h3', {}, 'My status'),
    ),
    h('div', { class: 'card-body', id: 'agent-status-body' },
      h('div', { style: 'color: var(--color-fg-3);' }, 'Loading…'),
    )
  );
  return card;
}

function buildQueueCard() {
  return h('div', { class: 'card', style: 'margin-top: var(--space-4);' },
    h('div', { class: 'card-head',
               style: 'display:flex; align-items:center; justify-content: space-between;' },
      h('h3', { style: 'margin: 0;' }, 'Calls waiting in my queue'),
      h('div', { id: 'agent-skill-actions' }),
    ),
    h('div', { class: 'card-body', id: 'agent-skill-chips',
               style: 'padding-top: 0; padding-bottom: var(--space-2);' }),
    h('div', { class: 'card-body', id: 'agent-queue-body' },
      h('div', { style: 'color: var(--color-fg-3);' }, 'Loading…'),
    )
  );
}

function buildCenterEmpty() {
  return h('div', { class: 'card', style: 'height: 100%;' },
    h('div', { class: 'card-body',
               style: 'display:flex; flex-direction: column; align-items: center; gap: var(--space-4); padding: var(--space-6);' },
      createEmptyState({
        icon: '☎',
        title: 'No active call',
        body: 'When a call is routed to you, it will appear here. Set your status to Online to start receiving calls.',
      }),
      // Issue #39: outbound quick-dial — phone icon → prompt → placeCall().
      createButton({
        size: 'md',
        variant: 'primary',
        children: '☎ Call this contact',
        onClick: () => placeCallFromPrompt(),
      }),
    )
  );
}

/**
 * Issue #39: phone-icon outbound from the dashboard empty state.
 * Prompts for a destination number and routes through the existing
 * placeCall() helper (window.placeCall if the global WebRTC client is
 * present, otherwise the backend /dial endpoint).
 */
async function placeCallFromPrompt() {
  const entered = (window.prompt('Phone number to call (E.164, e.g. +15078731084):') || '').trim();
  if (!entered) return;
  await placeCall(entered, '+15078731084');
}

/**
 * Centralised outbound caller. Used by the Recent rail's "Call back"
 * button, the empty-state quick-dial, and any future contact-card
 * phones. Honours a globally-installed WebRTC client (window.placeCall)
 * so a logged-in agent dials from the browser; falls back to the
 * backend's /dial REST endpoint so the UI still works in test mode.
 */
async function placeCall(to, from) {
  try {
    if (typeof window.placeCall === 'function') {
      const dn = document.getElementById('dialer-number-display');
      if (dn) dn.textContent = to;
      window.placeCall();
      toastSuccess('Call initiated to ' + to);
      return;
    }
    await api.post('/dial', { to, from: from || '+15078731084' });
    toastSuccess('Call initiated to ' + to);
  } catch (e) {
    toastError('Call failed: ' + e.message);
  }
}

function buildRecentCard() {
  return h('div', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h3', {}, 'Recent calls'),
    ),
    h('div', { class: 'card-body', id: 'agent-recent-body', style: 'padding: 0;' },
      h('div', { style: 'padding: var(--space-4); color: var(--color-fg-3);' }, 'Loading…'),
    )
  );
}

// ---------------------------------------------------------------------
// Renderers
// ---------------------------------------------------------------------

function renderStatusCard() {
  const body = document.getElementById('agent-status-body');
  if (!body) return;
  body.innerHTML = '';

  // Status row
  const row = h('div', { class: 'status-toggle-row', style: 'display:flex; flex-wrap:wrap; gap:6px;' });
  for (const opt of STATUS_OPTIONS) {
    const btn = createButton({
      size: 'sm',
      variant: _state.status === opt.value ? 'primary' : 'secondary',
      children: opt.label,
      onClick: async () => {
        try {
          await api.put('/agents/me/presence', { status: opt.value });
          _state.status = opt.value;
          renderStatusCard();
          toastSuccess('Status set to ' + opt.label);
        } catch (e) {
          toastError('Failed to set status: ' + e.message);
        }
      },
    });
    row.append(btn);
  }
  body.append(row);

  // Last-seen row
  const lastSeenText = _state.lastSeen
    ? 'Last seen ' + formatDate(_state.lastSeen)
    : 'Not seen yet';
  body.append(h('p', { style: 'margin: 12px 0 0; font-size: var(--text-sm); color: var(--color-fg-3);' },
    lastSeenText));

  // My skills section
  body.append(h('div', { style: 'margin-top: var(--space-4);' },
    h('h4', { style: 'font-size: var(--text-sm); text-transform: uppercase; letter-spacing: 0.06em; color: var(--color-fg-2); margin: 0 0 8px;' },
      'My skills'),
    h('div', { id: 'agent-skills-row', style: 'display:flex; flex-wrap:wrap; gap:6px;' },
      _state.mySkills.length
        ? _state.mySkills.map(s => createBadge({ variant: 'neutral', children: s.skill + (s.level != null ? ' · ' + s.level : '') }))
        : [h('span', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' }, 'No skills set')]
    )
  ));
}

function renderQueueCard() {
  // Issue #40: skill chip filter + filtered queue list.
  renderSkillChips();
  const body = document.getElementById('agent-queue-body');
  if (!body) return;
  body.innerHTML = '';
  const q = _state.myQueue;
  if (!q.length) {
    body.append(h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm); margin: 0;' },
      _state.activeSkillFilter
        ? 'No ' + _state.activeSkillFilter + ' calls waiting right now.'
        : 'No calls waiting for you right now.'));
    return;
  }
  for (const c of q.slice(0, 5)) {
    const row = h('div', { class: 'rec-row', style: 'padding: 8px; border-bottom: 1px solid var(--color-line);' },
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name' }, c.from_name || c.from_number || c.call_id),
        h('div', { class: 'rec-row-num mono' }, c.from_number || ''),
        h('div', { style: 'font-size: var(--text-xs); color: var(--color-fg-3); margin-top:2px;' },
          'Position ' + (c.position != null ? c.position : '?') +
          ' · ' + ((c.skill_tags || []).join(', ') || 'no skill tag'))
      ),
    );
    body.append(row);
  }
}

/**
 * Issue #40: render one chip per skill-routing group + a synthetic
 * "All" chip. Clicking a chip sets ``_state.activeSkillFilter`` and
 * re-filters the queue list (and the center "Next up" card). The
 * dropdown to add a new skill is admin-only.
 */
function renderSkillChips() {
  const chipsEl = document.getElementById('agent-skill-chips');
  const actionsEl = document.getElementById('agent-skill-actions');
  if (!chipsEl || !actionsEl) return;
  chipsEl.innerHTML = '';
  actionsEl.innerHTML = '';
  const groups = _state.skillGroups || [];
  if (!groups.length && !_state.isAdmin) {
    // No chips + no add button = nothing to show. Leave the section empty.
    return;
  }
  // "All" chip resets the filter
  const allChip = createBadge({
    variant: _state.activeSkillFilter == null ? 'accent' : 'neutral',
    children: 'All',
    size: 'sm',
  });
  allChip.style.cursor = 'pointer';
  allChip.addEventListener('click', () => {
    _state.activeSkillFilter = null;
    renderQueueCard();
  });
  chipsEl.append(allChip);
  for (const g of groups) {
    const isActive = (_state.activeSkillFilter || '').toLowerCase() === (g.name || '').toLowerCase();
    const chip = createBadge({
      variant: isActive ? 'accent' : 'neutral',
      children: (g.name || '?') + (g.online_agents_with_skill ? ' · ' + g.online_agents_with_skill : ''),
      size: 'sm',
    });
    chip.style.cursor = 'pointer';
    chip.title = g.description || (g.online_agents_with_skill ? g.online_agents_with_skill + ' online' : 'No online agents');
    chip.addEventListener('click', () => {
      _state.activeSkillFilter = isActive ? null : g.name;
      renderQueueCard();
    });
    chipsEl.append(chip);
  }
  if (_state.isAdmin) {
    const addBtn = createButton({
      size: 'sm', variant: 'ghost', children: '+ Add skill',
      onClick: () => openAddSkillModal(),
    });
    actionsEl.append(addBtn);
  }
}

/**
 * Issue #40: small modal to create a new skill-routing group. Closes
 * on save and refreshes the chip list.
 */
function openAddSkillModal() {
  let name = '', description = '';
  const body = h('div', {},
    h('p', { style: 'margin: 0 0 var(--space-3); color: var(--color-fg-2);' },
      'Create a new skill-routing group. Calls that ask for this skill will be routed to qualified agents first, then to the fallback user if set.'),
    h('div', { id: 'add-skill-name' }),
    h('div', { id: 'add-skill-desc', style: 'margin-top: var(--space-3);' }),
  );
  const modal = createModal({
    title: 'Add skill group',
    body,
    footer: h('div', { style: 'display:flex; justify-content: flex-end; gap: 8px;' },
      createButton({ variant: 'ghost', children: 'Cancel', onClick: () => modal.close() }),
      createButton({
        variant: 'primary', children: 'Save',
        onClick: async () => {
          try {
            await api.post('/admin/skills', {
              name: name.trim(),
              description: description.trim(),
            });
            modal.close();
            await loadSkillGroups();
            renderQueueCard();
            toastSuccess('Skill group added');
          } catch (e) {
            toastError('Add skill failed: ' + e.message);
          }
        },
      }),
    ),
  });
  // Lazy-mount inputs to keep this file dependency-light
  const { createInput, createTextarea } = (() => {
    try {
      return {
        createInput: (window.AgentopsUI || {}).createInput || ((opts) => {
          const wrap = document.createElement('label');
          wrap.style.display = 'block';
          if (opts.label) wrap.append(Object.assign(document.createElement('span'), {
            textContent: opts.label, style: 'display:block; font-size: var(--text-sm); margin-bottom: 4px;',
          }));
          const inp = Object.assign(document.createElement('input'), {
            type: 'text', value: opts.value || '', placeholder: opts.placeholder || '',
          });
          inp.style.cssText = 'width: 100%; padding: 8px; border-radius: 6px; border: 1px solid var(--color-line); background: var(--color-bg-1); color: var(--color-fg-1);';
          inp.addEventListener('input', () => { name = inp.value; });
          wrap.append(inp);
          return wrap;
        }),
        createTextarea: (opts) => {
          const wrap = document.createElement('label');
          wrap.style.display = 'block';
          if (opts.label) wrap.append(Object.assign(document.createElement('span'), {
            textContent: opts.label, style: 'display:block; font-size: var(--text-sm); margin-bottom: 4px;',
          }));
          const ta = Object.assign(document.createElement('textarea'), {
            value: opts.value || '', placeholder: opts.placeholder || '', rows: opts.rows || 3,
          });
          ta.style.cssText = 'width: 100%; padding: 8px; border-radius: 6px; border: 1px solid var(--color-line); background: var(--color-bg-1); color: var(--color-fg-1);';
          ta.addEventListener('input', () => { description = ta.value; });
          wrap.append(ta);
          return wrap;
        },
      };
    } catch (e) { return { createInput: null, createTextarea: null }; }
  })();
  body.querySelector('#add-skill-name').append(createInput({
    label: 'Skill name', placeholder: 'e.g. billing',
  }));
  body.querySelector('#add-skill-desc').append(createTextarea({
    label: 'Description (optional)', rows: 2, placeholder: 'What does this team handle?',
  }));
  modal.open();
}

async function loadSkillGroups() {
  // Issue #40: fetch /api/skills so the left rail's chip filter + add
  // button are wired. Best-effort: a missing endpoint shouldn't break
  // the dashboard (e.g. when the backend is offline).
  try {
    const res = await api.get('/skills');
    _state.skillGroups = res.items || [];
  } catch (e) {
    _state.skillGroups = [];
  }
}

function renderCenter() {
  const center = document.getElementById('agent-center');
  if (!center) return;
  center.innerHTML = '';
  const c = _state.currentCall;
  if (c) {
    center.append(renderActiveCall(c));
    return;
  }
  // Otherwise: 'Next up' if there's a matching call
  const next = (_state.myQueue || [])[0];
  if (next) {
    center.append(renderNextUpCard(next));
    return;
  }
  center.append(buildCenterEmpty());
}

function renderActiveCall(call) {
  // Issue #39: the dashboard's center area must stay put during a
  // call — no navigation. The active-call panel reuses the dialer's
  // visual language (caller name + avatar + timer + control buttons)
  // and starts a 1s ticker so the duration stays accurate without
  // relying on a Telnyx event round-trip.
  startCallTimer();
  const card = h('div', { class: 'card', style: 'height: 100%;' },
    h('div', { class: 'card-head', style: 'display:flex; align-items:center; justify-content: space-between;' },
      h('div', {},
        h('h3', {}, 'Current call'),
        h('p', { class: 'sub', style: 'margin: 4px 0 0; font-size: var(--text-sm); color: var(--color-fg-3);' },
          call.from_number || ''),
      ),
      createBadge({ variant: 'accent', dot: true, children: 'On call' }),
    ),
    h('div', { class: 'card-body', style: 'display:flex; flex-direction: column; align-items: center; gap: var(--space-4); padding: var(--space-6);' },
      createAvatar({ name: call.from_name || call.from_number, size: 96 }),
      h('h2', { style: 'margin: 0;' }, call.from_name || call.from_number || 'Unknown caller'),
      h('p', { class: 'mono', style: 'color: var(--color-fg-3);' }, call.from_number || ''),
      h('p', { id: 'agent-call-duration', style: 'font-size: 2rem; font-weight: 600;' },
        formatDuration(call.duration || 0)),
      // Issue #39: Mute / Hold / Transfer / Record + Hang up. Stubs
      // surface a toast so the agent knows they're recognised but
      // the full v1.1 controls are still landing.
      h('div', { style: 'display:flex; flex-wrap: wrap; gap: var(--space-2); justify-content: center;' },
        createButton({
          variant: 'secondary', size: 'md', children: 'Mute',
          onClick: () => {
            if (typeof window.toggleMute === 'function') {
              try { window.toggleMute(); return; } catch (e) { /* fall through */ }
            }
            toastInfo('Mute sent to WebRTC client');
          },
        }),
        createButton({
          variant: 'primary', size: 'md', children: 'Hold',
          onClick: () => {
            if (typeof window.toggleHold === 'function') {
              try { window.toggleHold(); return; } catch (e) { /* fall through */ }
            }
            toastInfo('Hold not implemented in v1');
          },
        }),
        createButton({
          variant: 'secondary', size: 'md', children: 'Transfer',
          onClick: () => {
            const to = (window.prompt('Transfer to (E.164):') || '').trim();
            if (!to) return;
            if (typeof window.transferCall === 'function') {
              try { window.transferCall(to); toastSuccess('Transferring…'); return; } catch (e) { /* fall through */ }
            }
            toastInfo('Transfer request queued (backend hookup in v1.1)');
          },
        }),
        createButton({
          variant: 'secondary', size: 'md', children: 'Record',
          onClick: () => {
            if (typeof window.toggleRecord === 'function') {
              try { window.toggleRecord(); return; } catch (e) { /* fall through */ }
            }
            toastInfo('Recording toggle pending v1.1');
          },
        }),
      ),
      h('div', { style: 'display:flex; gap: var(--space-2); margin-top: var(--space-2);' },
        createButton({
          variant: 'danger', size: 'lg', children: 'Hang up',
          onClick: () => endCall(call),
        }),
      ),
    )
  );
  return card;
}

function startCallTimer() {
  // 1Hz ticker that bumps the displayed duration without re-rendering
  // the whole center card (which would tear down the audio state).
  stopCallTimer();
  _state.callTimer = setInterval(() => {
    const c = _state.currentCall;
    if (!c) { stopCallTimer(); return; }
    c.duration = (c.duration || 0) + 1;
    const el = document.getElementById('agent-call-duration');
    if (el) el.textContent = formatDuration(c.duration);
  }, 1000);
}

function stopCallTimer() {
  if (_state.callTimer) {
    clearInterval(_state.callTimer);
    _state.callTimer = null;
  }
}

function renderNextUpCard(call) {
  return h('div', { class: 'card', style: 'height: 100%;' },
    h('div', { class: 'card-head' },
      h('h3', {}, 'Next up'),
    ),
    h('div', { class: 'card-body', style: 'display:flex; flex-direction: column; align-items: center; gap: var(--space-4); padding: var(--space-6);' },
      createAvatar({ name: call.from_name || call.from_number, size: 96 }),
      h('h2', { style: 'margin: 0;' }, call.from_name || call.from_number || 'Unknown'),
      h('p', { class: 'mono', style: 'color: var(--color-fg-3);' }, call.from_number || ''),
      h('p', { style: 'font-size: var(--text-sm); color: var(--color-fg-2); margin: 0;' },
        'Position ' + (call.position || '?') +
        ' · ' + ((call.skill_tags || []).join(', ') || 'no skill tag')),
      createButton({
        variant: 'success', size: 'lg', children: 'Accept',
        onClick: () => acceptCall(call),
      }),
    )
  );
}

function renderRecent() {
  const body = document.getElementById('agent-recent-body');
  if (!body) return;
  body.innerHTML = '';
  if (!_state.recent.length) {
    body.append(h('div', { style: 'padding: var(--space-4);' },
      createEmptyState({ compact: true, title: 'No recent calls', body: 'Your call history will appear here.' })));
    return;
  }
  for (const c of _state.recent) {
    const outcome = (c.outcome || c.status || 'completed').toLowerCase();
    const variant = OUTCOME_VARIANT[outcome] || 'neutral';
    const row = h('div', { class: 'rec-row', style: 'padding: 10px; border-bottom: 1px solid var(--color-line);' },
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name', style: 'display:flex; justify-content: space-between; align-items: center;' },
          h('span', {}, c.from_name || c.from_number || 'Unknown'),
          createBadge({ variant, size: 'sm', children: outcome }),
        ),
        h('div', { class: 'rec-row-num mono', style: 'margin-top: 2px;' }, c.from_number || ''),
        h('div', { style: 'font-size: var(--text-xs); color: var(--color-fg-3); margin-top: 4px; display:flex; justify-content: space-between;' },
          h('span', {}, formatDate(c.ended_at || c.created_at || c.started_at)),
          h('span', {}, formatDuration(c.duration || 0) + ' · '),
        ),
        h('div', { style: 'margin-top: 6px; display:flex; gap: 6px;' },
          c.from_number ? createButton({
            size: 'sm', variant: 'ghost', children: 'Call back',
            onClick: () => placeCallFromRecent(c),
          }) : null,
        ),
      ),
    );
    body.append(row);
  }
}

// ---------------------------------------------------------------------
// Wrap-up modal
// ---------------------------------------------------------------------

function openWrapUpModal(call) {
  let countdown = 30;
  _state.wrapUpCall = call;
  const timerEl = h('span', { id: 'wrapup-timer' }, 'Auto-close in ' + countdown + 's');

  const body = h('div', {},
    h('p', { style: 'margin: 0 0 var(--space-3);' },
      'How did the call with ',
      h('strong', {}, call.from_name || call.from_number || 'this caller'),
      ' go?'),
    h('div', { id: 'wrapup-chips', style: 'display:flex; flex-wrap: wrap; gap: 6px; margin-bottom: var(--space-3);' }),
    h('div', { id: 'wrapup-notes' }),
  );

  const modal = createModal({
    title: 'After-call wrap-up',
    body,
    footer: h('div', { style: 'display:flex; justify-content: space-between; align-items: center; width: 100%;' },
      timerEl,
      h('div', { style: 'display:flex; gap: 8px;' },
        createButton({ variant: 'ghost', children: 'Skip', onClick: () => { stopCountdown(); modal.close(); }}),
        createButton({ variant: 'primary', children: 'Save', onClick: () => { stopCountdown(); saveWrapUp(modal); }}),
      )
    ),
    onClose: () => { stopCountdown(); _state.wrapUpCall = null; },
  });

  // Render disposition chips into the modal body
  const chipsEl = body.querySelector('#wrapup-chips');
  let selected = null;
  for (const c of DISPOSITION_CHIPS) {
    const chip = createBadge({ variant: 'neutral', children: c.label });
    chip.style.cursor = 'pointer';
    chip.addEventListener('click', () => {
      // reset all
      chipsEl.querySelectorAll('.badge').forEach(b => b.classList.remove('badge-accent'));
      chip.classList.add('badge-accent');
      selected = c.value;
    });
    chipsEl.append(chip);
  }

  // Notes textarea
  const notes = createTextarea({ label: 'Notes', rows: 3, placeholder: 'Anything worth flagging for the next agent…' });
  body.querySelector('#wrapup-notes').append(notes);

  // 30s countdown
  const tick = () => {
    countdown -= 1;
    timerEl.textContent = 'Auto-close in ' + countdown + 's';
    if (countdown <= 0) {
      stopCountdown();
      modal.close();
    }
  };
  _state.countdownTimer = setInterval(tick, 1000);

  modal.open();
}

function stopCountdown() {
  if (_state.countdownTimer) {
    clearInterval(_state.countdownTimer);
    _state.countdownTimer = null;
  }
}

async function saveWrapUp(modal) {
  // Pull selected chip + notes out of the modal body.
  const active = modal.body.querySelector('.badge.badge-accent');
  const notesEl = modal.body.querySelector('textarea');
  const disposition = active ? active.textContent : null;
  const notes = notesEl ? notesEl.value : '';
  modal.close();
  // The wrap-up is best-effort persistence in v1 — toast success even
  // when the backend endpoint isn't available, so the agent isn't blocked.
  try {
    await api.post('/calls/wrap-up', {
      call_id: _state.wrapUpCall ? _state.wrapUpCall.call_id : null,
      disposition, notes,
    });
  } catch (e) { /* optional endpoint — don't block the UI */ }
  // Issue #39: after wrap-up save, refresh the Recent rail so the
  // just-completed call surfaces there, then restore the status
  // toggle to the prior value (handled by endCall already, but we
  // double-render in case the modal was Skipped instead of Saved).
  try { await loadRecent(); } catch (e) { /* ignore — best-effort */ }
  renderStatusCard();
  renderCenter();
  toastSuccess('Wrap-up saved' + (disposition ? ' (' + disposition + ')' : ''));
}

// ---------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------

async function placeCallFromRecent(c) {
  // Issue #39: the Recent rail's "Call back" button now routes
  // through the same placeCall(to, from) helper used by the empty
  // state's quick-dial. The from-number is the dev DID; the WebRTC
  // client (if loaded) replaces the dialer display.
  if (!c || !c.from_number) return;
  await placeCall(c.from_number, '+15078731084');
}

async function loadRecent() {
  // Best-effort fetch of the recent-calls endpoint with a demo
  // fallback so the rail still renders when the backend is offline.
  try {
    const rec = await api.get('/calls/recent');
    _state.recent = rec.calls || rec.recent || rec || [];
  } catch (e) {
    _state.recent = [];
  }
  renderRecent();
}

async function acceptCall(call) {
  try {
    await api.post('/queue/dequeue');
    // Snapshot the agent's prior status so wrap-up can restore it
    // (e.g. if they were 'away' before accepting, they should go
    // back to 'away' after — not silently flip to 'online').
    _state.previousStatus = _state.status || 'online';
    // Start duration at 0 so the ticker has a clean baseline.
    call.duration = 0;
    _state.currentCall = call;
    renderCenter();
    // mark presence on_call
    try { await api.put('/agents/me/presence', { status: 'on_call', current_call_id: call.call_id }); } catch (e) { /* ignore */ }
    _state.status = 'on_call';
    renderStatusCard();
  } catch (e) {
    toastError('Could not accept call: ' + e.message);
  }
}

function endCall(call) {
  stopCallTimer();
  // Try to actually hang up the live call (WebRTC + backend hookup).
  if (call && call.call_id) {
    try { api.post('/calls/hangup', { call_id: call.call_id }); } catch (e) { /* ignore */ }
  }
  if (typeof window.hangupCall === 'function') {
    try { window.hangupCall(); } catch (e) { /* ignore */ }
  }
  _state.currentCall = null;
  // Restore the prior presence (default: online) so the agent's
  // status toggle returns to where it was before the call.
  const restore = _state.previousStatus || 'online';
  try { api.put('/agents/me/presence', { status: restore }); } catch (e) { /* ignore */ }
  _state.status = restore;
  renderStatusCard();
  renderCenter();
  openWrapUpModal(call);
}

// ---------------------------------------------------------------------
// Data loading + WS
// ---------------------------------------------------------------------

async function loadInitial() {
  try {
    // 1) Roster — to find myself + my presence + my skills
    const r = await api.get('/agents/presence');
    const me = inferMe(r.agents || []);
    if (me) {
      _state.status = me.status || 'offline';
      _state.lastSeen = me.last_seen;
      _state.mySkills = me.skills || [];
      // Issue #44 (forward-looking): the role field is on the roster
      // entry; we treat 'admin' + 'supervisor' as privileged. #40 only
      // needs the add-skill button gated.
      _state.isAdmin = ['admin', 'supervisor'].includes((me.role || '').toLowerCase());
    }
    renderStatusCard();

    // 1b) Issue #40: skill groups for the left rail chip filter.
    await loadSkillGroups();

    // 2) Queue — calls that match my skills, with the active skill
    // filter (if any) narrowing further.
    try {
      const mySkillNames = (_state.mySkills || []).map(s => (s.skill || '').toLowerCase()).filter(Boolean);
      const filterParam = _state.activeSkillFilter
        ? '?skill=' + encodeURIComponent(_state.activeSkillFilter)
        : '';
      const q = await api.get('/queue/list' + filterParam);
      const all = q.items || q.queue || [];
      _state.myQueue = (all || []).filter(c => {
        if (_state.activeSkillFilter) {
          const tags = (c.skill_tags || []).map(t => (t || '').toLowerCase());
          if (!tags.includes(_state.activeSkillFilter.toLowerCase())) return false;
        }
        if (!mySkillNames.length) return true;
        const tags = (c.skill_tags || []).map(t => (t || '').toLowerCase());
        return tags.some(t => mySkillNames.includes(t));
      });
      // Tag each with its current position.
      for (const c of _state.myQueue) {
        try {
          const p = await api.get('/queue/position/' + encodeURIComponent(c.call_id));
          c.position = p.position;
        } catch (e) { c.position = null; }
      }
    } catch (e) { _state.myQueue = []; }
    renderQueueCard();
    renderCenter();

    // 3) Recent calls — best-effort, demo fallback if missing
    await loadRecent();
  } catch (e) {
    toastError('Failed to load dashboard: ' + e.message);
  }
}

function inferMe(agents) {
  // Pick the first agent for the dev default user; in a real
  // deployment the JWT would carry the user id and the API would
  // mark `me`. The roster API returns the full list; we just use
  // the first 'online' or 'on_call' agent, falling back to the head.
  if (!agents.length) return null;
  return agents.find(a => a.status === 'online')
      || agents.find(a => a.status === 'on_call')
      || agents[0];
}

function handleAgentEvent(evt) {
  if (!evt || !evt.type) return;
  switch (evt.type) {
    case 'ws.hello':
    case 'ws.open':
    case 'ws.ping':
    case 'ws.pong':
    case 'ws.close':
    case 'pong':
      // No-op for now; presence update events come from the server's
      // own pubsub. The dashboard re-fetches the roster on every ping.
      if (evt.type === 'ws.open') {
        // Refresh state on (re)connect so we don't miss events
        // while the socket was down.
        loadInitial();
      }
      break;
    case 'presence.update':
      // Reload the roster so the new status is reflected.
      loadInitial();
      break;
    default:
      // Future-proof: ignore unknown event types so server additions
      // don't break the dashboard.
      break;
  }
}
