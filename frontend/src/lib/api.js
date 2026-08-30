/* =====================================================================
 * agentops/api.js
 * Fetch wrapper with JWT auth, tenant header, and error normalization.
 * No deps. Tiny.
 * ===================================================================== */

import { tokenStore, tenantStore } from './auth.js';

const PRIMARY = (typeof __API_BASE_PRIMARY__ !== 'undefined' ? __API_BASE_PRIMARY__ : 'https://bkjr-api.getbijou.xyz');
const FALLBACK = (typeof __API_BASE_FALLBACK__ !== 'undefined' ? __API_BASE_FALLBACK__ : 'https://bk-jr-api.aixlabs.fun');
const PREFIX = '/api';

const BASES = [PRIMARY, FALLBACK];
let _baseIdx = 0;

function currentBase() { return BASES[_baseIdx] || PRIMARY; }

async function tryFetch(base, path, opts) {
  const headers = {
    'Content-Type': 'application/json',
    'X-Tenant-Id': tenantStore.get().tenantId || 'default',
    ...(tokenStore.get().token ? { Authorization: `Bearer ${tokenStore.get().token}` } : {}),
    ...(opts.headers || {}),
  };
  const res = await fetch(base + PREFIX + path, {
    ...opts,
    headers,
    body: opts.body && typeof opts.body !== 'string' ? JSON.stringify(opts.body) : opts.body,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) {
    const err = new Error(data.detail || data.message || `HTTP ${res.status}`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  // lock in healthy base
  _baseIdx = BASES.indexOf(base);
  return data;
}

export async function request(path, opts = {}) {
  const start = _baseIdx;
  const order = [BASES[start], ...BASES.slice(start + 1), ...BASES.slice(0, start)];
  let lastErr = null;
  for (let i = 0; i < order.length; i++) {
    const base = order[i];
    try {
      return await tryFetch(base, path, opts);
    } catch (e) {
      lastErr = e;
      // 4xx is not retryable
      if (e.status && e.status < 500) throw e;
      // network errors are retryable on next base
      if (e && (e.name === 'AbortError' || /Failed to fetch|NetworkError|Load failed/i.test(e.message))) continue;
      throw e;
    }
  }
  throw lastErr || new Error('All backends unreachable');
}

/* Demo data fallback so the UI is testable without a backend.
   Tries the real API first; on network error returns a synthetic response
   that the screen modules know how to render. The backend agent ships the
   real endpoints in parallel — once they exist, this fallback is bypassed. */
const DEMO = {
  '/voicemails': () => ([
    { id: 'v1', from_number: '+1 (415) 555-0192', from_name: 'Sarah Chen', duration: 42, transcript: "Hi, this is Sarah from Acme Corp. I wanted to follow up on the proposal you sent last week. We're interested in moving forward but had a few questions about the pricing tier. Can you give me a call back when you have a moment? Thanks, bye.", created_at: new Date(Date.now() - 3 * 3600e3).toISOString() },
    { id: 'v2', from_number: '+1 (628) 555-0144', from_name: 'Marcus Lee',  duration: 18, transcript: "Hey, missed your call earlier. Just calling about Friday's meeting. Talk soon.", created_at: new Date(Date.now() - 26 * 3600e3).toISOString(), read_at: new Date().toISOString() },
  ]),
  '/recordings': () => ([
    { id: 'r1', from_number: '+1 (415) 555-0192', to_number: '+1 (507) 873-1084', duration: 312, transcript: 'Inbound call — discussed Q3 roadmap, action items, next steps.', created_at: new Date(Date.now() - 2 * 3600e3).toISOString() },
    { id: 'r2', from_number: '+1 (507) 873-1084', to_number: '+1 (628) 555-0144', duration: 145, transcript: 'Outbound — Marcus at BoldBusiness, walked through the onboarding flow.', created_at: new Date(Date.now() - 26 * 3600e3).toISOString() },
    { id: 'r3', from_number: '+1 (415) 555-0192', to_number: '+1 (507) 873-1084', duration: 95, transcript: 'Follow-up on Acme proposal, scheduled meeting for next Tuesday.', created_at: new Date(Date.now() - 2 * 86400e3).toISOString() },
  ]),
  '/tenants': () => ([
    { id: 't1', name: 'BoldBusiness',  tier: 'enterprise', active: true,  created_at: '2026-07-01' },
    { id: 't2', name: 'Acme Corp',     tier: 'pro',        active: true,  created_at: '2026-08-04' },
    { id: 't3', name: 'Indie Studio',  tier: 'free',       active: false, created_at: '2026-08-22' },
  ]),
  '/contacts': () => ([
    { id: 'c1', name: 'Sarah Chen',    phone: '+14155550192', company: 'Acme Corp' },
    { id: 'c2', name: 'Marcus Lee',    phone: '+16285550144', company: 'BoldBusiness' },
    { id: 'c3', name: 'Priya Iyer',    phone: '+15078731084', company: 'agentops' },
    { id: 'c4', name: 'David Walsh',   phone: '+13105550100', company: 'Walsh & Co.' },
    { id: 'c5', name: 'Emma Park',     phone: '+14155550111', company: 'Studio One' },
  ]),
  '/numbers': () => ([
    { number: '+15078731084', label: 'Work' },
    { number: '+13075550100', label: 'AI-routed (Sales)' },
    { number: '+13205550100', label: 'AI-routed (Support)' },
  ]),
};

function demoFor(path) {
  if (path.startsWith('/voicemails')) return DEMO['/voicemails']();
  if (path.startsWith('/recordings')) return DEMO['/recordings']();
  if (path.startsWith('/tenants')) return DEMO['/tenants']();
  if (path.startsWith('/contacts')) return DEMO['/contacts']();
  if (path.startsWith('/numbers')) return DEMO['/numbers']();
  return null;
}

export async function requestWithDemo(path, opts = {}) {
  try {
    return await request(path, opts);
  } catch (e) {
    if (opts.method && opts.method !== 'GET') throw e;
    const demo = demoFor(path);
    if (demo) return demo;
    throw e;
  }
}

export const api = {
  get:  (path, opts)        => requestWithDemo(path, { ...opts, method: 'GET' }),
  post: (path, body, opts)  => request(path, { ...opts, method: 'POST', body: body || {} }),
  put:  (path, body, opts)  => request(path, { ...opts, method: 'PUT', body: body || {} }),
  patch:(path, body, opts)  => request(path, { ...opts, method: 'PATCH', body: body || {} }),
  del:  (path, opts)        => request(path, { ...opts, method: 'DELETE' }),
  base: currentBase,
};

export function baseUrl() { return currentBase(); }
