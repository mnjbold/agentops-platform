/* =====================================================================
 * agentops/ui/chart.js
 * Tiny zero-dep canvas bar chart. ~100 lines, no Chart.js.
 *
 * Used by the campaign test-mode modal and the analytics screen. The
 * shape is "horizontal bars with labels + counts" — readable at a glance
 * for outcome distributions. Designed for ~5-10 categories.
 * ===================================================================== */

const COLORS = {
  answer:    '#22c55e', // green
  voicemail: '#a78bfa', // purple
  no_answer: '#f59e0b', // amber
  busy:      '#eab308', // yellow
  failed:    '#ef4444', // red
  default:   '#7c8aa0', // grey
};

const COLOR_FOR = (k) => COLORS[k] || COLORS.default;

/**
 * Render a horizontal bar chart into a <canvas>.
 *
 * @param {HTMLCanvasElement} canvas
 * @param {Object} data  {label: count, ...} e.g. {answer: 60, voicemail: 20, ...}
 * @param {Object} [opts]
 * @param {string} [opts.title] - Optional small title rendered top-left
 * @param {number} [opts.height] - Bar height in px (default 24)
 * @param {number} [opts.gap] - Gap between bars (default 8)
 * @param {number} [opts.padding] - Outer padding (default 12)
 * @param {number} [opts.maxBars] - Cap on bars shown (default 8)
 */
export function renderBarChart(canvas, data, opts = {}) {
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  const padding = opts.padding ?? 12;
  const barH = opts.height ?? 24;
  const gap = opts.gap ?? 8;
  const maxBars = opts.maxBars ?? 8;

  // Sort by count desc, cap to maxBars
  const entries = Object.entries(data || {})
    .filter(([, v]) => Number(v) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, maxBars);
  const total = entries.reduce((s, [, v]) => s + Number(v), 0);

  // Reserve canvas height = padding + bars * (h+gap) + bottom label
  const cssW = canvas.clientWidth || 320;
  const cssH = padding * 2 + entries.length * (barH + gap) + 12;
  canvas.style.height = cssH + 'px';
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  if (opts.title) {
    ctx.fillStyle = '#9aa5b5';
    ctx.font = '600 11px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    ctx.fillText(opts.title, padding, padding - 2);
  }

  if (!entries.length) {
    ctx.fillStyle = '#9aa5b5';
    ctx.font = '12px system-ui, sans-serif';
    ctx.textBaseline = 'top';
    ctx.textAlign = 'center';
    ctx.fillText('No data yet', cssW / 2, cssH / 2 - 6);
    ctx.textAlign = 'left';
    return;
  }

  const labelW = 90;   // px reserved for outcome label
  const valueW = 56;   // px reserved for count + percent
  const innerX = padding + labelW;
  const innerW = Math.max(20, cssW - padding * 2 - labelW - valueW);
  const maxCount = Math.max(...entries.map(([, v]) => Number(v)), 1);

  entries.forEach(([key, count], i) => {
    const y = padding + i * (barH + gap);
    // Outcome label
    ctx.fillStyle = '#cdd5e0';
    ctx.font = '500 12px system-ui, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';
    ctx.fillText(key, innerX - 8, y + barH / 2);

    // Background bar (rounded)
    ctx.fillStyle = '#1f2937';
    roundRect(ctx, innerX, y, innerW, barH, 4);
    ctx.fill();

    // Filled bar
    const w = Math.max(2, (Number(count) / maxCount) * innerW);
    ctx.fillStyle = COLOR_FOR(key);
    roundRect(ctx, innerX, y, w, barH, 4);
    ctx.fill();

    // Count + percent
    const pct = total ? Math.round((Number(count) / total) * 100) : 0;
    ctx.fillStyle = '#cdd5e0';
    ctx.font = '600 12px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(
      `${count} (${pct}%)`,
      innerX + Math.min(w + 6, innerW - 56),
      y + barH / 2,
    );
  });
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, h / 2, w / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.lineTo(x + w - rr, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + rr);
  ctx.lineTo(x + w, y + h - rr);
  ctx.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  ctx.lineTo(x + rr, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - rr);
  ctx.lineTo(x, y + rr);
  ctx.quadraticCurveTo(x, y, x + rr, y);
  ctx.closePath();
}

/**
 * Render a small inline bar chart (no labels) into a 1-D bar.
 * Useful for "fill: 60/100" meters that don't need the full chart.
 */
export function renderMeter(canvas, percent, color = '#22c55e') {
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const dpr = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  const w = canvas.clientWidth || 200;
  const h = canvas.clientHeight || 8;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  // Background
  ctx.fillStyle = '#1f2937';
  roundRect(ctx, 0, 0, w, h, h / 2);
  ctx.fill();
  // Fill
  const fillW = Math.max(0, Math.min(w, (Math.max(0, Math.min(100, percent)) / 100) * w));
  if (fillW > 0) {
    ctx.fillStyle = color;
    roundRect(ctx, 0, 0, fillW, h, h / 2);
    ctx.fill();
  }
}
