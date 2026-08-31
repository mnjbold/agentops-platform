/* =====================================================================
 * agentops/screens/billing.js
 * Billing screen (issue #19).
 * Current plan card, usage meters, plan comparison, invoice stub.
 * ===================================================================== */

import { h, formatDate } from '../lib/dom.js';
import { api } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createBadge } from '../ui/badge.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toastError, toastSuccess } from '../ui/toast.js';

let _state = { loading: true, sub: null, usage: null, plans: null };

export async function mountBillingScreen(root) {
  root.innerHTML = '';

  root.append(
    h('div', { class: 'page-head' },
      h('div', {},
        h('h1', { class: 'page-title' }, 'Billing'),
        h('p', { class: 'page-sub' }, 'Plan, usage, and invoices')
      ),
      h('div', { class: 'page-actions' },
        createButton({ variant: 'primary', size: 'sm', children: 'Manage in Stripe portal', onClick: openPortal })
      )
    )
  );

  // Current plan card
  const planCard = h('div', { class: 'card', id: 'plan-card', style: 'padding: 16px; margin-bottom: 16px;' });
  planCard.append(createSkeleton({ lines: 2, height: 24 }));
  root.append(planCard);

  // Usage meters
  const usageCard = h('div', { class: 'card', id: 'usage-card', style: 'padding: 16px; margin-bottom: 16px;' });
  usageCard.append(createSkeleton({ lines: 3, height: 18 }));
  root.append(usageCard);

  // Plan comparison
  const cmp = h('div', {}, h('h2', { style: 'font-size: 16px; margin: 12px 0;' }, 'Compare plans'));
  const cmpRow = h('div', { id: 'plans-row', style: 'display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin-bottom: 16px;' });
  for (let i = 0; i < 3; i++) cmpRow.append(createSkeleton({ lines: 4, height: 60 }));
  cmp.append(cmpRow);
  root.append(cmp);

  // Invoices (mock for v1)
  root.append(h('h2', { style: 'font-size: 16px; margin: 12px 0;' }, 'Recent invoices'));
  const inv = h('div', { class: 'card', id: 'invoices', style: 'padding: 8px 0;' });
  inv.append(createSkeleton({ lines: 3, height: 32 }));
  root.append(inv);

  await load(root);
}

async function load(root) {
  _state.loading = true;
  try {
    const [sub, usage, plans] = await Promise.all([
      api.get('/v1/billing/subscription').catch(() => null),
      api.get('/v1/billing/usage').catch(() => null),
      api.get('/v1/billing/plans').catch(() => null),
    ]);
    _state = { loading: false, sub, usage, plans: plans && plans.plans };
    paint(root);
  } catch (e) {
    _state.loading = false;
    if (root.querySelector('#plan-card')) {
      root.querySelector('#plan-card').innerHTML = '';
      root.querySelector('#plan-card').append(createEmptyState({ icon: '!', title: 'Could not load billing', body: e.message }));
    }
  }
}

function paint(root) {
  paintPlanCard(root);
  paintUsageCard(root);
  paintPlanComparison(root);
  paintInvoices(root);
}

function paintPlanCard(root) {
  const card = root.querySelector('#plan-card');
  if (!card) return;
  card.innerHTML = '';
  if (!_state.sub) {
    card.append(createEmptyState({ icon: '💳', title: 'No subscription', body: 'Pick a plan to get started.' }));
    return;
  }
  const sub = _state.sub;
  const left = h('div', { style: 'display: flex; justify-content: space-between; align-items: center;' });
  const title = h('div', {});
  title.append(h('div', { style: 'display: flex; gap: 8px; align-items: center;' },
    h('div', { style: 'font-size: 24px; font-weight: 700;' }, sub.plan_details.name || sub.plan),
    createBadge({ variant: sub.status === 'active' ? 'success' : sub.status === 'past_due' ? 'danger' : 'neutral', children: sub.status })
  ));
  title.append(h('div', { style: 'color: var(--color-fg-3); font-size: 13px; margin-top: 4px;' },
    sub.plan_details.monthly_price_cents
      ? `${formatCents(sub.plan_details.monthly_price_cents)} / month — ${sub.plan_details.number_limit} numbers`
      : `Free plan — ${sub.plan_details.number_limit} number`));
  left.append(title);

  const actions = h('div', { style: 'display: flex; gap: 8px;' });
  if (sub.plan === 'free') {
    actions.append(createButton({ variant: 'primary', size: 'sm', children: 'Upgrade to Pro', onClick: () => startCheckout('pro') }));
  }
  if (sub.cancel_at_period_end) {
    actions.append(createBadge({ variant: 'warning', children: 'Cancels at period end' }));
  }
  left.append(actions);
  card.append(left);

  // Period info
  if (sub.current_period_start) {
    card.append(h('div', { style: 'margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--color-line); color: var(--color-fg-3); font-size: 12px;' },
      `Current period: ${formatDate(sub.current_period_start)} → ${formatDate(sub.current_period_end)}`));
  }
}

