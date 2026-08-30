/* =====================================================================
 * agentops/screens/stub.js
 * Lightweight stub screens for calls, messages, contacts, campaigns, etc.
 * The v5.0 monolithic PWA is still the source of truth for these screens
 * during the Vite migration. The stub shows demo content so the
 * screenshots reflect the new design system.
 * ===================================================================== */

import { h, formatDate, formatDuration } from '../lib/dom.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createAvatar } from '../ui/avatar.js';

const DEMO = {
  Calls: [
    { id: 'c1', direction: 'inbound',  from: '+1 (415) 555-0192', to: '+1 (507) 873-1084', duration: 312, when: '2h ago',    status: 'completed' },
    { id: 'c2', direction: 'outbound', from: '+1 (507) 873-1084', to: '+1 (628) 555-0144', duration: 145, when: '26h ago',   status: 'completed' },
    { id: 'c3', direction: 'inbound',  from: '+1 (212) 555-0100', to: '+1 (507) 873-1084', duration: 0,   when: 'yesterday', status: 'missed' },
    { id: 'c4', direction: 'outbound', from: '+1 (507) 873-1084', to: '+1 (310) 555-0100', duration: 88,  when: '2d ago',    status: 'voicemail' },
  ],
  Messages: [
    { id: 'm1', from: '+1 (415) 555-0192', preview: "Sounds good — let's lock it in for Tuesday 3pm PT.", when: '12m ago', unread: 2 },
    { id: 'm2', from: 'Acme Corp',         preview: 'Your invoice for August is ready to view.',         when: '2h ago',  unread: 0 },
    { id: 'm3', from: '+1 (628) 555-0144', preview: 'Got the files, will review tonight.',                when: '4h ago',  unread: 1 },
  ],
  Contacts: [
    { id: 'c1', name: 'Sarah Chen',  phone: '+14155550192', company: 'Acme Corp' },
    { id: 'c2', name: 'Marcus Lee',  phone: '+16285550144', company: 'BoldBusiness' },
    { id: 'c3', name: 'Priya Iyer',  phone: '+15078731084', company: 'agentops' },
    { id: 'c4', name: 'David Walsh', phone: '+13105550100', company: 'Walsh & Co.' },
  ],
  Campaigns: [
    { id: 'cp1', name: 'Q3 Outreach',    status: 'running',   progress: 64, contacts: 50, sent: 32 },
    { id: 'cp2', name: 'Acme Follow-up', status: 'completed', progress: 100, contacts: 12, sent: 12 },
    { id: 'cp3', name: 'Cold Lead',      status: 'paused',   progress: 38, contacts: 80, sent: 30 },
  ],
  Settings: [
    { id: 's1', label: 'Theme',         value: 'Dark (system default)' },
    { id: 's2', label: 'Default caller',value: '+1 (507) 873-1084 — Work' },
    { id: 's3', label: 'Recording',     value: 'Enabled' },
    { id: 's4', label: 'Transcription', value: 'Enabled' },
    { id: 's5', label: 'Voicemail',     value: 'AI-answered' },
  ],
};

export function mountStubScreen(root, { title, sub, legacyTab }) {
  root.innerHTML = '';
  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, title),
      h('p', { class: 'page-sub' }, sub)
    )
  ));

  // Card with demo content
  const card = h('div', { class: 'card' });
  card.append(h('div', { class: 'card-head' },
    h('div', {}, h('h3', {}, title)),
    h('span', { class: 'badge badge-info' }, 'Live demo data')
  ));
  const body = h('div', { class: 'card-body', style: 'padding: 0;' });
  body.append(renderTable(title));
  card.append(body);
  root.append(card);

  // Migration note
  const note = h('div', { class: 'card', style: 'margin-top: var(--space-4);' });
  note.append(h('div', { class: 'card-body', style: 'display:flex; align-items:center; justify-content:space-between; gap: var(--space-3);' },
    h('div', {},
      h('p', { style: 'margin: 0; font-size: var(--text-sm); color: var(--color-fg-2);' },
        'The v5.0 monolithic PWA fully implements this screen. The new Vite-served view keeps the legacy tab mounted in the background so existing flows continue to work during the migration.'
      )
    ),
    createButton({
      variant: 'ghost', size: 'sm',
      onClick: () => { if (typeof window.switchTab === 'function') window.switchTab(legacyTab); },
      children: 'Open legacy view →',
    })
  ));
  root.append(note);
}

function renderTable(title) {
  const list = DEMO[title];
  if (!list) return h('div', { style: 'padding: var(--space-5);' }, 'No demo data.');

  if (title === 'Calls' || title === 'Messages') return renderSimple(list, title);
  if (title === 'Contacts') return renderContacts(list);
  if (title === 'Campaigns') return renderCampaigns(list);
  if (title === 'Settings') return renderSettings(list);
  return renderSimple(list, title);
}

function renderSimple(list, title) {
  return h('div', {},
    ...list.map(item => h('div', { class: 'rec-row', style: 'border-radius: 0; border-left: 0; border-right: 0; margin: 0;' },
      createAvatar({ name: item.from || item.name || '?', size: 32 }),
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name' }, title === 'Calls' ? `${item.from} → ${item.to}` : item.from),
        h('div', { class: 'rec-row-num' },
          title === 'Calls'
            ? `${item.when} · ${item.status}${item.duration ? ' · ' + formatDuration(item.duration) : ''}`
            : item.preview
        )
      ),
      title === 'Calls' ? createBadge({ variant: item.status === 'missed' ? 'danger' : item.status === 'voicemail' ? 'warning' : 'success', children: item.status })
        : createBadge({ variant: item.unread > 0 ? 'accent' : 'neutral', children: item.unread > 0 ? `${item.unread} new` : 'Read' })
    ))
  );
}

function renderContacts(list) {
  return h('div', {},
    ...list.map(c => h('div', { class: 'rec-row', style: 'border-radius: 0; border-left: 0; border-right: 0; margin: 0;' },
      createAvatar({ name: c.name, size: 36 }),
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name' }, c.name),
        h('div', { class: 'rec-row-num mono' }, c.phone)
      ),
      h('div', {}, createBadge({ variant: 'neutral', children: c.company }))
    ))
  );
}

function renderCampaigns(list) {
  return h('div', {},
    ...list.map(c => h('div', { class: 'rec-row', style: 'border-radius: 0; border-left: 0; border-right: 0; margin: 0; align-items: center;' },
      h('div', { class: 'rec-row-meta' },
        h('div', { class: 'rec-row-name' }, c.name),
        h('div', { class: 'rec-row-num' }, `${c.contacts} contacts · ${c.sent} sent`)
      ),
      h('div', { style: 'flex: 0 0 200px;' },
        h('div', { class: 'pd-meter' },
          h('div', { class: 'pd-meter-fill', style: `width: ${c.progress}%;` })
        )
      ),
      h('div', {}, createBadge({ variant: c.status === 'running' ? 'info' : c.status === 'paused' ? 'warning' : 'success', dot: true, children: c.status }))
    ))
  );
}

function renderSettings(list) {
  return h('div', { style: 'padding: var(--space-3) var(--space-5);' },
    h('div', { class: 'grid-2', style: 'gap: var(--space-3);' },
      ...list.map(s => h('div', { style: 'display:flex; justify-content:space-between; padding: var(--space-3) 0; border-bottom: 1px solid var(--color-line);' },
        h('span', { style: 'color: var(--color-fg-2);' }, s.label),
        h('span', { style: 'color: var(--color-fg-0); font-weight: var(--weight-medium);' }, s.value)
      ))
    )
  );
}
