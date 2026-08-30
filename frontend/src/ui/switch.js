/* =====================================================================
 * agentops/ui/switch.js
 * Switch primitive. With label and description.
 * ===================================================================== */

import { h } from '../lib/dom.js';

let _uid = 0;
const nextId = () => `sw-${++_uid}`;

export function createSwitch(opts = {}) {
  const { label, description, checked = false, disabled, onChange, name, id = nextId() } = opts;
  const wrap = h('label', { class: `switch ${disabled ? 'is-disabled' : ''}`.trim(), for: id });
  const input = h('input', { type: 'checkbox', class: 'switch-input', id, name, checked, role: 'switch' });
  if (disabled) input.setAttribute('disabled', '');
  const track = h('span', { class: 'switch-track', 'aria-hidden': 'true' },
    h('span', { class: 'switch-thumb' })
  );
  if (onChange) input.addEventListener('change', (e) => onChange(e.target.checked, e));
  const text = h('span', { class: 'switch-text' });
  if (label) text.append(h('span', { class: 'switch-label' }, label));
  if (description) text.append(h('span', { class: 'switch-desc' }, description));
  wrap.append(input, track, text);
  wrap.input = input;
  wrap.value = (v) => { if (v === undefined) return input.checked; input.checked = !!v; input.dispatchEvent(new Event('change')); return input.checked; };
  return wrap;
}
