/* =====================================================================
 * agentops/screens/workflows.js
 * Visual no-code Workflow Designer (issue #13).
 *
 * Layout:
 *   ┌──────────────────────────────────────────────────────────┐
 *   │ Header: name + save + test buttons                       │
 *   ├──────────┬────────────────────────────────┬──────────────┤
 *   │ Templates│ Canvas (SVG: nodes + edges)     │ Properties   │
 *   │ Palette  │  - click palette → add node     │ Panel        │
 *   │          │  - click node → edit props      │              │
 *   │          │  - drag node → reposition       │              │
 *   │          │  - shift-drag from node → connect              │
 *   └──────────┴────────────────────────────────┴──────────────┘
 *
 * 4 starter templates ship in the backend; the top template strip
 * shows them as cards. Clicking one loads its graph into the editor.
 * ===================================================================== */

import { h, debounce } from '../lib/dom.js';
import { api, baseUrl } from '../lib/api.js';
import { createButton } from '../ui/button.js';
import { createInput, createTextarea } from '../ui/input.js';
import { createSelect } from '../ui/select.js';
import { createBadge } from '../ui/badge.js';
import { createEmptyState, createSkeleton } from '../ui/empty-state.js';
import { toast, toastError, toastSuccess } from '../ui/toast.js';

const NODE_TYPES = [
  { id: 'greeting',     label: 'Greeting',     icon: '♪', color: '#3a8dde' },
  { id: 'menu',         label: 'Menu (IVR)',   icon: '☰', color: '#9b59b6' },
  { id: 'forward',      label: 'Forward',      icon: '→', color: '#e67e22' },
  { id: 'forward_agent',label: 'Forward agent',icon: '☎', color: '#16a085' },
  { id: 'voicemail',    label: 'Voicemail',    icon: '✉', color: '#34495e' },
  { id: 'hangup',       label: 'Hangup',       icon: '⏏', color: '#7f8c8d' },
  { id: 'conditional',  label: 'Conditional',  icon: '?', color: '#f39c12' },
  { id: 'webhook',      label: 'Webhook',      icon: '⇄', color: '#2c3e50' },
  { id: 'ai_assistant', label: 'AI Assistant', icon: '✦', color: '#8e44ad' },
];

// In-memory state for the editor.
let _state = {
  workflowId: null,        // null = unsaved draft
  name: 'New workflow',
  graph: { nodes: [], edges: [] },
  selectedNodeId: null,
  pendingConnectFrom: null, // when user is mid-drag for an edge
  templates: [],
  zoom: 1,
  pan: { x: 0, y: 0 },
};

export async function mountWorkflowsScreen(root) {
  root.innerHTML = '';

  root.append(h('div', { class: 'page-head' },
    h('div', {},
      h('h1', { class: 'page-title' }, 'Workflows'),
      h('p', { class: 'page-sub' }, 'Visual IVR builder — drag nodes onto the canvas, connect them, save.')
    ),
    h('div', { class: 'page-actions' },
      createButton({ variant: 'ghost', size: 'sm',
        onClick: () => loadListings(root),
        children: 'Refresh' }),
      createButton({ variant: 'secondary', size: 'sm',
        onClick: () => startNew(root),
        children: 'New workflow' }),
      createButton({ variant: 'primary', size: 'sm',
        onClick: () => save(root),
        children: 'Save' }),
    )
  ));

  // Template strip
  const templates = h('div', { class: 'wf-templates', id: 'wf-templates' });
  root.append(templates);

  // Three-pane layout
  const layout = h('div', { class: 'wf-layout' });
  const palette = h('div', { class: 'wf-palette', id: 'wf-palette' });
  const canvasWrap = h('div', { class: 'wf-canvas-wrap' });
  const props = h('div', { class: 'wf-props', id: 'wf-props' });
  layout.append(palette, canvasWrap, props);
  root.append(layout);

  // List of existing workflows (collapsible footer; for v1 a dropdown at top works too)
  const existing = h('div', { class: 'wf-existing', id: 'wf-existing' });
  root.append(existing);

  renderPalette(palette);
  renderEmptyProps(props);
  await loadTemplates();
  renderTemplateStrip(templates);
  await loadListings(root);
  // Pre-load a starter so the canvas isn't empty.
  if (!_state.workflowId && !_state.graph.nodes.length) {
    if (_state.templates.length) loadTemplate(_state.templates[0].id);
  }
}

