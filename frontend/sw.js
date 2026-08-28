// agentops service worker — v0.4.0
// Strategy:
//   - Pre-cache shell on install
//   - index.html: ALWAYS network (so users get updates immediately)
//   - /api/* and bk-jr-api.aixlabs.fun/*: network-first, fall back to cache only on total failure
//   - Static assets (icons, manifest, sw.js itself): cache-first
const CACHE_NAME = 'agentops-v0-4-0';
const SHELL_CACHE = `${CACHE_NAME}-shell`;
const STATIC_CACHE = `${CACHE_NAME}-static`;
const RUNTIME_CACHE = `${CACHE_NAME}-runtime`;

const SHELL_URLS = [
  '/',
  '/manifest.json',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
];

// Anything matching this path is treated as a live API call: never stale.
const NETWORK_FIRST_PATHS = [
  /^\/api\//,
  /^https?:\/\/bk-jr-api\.aixlabs\.fun\//,
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      // Pre-cache best-effort. If any single request 404s at install time
      // (e.g. a future page isn't built yet), don't break the whole install.
      await Promise.allSettled(
        SHELL_URLS.map((u) =>
          fetch(u, { cache: 'no-cache' })
            .then((res) => (res.ok ? cache.put(u, res.clone()) : null))
            .catch(() => null)
        )
      );
      // Activate the new SW immediately so this version takes over on next load.
      await self.skipWaiting();
    })()
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      const keep = new Set([SHELL_CACHE, STATIC_CACHE, RUNTIME_CACHE]);
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((k) => !keep.has(k)).map((k) => caches.delete(k))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;

  // 1. index.html — always go to network. No cache lookup, no cache write.
  //    This is how we get zero-stale apps: every page load is a fresh fetch,
  //    and the browser will reuse its HTTP cache (200 OK disk cache) per
  //    its own Cache-Control header from the server.
  if (sameOrigin && (url.pathname === '/' || url.pathname === '/index.html')) {
    event.respondWith(
      fetch(req, { cache: 'reload' }).catch(
        () => caches.match('/') // offline fallback to the shell cache
      )
    );
    return;
  }

  // 2. Network-first for live API surfaces.
  if (NETWORK_FIRST_PATHS.some((rx) => rx.test(url.pathname) || rx.test(req.url))) {
    event.respondWith(
      (async () => {
        try {
          const fresh = await fetch(req);
          // Best-effort: cache the response for offline fallback.
          const cache = await caches.open(RUNTIME_CACHE);
          cache.put(req, fresh.clone());
          return fresh;
        } catch (e) {
          const cached = await caches.match(req);
          if (cached) return cached;
          // Total network failure + no cache: surface a clean 503-style
          // JSON error so the app's fetch handlers can degrade gracefully.
          return new Response(
            JSON.stringify({ error: 'offline', detail: 'network unreachable and no cached response' }),
            { status: 503, headers: { 'Content-Type': 'application/json' } }
          );
        }
      })()
    );
    return;
  }

  // 3. Static same-origin assets (icons, manifest, sw.js itself): cache-first.
  if (
    sameOrigin &&
    (url.pathname.startsWith('/icons/') ||
      url.pathname === '/manifest.json' ||
      url.pathname === '/sw.js')
  ) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(STATIC_CACHE);
        const cached = await cache.match(req);
        if (cached) return cached;
        const fresh = await fetch(req);
        if (fresh.ok) cache.put(req, fresh.clone());
        return fresh;
      })()
    );
    return;
  }

  // 4. Everything else (e.g. jsdelivr CDN bundle, Google Fonts) — passthrough.
  //    Browser HTTP cache is sufficient.
});
