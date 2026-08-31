/* =====================================================================
 * agentops/screens/agent-test.js
 * Agent Test Call panel (issue #14). Mini-dialer + live transcript.
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createInput } from '../ui/input.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toast, toastError, toastSuccess } from '../ui/toast.js';

let _state = { assistant: null, callId: null, roomId: null, token: null, log: [] };

function _getQueryId() {
  const h = window.location.hash.split('?')[1] || '';
  const params = new URLSearchParams(h);
  return params.get('id');
}

export async function mountAgentTestScreen(root) {
  root.innerHTML = '';

  const id = _getQueryId();

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Test assistant'),
      h('p', { class: 'page-sub' }, 'Start a test call and watch the live transcript.')
    )
  ));

  const layout = h('div', { class: 'agent-test-layout' });

  // Left: assistant picker + dialer
  const left = h('div', { class: 'card' });
  const lb = h('div', { class: 'card-body' });
  left.append(lb);
  layout.append(left);

  // Right: transcript
  const right = h('div', { class: 'card' });
  const rb = h('div', { class: 'card-body' });
  right.append(rb);
  layout.append(right);

  root.append(layout);

  lb.append(createSkeleton({ lines: 4, height: 28 }));
  rb.append(createSkeleton({ lines: 6, height: 22 }));

  // Load assistants
  try {
    const res = await api.get('/assistants');
    const all = res.assistants || [];
    if (id) {
      _state.assistant = all.find(a => a.id === id) || all[0];
    } else {
      _state.assistant = all[0];
    }
  } catch (e) {
    lb.innerHTML = '';
    lb.append(createEmptyState({ icon: '!', title: 'Could not load assistants', body: e.message }));
    rb.innerHTML = '';
    return;
  }

  lb.innerHTML = '';
  if (!_state.assistant) {
    lb.append(createEmptyState({ icon: '✦', title: 'No assistants', body: 'Create one first.' }));
    rb.innerHTML = '';
    return;
  }

  lb.append(h('div', { style: 'display: flex; align-items: center; gap: 8px;' },
    h('strong', {}, _state.assistant.name),
    _state.assistant.telnyx_id
      ? createBadge({ variant: 'success', children: 'live' })
      : createBadge({ variant: 'neutral', children: 'local' }),
  ));
  lb.append(h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' },
    _state.assistant.voice || '—'));

  const phone = createInput({ label: 'Test from (E.164)', value: '+15555550100' });
  lb.append(phone);

  const callBtn = createButton({
    variant: 'primary', size: 'lg',
    onClick: () => startCall(lb, rb, phone.input.value),
    children: '☎ Start test call',
  });
  lb.append(h('div', { style: 'margin-top: var(--space-3);' }, callBtn));

  const endBtn = createButton({
    variant: 'danger',
    onClick: () => endCall(rb),
    children: 'End call',
  });
  endBtn.style.display = 'none';
  endBtn.id = 'agent-test-end-btn';
  lb.append(endBtn);

  const note = h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm); margin-top: var(--space-3);' },
    'Live transcripts appear on the right. Use the form below to inject a user turn and watch the assistant respond.');
  lb.append(note);

  // Inject a line
  const inject = h('div', { style: 'display: flex; gap: 8px; margin-top: var(--space-3);' });
  const userInput = createInput({ placeholder: 'Type something the user would say…' });
  const userBtn = createButton({
    variant: 'secondary',
    onClick: () => injectUser(userInput.input.value, rb),
    children: 'Send as user',
  });
  inject.append(userInput, userBtn);
  lb.append(inject);

  // Right: transcript
  rb.innerHTML = '';
  rb.append(h('h3', { style: 'margin-top:0;' }, 'Live transcript'));
  const list = h('div', { id: 'agent-test-log',
    style: 'max-height: 60vh; overflow: auto; padding: var(--space-2); border: 1px solid var(--color-line); border-radius: 6px; background: var(--color-bg-2);' });
  rb.append(list);
}

async function startCall(lb, rb, fromNumber) {
  if (!_state.assistant) return;
  try {
    const res = await api.post('/assistants/' + _state.assistant.id + '/test-call', {});
    _state.callId = res.call_id;
    _state.roomId = res.room_id;
    _state.token = res.token;
    if (res.stub) {
      toast('Test mode: stub room (no real WebRTC); inject messages below to demo.');
    } else {
      toastSuccess('Test call started');
    }
    const endBtn = lb.querySelector('#agent-test-end-btn');
    if (endBtn) endBtn.style.display = '';
    // Reload the log
    await reloadLog(rb);
  } catch (e) {
    toastError('Could not start test: ' + e.message);
  }
}

async function endCall(rb) {
  _state.callId = null;
  toastSuccess('Call ended');
  await reloadLog(rb);
}

async function injectUser(text, rb) {
  if (!_state.assistant) return;
  if (!_state.callId) { toast('Start a test call first'); return; }
  if (!text?.trim()) return;
  try {
    await api.post('/assistants/' + _state.assistant.id + '/test-call/log',
      { call_id: _state.callId, role: 'user', content: text });
    // Simulate the assistant response so the demo UI is interactive
    // even without a real WebRTC session.
    await api.post('/assistants/' + _state.assistant.id + '/test-call/log',
      { call_id: _state.callId, role: 'assistant', content: '(demo) acknowledged: ' + text });
    await reloadLog(rb);
  } catch (e) {
    toastError('Inject failed: ' + e.message);
  }
}

async function reloadLog(rb) {
  if (!_state.assistant) return;
  try {
    const res = await api.get('/assistants/' + _state.assistant.id + '/call-log?limit=200');
    const list = rb.querySelector('#agent-test-log');
    if (!list) return;
    list.innerHTML = '';
    const rows = res.log || [];
    if (!rows.length) {
      list.append(h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' },
        'No transcript yet. Send a user turn to see the assistant reply.'));
      return;
    }
    for (const r of rows) {
      const isUser = r.role === 'user';
      const isAssistant = r.role === 'assistant';
      const isTool = r.role === 'tool';
      const isSystem = r.role === 'system';
      const bubble = h('div', {
        style: `margin-bottom: 8px; padding: 6px 10px; border-radius: 8px; max-width: 80%;
          ${isUser ? 'background: var(--color-accent); color: white; margin-left: auto;' :
            isAssistant ? 'background: var(--color-bg-1); color: var(--color-fg-0);' :
            isTool ? 'background: var(--color-warning); color: black;' :
            isSystem ? 'background: transparent; color: var(--color-fg-3); font-style: italic;' :
            'background: var(--color-bg-1);'}`,
      },
        h('div', { style: 'font-size: 11px; opacity: 0.7;' }, r.role),
        h('div', {}, r.content || (r.tool_name ? `tool: ${r.tool_name}` : '(no content)')),
      );
      list.append(bubble);
    }
    list.scrollTop = list.scrollHeight;
  } catch (e) {
    // Non-fatal: live transcript just shows whatever is in the buffer.
  }
}
