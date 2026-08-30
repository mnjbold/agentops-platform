/* =====================================================================
 * agentops/auth.js
 * JWT + tenant persistence. Tiny.
 * ===================================================================== */

import { persistedStore } from './store.js';

export const tokenStore = persistedStore('agentops.auth', { token: null, user: null });
export const tenantStore = persistedStore('agentops.tenant', { tenantId: 'default' });

export function isAuthed() { return !!tokenStore.get().token; }

export function login(token, user) {
  tokenStore.set({ token, user: user || tokenStore.get().user });
}

export function logout() {
  tokenStore.set({ token: null, user: null });
}

export function setTenant(id) { tenantStore.set({ tenantId: id }); }

export function requireAuth(redirect = '#/login') {
  if (!isAuthed()) {
    window.location.hash = redirect;
    return false;
  }
  return true;
}
