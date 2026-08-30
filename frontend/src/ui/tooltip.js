/* =====================================================================
 * agentops/ui/tooltip.js
 * Tooltip primitive. Lightweight, top/bottom/left/right placement.
 * ===================================================================== */

import { h } from '../lib/dom.js';

const PLACEMENT_OFFSET = 8;

export function createTooltip(target, opts = {}) {
  const { text, placement = 'top', delay = 200 } = opts;
  if (!text) return () => {};
  const tip = h('div', { class: `tooltip tooltip-${placement}`, role: 'tooltip' }, text);
  let visible = false, timer = null;

  function show() {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (visible) return;
      visible = true;
      document.body.append(tip);
      const r = target.getBoundingClientRect();
      const tw = tip.offsetWidth, th = tip.offsetHeight;
      const vw = window.innerWidth, vh = window.innerHeight;
      let top = 0, left = 0;
      switch (placement) {
        case 'top':    top = r.top - th - PLACEMENT_OFFSET; left = r.left + r.width / 2 - tw / 2; break;
        case 'bottom': top = r.bottom + PLACEMENT_OFFSET;    left = r.left + r.width / 2 - tw / 2; break;
        case 'left':   top = r.top + r.height / 2 - th / 2;  left = r.left - tw - PLACEMENT_OFFSET; break;
        case 'right':  top = r.top + r.height / 2 - th / 2;  left = r.right + PLACEMENT_OFFSET; break;
      }
      // keep on-screen
      left = Math.max(6, Math.min(vw - tw - 6, left));
      top  = Math.max(6, Math.min(vh - th - 6, top));
      tip.style.left = left + 'px';
      tip.style.top  = top  + 'px';
      tip.classList.add('is-visible');
    }, delay);
  }
  function hide() {
    clearTimeout(timer);
    if (!visible) return;
    visible = false;
    tip.classList.remove('is-visible');
    setTimeout(() => tip.remove(), 150);
  }

  target.addEventListener('mouseenter', show);
  target.addEventListener('mouseleave', hide);
  target.addEventListener('focus', show);
  target.addEventListener('blur', hide);
  return hide;
}