function renderPalette(root) {
  root.innerHTML = '';
  root.append(h('div', { class: 'wf-palette-title' }, 'Node palette'));
  const list = h('div', { class: 'wf-palette-list' });
  for (const t of NODE_TYPES) {
    const item = h('button', {
      type: 'button',
      class: 'wf-palette-item',
      style: `--node-color: ${t.color};`,
      onClick: () => addNode(t.id),
    },
      h('span', { class: 'wf-palette-icon', html: t.icon }),
      h('span', { class: 'wf-palette-label' }, t.label)
    );
    list.append(item);
  }
  root.append(list);
}

function renderEmptyProps(root) {
  root.innerHTML = '';
  root.append(h('div', { class: 'wf-props-empty' },
    h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' },
      'Select a node to edit its properties.')
  ));
}

async function loadTemplates() {
  try {
    const res = await api.get('/workflows/templates');
    _state.templates = res.templates || [];
  } catch (e) {
    _state.templates = [];
  }
}

function renderTemplateStrip(root) {
  root.innerHTML = '';
  if (!_state.templates.length) return;
  const list = h('div', { class: 'wf-template-strip' });
  for (const t of _state.templates) {
    const card = h('button', {
      type: 'button', class: 'wf-template-card',
      onClick: () => loadTemplate(t.id),
    },
      h('div', { class: 'wf-template-name' }, t.name),
      h('div', { class: 'wf-template-desc' }, t.description || `${t.node_count} nodes`),
    );
    list.append(card);
  }
  root.append(h('div', { class: 'wf-section-label' }, 'Starter templates'),
              list);
}

async function loadListings(root) {
  const container = root.querySelector('#wf-existing');
  if (!container) return;
  container.innerHTML = '';
  container.append(h('div', { class: 'wf-section-label' }, 'Your workflows'));
  try {
    const res = await api.get('/workflows');
    const items = res.workflows || [];
    if (!items.length) {
      container.append(h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm); margin: var(--space-2) 0;' },
        'No workflows yet. Pick a starter template above or click "New workflow".'));
      return;
    }
    const list = h('div', { class: 'wf-existing-list' });
    for (const w of items) {
      const row = h('div', { class: 'wf-existing-row' },
        h('div', {},
          h('div', { class: 'wf-existing-name' }, w.name),
          h('div', { class: 'wf-existing-meta' }, `v${w.version} · ${(w.graph?.nodes || []).length} nodes`)
        ),
        h('div', { style: 'display: flex; gap: var(--space-2);' },
          createButton({ variant: 'ghost', size: 'sm',
            onClick: () => openWorkflow(w.id, root),
            children: 'Edit' }),
          createButton({ variant: 'danger', size: 'sm',
            onClick: () => deleteWorkflow(w.id, root),
            children: 'Delete' }),
        )
      );
      list.append(row);
    }
    container.append(list);
  } catch (e) {
    container.append(h('p', { style: 'color: var(--color-fg-3);' }, e.message));
  }
}

async function openWorkflow(id, root) {
  try {
    const res = await api.get(`/workflows/${id}`);
    _state.workflowId = res.workflow.id;
    _state.name = res.workflow.name;
    _state.graph = res.workflow.graph || { nodes: [], edges: [] };
    _state.selectedNodeId = null;
    document.querySelector('.page-title').textContent = `Workflows — ${_state.name}`;
    redrawCanvas();
    renderEmptyProps(root.querySelector('#wf-props'));
  } catch (e) {
    toastError('Could not load workflow: ' + e.message);
  }
}

async function deleteWorkflow(id, root) {
  if (!confirm('Delete this workflow? Any number assignments will be cleared.')) return;
  try {
    await api.del(`/workflows/${id}`);
    toastSuccess('Workflow deleted');
    if (_state.workflowId === id) startNew(root);
    loadListings(root);
  } catch (e) {
    toastError('Delete failed: ' + e.message);
  }
}

function startNew(root) {
  _state.workflowId = null;
  _state.name = 'New workflow';
  _state.graph = { nodes: [], edges: [] };
  _state.selectedNodeId = null;
  document.querySelector('.page-title').textContent = 'Workflows';
  redrawCanvas();
  renderEmptyProps(root.querySelector('#wf-props'));
}

