/* =====================================================================
 * agentops/screens/recordings.js
 * Recordings screen. Search + waveform + download.
 * ===================================================================== */

import { h, formatDate, formatDuration, debounce } from '../lib/dom.js';
import { api, baseUrl } from '../lib/api.js';
import { createInput } from '../ui/input.js';
import { createButton } from '../ui/button.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError } from '../ui/toast.js';

let _state = { items: [], loading: true, error: null, q: '', from: '', to: '' };

export async function mountRecordingsScreen(root) {
  root.innerHTML = '';

  const head = h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Recordings'),
      h('p', { class: 'page-sub' }, 'Search, play, and download your call recordings')
    ),
    h('div', { class: 'page-actions' })
  );
  root.append(head);

  // Filters bar
  const filters = h('div', { class: 'card', style: 'padding: var(--space-3) var(--space-4); margin-bottom: var(--space-4);' });
  const search = createInput({
    placeholder: 'Search by number, agent, or transcript…',
    type: 'search',
    onInput: debounce((e) => { _state.q = e.target.value; load(root); }, 250),
  });
  search.input.classList.add('has-prefix');
  // Wrap input with a search icon prefix via a span overlay
  const searchWrap = h('div', { style: 'position:relative;' }, search,
    h('span', { style: 'position:absolute; left:12px; top:50%; transform:translateY(-50%); color:var(--color-fg-3); pointer-events:none;', 'aria-hidden': 'true', html: '⌕' })
  );
  // Actually position the prefix correctly: we need the input to have left padding. Easier: prepend the icon inside .field-control.
  const inner = search.querySelector('.field-control');
  if (inner) {
    const ico = h('span', { style: 'position:absolute; left:10px; top:50%; transform:translateY(-50%); color:var(--color-fg-3); pointer-events:none;', 'aria-hidden': 'true' }, '⌕');
    inner.append(ico);
    search.input.style.paddingLeft = '32px';
  }
  const fromInput = createInput({ type: 'date', placeholder: 'From', onChange: (e) => { _state.from = e.target.value; load(root); } });
  const toInput = createInput({ type: 'date', placeholder: 'To', onChange: (e) => { _state.to = e.target.value; load(root); } });
  const filterRow = h('div', { style: 'display:grid; grid-template-columns: 1fr 160px 160px; gap: var(--space-3);' },
    searchWrap, fromInput, toInput
  );
  filters.append(filterRow);
  root.append(filters);

  const list = h('div', { class: 'recordings-list', 'aria-busy': 'true' });
  root.append(list);

  await load(root);
}

async function load(root) {
  const list = root.querySelector('.recordings-list');
  if (!list) return;
  _state.loading = true;
  list.innerHTML = '';
  list.setAttribute('aria-busy', 'true');
  for (let i = 0; i < 4; i++) list.append(createSkeleton({ lines: 1, height: 56, width: '100%' }));

  try {
    const params = new URLSearchParams();
    if (_state.q) params.set('q', _state.q);
    if (_state.from) params.set('from', _state.from);
    if (_state.to) params.set('to', _state.to);
    const data = await api.get('/recordings' + (params.toString() ? '?' + params : ''));
    const items = Array.isArray(data) ? data : (data?.items || data?.recordings || []);
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
      title: 'Could not load recordings',
      body: e.message,
      action: createButton({ variant: 'primary', size: 'sm', onClick: () => load(root), children: 'Retry' }),
    }));
  }
}

function renderList(root, items) {
  const list = root.querySelector('.recordings-list');
  list.innerHTML = '';
  list.setAttribute('aria-busy', 'false');

  if (!items.length) {
    list.append(createEmptyState({
      icon: '♪',
      title: 'No recordings yet',
      body: "Make your first call and it'll show up here with a searchable transcript.",
    }));
    return;
  }

  for (const r of items) list.append(renderRow(r));
}

function renderRow(r) {
  const audioId = `rec-a-${r.id || Math.random().toString(36).slice(2)}`;
  const row = h('div', { class: 'rec-row', role: 'article' });
  const meta = h('div', { class: 'rec-row-meta' });
  meta.append(h('div', { class: 'rec-row-name' },
    `${r.from_number || 'Unknown'} → ${r.to_number || ''}`
  ));
  meta.append(h('div', { class: 'rec-row-num mono' }, r.transcript?.slice(0, 140) || '— no transcript —'));
  const sub = h('div', { class: 'rec-row-time' });
  sub.append(h('span', {}, formatDate(r.created_at || r.started_at) || ''));
  if (r.duration) sub.append(h('span', { class: 'rec-row-duration', style: 'margin-left:8px;' }, formatDuration(r.duration)));
  meta.append(sub);
  row.append(meta);

  // Mini waveform (deterministic per id)
  const wf = h('div', { class: 'waveform', 'aria-hidden': 'true' });
  const seed = (r.id || '').toString().split('').reduce((a, c) => a + c.charCodeAt(0), 0);
  for (let i = 0; i < 24; i++) {
    const h2 = 20 + ((Math.sin((seed + i) * 0.7) + 1) * 50);
    wf.append(h('span', { class: 'waveform-bar', style: `height:${h2}%;` }));
  }
  row.append(wf);

  const playBtn = createButton({
    variant: 'ghost', size: 'sm', icon: '▶', ariaLabel: 'Play recording',
    onClick: async () => {
      try {
        const audio = document.getElementById(audioId);
        if (audio.paused) { await audio.play(); playBtn.setLoading(true); }
        else { audio.pause(); playBtn.setLoading(false); }
        audio.addEventListener('ended', () => playBtn.setLoading(false), { once: true });
      } catch (e) { toastError('Could not play: ' + e.message); }
    }
  });
  row.append(playBtn);

  const dl = createButton({
    variant: 'ghost', size: 'sm', icon: '⬇', ariaLabel: 'Download MP3',
    onClick: () => {
      const url = (r.download_url || (baseUrl() + '/api/recordings/' + r.id + '/download?format=mp3'));
      const a = document.createElement('a'); a.href = url; a.download = (r.id || 'recording') + '.mp3'; a.click();
    }
  });
  row.append(dl);

  const audioSrc = r.audio_url || r.recording_url || r.url;
  row.append(h('audio', { id: audioId, src: audioSrc, preload: 'none', style: 'display:none;' }));
  return row;
}
