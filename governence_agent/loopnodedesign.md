# Loop / For-Each Node Design — `GraphNode.kind == "loop"`

**Status:** Proposed, rev 2 (design only — nothing here is built yet).
**Supersedes:** rev 1 of this file, and `finalize_stage_1.md` §3.2b (the
"for-each / fan-out per row" gap), now that §3.2a (paginate) has shipped
(`gateway/workflow_graph_interpreter.py::_exhaust_tool_call`, verified against
real production data — `_smoke/test_paginate_node_live.py`).
**Scope:** How a "My Workflow" graph runs a span of steps once per row of an
array rather than once per run — e.g. "for each matched customer, draft a
*personalized* win-back email," which paginate cannot do (paginate re-fetches
the same call; it has no body of other nodes to run per item).

**Every code claim below was re-verified against the working tree** at the time
of writing (branch `main`, after the paginate work). File:line references are
given so the next reader can re-check rather than trust. Anything still
undecided is called out as an **open question** and must be answered before the
step it blocks — there are exactly two left, both in §11.

---

## 0. Rev 1 → rev 2: what changed and why

Rev 1 had the right *shape* (a Map/fan-out node, sequential v1, synthetic
per-iteration step ids, paginate kept separate) — that shape matches what AWS
Step Functions (`Map`/`ItemProcessor`), Airflow (dynamic task mapping) and
Temporal (child workflows) all converged on, and it is kept. What it did not
have was production semantics. Six defects were found by reading the live code
against the draft; each one is fixed by a specific section below.

| # | Rev 1 said | Actually | Fixed in |
|---|---|---|---|
| 1 | Synthetic step ids `{node_id}#{i}` are enough | Rev 1 scoped only *step recording*, never *binding resolution*. `_resolve_binding` looks up the bare `node_id` (`workflow_graph_interpreter.py:88`); inside a loop no such step exists, so a body node wired to another body node resolves `None`, and `_resolve_node_args:104` then **drops the argument and calls the tool anyway**. Two-node bodies (draft → create) are the normal case and would have failed invisibly. | §4 (scope rules) |
| 2 | Body nodes are excluded from the top-level `order` walk and from edges | Edges are *derived*, never hand-authored — `chainEdges` in Step view (`MyWorkflowsPage.tsx:142`), `derivedEdges` in Canvas view (`graphModel.ts:121`), and a node with no node-source binding gets an implicit **trigger →** edge (`graphModel.ts:135`). Removing body nodes from the DAG therefore deletes the only guarantee that an outer node a body node reads from runs *before* the loop. | §3 (structured region) |
| 3 | Exempt body nodes from the `unreachable_node` check | The mandatory gate check flags a send-risk tool only if `n.node_id in reachable_without_gates` (`workflow_graph_store.py:349-362`). Exempting the body from reachability makes a send-risk tool inside a body **stop being flagged at all** — the exemption is itself the security regression, in v1, not v2. | §3, §5 |
| 4 | Banning `approval_gate` from the body means v1 can never pause mid-loop | False. There is a second, independent pause path with no `approval_gate` node involved: any `EXPORT`-risk tool_call can trigger the broad-export approval and return mid-walk (`workflow_graph_interpreter.py:484-495`, `backend/workflow_api.py:633`). A body containing `create_excel_report` pauses at iteration k. (The motivating case survives: `create_email_draft` is `risk=WRITE` — `policy/manifest.py:846-851` — so it never trips this.) | §5 (`loop_body_forbidden_risk`) |
| 5 | v2 gives each iteration its own approval | Impossible against today's resume: `workflows.resume_run:874-884` marks **every** pending/running approval step completed regardless of `approval_id`. With N pending per-iteration gates, approving one releases all N sends. | §10 (v2 prerequisite) |
| 7 | *(silent — never mentioned the LLM)* | With `USE_LOCAL_LLM=true` (the default), every `llm_transform` shares **one** self-hosted Qwen server with 3 concurrent slots, and our client calls it **synchronously on the asyncio event loop** (`orchestrator.py:519-532`, called at `:742` and `workflow_graph_interpreter.py:256`). A 25-iteration LLM body is 7.5–39 minutes during which the gateway serves *nobody* — while using 1 of 3 GPU slots. | §7.4 |
| 6 | `max_iterations: 500` | Not survivable as written. Every `add_step`/`complete_step` goes through `update_run` → `_save()`, which rewrites the **entire** runs JSON file (`workflows.py:431-451,177`); `run_dict:258` then ships every step to the UI. 500 iterations × a 2-node body ≈ 2,000 whole-file rewrites. And `run_graph` is awaited **inline in the HTTP handler** (`backend/workflow_api.py:99`), so that is one multi-minute request. | §6 (limits), §7 (durability) |

Rev 1's §5 also declared `max_iterations` "non-negotiable" while omitting the
exact lesson paginate had already learned and that the same document cited: a
count cap alone does not protect against a slow upstream, which is why every
paginate loop is bounded by **both** count and wall-clock
(`workflow_graph_interpreter.py:124-125`, validated at build time in
`workflow_graph_store.py:338-347`). Rev 2 applies the same dual bound (§6).

---

## 1. Invariants this design must not break

