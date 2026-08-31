/* =====================================================================
 * agentops/screens/supervisor.js
 * Phase E-B #44 — Supervisor team dashboard.
 *
 * Layout
 * ------
 *   - Top:    Team grid — one card per agent. Avatar, name, status
 *             dot, current call (if any) with caller + duration.
 *             Click a card → side panel with Monitor / Whisper /
 *             Barge choices.
 *   - Bottom: Queue snapshot — bar chart of waiting calls per skill.
 *
 * Only ``role=supervisor|admin`` users see this sidebar item; the API
 * enforces the same gate server-side.
 *
 * Issue #41-#43 contract: the dashboard's "Monitor" / "Whisper" /
 * "Barge" buttons call the supervisor session POST endpoints and
 * surface the call's participants list to confirm the session
 * started. The actual Telnyx audio plumbing ships in v1.1; v1 ships
 * the storage + UI contract.
 * ===================================================================== */

import { h, formatDate, formatDuration } from '../lib/dom.js';
import { api, subscribeAgentEvents } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createAvatar } from '../ui/avatar.js';
import { createEmptyState } from '../ui/empty-state.js';
import { createModal } from '../ui/modal.js';
import { toastError, toastInfo, toastSuccess } from '../ui/toast.js';

let _state = {
  roster: [],          // [{user_id, display_name, status, current_call_id, skills}, ...]
  skills: [],          // [{id, name, online_agents_with_skill, ...}, ...]
  queueCounts: {},     // {skill: waiting_count}
  totalWaiting: 0,
  selectedAgent: null, // the agent the user clicked; opens the side panel
  unsubscribe: null,
  pollTimer: null,
};

const PRESENCE_DOT = {
  online: 'success', away: 'warning', busy: 'warning',
  on_call: 'accent', offline: 'neutral',
};

export function mountSupervisorScreen(root) {
  root.innerHTML = '';
  const head = h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Supervisor'),
      h('p', { class: 'page-sub' }, 'Real-time view of your team. Listen, coach, or join any call.'),
    ),
    h('div', { class: 'page-actions' },
      createBadge({ variant: 'success', dot: true, children: 'Live' }),
    ),
  );
  root.append(head);

  // Two-row layout: team grid on top, queue snapshot on bottom
  const wrap = h('div', { class: 'supervisor-wrap' });

  const top = h('section', { class: 'card' },
    h('div', { class: 'card-head' },
      h('h3', { style: 'margin: 0;' }, 'Team'),
    ),
    h('div', { class: 'card-body', id: 'sup-team-grid',
               style: 'display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: var(--space-3);' },
      h('div', { style: 'color: var(--color-fg-3);' }, 'Loading…'),
    ),
  );

  const bottom = h('section', { class: 'card', style: 'margin-top: var(--space-4);' },
    h('div', { class: 'card-head' },
      h('h3', { style: 'margin: 0;' }, 'Queue snapshot'),
    ),
    h('div', { class: 'card-body', id: 'sup-queue-snap' },
      h('div', { style: 'color: var(--color-fg-3);' }, 'Loading…'),
    ),
  );

  wrap.append(top, bottom);
  root.append(wrap);

  // Side panel for monitor / whisper / barge choices
  const side = h('aside', { class: 'supervisor-side', id: 'sup-side-panel',
                            style: 'position: fixed; top: 80px; right: 24px; width: 360px; max-width: 95vw; ' +
                                   'background: var(--color-bg-1); border: 1px solid var(--color-line); ' +
                                   'border-radius: 12px; padding: var(--space-4); box-shadow: 0 8px 24px rgba(0,0,0,0.2); ' +
                                   'display: none; z-index: 50;' });
  root.append(side);

  loadInitial();
  if (_state.unsubscribe) { try { _state.unsubscribe(); } catch (e) {} }
  _state.unsubscribe = subscribeAgentEvents(handleAgentEvent);

  // Refresh the queue counts every 5s — cheaper than subscribing to
  // every call event and good enough for a team dashboard view.
  if (_state.pollTimer) clearInterval(_state.pollTimer);
  _state.pollTimer = setInterval(refreshQueue, 5000);
}

