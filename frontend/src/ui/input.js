/* =====================================================================
 * agentops/ui/input.js
 * Input primitive. Label, helper, error, prefix, suffix, clearable.
 * ===================================================================== */

import { h } from '../lib/dom.js';

let _uid = 0;
const nextId = () => `inp-${++_uid}`;

export function createInput(opts = {}) {
  const {
    label,
    helper,
    error,
    type = 'text',
    placeholder = '',
    value = '',
    name,
    id = nextId(),
    prefix,
    suffix,
    clearable = false,
    disabled = false,
    required = false,
    autoFocus = false,
    autoComplete,
    onInput,
    onChange,
    onEnter,
  } = opts;

  const wrap = h('div', { class: `field ${error ? 'has-error' : ''} ${disabled ? 'is-disabled' : ''}`.trim() });

  if (label) {
    const lbl = h('label', { for: id, class: 'field-label' }, label);
    if (required) lbl.append(h('span', { class: 'field-required', 'aria-hidden': 'true' }, '*'));
    wrap.append(lbl);
  }

  const inner = h('div', { class: 'field-control' });
  const input = h('input', {
    id, name, type, placeholder, value, required,
    class: 'field-input',
    disabled: disabled || undefined,
    autocomplete: autoComplete,
    'aria-invalid': error ? 'true' : undefined,
    'aria-describedby': error ? `${id}-err` : helper ? `${id}-help` : undefined,
  });

  if (prefix) inner.append(h('span', { class: 'field-affix field-prefix' }, prefix));
  inner.append(input);
  if (suffix) inner.append(h('span', { class: 'field-affix field-suffix' }, suffix));

  if (clearable) {
    const clear = h('button', {
      type: 'button',
      class: 'field-clear',
      'aria-label': 'Clear',
      tabIndex: 0,
      style: 'display:none;',
      onClick: () => { input.value = ''; input.dispatchEvent(new Event('input', { bubbles: true })); input.focus(); },
    }, '×');
    input.addEventListener('input', () => { clear.style.display = input.value ? '' : 'none'; });
    inner.append(clear);
  }

  wrap.append(inner);

  if (helper && !error) wrap.append(h('p', { class: 'field-helper', id: `${id}-help` }, helper));
  if (error) wrap.append(h('p', { class: 'field-error', id: `${id}-err`, role: 'alert' }, error));

  if (onInput) input.addEventListener('input', (e) => onInput(e, input));
  if (onChange) input.addEventListener('change', (e) => onChange(e, input));
  if (onEnter) input.addEventListener('keydown', (e) => { if (e.key === 'Enter') onEnter(e, input); });

  if (autoFocus) setTimeout(() => input.focus(), 0);

  wrap.input = input;
  wrap.value = (v) => { if (v === undefined) return input.value; input.value = v; return input.value; };
  wrap.focus = () => input.focus();
  wrap.setError = (msg) => {
    const old = wrap.querySelector('.field-error'); if (old) old.remove();
    wrap.classList.toggle('has-error', !!msg);
    if (msg) wrap.append(h('p', { class: 'field-error', role: 'alert' }, msg));
    input.setAttribute('aria-invalid', msg ? 'true' : 'false');
  };

  return wrap;
}

export function createTextarea(opts = {}) {
  const {
    label, helper, error, value = '', placeholder = '', name, id = nextId(),
    rows = 4, disabled, required, onInput,
  } = opts;
  const wrap = h('div', { class: `field ${error ? 'has-error' : ''}`.trim() });
  if (label) wrap.append(h('label', { for: id, class: 'field-label' }, label));
  const ta = h('textarea', { id, name, rows, placeholder, value, required, class: 'field-input field-textarea' });
  ta.value = value;
  if (disabled) ta.setAttribute('disabled', '');
  wrap.append(ta);
  if (helper && !error) wrap.append(h('p', { class: 'field-helper' }, helper));
  if (error) wrap.append(h('p', { class: 'field-error', role: 'alert' }, error));
  if (onInput) ta.addEventListener('input', (e) => onInput(e, ta));
  wrap.input = ta;
  wrap.value = (v) => { if (v === undefined) return ta.value; ta.value = v; return ta.value; };
  return wrap;
}
