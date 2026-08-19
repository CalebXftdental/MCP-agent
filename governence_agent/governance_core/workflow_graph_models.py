"""User-buildable workflow graph records ("My Workflow" — the no-code automation builder).

A WorkflowGraphDefinition projects into the SAME dict shape as the hardcoded
WorkflowTemplate records in gateway/workflows.py (see workflows.get_template /
_graph_template_dict), so the existing /workflows, /workflow-runs, and
automation-scheduling routes work identically for both without any changes to
those routes. GraphNode.node_id becomes a WorkflowStep.step_id 1:1 at run time
(see gateway/workflow_graph_interpreter.py) -- there is deliberately no
separate "run context" structure; a node's resolved output IS its matching
WorkflowStep.outputs, looked up directly, which is what makes resuming a
paused run replay-safe without any new WorkflowRun field.

ONE exception to that 1:1, and only one: a node inside a `loop` node's body runs
once per row, so it records one step per iteration under the synthetic id
`{node_id}#{i}` (workflow_graph_store.ITERATION_SEPARATOR; `#` is a reserved
character in user node ids because of it). Everything else still holds -- those
steps live in the same run.steps, a body node's output is still just its
matching step's outputs, and the loop adds no new WorkflowRun field either. The
scoping that makes "which step is mine" unambiguous is threaded through
execution as a parameter (LoopScope), never stored, which is what keeps this a
local relaxation instead of an ambient one.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    kind: str                                          # "trigger" | "tool_call" | "approval_gate" | "llm_transform" | "filter" | "loop" | "join"
    title: str = ""
    tool: str = ""                                      # canonical tool name; required iff kind == "tool_call"
    config: dict = field(default_factory=dict)          # literal arg values / node-kind-specific settings
    # Reserved config keys for kind == "tool_call" -- popped by the interpreter
    # before the rest of `config` is passed through as literal tool args, never
    # forwarded to the tool itself (see workflow_graph_interpreter.py's
    # _execute_tool_call_node / _exhaust_tool_call):
    #   paginate (bool)          -- repeat this same call, bumping `page`, while
    #                               the tool's own JSON response reports hasMore.
    #                               Legal on ANY tool_call node; a tool with no
    #                               hasMore-shaped output is a safe no-op.
    #   max_pages (int)          -- page-count cap, default 20.
    #   max_duration_sec (float) -- wall-clock cap on the whole loop, default 90.
    #                               Both caps apply together (whichever hits first)
    #                               -- a page-count cap alone doesn't protect
    #                               against a slow/degraded upstream.
    # Config keys for kind == "loop" (validated in workflow_graph_store, executed
    # in workflow_graph_interpreter's _execute_loop_node):
    #   body (list[str])         -- node ids this loop OWNS and runs once per row.
    #                               They stay real nodes with real edges; the loop
    #                               must dominate them and be their single entry.
    #   result_node (str)        -- which body node's output becomes results[i];
    #                               defaults to the body's topological last.
    #   max_iterations (int)     -- hard cap, default 25, ceiling 100.
    #   max_duration_sec (float) -- wall-clock cap, default 300, checked BETWEEN
    #                               iterations. Both caps apply, as for paginate.
    #   on_error ("fail"|        -- stop at the first failed row (default), or
    #             "continue")       record it and carry on.
    #   max_failures (int)       -- only with on_error="continue"; stop once
    #                               exceeded, reporting truncatedReason.
    # The loop's own `input` binding is the list to iterate. Body nodes reference
    # the current row with {"source": "loop_item", "path": ...} and its position
    # with {"source": "loop_index"} -- legal ONLY inside a body.
    # Config keys for kind == "join" (validated in workflow_graph_store, executed
    # in workflow_graph_interpreter's _execute_join_node): merges two already-
    # fetched arrays by a shared key -- e.g. a filter's `matched` customers (left)
    # enriched with a bulk lookup tool's rows (right) -- replacing what would
    # otherwise need a `loop` calling a per-record tool.
    #   left_key/right_key (str)  -- required; the field name to match rows on,
    #                                on the left and right array respectively.
    #   fields               -- which right-row fields to bring onto each merged
    #                            row: "*" (everything except right_key), a list of
    #                            bare field-name strings (kept under the same
    #                            name), or a list mixing those with {"from","as"}
    #                            objects (renamed -- needed when left and right
    #                            happen to share a field name that means something
    #                            different on each side).
    #   on_missing ("keep"|  -- a left row with no right-side match: "keep" (the
    #               "drop")     default) keeps it in `merged` unenriched, "drop"
    #                            excludes it. Either way it's always reported in
    #                            the `unmatched` output array -- never silently
    #                            invisible.
    #   table_name (str)    -- same convention as a filter node's table_name.
    # The join's own `left`/`right` bindings are the two arrays to merge -- no
    # loop_item/loop_index concept here, this node has no per-row body.
    input_bindings: dict = field(default_factory=dict)  # {arg_name: {"source": "node"|"trigger"|"literal"|"loop_item"|"loop_index", ...}}
    position: dict = field(default_factory=dict)        # UI-only {x, y}; ignored by the interpreter

    def public_dict(self) -> dict:
        return {
            "nodeId": self.node_id,
            "kind": self.kind,
            "title": self.title,
            "tool": self.tool,
            "config": dict(self.config),
            "inputBindings": dict(self.input_bindings),
            "position": dict(self.position),
        }


@dataclass(frozen=True)
class GraphEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    # Topology only -- no per-edge field metadata. The bound field name lives in
    # the TARGET node's input_bindings, so there is exactly one source of truth
    # for "what is wired to what," not a split between edge and node.

    def public_dict(self) -> dict:
        return {"edgeId": self.edge_id, "sourceNodeId": self.source_node_id, "targetNodeId": self.target_node_id}


@dataclass(frozen=True)
class GraphCheck:
    """One structured result from validate_graph -- a stable machine `check`
    id plus the same human-readable `message` it always produced, now with
    `severity` and (where applicable) which node it concerns broken out as
    real fields instead of embedded ad hoc in message text. Every check is
    "error" severity today (validate_graph has no lesser-severity checks
    yet); the field exists so a caller (e.g. the workflow copilot's
    scratchpad) can distinguish severities if/when one is ever added, rather
    than requiring every future check to also be a hard blocker."""
    check: str
    severity: str
    message: str
    node_id: str | None = None

    def public_dict(self) -> dict:
        return {"check": self.check, "severity": self.severity, "message": self.message, "nodeId": self.node_id}


@dataclass(frozen=True)
class WorkflowGraphVersion:
    version: int
    created_by: str
    created_at: float
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    notes: str = ""

    def public_dict(self) -> dict:
        return {
            "version": self.version,
            "createdBy": self.created_by,
            "createdAt": self.created_at,
            "nodes": [n.public_dict() for n in self.nodes],
            "edges": [e.public_dict() for e in self.edges],
            "notes": self.notes,
        }


@dataclass(frozen=True)
class WorkflowGraphDefinition:
    graph_id: str                       # this IS the template_id used everywhere downstream (workflows.py, automations, agents)
    display_name: str
    description: str = ""
    owner: str = ""                     # builder's consumer name -- used for build-time grant checks
    status: str = "draft"               # draft|active|disabled -- same vocabulary as WorkflowTemplate.status
    published_version: int = 0          # 0 = never published; new runs always pin to this version
    current_version: int = 1            # latest saved (possibly still-draft) version being edited
    versions: list[WorkflowGraphVersion] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def version_record(self, version: int | None = None) -> WorkflowGraphVersion | None:
        target = version if version is not None else self.current_version
        return next((v for v in self.versions if v.version == target), None)

    def public_dict(self, include_nodes: bool = False) -> dict:
        data = {
            "graphId": self.graph_id,
            "displayName": self.display_name,
            "description": self.description,
            "owner": self.owner,
            "status": self.status,
            "publishedVersion": self.published_version,
            "currentVersion": self.current_version,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if include_nodes:
            latest = self.version_record()
            data["nodes"] = [n.public_dict() for n in latest.nodes] if latest else []
            data["edges"] = [e.public_dict() for e in latest.edges] if latest else []
        return data
