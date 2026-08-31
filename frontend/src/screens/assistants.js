/* =====================================================================
 * agentops/screens/assistants.js
 * AI Assistant Builder (issue #14). List + create + edit assistants.
 * ===================================================================== */

import { h, formatDate, debounce } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createInput, createTextarea } from '../ui/input.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createSelect } from '../ui/select.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toast, toastError, toastSuccess } from '../ui/toast.js';

let _state = { items: [], loading: true, availableTools: [], selected: null, editing: null };

export async function mountAssistantsScreen(root) {
  root.innerHTML = '';

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'AI Assistants'),
      h('p', { class: 'page-sub' }, 'Create, edit, and test Telnyx AI assistants.')
    ),
    h('div', { class: 'page-actions' },
      createButton({ variant: 'primary', size: 'sm',
        onClick: () => startCreate(root),
        children: '+ New assistant' }),
    )
  ));

  const list = h('div', { class: 'assistants-list', 'aria-busy': 'true' });
  root.append(list);

  await load(root);
}

async function load(root) {
  _state.loading = true;
  const list = root.querySelector('.assistants-list');
  list.innerHTML = '';
  list.append(createSkeleton({ lines: 4, height: 64 }));
  try {
    const res = await api.get('/assistants');
    _state.items = res.assistants || [];
    _state.availableTools = res.available_tools || [];
    _state.loading = false;
    renderList(root);
  } catch (e) {
    list.innerHTML = '';
    list.append(createEmptyState({
      icon: '!', title: 'Could not load assistants', body: e.message,
      action: createButton({ variant: 'primary', size: 'sm',
        onClick: () => load(root), children: 'Retry' }),
    }));
  }
}

function renderList(root) {
  const list = root.querySelector('.assistants-list');
  list.innerHTML = '';
  if (!_state.items.length) {
    list.append(createEmptyState({
      icon: '✦',
      title: 'No assistants yet',
      body: 'Click "+ New assistant" to create your first one.',
    }));
    return;
  }
  for (const a of _state.items) {
    const row = h('div', { class: 'card', style: 'margin-bottom: var(--space-3);' },
      h('div', { class: 'card-body' },
        h('div', { style: 'display: flex; gap: var(--space-3); align-items: flex-start;' },
          h('div', { style: 'flex: 1;' },
            h('div', { style: 'display: flex; align-items: center; gap: var(--space-2);' },
              h('h3', { style: 'margin: 0;' }, a.name),
              a.telnyx_id
                ? createBadge({ variant: 'success', children: 'live' })
                : createBadge({ variant: 'neutral', children: 'local' }),
              h('span', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' }, a.voice || '—'),
            ),
            h('p', { style: 'color: var(--color-fg-2); margin: 6px 0; font-size: var(--text-sm);' },
              (a.system_prompt || '(no system prompt)').slice(0, 180) + (a.system_prompt?.length > 180 ? '…' : '')),
            a.greeting
              ? h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm); font-style: italic; margin: 4px 0;' },
                  'Greeting: "' + a.greeting + '"')
              : null,
            h('div', { style: 'display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px;' },
              ...(a.tool_ids || []).map(t => createBadge({ variant: 'accent', children: t }))
            ),
          ),
          h('div', { style: 'display: flex; flex-direction: column; gap: 6px;' },
            createButton({ variant: 'primary', size: 'sm',
              onClick: () => startTest(root, a),
              children: 'Test call' }),
            createButton({ variant: 'secondary', size: 'sm',
              onClick: () => startEdit(root, a),
              children: 'Edit' }),
            createButton({ variant: 'danger', size: 'sm',
              onClick: () => deleteOne(root, a),
              children: 'Delete' }),
          )
        )
      )
    );
    list.append(row);
  }
}

function startCreate(root) {
  _state.editing = { name: '', system_prompt: '', voice: 'Telnyx.KokoroTTS.af_heart',
                       model: 'openai/gpt-4o', greeting: '', tool_ids: [] };
  showEditor(root);
}