async function loadTemplate(name) {
  try {
    const res = await api.post(`/workflows/from-template/${name}`);
    _state.workflowId = res.workflow.id;
    _state.name = res.workflow.name;
    _state.graph = res.workflow.graph || { nodes: [], edges: [] };
    document.querySelector('.page-title').textContent = `Workflows — ${_state.name}`;
    redrawCanvas();
    toastSuccess(`Loaded template "${res.template}"`);
  } catch (e) {
    toastError('Template load failed: ' + e.message);
  }
}

function addNode(typeId) {
  const meta = NODE_TYPES.find(n => n.id === typeId);
  if (!meta) return;
  // Place new node near the visible center.
  const cx = 200 + Math.random() * 200;
  const cy = 200 + Math.random() * 120;
  const id = `${typeId}_${Math.random().toString(36).slice(2, 7)}`;
  const node = {
    id, type: typeId, label: meta.label,
    x: cx, y: cy,
    params: defaultParamsFor(typeId),
  };
  _state.graph.nodes.push(node);
  _state.selectedNodeId = id;
  redrawCanvas();
  const props = document.querySelector('#wf-props');
  if (props) renderNodeProps(props, node);
}

function defaultParamsFor(typeId) {
  switch (typeId) {
    case 'greeting':     return { text: 'Hello, thanks for calling.', voice: 'Telnyx.KokoroTTS.af_heart' };
    case 'menu':         return { prompt: 'Press 1 for sales, 2 for support.', options: { '1': '', '2': '' } };
    case 'forward':      return { to: '+15078731084', from: '+15078731084' };
    case 'forward_agent':return { skill: 'sales', timeout_secs: 30 };
    case 'voicemail':    return { max_length_secs: 60, transcribe: true };
    case 'hangup':       return {};
    case 'conditional':  return { condition: 'time_of_day', open: '09:00', close: '17:00', tz: 'UTC' };
    case 'webhook':      return { url: 'https://example.test/hook' };
    case 'ai_assistant': return { assistant_id: null };
    default: return {};
  }
}

async function save(root) {
  try {
    const payload = { name: _state.name, graph: _state.graph };
    let res;
    if (_state.workflowId) {
      res = await api.patch(`/workflows/${_state.workflowId}`, payload);
    } else {
      res = await api.post('/workflows', payload);
    }
    _state.workflowId = res.workflow.id;
    _state.name = res.workflow.name;
    _state.graph = res.workflow.graph || _state.graph;
    document.querySelector('.page-title').textContent = `Workflows — ${_state.name}`;
    toastSuccess('Saved');
    loadListings(root);
  } catch (e) {
    toastError('Save failed: ' + (e?.data?.detail || e.message));
  }
}

async function testRun(root) {
  // We need a saved workflow with a valid graph. Use the current draft
  // for a synthetic test if not saved.
  if (!_state.workflowId) {
    // Save first, then test.
    await save(root);
    if (!_state.workflowId) return;
  }
  try {
    const res = await api.post(`/api/workflows/${_state.workflowId}/test`, {
      from: '+15555550100', to: '+15078731084',
    });
    renderTimeline(res.history || []);
  } catch (e) {
    toastError('Test failed: ' + e.message);
  }
}

function renderTimeline(history) {
  // Show in a modal-like overlay.
  const existing = document.getElementById('wf-timeline-modal');
  if (existing) existing.remove();
  const list = h('div', { class: 'wf-timeline' },
    h('h3', { style: 'margin-top:0;' }, 'Test trace'),
    h('p', { style: 'color: var(--color-fg-3); font-size: var(--text-sm);' },
      'Synthetic call walked the graph. Each step is a node transition.'),
    h('ol', { style: 'padding-left: 20px;' },
      ...history.map(h => h('li', {},
        h('strong', {}, h.node_id),
        h('span', { style: 'color: var(--color-fg-3);' }, ' — ' + h.action)
      ))
    )
  );
  const close = h('button', {
    type: 'button',
    class: 'modal-close',
    style: 'position:absolute; top:12px; right:16px;',
    onClick: () => overlay.remove(),
  }, '×');
  const overlay = h('div', {
    id: 'wf-timeline-modal',
    style: 'position: fixed; inset: 0; background: rgba(0,0,0,0.4); display:flex; align-items:center; justify-content:center; z-index: 1000;'
  },
    h('div', {
      class: 'card',
      style: 'max-width: 480px; width: 90%; padding: var(--space-5); position:relative; max-height: 80vh; overflow:auto;'
    },
      close, list
    )
  );
  document.body.append(overlay);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
}

// ─────────── canvas ───────────

