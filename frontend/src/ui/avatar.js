/* =====================================================================
 * agentops/ui/avatar.js
 * Avatar primitive. Image + initials fallback + status dot.
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { initials, gravatarUrl } from '../lib/dom.js';

export function createAvatar(opts = {}) {
  const { src, name = '', email, size = 32, status, alt } = opts;
  const root = h('span', {
    class: `avatar avatar-size-${size}`,
    role: 'img',
    'aria-label': alt || name || 'Avatar',
  });
  if (status) root.append(h('span', { class: `avatar-status avatar-status-${status}`, 'aria-hidden': 'true' }));

  if (src) {
    const img = h('img', { src, alt: alt || name || '', class: 'avatar-img', loading: 'lazy' });
    img.addEventListener('error', () => {
      img.remove();
      root.append(fallback());
    }, { once: true });
    root.append(img);
  } else {
    root.append(fallback());
  }

  function fallback() {
    return h('span', { class: 'avatar-fallback', 'aria-hidden': 'true' },
      src ? gravatarUrl(email, size * 2) && h('img', { src: gravatarUrl(email, size * 2), alt: '', loading: 'lazy' }) : null,
      h('span', { class: 'avatar-initials' }, initials(name))
    );
  }
  return root;
}
