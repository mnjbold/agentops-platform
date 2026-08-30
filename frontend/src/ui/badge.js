/* =====================================================================
 * agentops/ui/badge.js
 * Badge primitive. Variants + sizes.
 * ===================================================================== */

import { h } from '../lib/dom.js';

const VARIANT = {
  neutral: 'badge-neutral',
  success: 'badge-success',
  warning: 'badge-warning',
  danger:  'badge-danger',
  info:    'badge-info',
  accent:  'badge-accent',
};

export function createBadge(opts = {}) {
  const { variant = 'neutral', size = 'md', dot = false, children, ariaLabel } = opts;
  const root = h('span', { class: `badge badge-${variant} badge-size-${size}`, 'aria-label': ariaLabel });
  if (dot) root.append(h('span', { class: 'badge-dot', 'aria-hidden': 'true' }));
  if (children != null) root.append(typeof children === 'string' ? document.createTextNode(children) : children);
  return root;
}

export function createSpinner(opts = {}) {
  const { size = 'md', label = 'Loading' } = opts;
  return h('span', { class: `spinner spinner-${size}`, role: 'status', 'aria-label': label },
    h('svg', { viewBox: '0 0 50 50', 'aria-hidden': 'true' },
      h('circle', { cx: 25, cy: 25, r: 20, class: 'spinner-track' }),
      h('circle', { cx: 25, cy: 25, r: 20, class: 'spinner-arc' })
    )
  );
}