function ensureCanvas() {
  let wrap = document.querySelector('.wf-canvas-wrap');
  if (!wrap) return null;
  let svg = wrap.querySelector('svg.wf-svg');
  if (!svg) {
    wrap.innerHTML = '';
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'wf-svg');
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', '100%');
    svg.style.background = 'var(--color-bg-1)';
    svg.style.borderRadius = 'var(--radius-md, 8px)';
    svg.style.userSelect = 'none';
    wrap.append(svg);
    // Add toolbar
    const toolbar = h('div', { class: 'wf-canvas-toolbar' },
      createButton({ variant: 'ghost', size: 'sm', onClick: () => testRun(wrap), children: 'Test with fake call' }),
      createButton({ variant: 'ghost', size: 'sm', onClick: () => zoom(0.9), children: '−' }),
      createButton({ variant: 'ghost', size: 'sm', onClick: () => zoom(1.1), children: '+' }),
    );
    wrap.append(toolbar);
  }
  return svg;
}

function redrawCanvas() {
  const svg = ensureCanvas();
  if (!svg) return;
  // Clear
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  // Grid background (very subtle)
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  const pat = document.createElementNS('http://www.w3.org/2000/svg', 'pattern');
  pat.setAttribute('id', 'wf-grid');
  pat.setAttribute('width', '20');
  pat.setAttribute('height', '20');
  pat.setAttribute('patternUnits', 'userSpaceOnUse');
  pat.innerHTML = '<circle cx="2" cy="2" r="1" fill="rgba(255,255,255,0.06)"/>';
  defs.append(pat);
  svg.append(defs);
  const gridRect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  gridRect.setAttribute('width', '100%');
  gridRect.setAttribute('height', '100%');
  gridRect.setAttribute('fill', 'url(#wf-grid)');
  svg.append(gridRect);

  // Edges first
  for (const e of _state.graph.edges) {
    const from = _state.graph.nodes.find(n => n.id === e.from);
    const to = _state.graph.nodes.find(n => n.id === e.to);
    if (!from || !to) continue;
    const x1 = from.x + 80, y1 = from.y + 20;
    const x2 = to.x, y2 = to.y + 20;
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const mx = (x1 + x2) / 2;
    path.setAttribute('d', `M ${x1} ${y1} C ${mx} ${y1} ${mx} ${y2} ${x2} ${y2}`);
    path.setAttribute('stroke', e === _state._hoverEdge ? 'var(--color-accent)' : 'rgba(255,255,255,0.4)');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('fill', 'none');
    svg.append(path);
    if (e.condition || e.branch) {
      const lbl = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      lbl.setAttribute('x', mx);
      lbl.setAttribute('y', (y1 + y2) / 2 - 4);
      lbl.setAttribute('text-anchor', 'middle');
      lbl.setAttribute('fill', 'var(--color-fg-3)');
      lbl.setAttribute('font-size', '11');
      lbl.textContent = e.condition || e.branch;
      svg.append(lbl);
    }
    // Arrowhead
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    arrow.setAttribute('points', `${x2-6},${y2-4} ${x2},${y2} ${x2-6},${y2+4}`);
    arrow.setAttribute('fill', 'rgba(255,255,255,0.4)');
    svg.append(arrow);
  }

  // Nodes
  for (const n of _state.graph.nodes) {
    const meta = NODE_TYPES.find(t => t.id === n.type) || { color: '#666' };
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('transform', `translate(${n.x}, ${n.y})`);
    g.style.cursor = 'grab';
    g.dataset.nodeId = n.id;
    const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rect.setAttribute('width', '160');
    rect.setAttribute('height', '40');
    rect.setAttribute('rx', '6');
    rect.setAttribute('fill', meta.color);
    rect.setAttribute('stroke', n.id === _state.selectedNodeId ? '#fff' : 'rgba(255,255,255,0.4)');
    rect.setAttribute('stroke-width', n.id === _state.selectedNodeId ? '2' : '1');
    g.append(rect);
    const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    txt.setAttribute('x', '12');
    txt.setAttribute('y', '17');
    txt.setAttribute('fill', '#fff');
    txt.setAttribute('font-size', '12');
    txt.setAttribute('font-weight', '600');
    txt.textContent = meta.label;
    g.append(txt);
    const sub = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    sub.setAttribute('x', '12');
    sub.setAttribute('y', '32');
    sub.setAttribute('fill', 'rgba(255,255,255,0.85)');
    sub.setAttribute('font-size', '10');
    sub.textContent = (n.label || n.id).slice(0, 22);
    g.append(sub);
    // Output handle (right side)
    const out = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    out.setAttribute('cx', '160');
    out.setAttribute('cy', '20');
    out.setAttribute('r', '5');
    out.setAttribute('fill', '#fff');
    out.dataset.role = 'out';
    g.append(out);
    // Input handle (left)
    const inp = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    inp.setAttribute('cx', '0');
    inp.setAttribute('cy', '20');
    inp.setAttribute('r', '5');
    inp.setAttribute('fill', 'rgba(255,255,255,0.7)');
    inp.dataset.role = 'in';
    g.append(inp);
    svg.append(g);
    attachNodeHandlers(g, n);
  }
}

