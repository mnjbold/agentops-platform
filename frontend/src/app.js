/* =====================================================================
 * agentops/app.js
 * Vite entry point. Mounts the design-system shell and starts the router.
 * ===================================================================== */

import './styles/base.css';
import './styles/components.css';
import './styles/layout.css';
import './styles/dialer.css';

import { createStore, persistedStore } from './lib/store.js';
import { createRouter } from './lib/router.js';
import { isAuthed, tokenStore, requireAuth, logout } from './lib/auth.js';
import { createButton } from './ui/button.js';
import { createBadge } from './ui/badge.js';
import { createAvatar } from './ui/avatar.js';
import { createTooltip } from './ui/tooltip.js';

import { mountLoginScreen }       from './screens/login.js';
import { mountDialerScreen }      from './screens/dialer.js';
import { mountVoicemailScreen }   from './screens/voicemail.js';
import { mountRecordingsScreen }  from './screens/recordings.js';
import { mountPowerDialerScreen } from './screens/power-dialer.js';
import { mountTenantsScreen }     from './screens/tenants.js';
import { mountAnalyticsScreen }   from './screens/analytics.js';
import { mountBillingScreen }     from './screens/billing.js';
import { mountAuditScreen }       from './screens/audit.js';
import { mountStubScreen }        from './screens/stub.js';
import { mountWorkflowsScreen }   from './screens/workflows.js';
import { mountAssistantsScreen }  from './screens/assistants.js';
import { mountVoiceLabScreen }    from './screens/voice-lab.js';
import { mountAgentTestScreen }   from './screens/agent-test.js';
import { mountNumbersScreen }     from './screens/numbers.js';

const themeStore = persistedStore('agentops.theme', { theme: 'dark' });

function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  themeStore.set({ theme: t });
}

const app = document.getElementById('app');
app.className = 'app-shell aurora-bg';

const sidebar = document.createElement('aside');
sidebar.className = 'app-sidebar';
const topbar = document.createElement('header');
topbar.className = 'app-topbar';
const main = document.createElement('main');
main.className = 'app-main';
main.id = 'screen';
app.append(sidebar, topbar, main);