function startEdit(root, a) {
  _state.editing = { ...a, tool_ids: [...(a.tool_ids || [])] };
  showEditor(root);
}

function startTest(root, a) {
  // Forward to the dedicated test screen.
  window.location.hash = '#/agent-test?id=' + a.id;
}

async function deleteOne(root, a) {
  if (!confirm('Delete assistant "' + a.name + '"?')) return;
  try {
    await api.del('/assistants/' + a.id);
    toastSuccess('Deleted');
    load(root);
  } catch (e) {
    toastError('Delete failed: ' + e.message);
  }
}

function showEditor(root) {
  const ed = _state.editing;
  const wrap = h('div', { class: 'card', style: 'margin-top: var(--space-4);' });
  wrap.append(h('div', { class: 'card-head' },
    h('h3', {}, ed.id ? 'Edit assistant' : 'New assistant')));
  const body = h('div', { class: 'card-body' });
  wrap.append(body);

  const name = createInput({ label: 'Name', value: ed.name || '' });
  name.input.addEventListener('input', e => ed.name = e.target.value);
  body.append(name);

  const sp = createTextarea({ label: 'System prompt', value: ed.system_prompt || '' });
  sp.textarea.rows = 5;
  sp.textarea.addEventListener('input', e => ed.system_prompt = e.target.value);
  body.append(sp);

  const voice = createSelect({
    label: 'Voice',
    value: ed.voice,
    options: [
      'Telnyx.KokoroTTS.af_heart', 'Telnyx.KokoroTTS.am_adam',
      'Telnyx.KokoroTTS.bf_emma', 'AWS.Polly.Joanna', 'AWS.Polly.Matthew',
      'Azure.en-US-JennyNeural', 'Azure.en-US-GuyNeural',
    ].map(v => ({ value: v, label: v })),
    onChange: e => ed.voice = e.target.value,
  });
  body.append(voice);

  const greet = createInput({ label: 'Greeting (optional)', value: ed.greeting || '' });
  greet.input.addEventListener('input', e => ed.greeting = e.target.value);
  body.append(greet);

  body.append(h('div', { style: 'margin-top: var(--space-3); font-size: var(--text-sm); color: var(--color-fg-2);' },
    'Tools'));
  for (const t of (_state.availableTools || [])) {
    const row = h('label', { style: 'display: flex; gap: 8px; align-items: center; padding: 4px 0;' },
      h('input', { type: 'checkbox',
        checked: (ed.tool_ids || []).includes(t.id) || undefined,
        onChange: (e) => {
          ed.tool_ids = ed.tool_ids || [];
          if (e.target.checked) {
            if (!ed.tool_ids.includes(t.id)) ed.tool_ids.push(t.id);
          } else {
            ed.tool_ids = ed.tool_ids.filter(x => x !== t.id);
          }
        }
      }),
      h('span', {}, t.label),
      h('span', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' }, '— ' + (t.description || '')),
    );
    body.append(row);
  }

  body.append(h('div', { style: 'display: flex; gap: var(--space-2); margin-top: var(--space-3);' },
    createButton({
      variant: 'primary',
      onClick: () => saveEditor(root, wrap),
      children: ed.id ? 'Save' : 'Create',
    }),
    createButton({
      variant: 'ghost',
      onClick: () => { _state.editing = null; wrap.remove(); },
      children: 'Cancel',
    })
  ));

  root.append(wrap);
  wrap.scrollIntoView({ behavior: 'smooth' });
}

async function saveEditor(root, wrap) {
  const ed = _state.editing;
  if (!ed.name?.trim()) { toastError('Name is required'); return; }
  try {
    if (ed.id) {
      await api.patch('/assistants/' + ed.id, ed);
      toastSuccess('Assistant updated');
    } else {
      await api.post('/assistants', ed);
      toastSuccess('Assistant created');
    }
    _state.editing = null;
    wrap.remove();
    load(root);
  } catch (e) {
    toastError('Save failed: ' + (e?.data?.detail || e.message));
  }
}
