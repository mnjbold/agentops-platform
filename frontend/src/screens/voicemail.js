/* =====================================================================
 * agentops/screens/voicemail.js
 * Voicemail inbox screen. List + transcript toggle + inline player.
 * ===================================================================== */

import { h, formatDate, formatDuration, debounce } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createAvatar } from '../ui/avatar.js';
import { createBadge } from '../ui/badge.js';
import { createButton } from '../ui/button.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError } from '../ui/toast.js';

let _state = { items: [], loading: true, error: null, filter: 'all' };

export async function mountVoicemailScreen(root) {
  root.innerHTML = '';
  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Voicemail'),
      h('p', { class: 'page-sub' }, 'Transcribed inbox with in-page playback')
    ),
    h('div', { class: 'page-actions' },
      createButton({ variant: 'ghost', size: 'sm', onClick: () => loadVoicemails(root), children: 'Refresh' })
    )
  ));

  const filters = h('div', { class: 'tabs-list', style: 'margin-bottom: var(--space-4); border-bottom: 1px solid var(--color-line);' });
  const filterOpts = [
    { id: 'all', label: 'All' },
    { id: 'unread', label: 'Unread' },
  ];
  let currentFilter = 'all';
  for (const f of filterOpts) {
    const b = h('button', { type: 'button', class: 'tabs-tab' + (f.id === currentFilter ? ' is-active' : ''), role: 'tab' }, f.label);
    b.addEventListener('click', () => {
      currentFilter = f.id;
      _state.filter = f.id;
      filters.querySelectorAll('.tabs-tab').forEach(t => t.classList.toggle('is-active', t.textContent === f.label));
      loadVoicemails(root);
    });
    filters.append(b);
  }
  root.append(filters);

  const list = h('div', { class: 'voicemail-list', 'aria-busy': 'true' });
  root.append(list);

  await loadVoicemails(root);
}

async function loadVoicemails(root) {
  const list = root.querySelector('.voicemail-list');
  if (!list) return;
  _state.loading = true;
  list.innerHTML = '';
  list.setAttribute('aria-busy', 'true');
  list.append(createSkeleton({ lines: 5, height: 56 }));

  try {
    const query = _state.filter === 'unread' ? '?unread=true' : '';
    const data = await api.get('/voicemails' + query);
    const items = Array.isArray(data) ? data : (data?.items || data?.voicemails || []);
    _state.items = items;
    _state.loading = false;
    _state.error = null;
    renderList(root, items);
  } catch (e) {
    _state.loading = false;
    _state.error = e.message;
    list.innerHTML = '';
    list.append(createEmptyState({
      icon: '!',
      title: 'Could not load voicemails',
      body: e.message,
      action: createButton({ variant: 'primary', size: 'sm', onClick: () => loadVoicemails(root), children: 'Retry' }),
    }));
  }
}

function renderList(root, items) {
  const list = root.querySelector('.voicemail-list');
  list.innerHTML = '';
  list.setAttribute('aria-busy', 'false');

  if (!items.length) {
    list.append(createEmptyState({
      icon: '✉',
      title: 'No voicemails yet',
      body: 'When calls go to voicemail, they show up here with transcripts.',
    }));
    return;
  }

  for (const v of items) {
    list.append(renderRow(root, v));
  }
}

function renderRow(root, v) {
  const transcriptId = `vm-tr-${v.id || Math.random().toString(36).slice(2)}`;
  const audioId = `vm-a-${v.id || Math.random().toString(36).slice(2)}`;
  const row = h('div', { class: 'vm-row' + (v.read_at ? '' : ' is-unread'), role: 'article' });
  row.append(createAvatar({ name: v.from_name || v.from_number || '?', size: 40 }));

  const meta = h('div', { class: 'vm-row-meta' });
  meta.append(h('div', { class: 'vm-row-name' }, v.from_name || v.from_number || 'Unknown'));
  meta.append(h('div', { class: 'vm-row-num mono' }, v.from_number || ''));
  const sub = h('div', { class: 'vm-row-time' });
  sub.append(h('span', {}, formatDate(v.created_at || v.received_at) || ''));
  if (v.duration) sub.append(h('span', { class: 'vm-row-duration', style: 'margin-left:8px;' }, formatDuration(v.duration)));
  if (v.read_at) sub.append(h('span', { style: 'margin-left:8px;' }, createBadge({ variant: 'neutral', size: 'sm', children: 'Read' })));
  meta.append(sub);
  row.append(meta);

  const playBtn = createButton({
    variant: 'ghost', size: 'sm', icon: '▶', ariaLabel: 'Play voicemail',
    onClick: async () => {
      try {
        const audio = document.getElementById(audioId);
        if (audio.paused) { await audio.play(); playBtn.setLoading(true); }
        else { audio.pause(); playBtn.setLoading(false); }
        audio.addEventListener('ended', () => playBtn.setLoading(false), { once: true });
        if (!v.read_at) markRead(v, row);
      } catch (e) { toastError('Could not play voicemail: ' + e.message); }
    }
  });
  row.append(playBtn);

  const transcriptBtn = createButton({
    variant: 'ghost', size: 'sm', ariaLabel: 'Toggle transcript',
    onClick: () => {
      const t = document.getElementById(transcriptId);
      t.classList.toggle('hidden');
    },
    children: 'Transcript'
  });
  row.append(transcriptBtn);

  const audioSrc = v.audio_url || v.recording_url || v.url;
  const audio = h('audio', { id: audioId, src: audioSrc, preload: 'none', style: 'display:none;' });
  row.append(audio);

  const transcript = h('div', { id: transcriptId, class: 'vm-row-transcript hidden', style: 'flex-basis:100%; padding-top:8px; font-size:13px; color:var(--color-fg-2); border-top:1px solid var(--color-line); margin-top:8px;' });
  transcript.textContent = v.transcript || 'No transcript available.';
  row.append(transcript);

  return row;
}

async function markRead(v, row) {
  try {
    if (!v.id) return;
    await api.patch(`/voicemails/${v.id}/read`);
    row.classList.remove('is-unread');
    v.read_at = new Date().toISOString();
  } catch (e) {
    // non-fatal; the user can manually mark later
    console.warn('mark-read failed', e);
  }
}