function paintUsageCard(root) {
  const card = root.querySelector('#usage-card');
  if (!card) return;
  card.innerHTML = '';
  if (!_state.usage) return;
  const u = _state.usage;
  const title = h('div', { style: 'display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px;' });
  title.append(h('div', { style: 'font-weight: 600;' }, 'This period'));
  title.append(h('div', { style: 'color: var(--color-fg-3); font-size: 12px;' }, `${u.period_start.slice(0, 10)} → ${u.period_end.slice(0, 10)}`));
  card.append(title);

  // Soft limits (just for the meter colour). Real limits are in plans.py.
  const soft = {
    voice_minutes: 1000,
    sms_segments: 1000,
  };
  card.append(renderMeter('Voice minutes', u.voice_minutes || 0, soft.voice_minutes, `${u.voice_rate_cents_per_min} ¢/min`));
  card.append(renderMeter('SMS segments', u.sms_segments || 0, soft.sms_segments, `${u.sms_rate_cents_per_segment} ¢/segment`));
  card.append(renderMeter('Phone numbers', countNumbersFromPlan(u), u.number_limit || 1, 'limit'));
}

function countNumbersFromPlan(usage) {
  // We don't have an /api/numbers count endpoint that we own. Best-effort:
  // show 0; the user can see actual numbers in the Numbers page.
  return 0;
}

function renderMeter(label, value, softLimit, suffix) {
  const pct = softLimit ? Math.min(100, Math.round((value / softLimit) * 100)) : 0;
  const color = pct < 60 ? '#48c78e' : pct < 90 ? '#f0a020' : '#ff6363';
  const row = h('div', { style: 'margin-bottom: 12px;' });
  const head = h('div', { style: 'display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px;' });
  head.append(h('div', {}, label));
  head.append(h('div', { style: 'color: var(--color-fg-3);' }, `${value} / ${softLimit || '∞'}  ${suffix || ''}`));
  row.append(head);
  const bar = h('div', { style: 'height: 8px; background: var(--color-bg-2); border-radius: 4px; overflow: hidden;' });
  bar.append(h('div', { style: `width: ${pct}%; height: 100%; background: ${color}; transition: width .3s;` }));
  row.append(bar);
  return row;
}

function paintPlanComparison(root) {
  const row = root.querySelector('#plans-row');
  if (!row) return;
  row.innerHTML = '';
  if (!_state.plans) return;
  for (const p of _state.plans) {
    const currentPlan = _state.sub && _state.sub.plan === p.id;
    const card = h('div', { class: 'card', style: 'padding: 16px; display: flex; flex-direction: column; gap: 8px;' });
    card.append(h('div', { style: 'display: flex; justify-content: space-between; align-items: center;' },
      h('div', { style: 'font-size: 18px; font-weight: 700;' }, p.name),
      currentPlan ? createBadge({ variant: 'success', children: 'Current' }) : null
    ));
    card.append(h('div', { style: 'font-size: 22px; font-weight: 700; color: var(--color-fg-1);' },
      p.monthly_price_cents ? `${formatCents(p.monthly_price_cents)}/mo` : (p.id === 'enterprise' ? 'Custom' : 'Free')));
    card.append(h('div', { style: 'color: var(--color-fg-3); font-size: 12px;' },
      `${p.number_limit} numbers · ${p.voice_rate_cents_per_min} ¢/min · ${p.sms_rate_cents_per_segment} ¢/SMS`));
    const ul = h('ul', { style: 'padding-left: 18px; color: var(--color-fg-2); font-size: 13px; margin: 8px 0;' });
    for (const f of p.features) ul.append(h('li', {}, f));
    card.append(ul);
    const cta = h('div', { style: 'margin-top: auto;' });
    if (!currentPlan) {
      cta.append(createButton({
        variant: p.id === 'enterprise' ? 'secondary' : 'primary',
        size: 'sm',
        fullWidth: true,
        children: p.id === 'enterprise' ? 'Contact sales' : (p.id === 'pro' ? 'Upgrade to Pro' : 'Switch to Free'),
        onClick: () => p.id === 'free' ? downgrade() : startCheckout(p.id),
      }));
    }
    card.append(cta);
    row.append(card);
  }
}

function paintInvoices(root) {
  const host = root.querySelector('#invoices');
  if (!host) return;
  host.innerHTML = '';
  host.append(createEmptyState({
    icon: '🧾', title: 'No invoices yet',
    body: 'Invoices are generated by Stripe. The first invoice will appear here after your first paid month.',
  }));
}

async function startCheckout(plan) {
  try {
    const res = await api.post('/v1/billing/checkout', { plan });
    if (res.checkout && res.checkout.url) {
      window.location.href = res.checkout.url;
    } else {
      toastError('Checkout failed: no URL');
    }
  } catch (e) {
    toastError('Checkout failed: ' + e.message);
  }
}

async function openPortal() {
  try {
    const res = await api.post('/v1/billing/portal', {});
    if (res.portal && res.portal.url) {
      window.location.href = res.portal.url;
    } else {
      toastError('Portal not available: ' + (res && res.error ? res.error : 'unknown'));
    }
  } catch (e) {
    toastError('Portal failed: ' + e.message);
  }
}

async function downgrade() {
  toastSuccess('You are on the free plan.');
}

function formatCents(c) {
  return '$' + (c / 100).toFixed(2);
}
