/* =====================================================================
 * agentops/store.js
 * 30-line pubsub store. No deps.
 *
 * Usage:
 *   const s = createStore({ count: 0 });
 *   s.subscribe((state, prev) => ...);
 *   s.set({ count: 1 });
 *   s.update(s => ({ ...s, count: s.count + 1 }));
 *   s.get();
 * ===================================================================== */

export function createStore(initial = {}) {
  let state = { ...initial };
  const listeners = new Set();

  function get() { return state; }

  function set(patch) {
    const prev = state;
    state = typeof patch === 'function' ? patch(state) : { ...state, ...patch };
    if (state !== prev) listeners.forEach(fn => { try { fn(state, prev); } catch (e) { console.error(e); } });
    return state;
  }

  function update(fn) { return set(fn); }

  function subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  return { get, set, update, subscribe };
}

/* Persistence helper */
export function persistedStore(key, initial) {
  let restored = initial;
  try {
    const raw = localStorage.getItem(key);
    if (raw) restored = { ...initial, ...JSON.parse(raw) };
  } catch (e) { /* ignore */ }
  const s = createStore(restored);
  s.subscribe((state) => {
    try { localStorage.setItem(key, JSON.stringify(state)); } catch (e) { /* ignore */ }
  });
  return s;
}
