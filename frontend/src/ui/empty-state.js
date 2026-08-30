/* =====================================================================
 * agentops/ui/empty-state.js
 * EmptyState primitive. icon + title + body + action.
 * ===================================================================== */

import { h } from '../lib/dom.js';

export function createEmptyState(opts = {}) {
  const { icon, title = 'Nothing here yet', body, action, compact = false } = opts;
  const root = h('div', { class: `empty-state ${compact ? 'is-compact' : ''}`.trim() });
  if (icon) root.append(h('div', { class: 'empty-icon', 'aria-hidden': 'true', html: icon }));
  if (title) root.append(h('h3', { class: 'empty-title' }, title));
  if (body) root.append(h('p', { class: 'empty-body' }, body));
  if (action) root.append(action);
  return root;
}

export function createSkeleton(opts = {}) {
  const { lines = 3, height = 14, width = '100%', rounded = true } = opts;
  const root = h('div', { class: 'skeleton-stack', 'aria-hidden': 'true' });
  for (let i = 0; i < lines; i++) {
    root.append(h('div', {
      class: `skeleton ${rounded ? 'is-rounded' : ''}`,
      style: `height:${height}px; width:${typeof width === 'number' ? width + '%' : width}`,
    }));
  }
  return root;
}