The interpreter's module docstring (`workflow_graph_interpreter.py:1-19`) names
three load-bearing invariants. Loop is the first feature that touches all
three, so state up front exactly what happens to each:

| Invariant | Status under this design |
|---|---|
| "A node whose step already exists is never re-executed" | **Preserved, verbatim** — keyed on the *effective step id*, which is the synthetic `{node_id}#{i}` inside a body and the bare `node_id` everywhere else. |
| "A node's inputs are always resolved by reading `run.steps`/`run.inputs` directly, never from a separate in-memory cache" | **Preserved.** `loop_item` is *not* a cache: it is a value derived from the loop node's own already-durable input array, re-derivable on any re-walk from `run.steps` alone (§4.3). No new `WorkflowRun` field. |
| "`GraphNode.node_id == WorkflowStep.step_id` by construction" | **Relaxed, deliberately, exactly once** — for nodes owned by a loop. This is the one invariant loop cannot keep, and the entire rest of the design exists to make the relaxation local and checkable rather than ambient. `workflow_graph_models.py:1-12`'s module note must be updated in the same commit. |

Anything that would relax a *fourth* invariant is out of scope. In particular
this design adds **no new `WorkflowRun` field and no separate run-context
structure** — that is what keeps resume replay-safe.

---

## 2. Decisions locked

