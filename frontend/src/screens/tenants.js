/* =====================================================================
 * agentops/screens/tenants.js
 * Tenants admin. Gated by role=admin. List + create form (shows API key once).
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { tokenStore } from '../lib/auth.js';
import { createInput, createTextarea } from '../ui/input.js';
import { createSelect } from '../ui/select.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError, toastSuccess } from '../ui/toast.js';

export async function mountTenantsScreen(root) {
  root.innerHTML = '';
  const user = tokenStore.get().user;
  if (!user || user.role !== 'admin') {
    root.append(createEmptyState({
      icon: '🔒',
      title: 'Admin only',
      body: 'You need an admin account to manage tenants. Sign in as admin@admin.com / admin to try.',
    }));
    return;
  }

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Tenants'),
      h('p', { class: 'page-sub' }, 'Multi-tenant provisioning. Create workspaces, scope API keys.')
    ),
    h('div', { class: 'page-actions' },
      createButton({ variant: 'primary', size: 'sm', onClick: () => openCreate(root), children: '+ New tenant' })
    )
  ));

  const list = h('div', { id: 'tenants-list', 'aria-busy': 'true' });
  list.append(createSkeleton({ lines: 4, height: 56 }));
  root.append(list);

  await load(root);
}

async function load(root) {
  const list = root.querySelector('#tenants-list');
  if (!list) return;
  list.innerHTML = '';
  list.setAttribute('aria-busy', 'true');
  for (let i = 0; i < 3; i++) list.append(createSkeleton({ lines: 1, height: 56 }));
  try {
    const data = await api.get('/tenants');
    const items = Array.isArray(data) ? data : (data?.items || []);
    renderList(root, items);
  } catch (e) {
    list.innerHTML = '';
    list.append(createEmptyState({
      icon: '!', title: 'Could not load tenants', body: e.message,
      action: createButton({ variant: 'primary', size: 'sm', onClick: () => load(root), children: 'Retry' }),
    }));
  }
}

function renderList(root, items) {
  const list = root.querySelector('#tenants-list');
  list.innerHTML = '';
  list.setAttribute('aria-busy', 'false');
  if (!items.length) {
    list.append(createEmptyState({
      icon: '🏢', title: 'No tenants yet',
      body: 'Create your first workspace to issue scoped API keys.',
      action: createButton({ variant: 'primary', size: 'sm', onClick: () => openCreate(root), children: 'Create tenant' }),
    }));
    return;
  }
  for (const t of items) {
    const row = h('div', { class: 'rec-row' },
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name' }, t.name || '(unnamed)'),
        h('div', { class: 'rec-row-num mono' }, t.id || t.tenant_id || '')
      ),
      h('div', {}, createBadge({ variant: t.tier === 'enterprise' ? 'accent' : t.tier === 'pro' ? 'info' : 'neutral', children: t.tier || 'free' })),
      h('div', {}, createBadge({ variant: t.active !== false ? 'success' : 'danger', children: t.active !== false ? 'Active' : 'Disabled' }))
    );
    list.append(row);
  }
}

function openCreate(root) {
  const overlay = h('div', { class: 'modal-overlay is-open', style: 'opacity:1;', role: 'dialog', 'aria-modal': 'true' });
  const dialog = h('div', { class: 'modal-dialog', style: 'max-width: 520px;' });
  const errBox = h('div', { class: 'login-error', style: 'display:none;' });
  const nameIn = createInput({ label: 'Tenant name', placeholder: 'Acme Corp', required: true, autoFocus: true });
  const tierIn = createSelect({ label: 'Tier', value: 'pro', options: [
    { value: 'free', label: 'Free' },
    { value: 'pro', label: 'Pro' },
    { value: 'enterprise', label: 'Enterprise' },
  ]});

  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });

  const submit = createButton({
    variant: 'primary', children: 'Create', onClick: async () => {
      const n = nameIn.value().trim();
      if (!n) { errBox.textContent = 'Name is required'; errBox.style.display = ''; return; }
      try {
        const res = await api.post('/tenants', { name: n, tier: tierIn.value() });
        // Show API key once (it's only returned on creation)
        if (res?.api_key) showApiKeyOnce(res);
        else { toastSuccess('Tenant created'); }
        close();
        load(root);
      } catch (e) {
        errBox.textContent = e.message;
        errBox.style.display = '';
      }
    }
  });
  const cancel = createButton({ variant: 'ghost', children: 'Cancel', onClick: close });

  dialog.append(
    h('div', { class: 'modal-head' },
      h('h2', { class: 'modal-title' }, 'New tenant'),
      h('button', { type: 'button', class: 'modal-close', 'aria-label': 'Close', onClick: close }, '×')
    ),
    h('div', { class: 'modal-body' },
      errBox,
      h('div', { style: 'display:flex; flex-direction:column; gap: var(--space-3);' },
        nameIn, tierIn
      )
    ),
    h('div', { class: 'modal-foot' }, cancel, submit)
  );
  overlay.append(dialog);
  document.body.append(overlay);
  setTimeout(() => nameIn.focus(), 0);
}

function showApiKeyOnce(res) {
  const overlay = h('div', { class: 'modal-overlay is-open', style: 'opacity:1;', role: 'dialog', 'aria-modal': 'true' });
  const dialog = h('div', { class: 'modal-dialog', style: 'max-width: 520px;' });
  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });

  const key = res.api_key;
  const code = h('code', { style: 'display:block; padding: 12px; background: var(--color-bg-1); border: 1px solid var(--color-line); border-radius: var(--radius-md); font-family: var(--font-mono); font-size: 12px; word-break: break-all; user-select: all;' }, key);
  const copy = createButton({
    variant: 'secondary', children: 'Copy', onClick: async () => {
      try { await navigator.clipboard.writeText(key); toastSuccess('Copied'); } catch (e) { toastError('Copy failed'); }
    }
  });

  dialog.append(
    h('div', { class: 'modal-head' },
      h('h2', { class: 'modal-title' }, 'Tenant created'),
      h('button', { type: 'button', class: 'modal-close', 'aria-label': 'Close', onClick: close }, '×')
    ),
    h('div', { class: 'modal-body' },
      h('p', { style: 'margin: 0 0 12px; color: var(--color-fg-2);' },
        'Save this API key now — it will not be shown again.'
      ),
      code,
      h('div', { style: 'margin-top: 12px; display: flex; gap: var(--space-2); justify-content: flex-end;' }, copy)
    ),
    h('div', { class: 'modal-foot' },
      createButton({ variant: 'primary', onClick: close, children: "I've saved it" })
    )
  );
  overlay.append(dialog);
  document.body.append(overlay);
}
