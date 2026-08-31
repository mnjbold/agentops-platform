/* =====================================================================
 * agentops/screens/voice-lab.js
 * Voice Lab (issue #14). Pick a voice, type text, preview audio.
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createInput, createTextarea } from '../ui/input.js';
import { createButton } from '../ui/button.js';
import { createSelect } from '../ui/select.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toast, toastError, toastSuccess } from '../ui/toast.js';

let _state = { voices: [], voice: '', text: '' };

export async function mountVoiceLabScreen(root) {
  root.innerHTML = '';

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Voice Lab'),
      h('p', { class: 'page-sub' }, 'Preview Telnyx TTS voices before assigning them to an assistant.')
    )
  ));

  const card = h('div', { class: 'card' });
  const body = h('div', { class: 'card-body' });
  card.append(body);
  root.append(card);

  body.append(h('div', { class: 'skeleton-line', style: 'height: 24px; width: 60%; margin-bottom: var(--space-3);' }));
  body.append(createSkeleton({ lines: 4, height: 28 }));

  // Load voices
  try {
    const res = await api.get('/voice-lab/voices');
    _state.voices = res.voices || [];
    if (_state.voices.length && !_state.voice) {
      _state.voice = _state.voices[0].id;
      _state.text = 'Hello, this is a quick test of the ' + _state.voices[0].name + ' voice.';
    }
  } catch (e) {
    _state.voices = [];
  }

  body.innerHTML = '';

  const voiceSel = createSelect({
    label: 'Voice',
    value: _state.voice,
    options: _state.voices.map(v => ({ value: v.id, label: `${v.name} (${v.provider})` })),
    onChange: e => { _state.voice = e.target.value; },
  });
  body.append(voiceSel);

  const text = createTextarea({
    label: 'Sample text (max 500 chars)',
    value: _state.text,
  });
  text.textarea.rows = 4;
  text.textarea.maxLength = 500;
  text.textarea.addEventListener('input', e => { _state.text = e.target.value; });
  body.append(text);

  body.append(h('div', { style: 'display: flex; gap: var(--space-2); align-items: center;' },
    createButton({ variant: 'primary',
      onClick: () => preview(body),
      children: '▶ Preview' }),
    h('span', { id: 'vl-status', style: 'color: var(--color-fg-3); font-size: var(--text-sm);' })
  ));

  const audioRow = h('div', { id: 'vl-audio', style: 'margin-top: var(--space-3);' });
  body.append(audioRow);
}

async function preview(body) {
  const status = body.querySelector('#vl-status');
  const audioRow = body.querySelector('#vl-audio');
  if (!_state.voice) { toastError('Pick a voice first'); return; }
  if (!_state.text?.trim()) { toastError('Type some text first'); return; }
  if (status) status.textContent = 'Generating…';
  audioRow.innerHTML = '';
  try {
    const res = await api.post('/voice-lab/preview', { text: _state.text, voice: _state.voice });
    if (res.error) {
      status.textContent = 'Preview failed: ' + res.error;
      toastError(res.error);
      return;
    }
    if (res.audio_url) {
      const audio = h('audio', { controls: 'true', src: res.audio_url });
      audioRow.append(audio);
      audio.play().catch(() => {});
      status.textContent = 'Playing hosted audio';
    } else if (res.audio_base64) {
      const audio = h('audio', { controls: 'true',
        src: 'data:audio/mpeg;base64,' + res.audio_base64 });
      audioRow.append(audio);
      audio.play().catch(() => {});
      status.textContent = 'Playing inline audio';
    } else {
      status.textContent = res.note || 'No audio returned';
    }
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
    toastError('Preview failed: ' + e.message);
  }
}