function attachNodeHandlers(g, node) {
  let dragging = null;
  g.addEventListener('mousedown', (e) => {
    e.stopPropagation();
    if (e.target?.dataset?.role === 'out') {
      _state.pendingConnectFrom = node.id;
      return;
    }
    _state.selectedNodeId = node.id;
    const props = document.querySelector('#wf-props');
    if (props) renderNodeProps(props, node);
    redrawCanvas();
    const pt = svgPointFromEvent(e);
    dragging = { offsetX: pt.x - node.x, offsetY: pt.y - node.y };
  });
  g.addEventListener('mouseup', (e) => {
    if (_state.pendingConnectFrom && _state.pendingConnectFrom !== node.id) {
      // Finish an edge
      const from = _state.pendingConnectFrom;
      const to = node.id;
      if (!_state.graph.edges.find(ed => ed.from === from && ed.to === to)) {
        _state.graph.edges.push({ from, to });
      }
      _state.pendingConnectFrom = null;
      redrawCanvas();
    }
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const pt = svgPointFromEvent(e);
    node.x = Math.max(0, pt.x - dragging.offsetX);
    node.y = Math.max(0, pt.y - dragging.offsetY);
    redrawCanvas();
  });
  window.addEventListener('mouseup', () => { dragging = null; });
}

function svgPointFromEvent(e) {
  const svg = document.querySelector('svg.wf-svg');
  if (!svg) return { x: 0, y: 0 };
  const pt = svg.createSVGPoint();
  pt.x = e.clientX; pt.y = e.clientY;
  return pt.matrixTransform(svg.getScreenCTM().inverse());
}

function zoom(factor) {
  _state.zoom = Math.max(0.4, Math.min(2, _state.zoom * factor));
  const svg = document.querySelector('svg.wf-svg');
  if (svg) {
    svg.style.transform = `scale(${_state.zoom})`;
    svg.style.transformOrigin = '0 0';
  }
}

// ─────────── properties panel ───────────

