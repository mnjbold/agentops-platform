/* =====================================================================
 * agentops/ui/button.js
 * Button primitive. Vanilla HTMLElement factory.
 *
 * createButton({ variant, size, loading, icon, fullWidth, children, onClick })
 *   variant: primary | secondary | ghost | danger | success
 *   size:    sm | md | lg
 * ===================================================================== */

import { h } from '../lib/dom.js';

const VARIANT = {
  primary:   'btn-primary',
  secondary: 'btn-secondary',
  ghost:     'btn-ghost',
  danger:    'btn-danger',
  success:   'btn-success',
};

const SIZE = {
  sm: 'btn-sm',
  md: 'btn-md',
  lg: 'btn-lg',
};

export function createButton(opts = {}) {
  const {
    variant = 'primary',
    size = 'md',
    loading = false,
    disabled = false,
    fullWidth = false,
    icon = null,         // HTMLElement or string (svg use ref)
    iconRight = null,
    type = 'button',
    ariaLabel,
    onClick,
    children,
    className = '',
    id,
    href,
  } = opts;

  const classes = ['btn', VARIANT[variant] || VARIANT.primary, SIZE[size] || SIZE.md];
  if (fullWidth) classes.push('btn-full');
  if (loading) classes.push('is-loading');
  if (className) classes.push(className);

  const tag = href ? 'a' : 'button';
  const attrs = {
    class: classes.join(' '),
    type: tag === 'button' ? type : undefined,
    id,
    'aria-label': ariaLabel,
    'aria-busy': loading || undefined,
    'aria-disabled': disabled || loading || undefined,
    tabIndex: disabled ? -1 : 0,
  };
  if (href) { attrs.href = href; attrs.role = 'button'; }
  if (disabled) attrs.disabled = true;

  const el = h(tag, attrs);

  if (loading) {
    el.append(h('span', { class: 'btn-spinner', 'aria-hidden': 'true' }));
  } else if (icon) {
    el.append(typeof icon === 'string'
      ? h('span', { class: 'btn-icon', 'aria-hidden': 'true', html: icon })
      : icon);
  }

  if (children != null) el.append(typeof children === 'string' ? document.createTextNode(children) : children);

  if (iconRight && !loading) {
    el.append(typeof iconRight === 'string'
      ? h('span', { class: 'btn-icon', 'aria-hidden': 'true', html: iconRight })
      : iconRight);
  }

  if (onClick) el.addEventListener('click', (e) => {
    if (disabled || loading) { e.preventDefault(); return; }
    onClick(e);
  });

  // Methods
  el.setLoading = (v) => {
    el.classList.toggle('is-loading', !!v);
    el.setAttribute('aria-busy', !!v || null);
    if (v) el.setAttribute('aria-disabled', 'true');
    else el.removeAttribute('aria-disabled');
  };
  el.setDisabled = (v) => {
    if (v) { el.setAttribute('disabled', ''); el.setAttribute('aria-disabled', 'true'); el.tabIndex = -1; }
    else   { el.removeAttribute('disabled'); el.removeAttribute('aria-disabled'); el.tabIndex = 0; }
  };

  return el;
}