async function loadInitial() {
  try {
    await Promise.all([loadRoster(), loadSkills(), refreshQueue()]);
  } catch (e) {
    toastError('Failed to load supervisor dashboard: ' + e.message);
  }
}

async function loadRoster() {
  try {
    const r = await api.get('/agents/presence');
    _state.roster = r.agents || [];
    renderTeamGrid();
  } catch (e) { _state.roster = []; }
}

async function loadSkills() {
  try {
    const r = await api.get('/skills');
    _state.skills = r.items || [];
  } catch (e) { _state.skills = []; }
}

async function refreshQueue() {
  try {
    const r = await api.get('/queue/stats');
    _state.totalWaiting = r.waiting || 0;
    // Per-skill counts: fetch each group the operator has configured
    const counts = {};
    for (const sk of (_state.skills || [])) {
      try {
        const s = await api.get('/queue/stats?skill=' + encodeURIComponent(sk.name));
        counts[sk.name] = s.waiting || 0;
      } catch (e) { counts[sk.name] = 0; }
    }
    _state.queueCounts = counts;
    renderQueueSnapshot();
  } catch (e) { /* keep the last value */ }
}

function renderTeamGrid() {
  const grid = document.getElementById('sup-team-grid');
  if (!grid) return;
  grid.innerHTML = '';
  if (!_state.roster.length) {
    grid.append(createEmptyState({ title: 'No agents in this tenant', body: 'Invite an agent to start monitoring.' }));
    return;
  }
  for (const a of _state.roster) {
    grid.append(buildAgentCard(a));
  }
}

function buildAgentCard(agent) {
  const dotVariant = PRESENCE_DOT[agent.status] || 'neutral';
  const onCall = agent.status === 'on_call' && agent.current_call_id;
  const skillNames = (agent.skills || []).map(s => s.skill).filter(Boolean);
  const card = h('div', {
    class: 'agent-card',
    style: 'border: 1px solid var(--color-line); border-radius: 8px; padding: var(--space-3); ' +
           'cursor: pointer; transition: background 120ms;',
    onclick: () => openSidePanel(agent),
  });
  card.addEventListener('mouseenter', () => { card.style.background = 'var(--color-bg-2)'; });
  card.addEventListener('mouseleave', () => { card.style.background = 'transparent'; });
  card.append(
    h('div', { style: 'display:flex; gap: var(--space-3); align-items: center;' },
      createAvatar({ name: agent.display_name || agent.email, size: 48, status: agent.status }),
      h('div', { style: 'flex:1; min-width: 0;' },
        h('div', { style: 'display:flex; align-items: center; gap: 6px;' },
          h('strong', { style: 'overflow: hidden; text-overflow: ellipsis; white-space: nowrap;' },
            agent.display_name || agent.email),
        ),
        h('div', { style: 'display:flex; gap: 6px; margin-top: 4px; flex-wrap: wrap;' },
          createBadge({ variant: dotVariant, size: 'sm', dot: true, children: agent.status || 'offline' }),
          agent.role ? createBadge({ variant: 'neutral', size: 'sm', children: agent.role }) : null,
        ),
      ),
    ),
  );
  // Current-call row
  if (onCall) {
    card.append(h('div', { style: 'margin-top: var(--space-3); padding-top: var(--space-3); border-top: 1px dashed var(--color-line); font-size: var(--text-sm);' },
      h('div', { style: 'display:flex; justify-content: space-between; align-items: center;' },
        h('span', { style: 'color: var(--color-fg-3);' }, 'On call'),
        createButton({
          size: 'sm', variant: 'primary', children: '👂 Monitor',
          onClick: (e) => { e.stopPropagation(); quickStart(agent, 'monitor'); },
        }),
      ),
      h('div', { class: 'mono', style: 'margin-top: 4px; color: var(--color-fg-2);' },
        agent.current_call_id),
    ));
  } else {
    card.append(h('div', { style: 'margin-top: var(--space-3); font-size: var(--text-sm); color: var(--color-fg-3);' },
      'Available' + (skillNames.length ? ' · ' + skillNames.join(', ') : '')));
  }
  return card;
}

