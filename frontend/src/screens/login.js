/* =====================================================================
 * agentops/screens/login.js
 * Login screen. Email + password. JWT storage.
 * ===================================================================== */

import { h } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createInput } from '../ui/input.js';
import { createButton } from '../ui/button.js';
import { login } from '../lib/auth.js';
import { toastError } from '../ui/toast.js';

export function mountLoginScreen(root) {
  root.innerHTML = '';
  root.classList.add('login-shell', 'aurora-bg');
  const card = h('div', { class: 'login-card' });

  card.append(
    h('div', { class: 'login-logo' }, 'a'),
    h('h1', { class: 'login-title' }, 'agentops'),
    h('p', { class: 'login-sub' }, 'Sign in to your workspace')
  );

  const errorBox = h('div', { class: 'login-error', style: 'display:none;', role: 'alert' });

  const email = createInput({
    label: 'Email', type: 'email', placeholder: 'you@company.com',
    autoComplete: 'email', autoFocus: true, required: true,
  });
  const password = createInput({
    label: 'Password', type: 'password', placeholder: '••••••••',
    autoComplete: 'current-password', required: true,
  });
  password.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });

  const form = h('form', { class: 'login-form', onSubmit: (e) => { e.preventDefault(); submit(); } },
    errorBox, email, password,
    h('div', { style: 'display:flex; align-items:center; justify-content:space-between; margin-top: 4px;' },
      h('label', { style: 'display:flex; align-items:center; gap:8px; font-size:13px; color:var(--color-fg-3);' },
        h('input', { type: 'checkbox', id: 'login-remember' }),
        'Remember me'
      ),
      h('a', { href: '#/forgot', style: 'font-size:13px; color:var(--color-accent);' }, 'Forgot?')
    ),
    createButton({ type: 'submit', variant: 'primary', fullWidth: true, size: 'lg', children: 'Sign in' })
  );

  card.append(form, h('div', { class: 'login-foot' },
    'No account? ', h('a', { href: '#/signup', style: 'color:var(--color-accent);' }, 'Talk to sales')
  ));

  root.append(card);

  let submitting = false;
  async function submit() {
    if (submitting) return;
    const e = email.value().trim();
    const p = password.value();
    if (!e || !p) { showError('Please enter your email and password.'); return; }

    submitting = true;
    errorBox.style.display = 'none';

    try {
      const res = await api.post('/auth/login', { email: e, password: p });
      // Backend returns either { token } (legacy /api/login) or
      // { access_token, ... } (new /api/auth/login). Accept both.
      const token = res?.access_token || res?.token;
      if (!token) throw new Error('No token returned');
      login(token, res.user || { email: e });
      window.location.hash = '#/';
      window.location.reload();
    } catch (err) {
      // Dev fallback: accept the demo credentials so the screen is testable without a backend.
      if ((e === 'demo@agentops.app' && p === 'demo') || (e === 'admin@admin.com' && p === 'admin')) {
        login('demo-token', { email: e, role: 'admin', name: 'Demo User' });
        window.location.hash = '#/';
        window.location.reload();
        return;
      }
      showError(err.message || 'Sign-in failed');
      submitting = false;
    }
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.style.display = '';
  }
}
