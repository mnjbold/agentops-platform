/* =====================================================================
 * agentops/router.js
 * Hash-based router. No deps.
 *
 * Usage:
 *   const router = createRouter({
 *     '/':           () => mountDashboard(),
 *     '/dialer':     () => mountDialer(),
 *     '/login':      () => mountLogin(),
 *   });
 *   router.start();
 * ===================================================================== */

export function createRouter(routes, { onChange } = {}) {
  function parse() {
    const hash = window.location.hash.replace(/^#/, '') || '/';
    const [path, queryStr] = hash.split('?');
    const query = Object.fromEntries(new URLSearchParams(queryStr || ''));
    return { path, query };
  }

  async function resolve() {
    const { path, query } = parse();
    // try exact, then prefix matches
    const handler = routes[path] || routes['*'];
    if (!handler) { console.warn('No route for', path); return; }
    try { await handler({ path, query }); } catch (e) { console.error('Route error', e); }
    if (onChange) onChange({ path, query });
    // Update active nav links
    document.querySelectorAll('[data-nav-link]').forEach(el => {
      const target = el.getAttribute('data-nav-link');
      el.classList.toggle('is-active', target === path || (target !== '/' && path.startsWith(target)));
    });
  }

  function navigate(path, { replace = false } = {}) {
    const url = '#' + path;
    if (replace) window.location.replace(url);
    else window.location.hash = url.slice(1);
  }

  function start() {
    window.addEventListener('hashchange', resolve);
    resolve();
  }

  return { start, navigate, resolve, parse };
}