function openSidePanel(agent) {
  const side = document.getElementById('sup-side-panel');
  if (!side) return;
  _state.selectedAgent = agent;
  side.style.display = 'block';
  side.innerHTML = '';
  const header = h('div', { style: 'display:flex; align-items: center; justify-content: space-between;' },
    h('div', {},
      h('h3', { style: 'margin: 0;' }, agent.display_name || agent.email),
      h('p', { style: 'margin: 4px 0 0; color: var(--color-fg-3); font-size: var(--text-sm);' },
        'Pick a mode to join this agent’s call.'),
    ),
    createButton({ variant: 'ghost', size: 'sm', children: '✕',
                   onClick: () => { side.style.display = 'none'; _state.selectedAgent = null; } }),
  );
  side.append(header);
  if (agent.status !== 'on_call' || !agent.current_call_id) {
    side.append(h('p', { style: 'margin: var(--space-3) 0 0; color: var(--color-fg-3);' },
      'This agent is not on a call right now. There’s nothing to listen to yet.'));
    return;
  }
  // The agent IS on a call — show the three actions
  const callId = agent.current_call_id;
  const modeRow = h('div', { style: 'display:flex; flex-direction: column; gap: 6px; margin-top: var(--space-3);' },
    h('div', { style: 'display:flex; gap: 6px;' },
      createButton({
        variant: 'secondary', size: 'md', children: '👂 Monitor',
        onClick: () => startMode(callId, agent, 'monitor'),
      }),
      createButton({
        variant: 'primary', size: 'md', children: '🎧 Whisper',
        onClick: () => startMode(callId, agent, 'whisper'),
      }),
      createButton({
        variant: 'danger', size: 'md', children: '🎙 Barge',
        onClick: () => startMode(callId, agent, 'barge'),
      }),
    ),
    h('p', { style: 'margin: var(--space-2) 0 0; color: var(--color-fg-3); font-size: var(--text-xs);' },
      'Monitor: silent. Whisper: agent can hear you. Barge: 3-way.'),
  );
  side.append(modeRow);

  // Active sessions (if any) + end buttons
  refreshCallPanel(callId);
}

async function refreshCallPanel(callId) {
  const side = document.getElementById('sup-side-panel');
  if (!side) return;
  let list = side.querySelector('#sup-active-sessions');
  if (!list) {
    list = h('div', { id: 'sup-active-sessions', style: 'margin-top: var(--space-3); border-top: 1px solid var(--color-line); padding-top: var(--space-3);' });
    side.append(list);
  }
  list.innerHTML = '';
  try {
    const res = await api.get('/calls/' + encodeURIComponent(callId) + '/supervisor');
    const open = (res.sessions || []).filter(s => !s.left_at);
    if (!open.length) {
      list.append(h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm); margin: 0;' },
        'No active supervisor sessions on this call.'));
      return;
    }
    for (const s of open) {
      const row = h('div', { style: 'display:flex; align-items: center; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed var(--color-line);' },
        h('span', {},
          createBadge({ variant: s.mode === 'barge' ? 'danger' : s.mode === 'whisper' ? 'accent' : 'info',
                        size: 'sm', children: s.mode }),
          ' ' + (s.supervisor_user_id || ''),
        ),
        createButton({ size: 'sm', variant: 'ghost', children: 'End',
                       onClick: async () => {
                         try {
                           await api.post('/supervisor/sessions/' + s.id + '/end');
                           toastSuccess('Session ended');
                           await refreshCallPanel(callId);
                         } catch (e) { toastError('End failed: ' + e.message); }
                       } }),
      );
      list.append(row);
    }
  } catch (e) {
    list.append(h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm); margin: 0;' },
      'Could not load active sessions.'));
  }
}

