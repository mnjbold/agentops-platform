/* =====================================================================
 * agentops/screens/dialer.js
 * Dialer screen — the headline screen. Number pad + DTMF + active call.
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createAvatar } from '../ui/avatar.js';
import { createBadge } from '../ui/badge.js';
import { toastError, toastSuccess } from '../ui/toast.js';

// Public surface for the dialer. Backed by the existing v5.0 in index.html
// until the full WebRTC integration is moved over. This module renders a
// dialer UI shell that reuses the global `placeCall()` / `hangupCall()` from
// the existing app and gives it the new design system look.

export function mountDialerScreen(root) {
  root.innerHTML = '';

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Softphone'),
      h('p', { class: 'page-sub' }, 'Make and receive calls from your browser')
    ),
    h('div', { class: 'page-actions' },
      h('span', { class: 'badge badge-success', id: 'ds-status' }, 'Ready')
    )
  ));

  const wrap = h('div', { class: 'dialer-wrap' });
  const stage = h('div', { class: 'dialer-stage' });
  const side = h('div', { class: 'card', style: 'padding: var(--space-4);' });

  // Display + pad
  const display = h('div', { class: 'dialer-display' });
  const numEl = h('div', { class: 'dialer-num', id: 'ds-num' }, '');
  const from = h('div', { class: 'dialer-from' },
    h('span', {}, 'From'),
    h('select', { id: 'ds-from', style: 'background:transparent; border:0; color:inherit; font:inherit;' })
  );
  // Populate from-numbers lazily
  api.get('/numbers').then((d) => {
    const items = Array.isArray(d) ? d : (d?.items || []);
    const sel = from.querySelector('select');
    sel.innerHTML = '';
    if (!items.length) sel.append(h('option', { value: '+15078731084' }, '+1 507 873 1084 — Work'));
    for (const n of items) {
      const v = n.number || n.phone_number;
      sel.append(h('option', { value: v }, `${v} — ${n.label || n.friendly_name || ''}`));
    }
  }).catch(() => {});

  display.append(numEl, from);

  // Keypad
  const pad = h('div', { class: 'keypad', id: 'ds-keypad', role: 'group', 'aria-label': 'Number pad' });
  const keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#'];
  const sublabels = { '2': 'ABC', '3': 'DEF', '4': 'GHI', '5': 'JKL', '6': 'MNO', '7': 'PQRS', '8': 'TUV', '9': 'WXYZ', '0': '+' };
  for (const k of keys) {
    const btn = h('button', { type: 'button', class: 'key', 'aria-label': k, 'data-digit': k });
    btn.append(h('span', { class: 'digit' }, k));
    if (sublabels[k]) btn.append(h('span', { class: 'sub' }, sublabels[k]));
    btn.addEventListener('click', () => pressKey(k, btn));
    pad.append(btn);
  }
  function pressKey(k, btn) {
    if (navigator.vibrate) try { navigator.vibrate(10); } catch (e) {}
    playDTMF(k);
    btn.classList.add('is-pressed');
    setTimeout(() => btn.classList.remove('is-pressed'), 120);
    const cur = numEl.textContent;
    numEl.textContent = (cur || '') + k;
  }
  function playDTMF(k) {
    try {
      const A = window.AudioContext || window.webkitAudioContext;
      if (!A) return;
      const ctx = playDTMF._ctx || (playDTMF._ctx = new A());
      const freqs = {
        '1': [697, 1209], '2': [697, 1336], '3': [697, 1477],
        '4': [770, 1209], '5': [770, 1336], '6': [770, 1477],
        '7': [852, 1209], '8': [852, 1336], '9': [852, 1477],
        '*': [941, 1209], '0': [941, 1336], '#': [941, 1477],
      }[k];
      if (!freqs) return;
      const now = ctx.currentTime;
      for (const f of freqs) {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = f;
        o.type = 'sine';
        g.gain.setValueAtTime(0.0001, now);
        g.gain.exponentialRampToValueAtTime(0.15, now + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, now + 0.12);
        o.connect(g).connect(ctx.destination);
        o.start(now); o.stop(now + 0.13);
      }
    } catch (e) { /* ignore */ }
  }

  // Action row
  const actions = h('div', { class: 'dialer-display', style: 'background:transparent; border:0; padding:0;' });
  const clr = h('button', { type: 'button', class: 'key key-back', 'aria-label': 'Clear', style: 'max-width:72px; aspect-ratio: 1.2;' }, 'CLR');
  clr.addEventListener('click', () => { numEl.textContent = ''; });
  const call = h('button', { type: 'button', class: 'key key-call', 'aria-label': 'Call', style: 'max-width:80px;' });
  call.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>';
  call.addEventListener('click', () => placeCallFromUI());
  const back = h('button', { type: 'button', class: 'key key-back', 'aria-label': 'Backspace', style: 'max-width:72px; aspect-ratio: 1.2;' });
  back.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 4H8l-7 8 7 8h13a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z"/><line x1="18" y1="9" x2="12" y2="15"/><line x1="12" y1="9" x2="18" y2="15"/></svg>';
  back.addEventListener('click', () => { numEl.textContent = (numEl.textContent || '').slice(0, -1); });
  actions.append(h('div', { style: 'display:flex; gap: 14px; justify-content:center; align-items:center;' }, clr, call, back));

  stage.append(display, pad, actions);
  wrap.append(stage);

  // Side card
  side.append(h('h3', { style: 'margin: 0 0 12px; font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--color-fg-2);' }, 'Contacts'),
    h('div', { id: 'ds-contacts', style: 'display:flex; flex-direction:column; gap:4px;' }));
  wrap.append(side);
  root.append(wrap);

  // Load contacts
  api.get('/contacts').then((d) => {
    const items = Array.isArray(d) ? d : (d?.items || []);
    const list = root.querySelector('#ds-contacts');
    list.innerHTML = '';
    for (const c of items.slice(0, 10)) {
      const row = h('div', { class: 'rec-row', style: 'padding: 8px; cursor: pointer;' },
        createAvatar({ name: c.name, size: 28 }),
        h('div', { class: 'rec-row-meta' },
          h('div', { class: 'rec-row-name' }, c.name || c.phone || ''),
          h('div', { class: 'rec-row-num mono' }, c.phone || c.number || '')
        )
      );
      row.addEventListener('click', () => { numEl.textContent = c.phone || c.number || ''; });
      list.append(row);
    }
  }).catch(() => {});

  // Keyboard: digits when not focused on an input
  document.addEventListener('keydown', onKey);

  async function placeCallFromUI() {
    const num = (numEl.textContent || '').trim();
    if (!num) { toastError('Enter a number to call'); return; }
    try {
      // Use the existing global placeCall() if present (v5.0 in index.html)
      if (typeof window.placeCall === 'function') {
        const dn = document.getElementById('dialer-number-display');
        if (dn) dn.textContent = num;
        window.placeCall();
        return;
      }
      // Otherwise: pure backend dial (no WebRTC) — useful for testing.
      const fromNum = (from.querySelector('select')?.value) || '+15078731084';
      const res = await api.post('/dial', { to: num, from: fromNum });
      toastSuccess('Call initiated');
    } catch (e) {
      toastError('Call failed: ' + e.message);
    }
  }

  function onKey(e) {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    if (e.key === 'Backspace') { numEl.textContent = (numEl.textContent || '').slice(0, -1); return; }
    if (e.key === 'Enter') { placeCallFromUI(); return; }
    if (/^[0-9*#]$/.test(e.key)) {
      const btn = pad.querySelector(`[data-digit="${e.key}"]`);
      if (btn) pressKey(e.key, btn);
    }
  }
}