| # | Decision | Choice |
|---|---|---|
| 1 | Concurrency | **Sequential only, v1** — and now for a second, harder reason than rev 1 gave. Rev 1's reason stands (bodies create durable artifacts and, in v2, pending approvals; sequential keeps the audit trail and approval ordering single-threaded). The new one: with `USE_LOCAL_LLM=true` the whole app shares one self-hosted GPU with **3 concurrent request slots**, of which a batch workload may use exactly one (§7.4). `max_concurrency` for an LLM-bearing body is capped by hardware, not preference. |
| 2 | Paginate vs. loop | **Separate, confirmed.** They compose: a loop's input array is typically a `paginate:true` tool_call node's merged output or a `filter` node's `matched`. Neither replaces the other. |
| 3 | Body membership | **A structured region, not an id set** — the loop node must *dominate* every body node, and the body must be a contiguous single-entry region (§3). This is what lets every existing graph check keep working unchanged and fail-closed. |
| 4 | Step identity for body nodes | `{node_id}#{i}`, 0-based (`n_draft#0`). `#` becomes a reserved character in user node ids (§5, mirroring `RESERVED_NODE_ID_SUFFIX` at `workflow_graph_store.py:32`). |
| 5 | Caps | Dual bound, both mandatory, both surfaced: `max_iterations` **and** `max_duration_sec` — same convention and same vocabulary as paginate (`truncated` / `truncatedReason`). Plus a build-time governed-call ceiling (§6). |
| 6 | Failure policy | Explicit and authored, not implied: `on_error: "fail"` (default, matches today's "one failed step fails the run") or `"continue"` with `max_failures`. A fan-out that dies at iteration 87 of 100 having already created 86 artifacts must not be indistinguishable from one that died at iteration 1 (§8). |
| 7 | v1 body kinds | `tool_call` (risk `READ`/`WRITE` only) and `llm_transform`. No `approval_gate`, no `filter`, no nested `loop`, and — new in rev 2 — **no `EXPORT`-risk tool**, because `EXPORT` has its own implicit pause path (rev-2 finding #4). |

---

## 3. The model: a loop is a structured region, not an exempt id list

This is the one architectural change from rev 1, and everything else follows
from it.

**Rev 1:** body nodes leave the DAG and get exempted from validation.
**Rev 2:** body nodes stay in the DAG, keep their edges, and are *owned* by the
loop. The top-level walk skips **executing** them; it does not skip **ordering**
them, and nothing skips **validating** them.

Concretely, a graph is legal iff:

1. Every id in `config.body` names a real node in the same version.
2. **Dominance** — every path from the trigger to a body node passes through
   the loop node. Computable with the machinery already in the store: a body
   node must be unreachable from the trigger once the loop node is removed
   (`_reachable_from(trigger, ..., exclude_ids={loop_id})` — the existing helper
   at `workflow_graph_store.py:218` already supports severing by node, it just
   takes `exclude_kinds` today and needs an `exclude_ids` sibling).
3. **Single entry** — exactly one body node has an in-edge from the loop node;
   every other body node's in-edges come only from other body nodes in the same
   loop.
4. **No leakage out** — no node outside the body may bind to a body node's
   output. A per-iteration value has no single meaning outside the loop;
   downstream consumers read the loop node's aggregate output instead (§4.4).
5. **Non-overlap** — a node belongs to at most one loop's body.

Why this is worth the extra validation:

- `_topological_order` (`workflow_graph_store.py:202`) is unchanged, and it
  still orders every outer producer before the loop, because the body's edges
  are still real. Rev-2 finding #2 disappears.
- `unreachable_node` (`:293`) is unchanged — body nodes *are* reachable, via the
  loop. No exemption, so rev-2 finding #3 disappears with it.
- The mandatory send-gate check (`:349-362`) is unchanged and stays
  **fail-closed**: a send-risk tool in a body is still `reachable_without_gates`
  from the trigger and is still blocked. (v1 forbids it outright anyway — but
  the check is what makes that belt-and-braces rather than a single point of
  failure.)
- Both builder views already produce the required edges for free. In Step view
  a loop is a contiguous span and `chainEdges` (`MyWorkflowsPage.tsx:142`)
  emits `loop → body[0] → body[1] → next` naturally. In Canvas view only one
  small change is needed (§9).

Dominance + single-entry is the standard structured-region formulation — the
same one Step Functions enforces by making `ItemProcessor` a nested state
machine and Airflow enforces with `TaskGroup`. We get the same guarantee
without a nested document, because the region is expressed as ownership over a
flat node list.

---

## 4. Config, binding scope, and output

### 4.1 Node config

```jsonc
{
  "nodeId": "n_loop",
  "kind": "loop",
  "title": "For each matched customer",
  "inputBindings": {
    // The array to iterate. Must resolve to a list (§8.1).
    "input": {"source": "node", "node_id": "n_filter", "path": "matched"}
  },
  "config": {
    "body": ["n_draft_email", "n_create_draft"],  // owned nodes; order is the EDGES', not this list's
    "item_binding_name": "loop_item",             // reserved; see §4.2. Rename support is v2.
    "result_node": "n_create_draft",              // which body node's output becomes results[i];
                                                  // defaults to the body's topological last
    "max_iterations": 25,                         // dual bound -- see §6
    "max_duration_sec": 300,
    "on_error": "fail",                           // "fail" (default) | "continue"
    "max_failures": 0                             // only meaningful when on_error == "continue"
  }
}
```

`body` is a membership declaration, not an ordering. Execution order inside the
body is the topological order of the body's own edges — one source of truth for
"what runs after what," exactly as `GraphEdge`'s own note demands
(`workflow_graph_models.py:53-60`).

### 4.2 The `loop_item` binding source

```jsonc
{"source": "loop_item"}                    // the whole item (scalar arrays: a plain id/string)
{"source": "loop_item", "path": "customerId"}   // one key out of a row dict
{"source": "loop_index"}                   // 0-based iteration number
```

- Legal **only** on a node owned by a loop; a `loop_item` binding anywhere else
  is a build-time blocker (§5). This matters because `_resolve_binding:90`
  returns `None` for an unrecognized source and `_resolve_node_args:104` then
  silently omits the argument — precisely the silent-misconfiguration class
  this codebase rejects at author time.
- A `path` naming a key the row does not have follows the **existing**
  convention: resolves `None`, argument omitted, tool default applies
  (`workflow_graph_interpreter.py:96-107`). No new behaviour is invented here;
  the builder surfaces it as a warning instead (§9).

### 4.3 Scope resolution rules (the complete set)

Inside iteration `i` of loop `L`, `_resolve_binding` resolves:

| Binding | Resolves to |
|---|---|
| `{"source": "loop_item"}` / `{"source": "loop_index"}` | The current item / `i`. Legal only inside a body. |
| `{"source": "node", "node_id": X}` where X ∈ `L.body` | Step `X#{i}` — **the current iteration's** output. This is rev-2 finding #1's fix, and it lives in the resolver, not in the step-id generator. |
| `{"source": "node", "node_id": X}` where X ∉ `L.body` | Step `X` — global, unchanged. |
| `{"source": "trigger"}` / `{"source": "literal"}` | Unchanged. |

Implementation shape: `_resolve_binding(binding, run)` gains one optional
parameter — an immutable `LoopScope(item, index, owned_ids)` — threaded through
`_resolve_node_args` and each `_execute_*_node`. **Not** a module-global and
**not** stored on the run: threading it is what keeps nested loops (v2) from
colliding and keeps the "no separate run context" invariant true (§1).

### 4.4 Aggregate output (on the loop node's own step)

```jsonc
{
  "itemCount": 117,          // what the input array actually had
  "iterations": 25,          // how many ran
  "succeeded": 24,
  "failed": 1,
  "results": [ /* result_node's output per SUCCEEDED iteration, trimmed per §7.2 */ ],
  "errors": [ {"index": 12, "nodeId": "n_create_draft", "message": "..."} ],
  "artifactIds": ["af_...", "af_..."],   // every artifact any iteration produced
  "truncated": true,
  "truncatedReason": "max_iterations"    // null | max_iterations | max_duration | cancelled | max_failures
}
```

`artifactIds` is not optional bookkeeping: the interpreter's
`accumulated_artifact_ids` (`workflow_graph_interpreter.py:462`) is what a later
approval gate attaches to and what the run record ends up listing, and it is
replaced wholesale by `mark_approval_required`/`update_run` — so every
per-iteration artifact must be appended there as it is produced, or 116 of 117
drafts silently vanish from the run.

---

## 5. `validate_graph` additions

All of these are `severity: "error"` blockers, following the existing
`GraphCheck` vocabulary (`workflow_graph_models.py:66`). Check ids are stable
strings, same convention as `send_risk_without_approval_gate`.

| Check id | Rejects |
|---|---|
| *(existing)* `unknown_node_kind` | Add `"loop"` to `VALID_NODE_KINDS` (`workflow_graph_store.py:46`). Until then a `loop` node is **silently skipped** at run time — the exact failure that set already exists to prevent. |
| `reserved_node_id_char` | Any user node id containing `#`. Mirrors the existing `RESERVED_NODE_ID_SUFFIX` check (`:32`, `:266-268`); without it a hand-authored `n_draft#0` collides with a synthetic id. |
| `loop_body_unknown_node` | A `config.body` id that names no node. |
| `loop_body_not_dominated` | A body node reachable from the trigger with the loop node removed (§3 rule 2). |
| `loop_body_multiple_entries` | More than one body node with an in-edge from the loop node (§3 rule 3). |
| `loop_body_output_escapes` | Any non-body node binding to a body node's output (§3 rule 4). |
| `loop_body_shared` | A node listed in two loops' bodies (§3 rule 5). |
| `loop_body_invalid_kind` | A body node whose kind is not `tool_call`/`llm_transform` (v1). Covers nested loops and `approval_gate`-in-body in one rule. |
| `loop_body_forbidden_risk` | A body `tool_call` whose `policy.risk` is `SEND` **or `EXPORT`**. `EXPORT` is the rev-2 finding #4 fix: it has an implicit pause path v1 cannot resume from. Message must say *why*, and name the tool. |
| `loop_item_outside_body` | A `loop_item`/`loop_index` binding on a node no loop owns (§4.2). |
| `loop_missing_input` | `inputBindings.input` absent. Nothing sensible to iterate. |
| `invalid_loop_config` | `max_iterations`/`max_duration_sec`/`max_failures` non-positive or non-numeric. Same shape and rationale as `invalid_paginate_config` (`:338-347`). |
| `loop_iteration_ceiling` | `max_iterations` above the hard ceiling (§6). |
| `loop_call_budget` | `max_iterations × (tool_call nodes in body)` above the governed-call ceiling (§6). This is `finalize_stage_1.md` §5's open per-run budget question, **closed** — as a build-time blocker rather than a run-time surprise. |
| `loop_llm_budget` | A body containing an `llm_transform` with `max_iterations` above the LLM-specific ceiling (§6, §7.4). Message must name the real reason — one shared local GPU with 3 concurrent slots — and point at the single-call alternative, since a builder cannot otherwise guess why 25 tool calls are fine and 25 LLM calls are not. |

`missing_tool_access` / `required_categories_for_version` /
`output_types_for_version` (`workflow_graph_store.py:416-447`) all iterate
`version.nodes` and match on `kind == "tool_call"`, so body nodes are covered by
them **already, with no change** — grants and category requirements apply
per-tool regardless of nesting. Verified by reading; add a regression test
rather than code.

---

## 6. Limits

| Limit | v1 value | Why |
|---|---|---|
| `max_iterations` default | **25** | A default that fits inside one synchronous HTTP request (`backend/workflow_api.py:99`) with an LLM call per iteration. Rev 1's 500 did not. |
| `max_iterations` ceiling | **100**, build-time blocker | Raising this is gated on §7 durability + background execution, not on a config edit. |
| `max_duration_sec` default | **300**, loop-wide | Paginate's lesson (`:124-125`): a count cap alone does not bound a slow upstream. Checked *between* iterations, so a single iteration can overrun it — the bound is on starting new work, not on preempting running work. |
| Governed-call ceiling | **200** calls (`max_iterations × body tool_calls`), build-time | Every body tool call is a real governed call through `_govern`. This makes the blast radius of a published graph knowable before it runs. |
| Cancellation | Checked between iterations | `cancel_run` (`workflows.py:848`) exists but nothing in `_interpret` re-reads run status mid-walk, so today a long fan-out is uninterruptible. Re-read via `get_run_record` between iterations; on `cancelled`, stop with `truncatedReason: "cancelled"`. |
| LLM lane for body `llm_transform`s | **1 slot**, `< max_running_requests` | The local server has 3 concurrent slots total (§7.4). A loop must never occupy them all, or an interactive copilot user queues behind it invisibly. |
| Per-iteration `max_tokens` | **512** for a body `llm_transform` (vs. the 2048 chat default) | Decode time *is* the cost: 2048 tokens ≈ 94 s/iteration at the measured 21.86 tok/s. Also force `enable_thinking: false` in a body (§7.4). |
| `max_iterations` when the body contains an `llm_transform` **and** `USE_LOCAL_LLM=true` | **5**, build-time blocker | 25 LLM iterations is 7.5–39 min of GPU-serialized work on a 3-slot box. Fan-out with an LLM in the body is a batch job; until §12 step 0b + background execution land, keep it to something a person will actually wait for. |

Every one of these stops the loop **loudly** — `truncated: true` plus a reason,
never a silently short result. Same contract paginate already ships.

---

## 7. Durability and the run record

This is the section rev 1 did not have, and it is what makes the difference
between a demo and a production node.

### 7.1 Write amplification

`add_step`/`complete_step`/`fail_step` each call `update_run` → `_save()`,
which serializes and rewrites the whole runs file (`workflows.py:431-451`,
`:177`). One iteration of a 2-node body is 4 such writes; 25 iterations is 100
full-file rewrites of a file that is simultaneously growing.

**Required change:** a deferred-save scope in `workflows.py` —

```python
with workflows.deferred_save():      # one _save() on exit, not one per mutation
    ...run one iteration...
```

so an iteration costs exactly one write. In-memory `_RUNS` stays consistent
throughout; only the fsync is batched. A crash inside the scope loses at most
one iteration's *record* — and §8.3 makes that state detectable rather than
silently "done". Without this change, `max_iterations` cannot exceed ~10.

### 7.2 Record size

`run_dict` (`workflows.py:258`) returns every step with full `outputs` to the
UI, and a body tool_call's outputs can be a whole row set. 25 iterations × full
outputs is a run record two orders of magnitude larger than anything today.

**Required change:** after iteration `i` completes, compact its steps' stored
outputs — keep scalars, `artifactId`, counts, and a bounded preview of any
list-valued field; drop the bulk. Capture `result_node`'s value for `results[i]`
*before* compaction.

This is safe **only** under the v1 rule that nothing pauses inside a body: a
completed iteration's outputs are never re-read, because in-body bindings only
ever reference the *current* iteration. It is therefore a v1-only affordance,
and §10 lists revisiting it as a hard prerequisite for v2.

### 7.3 Execution model

`run_graph` is awaited inline in the request handler
(`backend/workflow_api.py:99`); scheduled/automation runs go through the same
path. A loop makes runs minutes-long instead of seconds-long, which is a
different operational regime: proxy and client timeouts, `workflow_health`'s
`stuck_after_sec` (`workflows.py:454`), and retry behaviour all start to matter.

v1 ships **within** the synchronous model, which is exactly why the caps in §6
are what they are. Moving execution to a background task (and returning
`202 + runId`) is the prerequisite for raising them, and is its own piece of
work — not smuggled in here.

### 7.4 The local-LLM constraint (`USE_LOCAL_LLM=true`) — the binding limit

This is the tightest constraint on the whole feature and rev 1 did not mention
it. When `USE_LOCAL_LLM=true` (the default —
`gateway/orchestrator.py:481-484`, `appservice.settings.json:33`), **every**
`llm_transform` in every loop iteration, **every** copilot turn, and the
background chat-sweep summarizer (`backend/chat.py:318-332`) all contend for one
self-hosted Qwen3.8-27B-FP8 sglang server on a single 48GB MIG slice. There are
two separate contention layers and they need separate fixes.

> The platform-wide version of this problem — many people using the copilot at
> once, against the same 3-slot GPU — is `concurrency_and_scale.md`. This
> section is the loop node's slice of it; §12 step 0b is that document's
> Stage A.

**Layer 1 — our process (the actual bug, worse than GPU contention).**
`default_llm_complete` returns the **synchronous** `openai.OpenAI` client
(`orchestrator.py:519-532`), and it is called *directly on the asyncio event
loop* in both consumers: `run_chat:742` and `_execute_llm_transform_node`
(`workflow_graph_interpreter.py:256`). A blocking call on the loop stalls the
whole ASGI process, not just the caller — during one inference the gateway
cannot read or write **any** socket, so a second user's copilot request is not
"waiting for the GPU," it is not being served at all.

A loop node makes this catastrophic rather than merely bad. Measured server
numbers from the deployment doc (`Qwen3.6_27B/deploy.md:140-168`): steady-state
decode **21.86 tok/s**, and `GOVERNANCE_CHAT_MAX_TOKENS=2048`
(`appservice.settings.json:30`).

| Body shape | Per iteration | 25 iterations |
|---|---|---|
| A ~400-token email draft | ~18 s | **~7.5 min** |
| Worst case, 2048 tokens | ~94 s | **~39 min** |

That is 7.5–39 minutes of *total gateway unavailability* — while using **1 of
the server's 3 concurrent slots**. We would monopolize 100% of the app to
consume 33% of the GPU. Any loop with an `llm_transform` body is unshippable
until this is fixed; it is listed as a prerequisite in §12 step 0b.

**Layer 2 — the inference server.** sglang does continuous batching, so
concurrent requests interleave rather than serialize, and requests beyond
capacity **queue** (FIFO) rather than fail. Capacity is set by
`--max-mamba-cache-size`, *not* by `--mem-fraction-static` or
`--context-length`: `max_running_requests = 3` in the settled 2026-08-17 config
(`deploy.md:127,138-157`). So the GPU is genuinely capable of serving a copilot
turn *concurrently* with loop iterations — at ~150 req/day, the GPU is not the
bottleneck, our client is. But 3 slots is small enough that an unthrottled loop
would fill them and push interactive users behind a server-side queue we cannot
see or prioritize.

> **Confirm before sizing lanes:** the same doc's "Config reference" systemd
> snippet still shows `--max-mamba-cache-size 32` (→ 6 slots) while the settled
> table says 16 (→ 3). Check the live value (`/get_server_info`) rather than
> trusting either; the design below reads it from config, and must never
> hard-code 3.

**The design: one broker, two priority lanes.**

Loop iterations are *batch* work; chat and the copilot are *interactive* work
with a human watching. Industry-standard answer is class-based admission control
in front of the model, and it is small:

1. **Never block the loop.** One process-wide async LLM client — `AsyncOpenAI`,
   or the existing sync client wrapped in `asyncio.to_thread`. This alone fixes
   today's chat head-of-line blocking, independent of loop.
2. **A single broker every consumer goes through**, holding two semaphores sized
   from the server's real capacity `C`:
   - `interactive` — chat, copilot, sweep: up to `C` slots.
   - `batch` — loop iterations: **1 slot, and it must be strictly less than
     `C`**, so a loop can *never* starve interactive work. With `C=3` that is
     `batch=1`, leaving 2 slots always available to a person.
   Sequential-only iteration (§2 decision 1) is therefore not just an audit-trail
   choice — with `C=3` it is what the hardware affords. `max_concurrency` is
   bounded by the batch lane forever, not by our own preference.
3. **Bounded queue + deadline, fail loudly.** A batch request that cannot get a
   slot within its deadline becomes an iteration failure with
   `"llm_busy"` — never an unbounded backlog, never a silent stall. Same
   never-truncate-silently contract as §6.
4. **Token budget, not just an iteration count.** GPU seconds are the real
   resource: `max_iterations` bounds *calls*, `max_duration_sec` bounds
   wall-clock, and a per-iteration `max_tokens` bounds each call's decode time.
   Loop bodies should also force `enable_thinking: false` — reasoning is ~2× the
   tokens (`orchestrator.py:520-524`), it is on by default server-side
   (`--reasoning-parser qwen3`), and per-row drafting does not need it. Halving
   output tokens halves the loop's GPU time.
5. **Treat the endpoint as genuinely unavailable sometimes.** That VM runs one
   workload at a time and can be switched to `training`/`deepseek`, at which
   point the endpoint is *fully unreachable with no request draining*
   (`Qwen3.6_27B/how_to_switch_mode.sh:9-19,46-47`). A 30-minute loop has a real
   chance of straddling a switch. So: bounded retry with jitter, then stop the
   loop with `truncatedReason: "llm_unavailable"`, preserving completed
   iterations (§8.2) rather than failing the lot.
6. **The caps differ per backend.** `USE_LOCAL_LLM=false` routes to Claude
   Sonnet (`orchestrator.py:509-510`), which is elastic and concurrent — an
   order of magnitude more fan-out is safe there. Read the effective limit from
   the active backend instead of hard-coding one number for both.

**Cheapest win, and it belongs in the builder UI, not the runtime:** N
personalized drafts do not require N inferences. One `llm_transform` over the
whole matched set (or a template plus a single pass) is one prefill and one
decode instead of 25 of each, and on a 3-slot local GPU that is the difference
between 8 minutes and 20 seconds. The loop node should exist for *per-row tool
calls* (`create_email_draft` per customer — `risk=WRITE`, no LLM involved); an
LLM call **inside** a body should be the exception the builder nudges you away
from, with the row-count × token cost shown at author time.

### 7.5 What this requires outside the interpreter

**Hosting / VM side — recommendation: change nothing about the sglang config.**

More concurrency is the wrong trade and the deployment doc already measured why:
`--max-mamba-cache-size` 16 → 3 slots with a 126,062-token pool, and the
3 × 40,960 = 122,880 ≤ 126,062 full-context guarantee actually holds; going to 32
(6 slots) drops the pool to ~88,478, where 6 × 40,960 = 245,760 was never
deliverable (`deploy.md:145-157`). Buying slots with context headroom to serve a
*batch* workload would degrade the three interactive products this box exists
for. The right fix for batch work on a scarce accelerator is a queue and a lane
(§7.4), in our process, where we can see and prioritize it. What is worth doing
on the VM, in order:

1. **Verify live capacity — read-only, do this first.** `deploy.md`'s settled
   table (16 → 3) and its "Config reference" systemd snippet (32 → 6) disagree.
   `/get_server_info` settles it. Lane sizing reads this value; it is never
   hard-coded.
2. **Optional, only if LLM-in-body ships:** enable sglang's Prometheus metrics so
   queue depth and wait time are visible. One flag, one restart. Without it,
   "the GPU is busy" and "our gateway is blocked" look identical from outside.
3. **Process, not config: mode switches.** `serving` → `training`/`deepseek`
   stops the endpoint with **no request draining**
   (`how_to_switch_mode.sh:9-19,46-47`), and `Restart=always` covers crashes but
   not a deliberate stop. Long runs need either a maintenance flag the gateway
   reads (refuse to *start* new loop runs) or an operational rule about active
   runs. Cheap either way, and §7.4 point 5 makes the run survive it.
4. **Not needed, and not possible on this box:** a second concurrent
   config/model — two sglang processes need ~57GB of weights on a 48GB card
   (`deploy.md:309`). If LLM-heavy fan-out ever becomes a product requirement,
   the answer is `USE_LOCAL_LLM=false` for that workload, or separate hardware —
   not retuning this server.
5. **Also not needed for the motivating case at all.** Per-row
   `create_email_draft` is `risk=WRITE` with zero inference; its load lands on
   miniERP GraphQL, not the GPU.

**Repo side — the two heaviest items are pre-existing bugs, not loop features.**
§12 steps 0 and 0b (crashed-`running` step; blocking LLM calls) are both
independently shippable and both improve the system with no loop node present.
Beyond them, and beyond §9's frontend work:

- **Config surfaces move together, in three places:** `appservice.settings.json`,
  `gateway/.env.local.example`, and `DEPLOY.md`'s env table. New vars for lane
  sizes and loop caps must land in all three or the next deploy silently runs
  defaults.
- **The copilot's system prompt** (`WORKFLOW_COPILOT_SYSTEM_PROMPT`) needs the
  loop shape *and* the "one call over N calls" rule from §7.4 — it authors graphs
  against the same validator, so without it it will confidently propose the
  expensive shape.
- **Fast-follow worth doing once loops exist:** `minierp_core/graphql_client.py:194`
  opens a **new `httpx.AsyncClient` per governed call**, so a fresh TCP+TLS
  handshake per iteration. Invisible at 1-2 calls per run, wasteful at 50.

Two findings that *reduce* the work, both verified:

- **The ERP path is already correctly async** (`httpx.AsyncClient`, 20s timeout
  via `MINIERP_TIMEOUT_SEC` — `graphql_client.py:91-92,194`). A tool-call-only
  body never blocks the event loop; **only `llm_transform` does.** That is the
  clean line between the loop shape that is nearly ready and the one that needs
  §12 step 0b first. Note the arithmetic though: 50 governed calls × a 20s
  timeout is a ~16-minute worst case with no LLM involved at all, which is §7.3's
  background-execution argument restated in ERP terms.
- **Our edge rate limiter does not apply.** It counts inbound HTTP requests
  (`governance_core/edge.py:193-204`, default 100/hour), and a workflow run is
  **one** request however many governed calls it makes internally. So it neither
  obstructs a loop nor protects against one — which is precisely why §6's
  build-time governed-call ceiling is the only real bound that exists.

---

## 8. Failure semantics

### 8.1 Bad input

`input` resolving to anything that is not a list — including `None` — **fails
the loop node** with a clear message. Deliberately *unlike* the filter node,
which coerces to `[]` (`workflow_graph_interpreter.py:389-390`): a filter over
nothing is a legitimate empty result, whereas a fan-out that silently does
nothing is indistinguishable from a mis-wired graph. An empty list is fine and
completes with `iterations: 0`.

### 8.2 An iteration fails

An iteration fails when any body step fails — i.e. `_govern`'s result carries a
`_FAILURE_STATUSES` marker (`workflow_graph_interpreter.py:43`) or an
`llm_transform` errors. Note `approval_required` and `denied` are in that set,
so a governed per-item approval is an iteration failure, not a pause.

- `on_error: "fail"` (default) — stop immediately, mark the loop step and the
  run failed, and **record what already happened**: `succeeded`, `failed`, the
  per-iteration `errors`, and every `artifactIds` produced so far. Today's
  "one failed step fails the run" leaves no such record; with N durable
  side-effects already committed, that is not acceptable.
- `on_error: "continue"` — record the error, continue, stop early if
  `failed > max_failures` with `truncatedReason: "max_failures"`.

Neither mode rolls anything back. There is no compensation model here and this
design does not invent one — it makes partial completion *legible* instead.
Say so in the builder UI in those words (§9).

### 8.3 Crash mid-iteration — a pre-existing bug loop would multiply

`_execute_tool_call_node` adds its step as `running` *before* the governed call
(`:208`). If the process dies between the two, the next walk hits the
"already attempted" branch (`:512-521`): the step is not `failed`, is not an
`approval_gate`, has no export step — so it falls through to *"fully resolved →
nothing to do"* with empty outputs. A crashed-mid-call node is silently treated
as a **successful** one, and anything bound to its output resolves `None` and is
omitted.

That is latent today; a loop multiplies the exposure window by the iteration
count. **Fix it before building loop** (§12 step 0): a non-approval step found
in `running` on a re-walk means the previous attempt died mid-flight and must
fail the run explicitly, naming the step.

---

## 9. Builder / frontend work

Rev 1 omitted this entirely; it is plausibly more work than the interpreter
change. Both views edit the same node list (`MyWorkflowsPage.tsx:73-93`) and
both must handle loop.

- **`api.ts`** — add `{ source: 'loop_item'; path?: string }` and
  `{ source: 'loop_index' }` to `WorkflowBinding` (`:1707-1710`), and `'loop'`
  to `GraphNodeKind` (`:1712`).
- **Step view** — a loop renders as a bracket: a "Repeat for each ⟨list⟩" header
  card, its body steps indented beneath it, and an end cap. Reorder/remove must
  respect the region (moving a step out of the bracket removes it from `body`;
  moving the header moves the whole span). Contiguity is what makes the region
  valid, so the builder should make non-contiguity unrepresentable rather than
  merely invalid at save time.
- **Canvas view** — `derivedEdges` (`graphModel.ts:121-141`) currently gives a
  node with no node-source binding an implicit **trigger** edge (`:135`). A body
  node whose only input is `loop_item` would get exactly that, breaking
  dominance. Change: for a node owned by a loop, the implicit edge comes from
  **the loop node**. That one line is the whole Canvas fix; everything else
  (cycles, reachability, gate mirror at `:275-312`) then keeps working.
- **`graphModel.ts`** — `inputSlotsFor` (`:30`) gains the loop's `input` slot;
  `outputPinsFor` (`:48`) gains the §4.4 keys; `bindingFromPort` (`:67`) emits
  `loop_item` bindings when dragging from the loop's item port. Mirror the
  §5 v1 body restrictions client-side so a builder is stopped while authoring,
  not at save.
- **`stepMeta.ts` / `StepConfigFields.tsx` / `StepModal.tsx`** — icon, label,
  one-line summary ("Repeat for each of *Filter → matched*, up to 25"), and a
  config form for the caps and `on_error`. State the partial-completion
  contract (§8.2) in the form, not only in docs.
- **Run view** — 25 iterations × 2 steps is 50 step rows. Group per-iteration
  steps under the loop step, collapsed by default, showing
  `succeeded/failed/truncated` at the group header.
- **Workflow copilot** (`gateway/app.py::propose_graph`) — it authors graphs
  against the same validator; it must know the loop shape and the §5 rules or
  it will confidently emit graphs that get rejected.

---

## 10. Explicitly out of scope for v1

| Deferred | Why, and what v2 must fix first |
|---|---|
| `approval_gate` inside a body | Needs resume-mid-loop. **Blocked on** `workflows.resume_run:874-884`, which completes *every* pending approval step regardless of `approval_id` — with N per-iteration gates, one approval releases all N sends (rev-2 finding #5). That is a security fix, not a feature, and it must land before anything creates multiple concurrent approvals in one run. |
| `SEND`/`EXPORT`-risk tools in a body | Both need a pause inside the body; `EXPORT` additionally has the implicit broad-export path (`:484-495`). Also revisits §7.2 output compaction, since a resumed run *does* re-read completed iterations. |
| Nested loops | The `LoopScope` threading in §4.3 is designed to permit it later; nothing else is. |
| `filter` in a body | A per-row table to filter has no obvious meaning yet. Wait for a real workflow to ask. |
| `max_concurrency` | Blocked on §7 durability + background execution, and on approval ordering staying single-threaded. |

The v1 line is drawn where it is because the motivating case sits inside it:
`create_email_draft` is `risk=WRITE` (`policy/manifest.py:846-851`), so
"draft 117 personalized win-back emails, human sends them" is fully buildable in
v1 without any pause, resume, or approval machinery. "Auto-send per row, each
individually approved" is v2 and needs its own design pass.

---

## 11. Open questions (answer before the step they block)

1. **Is "draft per row, human sends" enough for the workflows you actually have
   in mind?** If auto-send-per-row is a near-term requirement, the §10 v2 items
   (starting with the `resume_run` fix) move ahead of §12 steps 3-4, and the
   §7.2 compaction affordance has to be dropped. Blocks: step 1.
2. **Is 25 iterations / 200 governed calls the right v1 envelope?** These are
   set by the synchronous execution model (§7.3), not by anything intrinsic. If
   real workflows need 500 rows on day one, the honest sequencing is background
   execution *first*, loop second. Blocks: step 2.

---

## 12. Sequencing

0. **Prerequisite fix, independent of loop:** the crashed-`running`-step bug
   (§8.3) — a non-approval step found `running` on a re-walk must fail the run,
   not silently pass as complete. Small, testable, worth shipping on its own.
0b. **Prerequisite fix, also independent of loop, also worth shipping alone:**
   stop blocking the event loop on LLM calls (§7.4 layer 1) and put both
   consumers behind one broker with an interactive and a batch lane. This is a
   *present-day* concurrency bug — two people using chat today already serialize
   the whole gateway — and it is a hard gate on any `llm_transform` in a loop
   body. Verify with the obvious test: hold one long inference open and confirm
   an unrelated endpoint still responds.
1. Answer open question 1 (§11). It decides the v1 body restrictions.
2. Answer open question 2 (§11). It decides the caps, and possibly the ordering
   of this whole list.
3. Model + store: `"loop"` in `VALID_NODE_KINDS`, config shape (§4.1),
   `#` reservation, and the `workflow_graph_models.py` docstring update
   recording the one relaxed invariant (§1).
4. `validate_graph`: every check in §5, each with a test that a graph violating
   it is rejected *with the right check id*. Region checks (dominance, single
   entry, no escape) first — they are what keeps everything else fail-closed.
5. `workflows.deferred_save()` (§7.1) + output compaction (§7.2).
6. Interpreter: `LoopScope` threading through `_resolve_binding`/
   `_resolve_node_args`/the `_execute_*` functions (§4.3), the iteration driver,
   caps and cancellation (§6), failure policy (§8), artifact accumulation
   (§4.4). Body executors are reused unchanged — the only new execution logic is
   the driver.
7. Frontend (§9): `api.ts` types → Step-view bracket → Canvas implicit-edge fix
   → config form → grouped run view.
8. Tests, same two-tier pattern as the paginate work:
   - **Mechanics, no server** — scope resolution (body→body binding lands on
     `X#{i}`, body→outer stays global, `loop_item` outside a body is rejected);
     empty array; non-list input; both caps; `on_error` in both modes;
     cancellation mid-loop; every §5 check id.
   - **Live, bounded** — loop over a small real customer list with
     `create_email_draft` as the body: N drafts, N *distinct* artifacts, correct
     `loop_item` per iteration, all N ids on the run record, and a deliberate
     `max_iterations` truncation asserting `truncated`/`truncatedReason`.

**Definition of done for v1:** a published graph cannot express an unbounded
loop, cannot express one whose blast radius is unknown at publish time, cannot
pause inside a body, and cannot fail partway through without the run record
saying exactly how far it got and what it already created.