function quickStart(agent, mode) {
  if (!agent || !agent.current_call_id) {
    toastInfo('This agent is not on a call right now.');
    return;
  }
  startMode(agent.current_call_id, agent, mode);
}

async function startMode(callId, agent, mode) {
  // The current user is the supervisor. We send the JWT-derived user
  // id; the backend resolves it from the token if absent.
  try {
    const res = await api.post(
      '/calls/' + encodeURIComponent(callId) + '/supervisor/' + mode,
      { supervisor_user_id: currentUserId() },
    );
    toastSuccess(mode + ' session started for ' + (agent.display_name || agent.email));
    if (res && res.audio && res.audio.audio_routed === false) {
      toastInfo('Storage recorded. Audio routing is a v1.1 (provider-specific).');
    }
    await refreshCallPanel(callId);
  } catch (e) {
    toastError('Could not start ' + mode + ': ' + e.message);
  }
}

function currentUserId() {
  // Pull the JWT-resolved user id from the auth store so the backend
  // records the right supervisor on the session. The /api/auth/me
  // helper already populates this on login.
  try {
    const tok = (window.agentops && window.agentops.tokenStore
                 && window.agentops.tokenStore.get()
                 && window.agentops.tokenStore.get().user) || null;
    if (tok && tok.id) return tok.id;
  } catch (e) { /* ignore */ }
  // Fall back to the first user in the tenant — fine for the dev tenant.
  return (_state.roster[0] || {}).user_id || '';
}

function renderQueueSnapshot() {
  const root = document.getElementById('sup-queue-snap');
  if (!root) return;
  root.innerHTML = '';
  const skills = _state.skills || [];
  const counts = _state.queueCounts || {};
  const total = _state.totalWaiting || 0;
  // If the operator hasn't created any skill groups, show the total
  // alone so the card isn't empty.
  if (!skills.length) {
    root.append(h('div', { style: 'display:flex; align-items: baseline; gap: var(--space-3);' },
      h('div', { style: 'font-size: 2.5rem; font-weight: 700;' }, String(total)),
      h('div', { style: 'color: var(--color-fg-3);' }, 'calls waiting across all skills'),
    ));
    return;
  }
  // Bar chart: each skill is a row with name + count + a bar that
  // scales to the busiest skill. Pure CSS — no chart library.
  const max = Math.max(1, ...Object.values(counts));
  const list = h('div', { style: 'display:flex; flex-direction: column; gap: 6px;' });
  for (const sk of skills) {
    const c = counts[sk.name] || 0;
    const pct = Math.max(2, Math.round((c / max) * 100));
    const row = h('div', { style: 'display:grid; grid-template-columns: 140px 1fr 40px; gap: 8px; align-items: center;' },
      h('div', { style: 'overflow: hidden; text-overflow: ellipsis; white-space: nowrap;' },
        sk.name + (sk.fallback_user_id ? ' *' : '')),
      h('div', { style: 'background: var(--color-bg-2); border-radius: 6px; height: 16px; position: relative; overflow: hidden;' },
        h('div', { style: 'background: linear-gradient(90deg, var(--color-accent), var(--color-accent-2, #4af)); ' +
                          'height: 100%; width: ' + pct + '%; transition: width 240ms; border-radius: 6px;' }),
      ),
      h('div', { class: 'mono', style: 'text-align: right;' }, String(c)),
    );
    list.append(row);
  }
  root.append(
    h('div', { style: 'display:flex; justify-content: space-between; margin-bottom: var(--space-3);' },
      h('div', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' },
        total + ' calls waiting' + (skills.length ? ' across ' + skills.length + ' skills' : '')),
      h('div', { style: 'color: var(--color-fg-3); font-size: var(--text-xs);' }, '* = has fallback'),
    ),
    list,
  );
}

function handleAgentEvent(evt) {
  if (!evt || !evt.type) return;
  if (evt.type === 'ws.open' || evt.type === 'presence.update') {
    // Refresh the roster so new presences / skill changes are picked up.
    loadRoster();
  }
}