/* === Sidebar ===================================================== */
function buildSidebar() {
  const items = [
    {
      heading: 'Workspace',
      items: [
        { id: 'dashboard', href: '#/',       label: 'Overview',   icon: '⌂' },
        { id: 'dialer',    href: '#/dialer',  label: 'Softphone',  icon: '☎' },
        { id: 'calls',     href: '#/calls',   label: 'Calls',      icon: '⤓' },
        { id: 'messages',  href: '#/messages',label: 'Messages',   icon: '✉' },
        { id: 'voicemail', href: '#/voicemail', label: 'Voicemail', icon: '⌖', badge: 0 },
        { id: 'recordings',href: '#/recordings',label: 'Recordings',icon: '♪' },
      ],
    },
    {
      heading: 'Outbound',
      items: [
        { id: 'campaigns',  href: '#/campaigns',  label: 'Campaigns',  icon: '⊞' },
        { id: 'power',      href: '#/power-dialer',label: 'Power Dialer', icon: '➤' },
        { id: 'contacts',   href: '#/contacts',   label: 'Contacts',   icon: '◉' },
      ],
    },
    {
      heading: 'Admin',
      items: [
        { id: 'tenants',  href: '#/tenants',  label: 'Tenants',  icon: '⌘' },
        { id: 'settings', href: '#/settings', label: 'Settings', icon: '⚙' },
        { id: 'agents',   href: '#/agents',   label: 'Agents',   icon: '✦' },
        { id: 'assistants', href: '#/assistants', label: 'AI Assistants', icon: '✦' },
        { id: 'voice-lab',  href: '#/voice-lab',  label: 'Voice Lab',    icon: '♪' },
        { id: 'analytics',  href: '#/analytics',  label: 'Analytics',  icon: '∑' },
        { id: 'billing',    href: '#/billing',    label: 'Billing',    icon: '$' },
        { id: 'audit',      href: '#/audit',      label: 'Audit log',  icon: '✓' },
      ],
    },
    {
      heading: 'Telephony',
      items: [
        { id: 'workflows', href: '#/workflows', label: 'Workflows', icon: '◇' },
        { id: 'numbers',   href: '#/numbers',   label: 'Numbers',   icon: '#' },
        { id: 'agent-test',href: '#/agent-test',label: 'Test agent',icon: '☎' },
      ],
    },
  ];

  sidebar.innerHTML = '';
  const nav = document.createElement('nav');
  nav.className = 'sidebar-nav';
  for (const group of items) {
    if (group.heading) {
      const h = document.createElement('div');
      h.className = 'sidebar-group-label';
      h.textContent = group.heading;
      nav.append(h);
    }
    for (const it of group.items) {
      const a = document.createElement('a');
      a.href = it.href;
      a.className = 'sidebar-link';
      a.dataset.navLink = it.href.replace(/^#/, '');
      a.innerHTML = `<span class="sidebar-icon" aria-hidden="true">${it.icon || ''}</span><span class="sidebar-label">${it.label}</span>` + (it.badge != null ? `<span class="sidebar-badge">${it.badge}</span>` : '');
      nav.append(a);
    }
  }
  sidebar.append(nav);

  // Theme toggle pinned at the bottom
  const themeBtn = createButton({
    variant: 'ghost', size: 'sm', fullWidth: true,
    'aria-label': 'Toggle theme',
    onClick: () => setTheme(themeStore.get().theme === 'dark' ? 'light' : 'dark'),
    children: (themeStore.get().theme === 'dark' ? '☾ Dark' : '☀ Light'),
  });
  themeBtn.style.marginTop = 'auto';
  sidebar.append(themeBtn);
}

function buildTopbar() {
  topbar.innerHTML = '';
  const title = document.createElement('div');
  title.className = 'topbar-title';
  title.textContent = 'agentops';
  topbar.append(title);
  const spacer = document.createElement('div');
  spacer.className = 'topbar-spacer';
  topbar.append(spacer);

  const user = tokenStore.get().user;
  if (user) {
    topbar.append(createAvatar({ name: user.name || user.email || '?', size: 32, status: 'online' }));
    const out = createButton({ variant: 'ghost', size: 'sm', children: 'Sign out', onClick: () => {
      logout();
      window.location.hash = '#/login';
      window.location.reload();
    }});
    topbar.append(out);
  } else {
    const login = createButton({ variant: 'primary', size: 'sm', children: 'Sign in', onClick: () => { window.location.hash = '#/login'; }});
    topbar.append(login);
  }
}

/* === Routing ===================================================== */
const routes = {
  '/':             () => mountStubScreen(main, { title: 'Overview', sub: 'Real-time view of your operations', legacyTab: 'dashboard' }),
  '/dialer':       () => mountDialerScreen(main),
  '/calls':        () => mountStubScreen(main, { title: 'Calls', sub: 'Call history', legacyTab: 'history' }),
  '/messages':     () => mountStubScreen(main, { title: 'Messages', sub: 'SMS & WhatsApp threads', legacyTab: 'inbox' }),
  '/voicemail':    () => mountVoicemailScreen(main),
  '/recordings':   () => mountRecordingsScreen(main),
  '/campaigns':    () => mountStubScreen(main, { title: 'Campaigns', sub: 'Outbound campaigns with AI', legacyTab: 'campaigns' }),
  '/power-dialer': () => mountPowerDialerScreen(main),
  '/contacts':     () => mountStubScreen(main, { title: 'Contacts', sub: 'Your address book', legacyTab: 'contacts' }),
  '/tenants':      () => mountTenantsScreen(main),
  '/settings':     () => mountStubScreen(main, { title: 'Settings', sub: 'Theme, account, integrations', legacyTab: 'admin' }),
  '/agents':       () => mountStubScreen(main, { title: 'Agents', sub: 'AI agent fleet', legacyTab: 'agents' }),
  '/workflows':    () => mountWorkflowsScreen(main),
  '/assistants':   () => mountAssistantsScreen(main),
  '/voice-lab':    () => mountVoiceLabScreen(main),
  '/agent-test':   () => mountAgentTestScreen(main),
  '/numbers':      () => mountNumbersScreen(main),
  '/analytics':    () => mountAnalyticsScreen(main),
  '/billing':      () => mountBillingScreen(main),
  '/audit':        () => mountAuditScreen(main),
  '/login':        () => mountLoginScreen(main),
  '/signup':       () => mountLoginScreen(main),
  '/forgot':       () => mountLoginScreen(main),
  '*':             () => {
    main.innerHTML = '';
    main.append(Object.assign(document.createElement('div'), { className: 'empty-state' }));
  },
};

// Auth-gated routes
const protectedPaths = ['/', '/dialer', '/calls', '/messages', '/voicemail', '/recordings', '/campaigns', '/power-dialer', '/contacts', '/tenants', '/settings', '/agents', '/workflows', '/assistants', '/voice-lab', '/agent-test', '/numbers'];
const router = createRouter(routes, {
  onChange: ({ path }) => {
    // mark active link
    document.querySelectorAll('.sidebar-link').forEach(l => {
      l.classList.toggle('is-active', l.dataset.navLink === path);
    });
    // auth gate
    if (protectedPaths.includes(path) && !isAuthed()) {
      // Demo: allow the user to opt in via demo creds
      window.location.hash = '#/login';
    }
  }
});

/* === Boot ======================================================== */
buildSidebar();
buildTopbar();
router.start();

// Re-render shell on theme change (so the toggle label updates)
themeStore.subscribe(() => {
  buildSidebar();
});

// expose for debug
window.agentops = { router, themeStore, tokenStore };
