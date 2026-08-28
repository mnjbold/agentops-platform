/* ============================================================================
 * agentops softphone — Campaigns & Missions tab
 * v0.4.0 — SaaS feature pack: manual SMS, scheduled SMS, mass SMS, mission
 * tracker, campaign wizard. Self-contained: injects its own tab + panel, does
 * not require changes to index.html beyond a <script src="campaigns.js" defer>
 * tag and a version bump.
 *
 * API: https://bk-jr-api.aixlabs.fun (X-Tenant-Id header, default "default")
 * ============================================================================
 */
(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // API wrapper
  // -------------------------------------------------------------------------
  const API = (() => {
    const base = 'https://bk-jr-api.aixlabs.fun';
    const tenantKey = 'agentops.tenantId';
    const getTenant = () => localStorage.getItem(tenantKey) || 'default';
    async function req(path, opts) {
      opts = opts || {};
      const r = await fetch(base + path, {
        method: opts.method || 'GET',
        headers: Object.assign(
          { 'Content-Type': 'application/json', 'X-Tenant-Id': getTenant() },
          opts.headers || {}
        ),
        body: opts.body
      });
      const text = await r.text();
      let data;
      try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { raw: text }; }
      if (!r.ok) {
        const err = new Error('API ' + r.status + ': ' + (data.detail || data.message || text || r.statusText));
        err.status = r.status;
        err.data = data;
        throw err;
      }
      return data;
    }
    return {
      tenantId: getTenant,
      setTenant: (id) => { localStorage.setItem(tenantKey, id || 'default'); },
      contacts: {
        list:   ()                       => req('/api/contacts'),
        create: (b)                      => req('/api/contacts', { method: 'POST', body: JSON.stringify(b) }),
        update: (id, b)                  => req('/api/contacts/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify(b) }),
        remove: (id)                     => req('/api/contacts/' + encodeURIComponent(id), { method: 'DELETE' }),
      },
      campaigns: {
        list:    ()                      => req('/api/campaigns'),
        get:     (id)                    => req('/api/campaigns/' + encodeURIComponent(id)),
        create:  (b)                     => req('/api/campaigns', { method: 'POST', body: JSON.stringify(b) }),
        update:  (id, b)                 => req('/api/campaigns/' + encodeURIComponent(id), { method: 'PATCH', body: JSON.stringify(b) }),
        remove:  (id)                    => req('/api/campaigns/' + encodeURIComponent(id), { method: 'DELETE' }),
        launch:  (id)                    => req('/api/campaigns/' + encodeURIComponent(id) + '/launch',  { method: 'POST' }),
        pause:   (id)                    => req('/api/campaigns/' + encodeURIComponent(id) + '/pause',   { method: 'POST' }),
        resume:  (id)                    => req('/api/campaigns/' + encodeURIComponent(id) + '/resume',  { method: 'POST' }),
        status:  (id)                    => req('/api/campaigns/' + encodeURIComponent(id) + '/status'),
      },
      sms: {
        send:           (b)              => req('/api/sms/send',          { method: 'POST', body: JSON.stringify(b) }),
        schedule:       (b)              => req('/api/sms/schedule',      { method: 'POST', body: JSON.stringify(b) }),
        scheduled:      ()               => req('/api/sms/scheduled'),
        cancelScheduled:(id)             => req('/api/sms/scheduled/' + encodeURIComponent(id), { method: 'DELETE' }),
        broadcast:      (b)              => req('/api/sms/broadcast',     { method: 'POST', body: JSON.stringify(b) }),
      },
      powerDialer: {
        start:  (b)                      => req('/api/calls/power-dialer/start', { method: 'POST', body: JSON.stringify(b) }),
        status: (sid)                    => req('/api/calls/power-dialer/' + encodeURIComponent(sid) + '/status'),
      },
    };
  })();

  // -------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------
  const state = {
    campaigns: [],
    contacts: [],
    scheduled: [],
    activeDetail: null,    // campaign id currently being polled
    detailPollHandle: null,
    bootRetries: 0,
  };

  // -------------------------------------------------------------------------
  // Tiny DOM helpers
  // -------------------------------------------------------------------------
  function el(tag, attrs, kids) {
    const e = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === 'class') e.className = attrs[k];
      else if (k === 'style') e.setAttribute('style', attrs[k]);
      else if (k === 'html') e.innerHTML = attrs[k];
      else if (k.startsWith('on') && typeof attrs[k] === 'function') e.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
      else if (attrs[k] === true) e.setAttribute(k, '');
      else if (attrs[k] !== false && attrs[k] != null) e.setAttribute(k, attrs[k]);
    }
    if (kids) {
      (Array.isArray(kids) ? kids : [kids]).forEach(c => {
        if (c == null) return;
        e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
    }
    return e;
  }
  function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
  // Backend wraps lists as {campaigns:[]}, {contacts:[]}, {scheduled:[]},
  // or returns bare arrays. Accept any of them.
  function unwrapList(data, keys) {
    if (Array.isArray(data)) return data;
    if (data && typeof data === 'object') {
      for (const k of keys) {
        if (Array.isArray(data[k])) return data[k];
        if (data[k] && typeof data[k] === 'object' && Array.isArray(data[k].items)) return data[k].items;
      }
    }
    return [];
  }
  // Some endpoints use "type" for the campaign kind, some use "kind". Accept either.
  function campaignKind(c) { return c.kind || c.type || 'sms'; }
  function fmtPhone(s) {
    if (!s) return '';
    const d = String(s).replace(/[^\d+]/g, '');
    if (d.length === 11 && d[0] === '1') return '+1 (' + d.slice(1,4) + ') ' + d.slice(4,7) + '-' + d.slice(7);
    if (d.length === 10) return '(' + d.slice(0,3) + ') ' + d.slice(3,6) + '-' + d.slice(6);
    return s;
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString();
  }
  function relTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso).getTime();
    const s = Math.floor((Date.now() - d) / 1000);
    if (s < 0) return 'in ' + relAbs(Math.abs(s));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    return Math.floor(s / 86400) + 'd ago';
  }
  function relAbs(s) {
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }
  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // -------------------------------------------------------------------------
  // Tab + panel injection
  // -------------------------------------------------------------------------
  function injectTab() {
    // Anchor: the existing "admin" tab button. We insert the campaigns tab
    // immediately after it so the order is: dialer, messages, history,
    // recordings, admin, campaigns.
    const adminTab = document.querySelector('[data-tab="admin"]');
    if (!adminTab) return false;

    // 1. Tab button. Reuse the same classes as the existing tab buttons so it
    //    sits in the tab bar identically. No inline onclick — we wire our own
    //    handler so the existing switchTab() stays untouched.
    const tab = el('div', {
      class: 'tab',
      'data-tab': 'campaigns',
      role: 'tab',
    });
    tab.innerHTML = '📣 Campaigns<span class="tab-badge hidden" id="campaigns-badge">0</span>';
    tab.addEventListener('click', () => switchToCampaigns());
    adminTab.parentElement.insertBefore(tab, adminTab.nextSibling);

    // 2. Tab panel. We follow the existing id="tab-<name>" + class="tab-pane"
    //    convention so the global switchTab() correctly toggles visibility
    //    (the new tab supports a self-contained flow but degrades gracefully
    //    if anyone calls switchTab('campaigns') directly).
    const adminPanel = document.getElementById('tab-admin');
    if (!adminPanel) return false;
    const panel = el('section', {
      id: 'tab-campaigns',
      class: 'tab-pane hidden',
    });
    panel.innerHTML = `
      <div class="space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 class="font-semibold">Campaigns &amp; Missions</h3>
            <p class="text-xs text-gray-500">Outbound SMS, mass broadcasts, scheduled sends, and live mission tracking.</p>
          </div>
          <div class="flex flex-wrap gap-2">
            <button class="btn btn-ghost" data-cmp-action="scheduled">⏰ Scheduled</button>
            <button class="btn btn-ghost" data-cmp-action="refresh">↻ Refresh</button>
            <button class="btn btn-primary" data-cmp-action="new-sms">+ Manual SMS</button>
            <button class="btn btn-primary" data-cmp-action="new-broadcast" style="background:var(--accent2);">+ Mass SMS</button>
            <button class="btn btn-primary" data-cmp-action="new-campaign">+ New Campaign</button>
          </div>
        </div>

        <div id="cmp-status" class="text-xs text-gray-500 mono">Loading campaigns…</div>

        <div id="cmp-list" class="panel p-0 overflow-hidden"></div>

        <div id="cmp-detail" class="hidden"></div>
      </div>
    `;
    adminPanel.parentElement.insertBefore(panel, adminPanel.nextSibling);

    return true;
  }

  function switchToCampaigns() {
    // Replicate the existing switchTab() behaviour but trigger our loader.
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('tab-active', t.dataset.tab === 'campaigns'));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.add('hidden'));
    const panel = document.getElementById('tab-campaigns');
    if (panel) panel.classList.remove('hidden');
    loadCampaigns();
  }

  // -------------------------------------------------------------------------
  // Campaign list (board)
  // -------------------------------------------------------------------------
  async function loadCampaigns() {
    const listEl = document.getElementById('cmp-list');
    const statusEl = document.getElementById('cmp-status');
    if (!listEl) return;
    if (statusEl) statusEl.textContent = 'Loading campaigns…';

    // Load campaigns + scheduled SMS in parallel. Contacts are lazy (only
    // needed when the user opens a modal that picks them).
    let campaigns = [], scheduled = [];
    try {
      [campaigns, scheduled] = await Promise.all([
        API.campaigns.list().catch(err => { console.warn('campaigns.list failed', err); return []; }),
        API.sms.scheduled().catch(err => { console.warn('sms.scheduled failed', err); return []; }),
      ]);
    } catch (e) {
      if (statusEl) statusEl.textContent = 'Backend not ready — retrying in 3s…';
      setTimeout(loadCampaigns, 3000);
      return;
    }
    state.campaigns = unwrapList(campaigns, ['campaigns', 'items']);
    state.scheduled = unwrapList(scheduled, ['scheduled', 'items']);

    // Also pick up any future contact cache fetch opportunistically. The list
    // page doesn't need it but the modals will. This warms the cache.
    API.contacts.list()
      .then(c => { state.contacts = unwrapList(c, ['contacts', 'items']); })
      .catch(err => { console.warn('contacts.list failed', err); state.contacts = []; });

    renderCampaignsList();
    if (statusEl) {
      const n = state.campaigns.length;
      statusEl.textContent = n === 0
        ? 'No campaigns yet — click + New Campaign, + Mass SMS, or + Manual SMS to start.'
        : n + ' campaign' + (n === 1 ? '' : 's') + ' loaded · ' + state.scheduled.length + ' scheduled SMS pending';
    }
    updateCampaignsBadge();
  }

  function renderCampaignsList() {
    const root = document.getElementById('cmp-list');
    clear(root);
    if (state.campaigns.length === 0) {
      root.appendChild(el('div', { class: 'p-8 text-center text-sm text-gray-500' },
        'No campaigns yet. Click a button above to create one.'));
      return;
    }

    // Card-per-row board
    const wrap = el('div', { class: 'divide-y divide-gray-800' });
    state.campaigns.forEach(c => wrap.appendChild(renderCampaignRow(c)));
    root.appendChild(wrap);
  }

  function renderCampaignRow(c) {
    const status = (c.status || 'draft').toLowerCase();
    const pillClass =
      status === 'running'   ? 'pill pill-on'   :
      status === 'completed' ? 'pill pill-on'   :
      status === 'paused'    ? 'pill pill-warn' :
      status === 'failed'    ? 'pill pill-off'  :
                               'pill pill-off';
    const typeClass = campaignKind(c) === 'sms' ? 'pill pill-on' : (campaignKind(c) === 'call' ? 'pill pill-warn' : 'pill pill-off');

    const total = c.total || c.contact_ids?.length || 0;
    const sent  = c.sent  || c.stats?.sent   || 0;
    const failed = c.failed || c.stats?.failed || 0;
    const pct = total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0;

    const actions = el('div', { class: 'flex flex-wrap gap-2' });
    actions.appendChild(el('button', { class: 'btn btn-ghost text-xs', 'data-cmp-action': 'open', 'data-cmp-id': c.id }, 'Open'));
    if (status === 'draft') {
      actions.appendChild(el('button', { class: 'btn btn-primary text-xs', 'data-cmp-action': 'launch', 'data-cmp-id': c.id }, 'Launch'));
    } else if (status === 'running') {
      actions.appendChild(el('button', { class: 'btn btn-ghost text-xs', 'data-cmp-action': 'pause', 'data-cmp-id': c.id }, 'Pause'));
    } else if (status === 'paused') {
      actions.appendChild(el('button', { class: 'btn btn-primary text-xs', 'data-cmp-action': 'resume', 'data-cmp-id': c.id }, 'Resume'));
    }
    actions.appendChild(el('button', { class: 'btn btn-ghost text-xs', 'data-cmp-action': 'delete', 'data-cmp-id': c.id }, '🗑'));

    return el('div', { class: 'p-4 flex flex-col md:flex-row md:items-center gap-3' }, [
      el('div', { class: 'flex-1 min-w-0' }, [
        el('div', { class: 'flex flex-wrap items-center gap-2' }, [
          el('div', { class: 'font-medium truncate' }, c.name || c.id),
          el('span', { class: typeClass }, campaignKind(c).toUpperCase()),
          el('span', { class: pillClass }, status.toUpperCase()),
          c.schedule_at ? el('span', { class: 'text-xs text-gray-500 mono' }, '· ⏰ ' + fmtDate(c.schedule_at)) : null,
        ]),
        el('div', { class: 'mt-2 text-xs text-gray-400 flex flex-wrap gap-x-4 gap-y-1' }, [
          el('span', null, 'Created ' + relTime(c.created_at)),
          el('span', null, 'Updated ' + relTime(c.updated_at)),
          c.message ? el('span', { class: 'truncate max-w-md' }, '"' + (c.message.length > 80 ? c.message.slice(0, 77) + '…' : c.message) + '"') : null,
        ]),
        total > 0 ? el('div', { class: 'mt-2' }, [
          el('div', { class: 'flex justify-between text-xs text-gray-500 mb-1' }, [
            el('span', null, sent + ' / ' + total + ' sent'),
            el('span', null, pct + '%'),
          ]),
          el('div', { class: 'w-full h-2 rounded bg-gray-800 overflow-hidden' }, [
            el('div', { class: 'h-2', style: 'width:' + pct + '%;background:linear-gradient(90deg,var(--accent),var(--accent2));' }),
          ]),
        ]) : null,
      ]),
      actions,
    ]);
  }

  function updateCampaignsBadge() {
    const badge = document.getElementById('campaigns-badge');
    if (!badge) return;
    const running = state.campaigns.filter(c => (c.status || '').toLowerCase() === 'running').length;
    if (running > 0) {
      badge.textContent = String(running);
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  }

  // -------------------------------------------------------------------------
  // Campaign detail / mission tracker
  // -------------------------------------------------------------------------
  function openCampaignDetail(id) {
    state.activeDetail = id;
    const detailEl = document.getElementById('cmp-detail');
    if (!detailEl) return;
    detailEl.classList.remove('hidden');
    detailEl.scrollIntoView({ behavior: 'smooth', block: 'start' });

    // Render skeleton
    detailEl.innerHTML = `
      <div class="panel p-5">
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-semibold">Mission tracker</h3>
          <button class="btn btn-ghost text-xs" data-cmp-action="close-detail">✕ Close</button>
        </div>
        <div id="cmp-detail-body" class="text-sm text-gray-400">Loading…</div>
      </div>
    `;

    pollDetail();
    if (state.detailPollHandle) clearInterval(state.detailPollHandle);
    state.detailPollHandle = setInterval(pollDetail, 3000);
  }

  function closeCampaignDetail() {
    state.activeDetail = null;
    if (state.detailPollHandle) { clearInterval(state.detailPollHandle); state.detailPollHandle = null; }
    const detailEl = document.getElementById('cmp-detail');
    if (detailEl) {
      detailEl.classList.add('hidden');
      clear(detailEl);
    }
  }

  async function pollDetail() {
    const id = state.activeDetail;
    if (!id) return;
    const body = document.getElementById('cmp-detail-body');
    if (!body) return;
    try {
      const detail = await API.campaigns.get(id);
      const status = (detail.status || detail.state || 'draft').toLowerCase();
      const statusPill = status === 'running' ? 'pill pill-on' : status === 'paused' ? 'pill pill-warn' : status === 'completed' ? 'pill pill-on' : 'pill pill-off';

      const total = detail.total || detail.contact_ids?.length || 0;
      const sent  = detail.sent  || detail.stats?.sent  || 0;
      const failed = detail.failed || detail.stats?.failed || 0;
      const pending = Math.max(0, total - sent - failed);
      const pct = total > 0 ? Math.min(100, Math.round((sent / total) * 100)) : 0;

      // Per-contact table (live snapshot)
      const perContact = detail.contacts || detail.recipients || [];
      const contactRows = perContact.length === 0
        ? '<tr><td colspan="4" class="text-xs text-gray-500">No contact-level data returned by backend yet.</td></tr>'
        : perContact.map(r => {
            const rs = (r.status || 'pending').toLowerCase();
            const rsPill = rs === 'sent' || rs === 'delivered' ? 'pill pill-on'
                         : rs === 'failed' ? 'pill pill-off'
                         : rs === 'sending' ? 'pill pill-warn'
                         : 'pill';
            return `<tr>
              <td>${escapeHtml(r.name || r.phone || r.to || '—')}</td>
              <td class="mono text-xs text-gray-500">${escapeHtml(fmtPhone(r.phone || r.to || ''))}</td>
              <td><span class="${rsPill}">${escapeHtml(rs.toUpperCase())}</span></td>
              <td class="text-xs text-gray-500 mono">${escapeHtml(r.sent_at ? fmtDate(r.sent_at) : '—')}</td>
            </tr>`;
          }).join('');

      body.innerHTML = `
        <div class="flex flex-wrap items-center gap-2 mb-3">
          <div class="font-medium text-base">${escapeHtml(detail.name || id)}</div>
          <span class="pill">${escapeHtml(campaignKind(detail).toUpperCase())}</span>
          <span class="${statusPill}">${escapeHtml(status.toUpperCase())}</span>
          <span class="text-xs text-gray-500">updated ${relTime(detail.updated_at)}</span>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-3">
          <div class="panel-2 p-3"><div class="text-xs text-gray-500">Total</div><div class="stat-num">${total}</div></div>
          <div class="panel-2 p-3"><div class="text-xs text-gray-500">Sent</div><div class="stat-num" style="color:var(--accent);">${sent}</div></div>
          <div class="panel-2 p-3"><div class="text-xs text-gray-500">Pending</div><div class="stat-num" style="color:var(--warn);">${pending}</div></div>
          <div class="panel-2 p-3"><div class="text-xs text-gray-500">Failed</div><div class="stat-num" style="color:var(--danger);">${failed}</div></div>
        </div>
        <div class="mb-3">
          <div class="flex justify-between text-xs text-gray-500 mb-1"><span>${sent} / ${total} sent</span><span>${pct}%</span></div>
          <div class="w-full h-2 rounded bg-gray-800 overflow-hidden">
            <div class="h-2" style="width:${pct}%;background:linear-gradient(90deg,var(--accent),var(--accent2));"></div>
          </div>
        </div>
        <div class="flex flex-wrap gap-2 mb-4">
          ${status === 'draft' ? `<button class="btn btn-primary text-xs" data-cmp-action="launch" data-cmp-id="${escapeHtml(id)}">Launch</button>` : ''}
          ${status === 'running' ? `<button class="btn btn-ghost text-xs" data-cmp-action="pause" data-cmp-id="${escapeHtml(id)}">Pause</button>` : ''}
          ${status === 'paused' ? `<button class="btn btn-primary text-xs" data-cmp-action="resume" data-cmp-id="${escapeHtml(id)}">Resume</button>` : ''}
          <button class="btn btn-ghost text-xs" data-cmp-action="delete" data-cmp-id="${escapeHtml(id)}">Delete campaign</button>
        </div>
        ${detail.message ? `<div class="panel-2 p-3 mb-3"><div class="text-xs text-gray-500 mb-1">Message</div><div class="text-sm">${escapeHtml(detail.message)}</div></div>` : ''}
        <div class="panel-2 p-0 overflow-hidden">
          <div class="p-3 border-b border-gray-800 text-xs uppercase text-gray-500 tracking-wide">Recipients</div>
          <div class="scroll-y" style="max-height: 320px;">
            <table>
              <thead><tr><th>Name</th><th>Phone</th><th>Status</th><th>Sent at</th></tr></thead>
              <tbody>${contactRows}</tbody>
            </table>
          </div>
        </div>
        <div class="text-[10px] text-gray-500 mt-2 mono">polling /api/campaigns/${escapeHtml(id)} every 3s</div>
      `;
    } catch (e) {
      body.innerHTML = '<div class="text-sm text-red-400">Mission status unavailable: ' + escapeHtml(e.message) + '</div>';
    }
  }

  // -------------------------------------------------------------------------
  // Create campaign wizard
  // -------------------------------------------------------------------------
  function openCreateWizard() {
    // 4-step wizard. We do it as one long form with a stepper header for
    // visual feedback. Step 1: name + kind. Step 2: contact picker.
    // Step 3: message / script. Step 4: schedule + launch.
    const modal = showModal('New Campaign', `
      <div class="space-y-4" id="cmp-wizard">
        <div class="flex gap-1 text-xs mono">
          <span class="cmp-step-pill" data-step="1" style="background:var(--accent);color:#001a14;padding:2px 8px;border-radius:999px;">1. Setup</span>
          <span class="text-gray-600">›</span>
          <span class="cmp-step-pill" data-step="2" style="background:var(--panel2);color:var(--muted);padding:2px 8px;border-radius:999px;">2. Audience</span>
          <span class="text-gray-600">›</span>
          <span class="cmp-step-pill" data-step="3" style="background:var(--panel2);color:var(--muted);padding:2px 8px;border-radius:999px;">3. Message</span>
          <span class="text-gray-600">›</span>
          <span class="cmp-step-pill" data-step="4" style="background:var(--panel2);color:var(--muted);padding:2px 8px;border-radius:999px;">4. Schedule</span>
        </div>

        <div data-wizard-step="1" class="space-y-3">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">Campaign name</label>
            <input type="text" id="cmp-w-name" placeholder="e.g. November reactivation blast" />
          </div>
          <div>
            <label class="text-xs text-gray-400 mb-1 block">Type</label>
            <div class="flex gap-2">
              <label class="flex items-center gap-2 px-3 py-2 panel-2 cursor-pointer" style="flex:1;">
                <input type="radio" name="cmp-w-kind" value="sms" checked /> 💬 SMS blast
              </label>
              <label class="flex items-center gap-2 px-3 py-2 panel-2 cursor-pointer" style="flex:1;">
                <input type="radio" name="cmp-w-kind" value="call" /> 📞 Power dialer
              </label>
            </div>
          </div>
        </div>

        <div data-wizard-step="2" class="space-y-3 hidden">
          <div class="flex items-center justify-between">
            <label class="text-xs text-gray-400">Pick recipients</label>
            <div class="flex gap-2">
              <button type="button" class="btn btn-ghost text-xs" data-cmp-action="wiz-pick-all">Select all</button>
              <button type="button" class="btn btn-ghost text-xs" data-cmp-action="wiz-pick-none">Clear</button>
              <button type="button" class="btn btn-ghost text-xs" data-cmp-action="wiz-new-contact">+ Contact</button>
            </div>
          </div>
          <div id="cmp-w-contacts" class="panel-2 p-2 space-y-1 scroll-y" style="max-height:280px;">
            <div class="text-xs text-gray-500 p-2">Loading contacts…</div>
          </div>
          <div class="text-xs text-gray-500"><span id="cmp-w-contact-count">0</span> selected</div>
        </div>

        <div data-wizard-step="3" class="space-y-3 hidden">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">Message</label>
            <textarea id="cmp-w-message" rows="6" placeholder="Hi {{name}}, this is agentops…"></textarea>
            <div class="flex justify-between text-xs text-gray-500 mt-1">
              <span>Use <code class="mono">{{name}}</code> / <code class="mono">{{phone}}</code> for merge tags.</span>
              <span><span id="cmp-w-charcount">0</span> / 1600</span>
            </div>
          </div>
        </div>

        <div data-wizard-step="4" class="space-y-3 hidden">
          <div>
            <label class="text-xs text-gray-400 mb-1 block">When to send</label>
            <div class="flex gap-2 mb-2">
              <label class="flex items-center gap-2 px-3 py-2 panel-2 cursor-pointer" style="flex:1;">
                <input type="radio" name="cmp-w-when" value="now" checked /> ▶ Launch now
              </label>
              <label class="flex items-center gap-2 px-3 py-2 panel-2 cursor-pointer" style="flex:1;">
                <input type="radio" name="cmp-w-when" value="later" /> ⏰ Schedule for later
              </label>
            </div>
            <input type="datetime-local" id="cmp-w-when" class="hidden" />
          </div>
          <div class="panel-2 p-3 text-xs text-gray-400">
            <div class="text-gray-500 mb-1 uppercase tracking-wide text-[10px]">Summary</div>
            <div id="cmp-w-summary">—</div>
          </div>
        </div>

        <div class="flex justify-between pt-2 border-t border-gray-800">
          <button type="button" class="btn btn-ghost" data-cmp-action="wiz-back">Back</button>
          <div class="flex gap-2">
            <button type="button" class="btn btn-ghost" data-cmp-action="wiz-cancel">Cancel</button>
            <button type="button" class="btn btn-primary" data-cmp-action="wiz-next">Next ›</button>
          </div>
        </div>
      </div>
    `, (root) => {
      // Lazy-load contacts and render the picker
      const list = root.querySelector('#cmp-w-contacts');
      const refreshCount = () => {
        const n = list ? list.querySelectorAll('input[type=checkbox]:checked').length : 0;
        const c = root.querySelector('#cmp-w-contact-count');
        if (c) c.textContent = String(n);
      };
      const renderContacts = (contacts) => {
        if (!list) return;
        if (!contacts.length) {
          list.innerHTML = '<div class="text-xs text-gray-500 p-3">No contacts yet. Click + Contact to add one.</div>';
          return;
        }
        list.innerHTML = contacts.map(c => `
          <label class="flex items-center gap-2 p-2 hover:bg-gray-800 rounded cursor-pointer">
            <input type="checkbox" value="${escapeHtml(c.id)}" data-phone="${escapeHtml(c.phone || '')}" data-name="${escapeHtml(c.name || '')}" />
            <div class="flex-1 min-w-0">
              <div class="text-sm">${escapeHtml(c.name || '(no name)')}</div>
              <div class="text-xs text-gray-500 mono">${escapeHtml(fmtPhone(c.phone || ''))}</div>
            </div>
          </label>
        `).join('');
        list.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', refreshCount));
      };
      if (state.contacts.length > 0) {
        renderContacts(state.contacts);
      } else {
        API.contacts.list()
          .then(data => { state.contacts = unwrapList(data, ['contacts', 'items']); renderContacts(state.contacts); })
          .catch(err => { list.innerHTML = '<div class="text-xs text-red-400 p-2">Failed to load contacts: ' + escapeHtml(err.message) + '</div>'; });
      }

      // Char counter
      const ta = root.querySelector('#cmp-w-message');
      const cc = root.querySelector('#cmp-w-charcount');
      if (ta && cc) ta.addEventListener('input', () => { cc.textContent = String(ta.value.length); });

      // Schedule datetime toggle
      root.querySelectorAll('input[name="cmp-w-when"]').forEach(r => {
        r.addEventListener('change', () => {
          const when = root.querySelector('#cmp-w-when');
          if (when) when.classList.toggle('hidden', r.value !== 'later' || !r.checked);
        });
      });

      // Wizard navigation
      let step = 1;
      const go = (s) => {
        step = s;
        root.querySelectorAll('[data-wizard-step]').forEach(d => d.classList.toggle('hidden', d.dataset.wizardStep !== String(s)));
        root.querySelectorAll('.cmp-step-pill').forEach(p => {
          const n = Number(p.dataset.step);
          p.style.background = n === s ? 'var(--accent)' : 'var(--panel2)';
          p.style.color = n === s ? '#001a14' : 'var(--muted)';
        });
        // Update summary on step 4
        if (s === 4) updateSummary();
        // Hide "Next" on final step, show "Launch"
        const next = root.querySelector('[data-cmp-action="wiz-next"]');
        if (next) next.textContent = s === 4 ? '🚀 Create & Launch' : 'Next ›';
      };
      const updateSummary = () => {
        const summary = root.querySelector('#cmp-w-summary');
        if (!summary) return;
        const name = root.querySelector('#cmp-w-name').value || '(unnamed)';
        const kind = root.querySelector('input[name="cmp-w-kind"]:checked').value;
        const n = root.querySelectorAll('#cmp-w-contacts input[type=checkbox]:checked').length;
        const when = root.querySelector('input[name="cmp-w-when"]:checked').value;
        const msg = (root.querySelector('#cmp-w-message').value || '').slice(0, 80);
        summary.innerHTML =
          '<div><b>Name:</b> ' + escapeHtml(name) + '</div>' +
          '<div><b>Type:</b> ' + kind.toUpperCase() + '</div>' +
          '<div><b>Recipients:</b> ' + n + '</div>' +
          '<div><b>When:</b> ' + (when === 'now' ? 'Launch immediately' : fmtDate(root.querySelector('#cmp-w-when').value)) + '</div>' +
          '<div class="truncate"><b>Message:</b> "' + escapeHtml(msg) + (msg.length === 80 ? '…' : '') + '"</div>';
      };

      root.querySelector('[data-cmp-action="wiz-next"]').addEventListener('click', async () => {
        if (step === 1) {
          const name = root.querySelector('#cmp-w-name').value.trim();
          if (!name) { toast('Please enter a campaign name', 'warn'); return; }
          go(2);
        } else if (step === 2) {
          const n = root.querySelectorAll('#cmp-w-contacts input[type=checkbox]:checked').length;
          if (n === 0) { toast('Pick at least one recipient', 'warn'); return; }
          go(3);
        } else if (step === 3) {
          const kind = root.querySelector('input[name="cmp-w-kind"]:checked').value;
          const msg = root.querySelector('#cmp-w-message').value.trim();
          if (kind === 'sms' && !msg) { toast('Message is required for SMS campaigns', 'warn'); return; }
          go(4);
        } else {
          // Submit
          const submit = root.querySelector('[data-cmp-action="wiz-next"]');
          submit.disabled = true;
          submit.textContent = 'Creating…';
          try {
            const name = root.querySelector('#cmp-w-name').value.trim();
            const kind = root.querySelector('input[name="cmp-w-kind"]:checked').value;
            const when = root.querySelector('input[name="cmp-w-when"]:checked').value;
            const dt = root.querySelector('#cmp-w-when').value;
            const message = root.querySelector('#cmp-w-message').value;
            const contact_ids = Array.from(root.querySelectorAll('#cmp-w-contacts input[type=checkbox]:checked'))
              .map(cb => cb.value);

            const body = {
              name, kind,
              contact_ids,
              message: kind === 'sms' ? message : null,
              script: kind === 'call' ? message : null,
              schedule_at: when === 'later' && dt ? new Date(dt).toISOString() : null,
            };
            const created = await API.campaigns.create(body);
            const id = created.id || created.campaign_id;
            if (when === 'now' && id) {
              try { await API.campaigns.launch(id); } catch (e) { console.warn('auto-launch failed', e); }
            }
            closeModal();
            toast('Campaign ' + (when === 'now' ? 'launched' : 'created'), 'ok');
            await loadCampaigns();
            if (id && when === 'now') openCampaignDetail(id);
          } catch (e) {
            toast('Failed: ' + e.message, 'err');
            submit.disabled = false;
            submit.textContent = '🚀 Create & Launch';
          }
        }
      });

      root.querySelector('[data-cmp-action="wiz-back"]').addEventListener('click', () => {
        if (step > 1) go(step - 1);
      });
      root.querySelector('[data-cmp-action="wiz-cancel"]').addEventListener('click', () => closeModal());
      root.querySelector('[data-cmp-action="wiz-pick-all"]').addEventListener('click', () => {
        root.querySelectorAll('#cmp-w-contacts input[type=checkbox]').forEach(cb => { cb.checked = true; });
        refreshCount();
      });
      root.querySelector('[data-cmp-action="wiz-pick-none"]').addEventListener('click', () => {
        root.querySelectorAll('#cmp-w-contacts input[type=checkbox]').forEach(cb => { cb.checked = false; });
        refreshCount();
      });
      root.querySelector('[data-cmp-action="wiz-new-contact"]').addEventListener('click', () => openContactCreateModal(refreshCount));
    });
  }

  // -------------------------------------------------------------------------
  // Manual SMS modal
  // -------------------------------------------------------------------------
  function openManualSMS() {
    const modal = showModal('Send Manual SMS', `
      <div class="space-y-3">
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Recipient</label>
          <div class="flex gap-2">
            <input type="text" id="cmp-ms-name" placeholder="Name" style="max-width:160px;" />
            <input type="tel" id="cmp-ms-phone" placeholder="+15551234567" class="flex-1" />
            <button type="button" class="btn btn-ghost text-xs" data-cmp-action="ms-pick">📇 Pick</button>
          </div>
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">From (optional, defaults to +15078731084)</label>
          <input type="tel" id="cmp-ms-from" placeholder="+15078731084" value="+15078731084" />
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Message</label>
          <textarea id="cmp-ms-text" rows="4" placeholder="Type your SMS…"></textarea>
          <div class="flex justify-between text-xs text-gray-500 mt-1">
            <span id="cmp-ms-segs">0 segments</span>
            <span><span id="cmp-ms-charcount">0</span> / 160 (or 153 over 1)</span>
          </div>
        </div>
        <div class="flex justify-between gap-2 pt-2 border-t border-gray-800">
          <label class="text-xs text-gray-400 flex items-center gap-2 cursor-pointer">
            <input type="checkbox" id="cmp-ms-schedule" /> Schedule for later
          </label>
          <div class="flex gap-2">
            <button type="button" class="btn btn-ghost" data-cmp-action="ms-cancel">Cancel</button>
            <button type="button" class="btn btn-ghost" data-cmp-action="ms-schedule">⏰ Schedule</button>
            <button type="button" class="btn btn-primary" data-cmp-action="ms-send">Send now</button>
          </div>
        </div>
        <input type="datetime-local" id="cmp-ms-when" class="hidden" />
      </div>
    `, (root) => {
      const phone = root.querySelector('#cmp-ms-phone');
      const from = root.querySelector('#cmp-ms-from');
      const text = root.querySelector('#cmp-ms-text');
      const segs = root.querySelector('#cmp-ms-segs');
      const cc = root.querySelector('#cmp-ms-charcount');
      const when = root.querySelector('#cmp-ms-when');
      const sched = root.querySelector('#cmp-ms-schedule');

      const updateCount = () => {
        const t = text.value || '';
        cc.textContent = String(t.length);
        const seg = t.length === 0 ? 0 : (t.length <= 160 ? 1 : Math.ceil(t.length / 153));
        segs.textContent = seg + ' segment' + (seg === 1 ? '' : 's');
      };
      text.addEventListener('input', updateCount);
      updateCount();

      sched.addEventListener('change', () => {
        when.classList.toggle('hidden', !sched.checked);
      });

      root.querySelector('[data-cmp-action="ms-pick"]').addEventListener('click', () => {
        openContactPicker((c) => {
          if (c) {
            root.querySelector('#cmp-ms-name').value = c.name || '';
            root.querySelector('#cmp-ms-phone').value = c.phone || '';
          }
        });
      });
      root.querySelector('[data-cmp-action="ms-cancel"]').addEventListener('click', () => closeModal());

      const doSend = async () => {
        const p = phone.value.trim();
        const tx = text.value.trim();
        if (!p) { toast('Phone is required', 'warn'); return; }
        if (!tx) { toast('Message is required', 'warn'); return; }
        const btnSend = root.querySelector('[data-cmp-action="ms-send"]');
        const btnSched = root.querySelector('[data-cmp-action="ms-schedule"]');
        btnSend.disabled = true; btnSched.disabled = true;
        try {
          const body = { to: p, text: tx };
          if (from.value.trim()) body.from = from.value.trim();
          if (sched.checked) {
            if (!when.value) throw new Error('Pick a date/time');
            body.run_at = new Date(when.value).toISOString();
            await API.sms.schedule(body);
            toast('Scheduled for ' + fmtDate(body.run_at), 'ok');
          } else {
            await API.sms.send(body);
            toast('SMS sent', 'ok');
          }
          closeModal();
          loadCampaigns();
        } catch (e) {
          toast('Failed: ' + e.message, 'err');
          btnSend.disabled = false; btnSched.disabled = false;
        }
      };
      root.querySelector('[data-cmp-action="ms-send"]').addEventListener('click', doSend);
      root.querySelector('[data-cmp-action="ms-schedule"]').addEventListener('click', () => {
        if (!sched.checked) sched.checked = true;
        when.classList.remove('hidden');
        doSend();
      });
    });
  }

  // -------------------------------------------------------------------------
  // Mass SMS broadcast modal
  // -------------------------------------------------------------------------
  function openBroadcast() {
    const modal = showModal('Mass SMS Broadcast', `
      <div class="space-y-3">
        <div>
          <label class="text-xs text-gray-400 mb-1 block">From (optional, defaults to +15078731084)</label>
          <input type="tel" id="cmp-bc-from" placeholder="+15078731084" value="+15078731084" />
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Recipients</label>
          <div class="flex gap-2 mb-2">
            <button type="button" class="btn btn-ghost text-xs" data-cmp-action="bc-pick-all">Select all</button>
            <button type="button" class="btn btn-ghost text-xs" data-cmp-action="bc-pick-none">Clear</button>
            <button type="button" class="btn btn-ghost text-xs" data-cmp-action="bc-new-contact">+ Contact</button>
          </div>
          <div id="cmp-bc-contacts" class="panel-2 p-2 space-y-1 scroll-y" style="max-height:240px;">
            <div class="text-xs text-gray-500 p-2">Loading contacts…</div>
          </div>
          <div class="text-xs text-gray-500 mt-1"><span id="cmp-bc-count">0</span> selected</div>
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Message (broadcast to all selected)</label>
          <textarea id="cmp-bc-text" rows="4" placeholder="Type your broadcast…"></textarea>
          <div class="flex justify-between text-xs text-gray-500 mt-1">
            <span><span id="cmp-bc-charcount">0</span> chars · est. <span id="cmp-bc-segs">0</span> segments × <span id="cmp-bc-recipients">0</span> = <span id="cmp-bc-total">0</span></span>
          </div>
        </div>
        <div class="flex justify-end gap-2 pt-2 border-t border-gray-800">
          <button type="button" class="btn btn-ghost" data-cmp-action="bc-cancel">Cancel</button>
          <button type="button" class="btn btn-primary" data-cmp-action="bc-send">🚀 Broadcast</button>
        </div>
      </div>
    `, (root) => {
      const list = root.querySelector('#cmp-bc-contacts');
      const refresh = () => {
        const n = list.querySelectorAll('input[type=checkbox]:checked').length;
        root.querySelector('#cmp-bc-count').textContent = String(n);
        root.querySelector('#cmp-bc-recipients').textContent = String(n);
        updateEst();
      };
      const updateEst = () => {
        const t = root.querySelector('#cmp-bc-text').value || '';
        root.querySelector('#cmp-bc-charcount').textContent = String(t.length);
        const seg = t.length === 0 ? 0 : (t.length <= 160 ? 1 : Math.ceil(t.length / 153));
        const n = list.querySelectorAll('input[type=checkbox]:checked').length;
        root.querySelector('#cmp-bc-segs').textContent = String(seg);
        root.querySelector('#cmp-bc-total').textContent = String(seg * n);
      };
      const renderContacts = (contacts) => {
        if (!contacts.length) { list.innerHTML = '<div class="text-xs text-gray-500 p-2">No contacts yet — add one first.</div>'; return; }
        list.innerHTML = contacts.map(c => `
          <label class="flex items-center gap-2 p-2 hover:bg-gray-800 rounded cursor-pointer">
            <input type="checkbox" value="${escapeHtml(c.id)}" />
            <div class="flex-1 min-w-0">
              <div class="text-sm">${escapeHtml(c.name || '(no name)')}</div>
              <div class="text-xs text-gray-500 mono">${escapeHtml(fmtPhone(c.phone || ''))}</div>
            </div>
          </label>
        `).join('');
        list.querySelectorAll('input[type=checkbox]').forEach(cb => cb.addEventListener('change', refresh));
      };
      if (state.contacts.length > 0) renderContacts(state.contacts);
      else {
        API.contacts.list()
          .then(data => { state.contacts = unwrapList(data, ['contacts', 'items']); renderContacts(state.contacts); })
          .catch(err => { list.innerHTML = '<div class="text-xs text-red-400 p-2">Failed: ' + escapeHtml(err.message) + '</div>'; });
      }
      root.querySelector('#cmp-bc-text').addEventListener('input', updateEst);

      root.querySelector('[data-cmp-action="bc-pick-all"]').addEventListener('click', () => {
        list.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = true; }); refresh();
      });
      root.querySelector('[data-cmp-action="bc-pick-none"]').addEventListener('click', () => {
        list.querySelectorAll('input[type=checkbox]').forEach(cb => { cb.checked = false; }); refresh();
      });
      root.querySelector('[data-cmp-action="bc-new-contact"]').addEventListener('click', () => openContactCreateModal(refresh));
      root.querySelector('[data-cmp-action="bc-cancel"]').addEventListener('click', () => closeModal());

      root.querySelector('[data-cmp-action="bc-send"]').addEventListener('click', async () => {
        const ids = Array.from(list.querySelectorAll('input[type=checkbox]:checked')).map(cb => cb.value);
        const text = root.querySelector('#cmp-bc-text').value.trim();
        if (ids.length === 0) { toast('Pick at least one recipient', 'warn'); return; }
        if (!text) { toast('Message is required', 'warn'); return; }
        const btn = root.querySelector('[data-cmp-action="bc-send"]');
        btn.disabled = true; btn.textContent = 'Sending…';
        try {
          const body = { text, contact_ids: ids };
          if (root.querySelector('#cmp-bc-from').value.trim()) body.from = root.querySelector('#cmp-bc-from').value.trim();
          const res = await API.sms.broadcast(body);
          toast('Broadcast queued: ' + (res.queued || ids.length) + ' message(s)', 'ok');
          closeModal();
          loadCampaigns();
        } catch (e) {
          toast('Broadcast failed: ' + e.message, 'err');
          btn.disabled = false; btn.textContent = '🚀 Broadcast';
        }
      });
    });
  }

  // -------------------------------------------------------------------------
  // Scheduled SMS list
  // -------------------------------------------------------------------------
  function openScheduled() {
    const refreshList = (root) => {
      const body = root.querySelector('#cmp-sched-body');
      body.innerHTML = '<div class="text-xs text-gray-500 p-3">Loading…</div>';
      API.sms.scheduled()
        .then(data => {
          const items = unwrapList(data, ['scheduled', 'items']);
          state.scheduled = items;
          if (items.length === 0) {
            body.innerHTML = '<div class="text-xs text-gray-500 p-4 text-center">No scheduled SMS pending.</div>';
            return;
          }
          body.innerHTML = items.map(s => {
            const when = fmtDate(s.run_at || s.scheduled_for);
            const isPast = new Date(s.run_at || s.scheduled_for) < new Date();
            return `
              <div class="p-3 border-b border-gray-800 flex flex-wrap items-center gap-2">
                <div class="flex-1 min-w-0">
                  <div class="text-sm font-medium">${escapeHtml(fmtPhone(s.to))}</div>
                  <div class="text-xs text-gray-500 mono">⏰ ${escapeHtml(when)} ${isPast ? '<span class="text-red-400">(overdue — should fire soon)</span>' : ''}</div>
                  <div class="text-xs text-gray-400 mt-1 truncate">"${escapeHtml((s.text || '').slice(0, 100))}${(s.text || '').length > 100 ? '…' : ''}"</div>
                </div>
                <button class="btn btn-ghost text-xs" data-cmp-action="sched-cancel" data-cmp-id="${escapeHtml(s.id)}">Cancel</button>
              </div>
            `;
          }).join('');
        })
        .catch(err => { body.innerHTML = '<div class="text-xs text-red-400 p-3">Failed: ' + escapeHtml(err.message) + '</div>'; });
    };

    showModal('Scheduled SMS', `
      <div class="space-y-3">
        <div class="flex items-center justify-between">
          <div class="text-xs text-gray-500">All pending scheduled outbound SMS.</div>
          <button class="btn btn-ghost text-xs" data-cmp-action="sched-refresh">↻ Refresh</button>
        </div>
        <div id="cmp-sched-body" class="panel-2 scroll-y" style="max-height:480px;"></div>
        <div class="flex justify-end pt-2 border-t border-gray-800">
          <button type="button" class="btn btn-ghost" data-cmp-action="sched-close">Close</button>
        </div>
      </div>
    `, (root) => {
      refreshList(root);
      root.querySelector('[data-cmp-action="sched-refresh"]').addEventListener('click', () => refreshList(root));
      root.querySelector('[data-cmp-action="sched-close"]').addEventListener('click', () => closeModal());
    });
  }

  // -------------------------------------------------------------------------
  // Contact picker (small modal) + create contact modal
  // -------------------------------------------------------------------------
  function openContactPicker(onPick) {
    showModal('Pick a contact', `
      <div class="space-y-2">
        <input type="text" id="cmp-cp-search" placeholder="Search by name or phone…" />
        <div id="cmp-cp-list" class="panel-2 p-1 space-y-1 scroll-y" style="max-height:380px;"></div>
        <div class="flex justify-between pt-2 border-t border-gray-800">
          <button class="btn btn-ghost text-xs" data-cmp-action="cp-new">+ New contact</button>
          <button class="btn btn-ghost" data-cmp-action="cp-close">Cancel</button>
        </div>
      </div>
    `, (root) => {
      const render = (contacts, q) => {
        const list = root.querySelector('#cmp-cp-list');
        if (!contacts.length) { list.innerHTML = '<div class="text-xs text-gray-500 p-3 text-center">No contacts.</div>'; return; }
        const filtered = q ? contacts.filter(c => (c.name || '').toLowerCase().includes(q) || (c.phone || '').includes(q)) : contacts;
        if (!filtered.length) { list.innerHTML = '<div class="text-xs text-gray-500 p-3 text-center">No match.</div>'; return; }
        list.innerHTML = filtered.map(c => `
          <button class="w-full text-left p-2 hover:bg-gray-800 rounded" data-cmp-id="${escapeHtml(c.id)}" data-cmp-action="cp-pick" data-name="${escapeHtml(c.name || '')}" data-phone="${escapeHtml(c.phone || '')}">
            <div class="text-sm">${escapeHtml(c.name || '(no name)')}</div>
            <div class="text-xs text-gray-500 mono">${escapeHtml(fmtPhone(c.phone || ''))}</div>
          </button>
        `).join('');
        list.querySelectorAll('[data-cmp-action="cp-pick"]').forEach(b => b.addEventListener('click', () => {
          onPick({ id: b.dataset.cmpId, name: b.dataset.name, phone: b.dataset.phone });
          closeModal();
        }));
      };
      const cb = (data) => { state.contacts = unwrapList(data, ['contacts', 'items']); render(state.contacts, ''); };
      if (state.contacts.length > 0) render(state.contacts, '');
      else API.contacts.list().then(cb).catch(err => { root.querySelector('#cmp-cp-list').innerHTML = '<div class="text-xs text-red-400 p-3">' + escapeHtml(err.message) + '</div>'; });
      root.querySelector('#cmp-cp-search').addEventListener('input', e => render(state.contacts, e.target.value.toLowerCase().trim()));
      root.querySelector('[data-cmp-action="cp-close"]').addEventListener('click', () => closeModal());
      root.querySelector('[data-cmp-action="cp-new"]').addEventListener('click', () => openContactCreateModal(() => API.contacts.list().then(cb)));
    });
  }

  function openContactCreateModal(onCreated) {
    showModal('New contact', `
      <div class="space-y-3">
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Name</label>
          <input type="text" id="cmp-cc-name" placeholder="e.g. Jane Doe" />
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Phone (E.164, e.g. +15551234567)</label>
          <input type="tel" id="cmp-cc-phone" placeholder="+15551234567" />
        </div>
        <div>
          <label class="text-xs text-gray-400 mb-1 block">Notes (optional)</label>
          <textarea id="cmp-cc-notes" rows="2" placeholder="Anything we should know…"></textarea>
        </div>
        <div class="flex justify-end gap-2 pt-2 border-t border-gray-800">
          <button class="btn btn-ghost" data-cmp-action="cc-cancel">Cancel</button>
          <button class="btn btn-primary" data-cmp-action="cc-save">Save</button>
        </div>
      </div>
    `, (root) => {
      root.querySelector('[data-cmp-action="cc-cancel"]').addEventListener('click', () => closeModal());
      root.querySelector('[data-cmp-action="cc-save"]').addEventListener('click', async () => {
        const name = root.querySelector('#cmp-cc-name').value.trim();
        const phone = root.querySelector('#cmp-cc-phone').value.trim();
        const notes = root.querySelector('#cmp-cc-notes').value.trim();
        if (!phone) { toast('Phone is required', 'warn'); return; }
        const btn = root.querySelector('[data-cmp-action="cc-save"]');
        btn.disabled = true; btn.textContent = 'Saving…';
        try {
          const created = await API.contacts.create({ name, phone, notes });
          // Refresh local cache
          API.contacts.list().then(c => { state.contacts = unwrapList(c, ['contacts', 'items']); }).catch(() => {});
          closeModal();
          if (onCreated) onCreated(created);
          toast('Contact saved', 'ok');
        } catch (e) {
          toast('Failed: ' + e.message, 'err');
          btn.disabled = false; btn.textContent = 'Save';
        }
      });
    });
  }

  // -------------------------------------------------------------------------
  // Toast (uses the global showToast if present, otherwise a lightweight
  // local fallback so the Campaigns tab still works in isolation).
  // -------------------------------------------------------------------------
  function toast(msg, kind) {
    if (typeof window.showToast === 'function') {
      try { window.showToast({ kind: kind || 'info', icon: kind === 'ok' ? '✅' : kind === 'err' ? '❌' : 'ℹ️', title: msg, durationMs: 3500 }); return; } catch (e) {}
    }
    // Fallback toast
    let host = document.getElementById('cmp-toast-host');
    if (!host) {
      host = el('div', { id: 'cmp-toast-host', style: 'position:fixed;bottom:20px;right:20px;z-index:99999;display:flex;flex-direction:column;gap:8px;' });
      document.body.appendChild(host);
    }
    const colors = { ok: 'var(--accent)', err: 'var(--danger)', warn: 'var(--warn)' };
    const t = el('div', { class: 'panel', style: 'padding:10px 14px;font-size:13px;border-left:3px solid ' + (colors[kind] || 'var(--accent)') + ';' }, msg);
    host.appendChild(t);
    setTimeout(() => { t.style.transition = 'opacity 0.3s'; t.style.opacity = '0'; }, 3000);
    setTimeout(() => { if (t.parentNode) t.parentNode.removeChild(t); }, 3500);
  }

  // -------------------------------------------------------------------------
  // Modal system (single shared overlay)
  // -------------------------------------------------------------------------
  function showModal(title, bodyHtml, onMount) {
    closeModal();
    const overlay = el('div', { id: 'cmp-modal', class: 'modal-bg', style: 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;' });
    const card = el('div', { class: 'panel', style: 'max-width:600px;width:100%;max-height:90vh;overflow-y:auto;padding:20px;' });
    card.innerHTML = `
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-lg">${escapeHtml(title)}</h3>
        <button class="btn btn-ghost text-xs" data-cmp-action="modal-close">✕</button>
      </div>
      <div id="cmp-modal-body">${bodyHtml}</div>
    `;
    overlay.appendChild(card);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
    document.body.appendChild(overlay);
    card.querySelector('[data-cmp-action="modal-close"]').addEventListener('click', closeModal);
    if (onMount) {
      try { onMount(card); } catch (e) { console.error('modal mount failed', e); toast('Modal init failed: ' + e.message, 'err'); }
    }
    return card;
  }
  function closeModal() {
    const m = document.getElementById('cmp-modal');
    if (m) m.parentNode.removeChild(m);
  }

  // -------------------------------------------------------------------------
  // Action dispatcher
  // -------------------------------------------------------------------------
  function handleAction(action, btn) {
    const id = btn.dataset.cmpId;
    switch (action) {
      case 'new-sms':       openManualSMS(); break;
      case 'new-broadcast': openBroadcast(); break;
      case 'new-campaign':  openCreateWizard(); break;
      case 'scheduled':     openScheduled(); break;
      case 'refresh':       loadCampaigns(); break;
      case 'open':          if (id) openCampaignDetail(id); break;
      case 'launch':        if (id) doLaunch(id); break;
      case 'pause':         if (id) doPause(id); break;
      case 'resume':        if (id) doResume(id); break;
      case 'delete':        if (id) doDelete(id); break;
      case 'close-detail':  closeCampaignDetail(); break;
      case 'sched-cancel':  if (id) doCancelScheduled(id); break;
    }
  }

  async function doLaunch(id) {
    try { await API.campaigns.launch(id); toast('Campaign launched', 'ok'); loadCampaigns(); }
    catch (e) { toast('Launch failed: ' + e.message, 'err'); }
  }
  async function doPause(id) {
    try { await API.campaigns.pause(id); toast('Paused', 'ok'); loadCampaigns(); }
    catch (e) { toast('Pause failed: ' + e.message, 'err'); }
  }
  async function doResume(id) {
    try { await API.campaigns.resume(id); toast('Resumed', 'ok'); loadCampaigns(); }
    catch (e) { toast('Resume failed: ' + e.message, 'err'); }
  }
  async function doDelete(id) {
    if (!confirm('Delete this campaign? This cannot be undone.')) return;
    try { await API.campaigns.remove(id); toast('Deleted', 'ok'); closeCampaignDetail(); loadCampaigns(); }
    catch (e) { toast('Delete failed: ' + e.message, 'err'); }
  }
  async function doCancelScheduled(id) {
    try { await API.sms.cancelScheduled(id); toast('Cancelled', 'ok'); document.querySelector('[data-cmp-action="modal-close"]').click(); openScheduled(); }
    catch (e) { toast('Cancel failed: ' + e.message, 'err'); }
  }

  // -------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------
  function boot() {
    if (!injectTab()) {
      state.bootRetries++;
      if (state.bootRetries > 40) {
        console.error('campaigns.js: could not find admin tab after 40 retries — giving up');
        return;
      }
      setTimeout(boot, 250);
      return;
    }
    // Delegated click handler for any [data-cmp-action] inside the document.
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-cmp-action]');
      if (!btn) return;
      // Avoid double-handling the wizard's own inner actions like "wiz-next"
      // that are handled inline in their respective modals. We still
      // forward them through here as a no-op for safety; the modal-local
      // listeners win because they were attached first.
      handleAction(btn.dataset.cmpAction, btn);
    });
    // Hook the global switchTab() so if anyone (e.g. a deep-link) calls
    // switchTab('campaigns'), we still load the data.
    if (typeof window.switchTab === 'function') {
      const orig = window.switchTab;
      window.switchTab = function (name) {
        orig(name);
        if (name === 'campaigns') loadCampaigns();
      };
    }
    console.log('[campaigns.js] v0.4.0 booted — tab injected, action dispatcher live');
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
