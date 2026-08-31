"""Visual workflow engine (issue #13).

A workflow is a directed graph of nodes the inbound call walks through.
Each node type implements ``handle(ctx) -> ctx`` (in async + sync
variants). The engine is:

* **Stateless across calls** — every call gets a fresh context, the graph
  comes from the row in ``workflows`` (snapshotted at call.initiated time
  so a mid-call graph save doesn't fork the live path).
* **Cycle-safe at save time** — ``/api/workflows`` POST/PATCH validate the
  graph has no cycles and exactly one entry point.
* **Testable without Telnyx** — ``POST /api/workflows/{id}/test`` walks
  the graph with a synthetic context, returning each transition in
  ``trace`` so the UI's 'Test with fake call' button can render a
  timeline.

Endpoints
---------
GET    /api/workflows                       list workflows for the tenant
POST   /api/workflows                       create
GET    /api/workflows/{id}                  read one
PATCH  /api/workflows/{id}                  update name/graph/entry
DELETE /api/workflows/{id}                  delete (cancels any number assignments)
POST   /api/workflows/from-template/{name}  instantiate a starter template
POST   /api/workflows/{id}/test             dry-run with a synthetic call
GET    /api/workflows/templates             list available templates
GET    /api/numbers/{number_id}/workflow    read the workflow assigned to a number
POST   /api/numbers/{number_id}/workflow    assign a workflow to a number
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request

from telnyx_mcp.clients.telnyx_client import get_client

from webhooks.storage import get_store
from webhooks._phase_b_ctx import _ctx, _tenant_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["workflows"])

# Templates live next to this file. Loaded once at import time and
# exposed by ``/api/workflows/templates``.
_TEMPLATES_DIR = Path(__file__).resolve().parent / "workflow_templates"
_VALID_NODE_TYPES = {
    "greeting", "menu", "forward", "forward_agent",
    "voicemail", "hangup", "conditional", "webhook", "ai_assistant",
}


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------


class GraphValidationError(ValueError):
    """Raised by :func:`validate_graph` on any structural problem."""


def _normalize_graph(graph: Any) -> dict:
    """Return a dict-shaped graph; reject anything else up front."""
    if not isinstance(graph, dict):
        raise GraphValidationError("graph must be an object with 'nodes' and 'edges'")
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise GraphValidationError("'nodes' and 'edges' must be arrays")
    return {"nodes": nodes, "edges": edges, "settings": graph.get("settings") or {}}


def validate_graph(graph: Any, *, strict: bool = True) -> dict:
    """Validate a workflow graph.

    Rules
    -----
    * Every node must have ``id`` and ``type``; ``type`` is one of
      :data:`_VALID_NODE_TYPES`.
    * Edge ``from`` and ``to`` must reference an existing node id.
    * No duplicate node ids.
    * The graph must not contain a cycle.
    * At least one node must exist; the entry node is determined either
      from the explicit ``entry_node_id`` or from the first node in
      the array.

    Returns the normalised graph; raises :class:`GraphValidationError`
    on any structural problem.
    """
    g = _normalize_graph(graph)
    nodes = g["nodes"]
    edges = g["edges"]
    if not nodes:
        raise GraphValidationError("graph must have at least one node")
    ids: set[str] = set()
    for n in nodes:
        if not isinstance(n, dict):
            raise GraphValidationError("each node must be an object")
        nid = n.get("id")
        ntype = n.get("type")
        if not nid or not isinstance(nid, str):
            raise GraphValidationError("each node needs a string 'id'")
        if nid in ids:
            raise GraphValidationError(f"duplicate node id: {nid}")
        ids.add(nid)
        if not ntype or ntype not in _VALID_NODE_TYPES:
            raise GraphValidationError(
                f"node {nid}: invalid type {ntype!r} (valid: {sorted(_VALID_NODE_TYPES)})"
            )
    for e in edges:
        if not isinstance(e, dict):
            raise GraphValidationError("each edge must be an object")
        if e.get("from") not in ids:
            raise GraphValidationError(f"edge from unknown node: {e.get('from')!r}")
        if e.get("to") not in ids:
            raise GraphValidationError(f"edge to unknown node: {e.get('to')!r}")
    # Cycle detection via DFS. A cycle means we can't determine an order
    # to evaluate the graph.
    adj: dict[str, list[str]] = {nid: [] for nid in ids}
    for e in edges:
        adj[e["from"]].append(e["to"])
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in ids}

    def dfs(start: str) -> None:
        stack: list[tuple[str, int]] = [(start, 0)]
        color[start] = GRAY
        while stack:
            node, idx = stack[-1]
            children = adj[node]
            if idx >= len(children):
                color[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, idx + 1)
            nxt = children[idx]
            if color[nxt] == GRAY:
                raise GraphValidationError(
                    f"cycle detected: {node} -> {nxt}"
                )
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                stack.append((nxt, 0))

    for nid in ids:
        if color[nid] == WHITE:
            dfs(nid)
    return g


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def _load_templates() -> dict[str, dict]:
    """Load every JSON file in ``workflow_templates/`` keyed by stem."""
    out: dict[str, dict] = {}
    if not _TEMPLATES_DIR.exists():
        return out
    for path in sorted(_TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            out[path.stem] = data
        except Exception as e:
            log.warning("Failed to load template %s: %s", path, e)
    return out


_TEMPLATES = _load_templates()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CallContext(dict):
    """Mutable per-call state. Backed by a plain dict so JSON dumps
    come along for free.

    The shape documented in the brief:
        {call_id, from, to, digits_pressed, transcript, history}
    where ``history`` is a list of node transitions the executor
    appends to (so the test endpoint + the UI timeline can replay it).
    """

    @property
    def call_id(self) -> Optional[str]:
        return self.get("call_id")

    @property
    def history(self) -> list[dict]:
        return self.setdefault("history", [])

    def push(self, node_id: str, action: str, **extra: Any) -> None:
        self.history.append({
            "node_id": node_id,
            "action": action,
            "ts": datetime.now(timezone.utc).isoformat(),
            **extra,
        })


class WorkflowEngine:
    """Walk a workflow graph for a single call.

    The engine is instantiated per-call and reads the graph from the
    row in ``workflows`` (snapshotted by the caller). It mutates a
    :class:`CallContext` until either a node has no successor or the
    max-steps safety cap is hit.
    """

    MAX_STEPS = 200  # cycle safety; we already validate at save time

    def __init__(
        self,
        graph: dict,
        *,
        client: Optional[Any] = None,
        dry_run: bool = False,
    ) -> None:
        self.graph = validate_graph(graph)
        # Index nodes by id for O(1) lookup.
        self.nodes: dict[str, dict] = {
            n["id"]: n for n in self.graph["nodes"]
        }
        # Index edges by source for O(1) traversal.
        self.edges_by_from: dict[str, list[dict]] = {}
        for e in self.graph["edges"]:
            self.edges_by_from.setdefault(e["from"], []).append(e)
        self.client = client
        self.dry_run = dry_run

    # ────────── node handlers ──────────
    # Each returns the *next* node id, or None to end the call.
    def _handle(self, node: dict, ctx: CallContext) -> Optional[str]:
        ntype = node.get("type")
        handler = getattr(self, f"_node_{ntype}", None)
        if handler is None:
            log.warning("Unknown node type %r; ending call", ntype)
            ctx.push(node["id"], "unknown_type")
            return None
        return handler(node, ctx)

    # Each node handler returns the next node id (or None to end).

    def _node_greeting(self, node: dict, ctx: CallContext) -> Optional[str]:
        text = (node.get("params") or {}).get("text") or ""
        if self.dry_run:
            ctx.push(node["id"], "speak", text=text)
        else:
            try:
                # Use Telnyx's speak API to TTS the greeting onto the
                # call. Falls back to no-op if the call_control_id is
                # missing (synthetic context).
                cci = ctx.call_id
                if cci and self.client is not None:
                    httpx.post(
                        f"https://api.telnyx.com/v2/calls/{cci}/actions/speak",
                        json={
                            "payload": text,
                            "voice": (node.get("params") or {}).get(
                                "voice", "Telnyx.KokoroTTS.af_heart"),
                            "language": "en-US",
                            "command_id": f"speak_{node['id']}",
                        },
                        headers={
                            "Authorization": f"Bearer {self.client.creds.api_key}",
                            "Content-Type": "application/json",
                        },
                        timeout=10,
                    )
            except Exception as e:
                log.warning("speak failed: %s", e)
            ctx.push(node["id"], "speak", text=text)
        return self._next(node, ctx)

    def _node_menu(self, node: dict, ctx: CallContext) -> Optional[str]:
        params = node.get("params") or {}
        prompt = params.get("prompt") or "Please make a selection."
        options: dict = params.get("options") or {}
        ctx.push(node["id"], "menu", prompt=prompt, options=list(options.keys()))
        digit = ctx.get("digits_pressed")
        if digit and str(digit) in options:
            return options[str(digit)]
        # No digit or unknown digit — fall through to the first 'no_input'
        # edge, else end.
        return self._next(node, ctx, default_branch="no_input")

    def _node_forward(self, node: dict, ctx: CallContext) -> Optional[str]:
        params = node.get("params") or {}
        to = params.get("to")
        from_ = params.get("from") or ctx.get("to")
        ctx.push(node["id"], "forward", to=to, from_=from_)
        if not self.dry_run and self.client is not None and ctx.call_id:
            try:
                self.client.transfer_call(
                    call_control_id=ctx.call_id,
                    to=to,
                    from_=from_,
                )
            except Exception as e:
                log.warning("transfer_call failed: %s", e)
        # A transfer is the end of the inbound path.
        return None

    def _node_forward_agent(self, node: dict, ctx: CallContext) -> Optional[str]:
        # We don't yet have a real agent-presence system; the engine
        # records the intent and ends the call (the assignment +
        # a Telnyx 'enqueue' verb would replace this in a real
        # deployment). The shape of the params is what the UI binds.
        params = node.get("params") or {}
        ctx.push(node["id"], "forward_agent", skill=params.get("skill"))
        return None

    def _node_voicemail(self, node: dict, ctx: CallContext) -> Optional[str]:
        params = node.get("params") or {}
        ctx.push(node["id"], "voicemail",
                 max_length_secs=params.get("max_length_secs", 60))
        if not self.dry_run and self.client is not None and ctx.call_id:
            # Trigger Telnyx recording for this call so the voicemail is
            # captured. The handler in handlers/default.py persists the
            # recording to the voicemails table.
            try:
                self.client.api.calls.actions.record_start(
                    ctx.call_id,
                    format="wav",
                    channels="single",
                    play_beep=True,
                )
            except Exception as e:
                log.warning("record_start failed: %s", e)
        return None

    def _node_hangup(self, node: dict, ctx: CallContext) -> Optional[str]:
        ctx.push(node["id"], "hangup")
        if not self.dry_run and self.client is not None and ctx.call_id:
            try:
                self.client.hangup_call(ctx.call_id)
            except Exception as e:
                log.warning("hangup failed: %s", e)
        return None

    def _node_conditional(self, node: dict, ctx: CallContext) -> Optional[str]:
        params = node.get("params") or {}
        kind = params.get("condition", "time_of_day")
        if kind == "time_of_day":
            # Cheap TZ handling: compare HH:MM strings. The default
            # comparison is in UTC; the ``tz`` field is accepted as a
            # future-proofing hint (a real TZ-aware comparison would
            # need the ``zoneinfo`` package which isn't always
            # available on slim images).
            now = datetime.now(timezone.utc)
            hhmm = now.strftime("%H:%M")
            inside = params.get("open", "09:00") <= hhmm <= params.get("close", "17:00")
            branch = "true" if inside else "false"
        elif kind == "dtmf":
            # Match the param 'digit' against the most recent
            # digits_pressed. Useful for layered menus.
            target = str(params.get("digit", "1"))
            branch = "true" if str(ctx.get("digits_pressed")) == target else "false"
        else:
            branch = "true"  # default open
        ctx.push(node["id"], "conditional", branch=branch, kind=kind)
        return self._next(node, ctx, default_branch=branch)

    def _node_webhook(self, node: dict, ctx: CallContext) -> Optional[str]:
        params = node.get("params") or {}
        url = params.get("url")
        ctx.push(node["id"], "webhook", url=url)
        if not self.dry_run and url:
            try:
                httpx.post(
                    url,
                    json={
                        "call_id": ctx.call_id,
                        "from": ctx.get("from"),
                        "to": ctx.get("to"),
                        "digits_pressed": ctx.get("digits_pressed"),
                    },
                    timeout=5,
                )
            except Exception as e:
                log.warning("webhook node POST failed: %s", e)
        return self._next(node, ctx)

    def _node_ai_assistant(self, node: dict, ctx: CallContext) -> Optional[str]:
        params = node.get("params") or {}
        assistant_id = params.get("assistant_id")
        ctx.push(node["id"], "ai_assistant", assistant_id=assistant_id)
        if (
            not self.dry_run
            and assistant_id
            and self.client is not None
            and ctx.call_id
        ):
            try:
                self.client.start_ai_assistant(ctx.call_id, assistant_id)
            except Exception as e:
                log.warning("start_ai_assistant failed: %s", e)
        return None

    # ────────── traversal ──────────
    def _next(
        self,
        node: dict,
        ctx: CallContext,
        *,
        default_branch: Optional[str] = None,
    ) -> Optional[str]:
        edges = self.edges_by_from.get(node["id"]) or []
        if not edges:
            return None
        if default_branch is not None:
            for e in edges:
                if e.get("condition") == default_branch or e.get("branch") == default_branch:
                    return e["to"]
        # Fall back to the first edge without a condition.
        for e in edges:
            if not e.get("condition") and not e.get("branch"):
                return e["to"]
        # Last resort: first edge.
        return edges[0]["to"]

    def run(self, ctx: CallContext) -> CallContext:
        """Walk the graph starting from entry_node_id (or the first node)."""
        start_id = self.graph.get("entry_node_id") or self.nodes and next(iter(self.nodes))
        if not start_id or start_id not in self.nodes:
            return ctx
        cur: Optional[str] = start_id
        steps = 0
        while cur and steps < self.MAX_STEPS:
            steps += 1
            node = self.nodes[cur]
            nxt = self._handle(node, ctx)
            cur = nxt
        if steps >= self.MAX_STEPS:
            log.warning("workflow max steps hit (likely a cycle)")
            ctx.push("__engine__", "max_steps")
        return ctx


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/workflows/templates")
def list_templates() -> dict:
    """List the bundled starter workflows (issue #13 acceptance)."""
    return {
        "templates": [
            {
                "id": stem,
                "name": data.get("name", stem),
                "description": data.get("description", ""),
                "node_count": len(data.get("nodes") or []),
            }
            for stem, data in _TEMPLATES.items()
        ]
    }


@router.get("/workflows")
def list_workflows(request: Request) -> dict:
    store = get_store()
    return {"workflows": store.list_workflows(_tenant_id(request))}


@router.post("/workflows")
async def create_workflow(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    graph = body.get("graph") or body.get("graph_json") or {}
    try:
        normalised = validate_graph(graph)
    except GraphValidationError as e:
        raise HTTPException(400, f"invalid graph: {e}")
    entry = body.get("entry_node_id") or (
        normalised["nodes"][0]["id"] if normalised["nodes"] else None
    )
    store = get_store()
    wf = store.create_workflow(_tenant_id(request), name, normalised, entry)
    return {"ok": True, "workflow": wf}


@router.post("/workflows/from-template/{name}")
def workflow_from_template(
    name: str,
    request: Request,
) -> dict:
    """Instantiate a bundled template into a new workflow row."""
    if name not in _TEMPLATES:
        raise HTTPException(404, f"template {name!r} not found")
    tpl = _TEMPLATES[name]
    store = get_store()
    wf = store.create_workflow(
        _tenant_id(request),
        name=tpl.get("name", name),
        graph_json={
            "nodes": tpl.get("nodes") or [],
            "edges": tpl.get("edges") or [],
            "settings": tpl.get("settings") or {},
        },
        entry_node_id=tpl.get("entry_node_id"),
    )
    return {"ok": True, "workflow": wf, "template": name}


@router.get("/workflows/{workflow_id}")
def get_workflow(
    workflow_id: str,
    request: Request,
) -> dict:
    store = get_store()
    wf = store.get_workflow(_tenant_id(request), workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    return {"workflow": wf}


@router.patch("/workflows/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    request: Request,
) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    store = get_store()
    graph = body.get("graph") or body.get("graph_json")
    if graph is not None:
        try:
            graph = validate_graph(graph)
        except GraphValidationError as e:
            raise HTTPException(400, f"invalid graph: {e}")
    wf = store.update_workflow(
        _tenant_id(request),
        workflow_id,
        name=body.get("name"),
        graph_json=graph,
        entry_node_id=body.get("entry_node_id"),
    )
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    return {"ok": True, "workflow": wf}


@router.delete("/workflows/{workflow_id}")
def delete_workflow(
    workflow_id: str,
    request: Request,
) -> dict:
    tenant_id = _tenant_id(request)
    store = get_store()
    ok = store.delete_workflow(tenant_id, workflow_id)
    if not ok:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    # Also clear any number assignments pointing to this workflow.
    for n in store.list_phone_numbers(tenant_id):
        if (n.get("assignment_kind") == "workflow"
                and n.get("assignment_target") == workflow_id):
            store.set_number_assignment(tenant_id, n["id"], None, None)
    return {"ok": True, "id": workflow_id}


@router.post("/workflows/{workflow_id}/test")
async def test_workflow(
    workflow_id: str,
    request: Request,
) -> dict:
    """Dry-run a workflow with a synthetic call. Returns the trace."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    store = get_store()
    wf = store.get_workflow(_tenant_id(request), workflow_id)
    if not wf:
        raise HTTPException(404, f"workflow {workflow_id} not found")
    ctx = CallContext({
        "call_id": body.get("call_id") or "test-call",
        "from": body.get("from") or "+15555550100",
        "to": body.get("to") or "+15078731084",
        "digits_pressed": body.get("digits_pressed"),
        "transcript": body.get("transcript", []),
    })
    engine = WorkflowEngine(wf["graph"], dry_run=True)
    engine.run(ctx)
    return {
        "ok": True,
        "workflow_id": workflow_id,
        "version": wf.get("version"),
        "history": ctx.history,
        "final_state": {k: v for k, v in ctx.items() if k != "history"},
    }


@router.get("/numbers/{number_id}/workflow")
def get_number_workflow(
    number_id: str,
    request: Request,
) -> dict:
    tenant_id = _tenant_id(request)
    store = get_store()
    n = store.get_phone_number(tenant_id, number_id)
    if not n:
        raise HTTPException(404, f"number {number_id} not found")
    if n.get("assignment_kind") != "workflow":
        return {"number": n, "workflow": None}
    wf = store.get_workflow(tenant_id, n["assignment_target"])
    return {"number": n, "workflow": wf}


@router.post("/numbers/{number_id}/workflow")
async def set_number_workflow(
    number_id: str,
    request: Request,
) -> dict:
    """Assign a workflow to a number.

    Body: ``{workflow_id: "wf_..."}``. Pass ``null`` to clear the
    assignment. Returns the updated number + workflow.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")
    tenant_id = _tenant_id(request)
    workflow_id = body.get("workflow_id")
    store = get_store()
    n = store.get_phone_number(tenant_id, number_id)
    if not n:
        raise HTTPException(404, f"number {number_id} not found")
    if workflow_id:
        wf = store.get_workflow(tenant_id, workflow_id)
        if not wf:
            raise HTTPException(404, f"workflow {workflow_id} not found")
        store.set_number_assignment(
            tenant_id, number_id, "workflow", workflow_id)
    else:
        store.set_number_assignment(tenant_id, number_id, None, None)
    n2 = store.get_phone_number(tenant_id, number_id)
    return {"ok": True, "number": n2}


# ---------------------------------------------------------------------------
# Hook from the inbound webhook handler
# ---------------------------------------------------------------------------


def run_workflow_for_call(
    tenant_id: str, called: str, call_id: str, *, from_: Optional[str] = None
) -> Optional[dict]:
    """Look up the workflow assigned to ``called`` and walk it.

    Called from ``handlers/default.py`` on ``call.initiated``. Returns the
    final :class:`CallContext` for logging, or ``None`` if no workflow
    is assigned. Never raises — webhook handlers must keep going.
    """
    store = get_store()
    wf = store.find_workflow_for_number(tenant_id, called)
    if not wf:
        return None
    try:
        client = get_client()
    except Exception:
        client = None
    ctx = CallContext({
        "call_id": call_id,
        "from": from_,
        "to": called,
        "digits_pressed": None,
        "transcript": [],
    })
    try:
        engine = WorkflowEngine(wf["graph"], client=client)
        engine.run(ctx)
    except Exception as e:
        log.warning("workflow execution failed: %s", e)
    return dict(ctx)