function renderNodeProps(root, node) {
  root.innerHTML = '';
  const meta = NODE_TYPES.find(t => t.id === node.type) || { label: node.type };
  root.append(h('div', { class: 'wf-props-title' }, meta.label));
  const idInput = createInput({ label: 'Node id (read-only)', value: node.id });
  idInput.input.disabled = true;
  root.append(idInput);
  const labelInput = createInput({ label: 'Display label', value: node.label || '' });
  labelInput.input.addEventListener('input', (e) => { node.label = e.target.value; redrawCanvas(); });
  root.append(labelInput);

  // Per-type params
  const params = node.params || (node.params = {});
  switch (node.type) {
    case 'greeting': {
      const t = createTextarea({ label: 'Greeting text', value: params.text || '' });
      t.textarea.addEventListener('input', (e) => { params.text = e.target.value; });
      root.append(t);
      const v = createInput({ label: 'Voice', value: params.voice || '' });
      v.input.addEventListener('input', (e) => { params.voice = e.target.value; });
      root.append(v);
      break;
    }
    case 'menu': {
      const t = createInput({ label: 'Prompt', value: params.prompt || '' });
      t.input.addEventListener('input', (e) => { params.prompt = e.target.value; });
      root.append(t);
      root.append(h('div', { style: 'font-size: var(--text-sm); color: var(--color-fg-2); margin-top: 8px;' },
        'Options: digit → target node id. Leave blank to end the call.'));
      const opts = params.options || (params.options = {});
      const knownIds = _state.graph.nodes.map(n => n.id).filter(id => id !== node.id);
      for (const digit of Object.keys(opts)) {
        const sel = createSelect({
          label: `Digit ${digit}`,
          value: opts[digit] || '',
          options: [{ value: '', label: '— end call —' }, ...knownIds.map(i => ({ value: i, label: i }))],
          onChange: (e) => { opts[digit] = e.target.value; redrawCanvas(); },
        });
        root.append(sel);
        const delBtn = createButton({
          variant: 'ghost', size: 'sm',
          onClick: () => { delete opts[digit]; renderNodeProps(root, node); redrawCanvas(); },
          children: `Remove digit ${digit}`,
        });
        root.append(delBtn);
      }
      const addBtn = createButton({
        variant: 'secondary', size: 'sm',
        onClick: () => {
          const nextDigit = String(Object.keys(opts).length + 1);
          opts[nextDigit] = '';
          renderNodeProps(root, node);
        },
        children: 'Add digit',
      });
      root.append(addBtn);
      break;
    }
    case 'forward': {
      const t = createInput({ label: 'Forward to (E.164)', value: params.to || '' });
      t.input.addEventListener('input', (e) => { params.to = e.target.value; });
      root.append(t);
      const f = createInput({ label: 'From (caller id)', value: params.from || '' });
      f.input.addEventListener('input', (e) => { params.from = e.target.value; });
      root.append(f);
      break;
    }
    case 'forward_agent': {
      const s = createInput({ label: 'Skill', value: params.skill || '' });
      s.input.addEventListener('input', (e) => { params.skill = e.target.value; });
      root.append(s);
      const t = createInput({ label: 'Timeout (secs)', value: params.timeout_secs || 30, type: 'number' });
      t.input.addEventListener('input', (e) => { params.timeout_secs = parseInt(e.target.value, 10) || 30; });
      root.append(t);
      break;
    }
    case 'voicemail': {
      const t = createInput({ label: 'Max length (secs)', value: params.max_length_secs || 60, type: 'number' });
      t.input.addEventListener('input', (e) => { params.max_length_secs = parseInt(e.target.value, 10) || 60; });
      root.append(t);
      const tr = createSelect({
        label: 'Transcribe',
        value: params.transcribe ? 'yes' : 'no',
        options: [{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }],
        onChange: (e) => { params.transcribe = e.target.value === 'yes'; },
      });
      root.append(tr);
      break;
    }
    case 'conditional': {
      const cond = createSelect({
        label: 'Condition',
        value: params.condition || 'time_of_day',
        options: [
          { value: 'time_of_day', label: 'Time of day' },
          { value: 'dtmf', label: 'DTMF digit match' },
        ],
        onChange: (e) => { params.condition = e.target.value; redrawCanvas(); },
      });
      root.append(cond);
      if (params.condition === 'dtmf') {
        const d = createInput({ label: 'Digit', value: params.digit || '1' });
        d.input.addEventListener('input', (e) => { params.digit = e.target.value; });
        root.append(d);
      } else {
        const o = createInput({ label: 'Open (HH:MM)', value: params.open || '09:00' });
        o.input.addEventListener('input', (e) => { params.open = e.target.value; });
        root.append(o);
        const c = createInput({ label: 'Close (HH:MM)', value: params.close || '17:00' });
        c.input.addEventListener('input', (e) => { params.close = e.target.value; });
        root.append(c);
        const tz = createInput({ label: 'Timezone (UTC offset)', value: params.tz || 'UTC' });
        tz.input.addEventListener('input', (e) => { params.tz = e.target.value; });
        root.append(tz);
      }
      break;
    }
    case 'webhook': {
      const u = createInput({ label: 'POST URL', value: params.url || '' });
      u.input.addEventListener('input', (e) => { params.url = e.target.value; });
      root.append(u);
      break;
    }
    case 'ai_assistant': {
      const a = createInput({ label: 'Assistant id (ast_…)', value: params.assistant_id || '' });
      a.input.addEventListener('input', (e) => { params.assistant_id = e.target.value; });
      root.append(a);
      break;
    }
    default: break;
  }

  // Delete node
  root.append(h('div', { style: 'margin-top: var(--space-4); border-top: 1px solid var(--color-line); padding-top: var(--space-3);' },
    createButton({
      variant: 'danger', size: 'sm',
      onClick: () => {
        _state.graph.nodes = _state.graph.nodes.filter(n => n.id !== node.id);
        _state.graph.edges = _state.graph.edges.filter(e => e.from !== node.id && e.to !== node.id);
        _state.selectedNodeId = null;
        redrawCanvas();
        renderEmptyProps(root);
      },
      children: 'Delete node',
    })
  ));
}
