/* =====================================================================
 * agentops/ui/select.js
 * Select primitive. Native <select> for accessibility + simplicity.
 * For searchable/autocomplete, use createSearchableSelect below.
 * ===================================================================== */

import { h } from '../lib/dom.js';

let _uid = 0;
const nextId = () => `sel-${++_uid}`;

export function createSelect(opts = {}) {
  const {
    label, helper, error, value, options = [], name, id = nextId(),
    placeholder = 'Select…', disabled, required, onChange, size,
  } = opts;

  const wrap = h('div', { class: `field ${error ? 'has-error' : ''}`.trim() });
  if (label) wrap.append(h('label', { for: id, class: 'field-label' }, label));

  const sel = h('select', { id, name, class: 'field-input field-select', required });
  if (placeholder) sel.append(h('option', { value: '', disabled: true, selected: value == null }, placeholder));
  for (const o of options) {
    const opt = h('option', { value: o.value }, o.label);
    if (o.value === value) opt.selected = true;
    sel.append(opt);
  }
  if (size) sel.size = size;
  if (disabled) sel.setAttribute('disabled', '');

  wrap.append(sel);
  if (helper && !error) wrap.append(h('p', { class: 'field-helper' }, helper));
  if (error) wrap.append(h('p', { class: 'field-error', role: 'alert' }, error));

  if (onChange) sel.addEventListener('change', (e) => onChange(e, sel));
  wrap.input = sel;
  wrap.value = (v) => { if (v === undefined) return sel.value; sel.value = v; sel.dispatchEvent(new Event('change')); return sel.value; };
  return wrap;
}

/* Searchable combobox built on a native input + datalist (lightweight, accessible) */
export function createSearchableSelect(opts = {}) {
  const {
    label, helper, error, value, options = [], name, id = nextId(),
    placeholder = 'Search…', disabled, onChange,
  } = opts;

  const wrap = h('div', { class: `field ${error ? 'has-error' : ''}`.trim() });
  if (label) wrap.append(h('label', { for: id, class: 'field-label' }, label));

  const listId = `${id}-list`;
  const input = h('input', {
    id, name, type: 'text', class: 'field-input',
    placeholder, list: listId, autocomplete: 'off',
    role: 'combobox', 'aria-expanded': 'false', 'aria-controls': listId,
  });
  input.value = value || '';
  const datalist = h('datalist', { id: listId });
  for (const o of options) datalist.append(h('option', { value: o.label, dataset: { val: o.value } }));

  wrap.append(input, datalist);
  if (helper && !error) wrap.append(h('p', { class: 'field-helper' }, helper));
  if (error) wrap.append(h('p', { class: 'field-error', role: 'alert' }, error));

  function emit() {
    const match = options.find(o => o.label === input.value);
    if (onChange) onChange({ value: match ? match.value : input.value, label: input.value }, input);
  }
  input.addEventListener('change', emit);
  input.addEventListener('input', debounce(emit, 200));
  if (disabled) input.setAttribute('disabled', '');

  wrap.input = input;
  wrap.value = (v) => { if (v === undefined) return input.value; input.value = v; return input.value; };
  return wrap;
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
