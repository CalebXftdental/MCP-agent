# Concurrency & Production Readiness — many users, one local GPU

**Status:** Proposed (design/plan only — nothing here is built yet).
**Scope:** What has to change for "a lot of people using My Workflow at the same
time — building workflows with the copilot and running them — against our
self-hosted Qwen." Platform-wide, not loop-specific;
`loopnodedesign.md` §7.4-§7.5 is the loop node's slice of the same problem and
depends on Stage A and Stage C below.

**Every claim here was verified against the working tree and against
`Qwen3.6_27B/deploy.md`** at the time of writing. `file:line` is given so the
next reader can re-check rather than trust. Estimates are labelled as estimates;
the measurement plan is §8.

---

## 1. What we run today (verified)

| | |
|---|---|
| Gateway topology | **One** uvicorn process, **one** worker — `startup.sh:62` (`exec python -m uvicorn app:app`, no `--workers`), Azure set to `--number-of-workers 1` (`DEPLOY.md:82`). The other six uvicorn processes in `startup.sh` are the per-backend MCP servers, not gateway replicas. |
| Route handlers | Effectively all `async def` (107 of the 110 `(request)` handlers under `gateway/backend/`; the 3 sync ones are helpers, not routes). **There is no threadpool parallelism** — one event loop serves every user. |
| Durable state | In-process module globals + whole-file rewrite: `_RUNS`/`_save()` (`gateway/workflows.py:177`), `_GRAPHS`/`_save()` (`governance_core/workflow_graph_store.py:174`), and the same pattern in template/approval/agent/automation stores. `_LOADED` is set once and never invalidated. |
| Locks | **None** on any store. The only `asyncio.Lock`s in the tree are token caches in the two GraphQL clients (`minierp_core/graphql_client.py:104`). |
| ERP calls | Correctly async, `httpx.AsyncClient`, 20s timeout (`minierp_core/graphql_client.py:91-92,194`). These do **not** block the loop. |
| LLM calls | **Synchronous `openai.OpenAI` client** (`gateway/orchestrator.py:519-532`) called **directly on the event loop** — `run_chat:742`, `_execute_llm_transform_node` (`gateway/workflow_graph_interpreter.py:256`), plus the background sweep (`backend/chat.py:318-332`). These block **everything**. |
| Model server | One sglang instance, one 48GB MIG slice, `max_running_requests = **3**`, 40,960 context, 126,062-token pool, ~21.86 tok/s steady-state decode (`deploy.md:137-168`). Requests past 3 queue FIFO, server-side, invisible to us. |
| Copilot shape | A multi-turn tool-calling loop: **up to 10 inferences per user message** (`WORKFLOW_CHAT_MAX_TURNS = 10`, `orchestrator.py:49`), each capped at 2048 output tokens (`appservice.settings.json:30`). |

The single most important consequence: **today's correctness depends on the
absence of concurrency.** Nothing interleaves because the one thing that could
interleave — an `await` inside a read-modify-write — is not currently protected,
and the traffic is low enough that nobody has hit it. Every item in §3 is a bug
that low usage is hiding.

---

## 2. What "concurrent users" actually costs on this hardware

Estimates, from the measured single-stream 21.86 tok/s. Per-stream decode under
a full batch of 3 will be **lower** than 21.86 and aggregate **higher**, but not
3× — that ratio is unmeasured here and §8 measures it before we promise numbers.

| Work | Inferences | Output tokens | GPU decode |
|---|---|---|---|
| One copilot message, typical | 2-4 | ~200-400 each | **~20-70 s** |
| One copilot message, worst case | 10 | 2048 each | ~15 min |
| One `llm_transform` in a workflow | 1 | ~400 | ~18 s |
| A 25-iteration loop with an LLM body | 25 | ~400 each | **~7.5 min** |

With 3 slots, the honest capacity of this box is roughly **3-5 people building
workflows at the same time** before queue wait becomes the dominant experience.
The 6th person's first token arrives a round or two late; at 10 simultaneous
users the tail is minutes.

That is a *sglang* queue — orderly, FIFO, survivable. What makes it unsurvivable
today is §3.1: we never get that far, because our own process falls over first.

---

## 3. Failure modes we already have

Ordered by how soon concurrent usage will hit them.

### 3.1 One inference freezes the entire gateway (present-day, severe)

**Two distinct resources are in play, and only one of them behaves the way you
would expect.** One inference occupies exactly **one** sglang slot — that part is
normal. The problem is a second resource we have only one of:

| Resource | How many | What one inference takes |
|---|---|---|
| sglang concurrent slots | 3 | 1 slot, for its duration |
| Gateway event loop | **1 thread** | **all of it**, for its duration |

`client.chat.completions.create(...)` is the **synchronous** client
(`orchestrator.py:519-532`): blocking socket reads, no `await` inside. asyncio
runs one coroutine at a time and only regains control at an `await`, so from
call to response the loop cannot run other coroutines, accept connections, read
request bytes, or write responses.

Three users sending a copilot message at the same moment:

```
t=0      A's handler calls llm_complete()  -> 1 slot busy, 2 idle
t=0-18s  event loop BLOCKED
         └─ B's and C's requests sit unread in the socket buffer;
            their handlers have not started -- we haven't parsed their HTTP yet
t=18s    A returns -> loop free -> B starts -> 1 slot busy, 2 idle
```

The damage is not that A took a slot. It is that **we never have more than one
request in flight**, so the other two slots cannot be filled even while idle —
and every unrelated request (page load, run list, approval) queues behind the
same thread. GPU ~33% utilized, gateway 0% available.

Fixing this is therefore not "get more slots"; it is making the call awaitable
so three can genuinely be in flight. **After Stage A the intuitive model holds
exactly**: one inference = one slot, three concurrent = three slots, the fourth
queues at sglang, and the broker's lanes only decide *which* requests get those
three. Note also that a copilot message is up to 10 *separate* sglang requests
with tool calls in between, so the slot is released and re-acquired per turn —
under contention a 4-turn message queues four times, which is why per-user
fairness beats plain FIFO (§4, Stage A).

### 3.2 Stale-object writes silently lose updates (present-day)

Every mutator takes a caller-held dataclass and writes it back wholesale:
`update_run(run, **changes)` → `replace(run, ...)` → `_RUNS[id] = updated`
(`workflows.py:431-437`). `_interpret` holds one `run` object across **many**
awaits (every `_govern` call), then writes it back.

Concretely, today: cancel a run while it is executing. `cancel_run` writes
`status="cancelled"` (`workflows.py:848-855`); the still-running `_interpret`
finishes and writes its own stale object with `status="completed"`
(`workflow_graph_interpreter.py:523`). **The cancel is silently undone.** The
same shape applies to a resume racing an in-flight pass, and to any two writers
touching one run. Loops widen the window from seconds to minutes.

### 3.3 Whole-file rewrites with a shared temp name

Every store writes `path.with_suffix(path.suffix + ".tmp")` — a **fixed** name —
then `replace()`s it (`workflows.py:177-180`, `workflow_graph_store.py:174-180`).
Two concurrent writers to one store would interleave into the same temp file.
Safe today only because nothing runs in parallel (§1); unsafe the moment
anything does — a threadpool call, a background worker, or a second process.

Also note the cost shape: `_save()` serializes **every** run in the store on
**every** step transition. That is O(all history) work per step.

### 3.4 Scheduled automations run serially inside one HTTP request

`_automation_run_due` loops over every due automation and `await`s each workflow
to completion in-band (`backend/automations.py:107-135`). N due automations = N
workflows back-to-back in one request. Add loops and a single trigger can occupy
the process for an hour. Automations are also the classic thundering herd —
everything due at the same minute fires together.

### 3.5 No bound on concurrent runs, and no recovery for interrupted ones

Nothing caps how many workflow runs execute at once, and runs execute inside the
request that started them (`backend/workflow_api.py:99`). Azure can restart or
swap the instance at any time; an in-flight run then dies with steps left
`running` — which the interpreter currently treats as **successfully complete**
on the next pass (`workflow_graph_interpreter.py:512-521`, see
`loopnodedesign.md` §8.3). `workflow_health` can *detect* stuck runs
(`workflows.py:454`) but nothing acts on it.

---

## 4. Target architecture, in stages

Each stage is independently shippable and independently valuable. A-B-C are
required before the loop node's LLM body; D is required before a second
instance.

### Stage A — never block the event loop; put one broker in front of the model

> **Status: built** (branch `llm-async-broker`) — `gateway/llm_broker.py`, wired
> into `orchestrator.py`'s six client builders and both chat loops, the workflow
> interpreter's `llm_transform`, and the `chat_log` summarizer chain (which is
> now async end to end). Verified by `_smoke/test_llm_broker.py` (27 checks,
> including the regression test that an unrelated coroutine keeps running during
> a blocking inference — it measures 0 ticks against the old code). Items 1 and 2
> below are done; item 3's queue feedback is served as `503 assistant_busy` with
> lane/queue numbers, and as an SSE `error` frame on the streaming endpoints. The
> remaining piece is the front-end presentation of that state.

1. **Async LLM I/O.** `AsyncOpenAI`, or the existing sync client wrapped in
   `asyncio.to_thread`. Explicit connect/read timeouts (the SDK default is far
   too long for an interactive path).
2. **One process-wide LLM broker** that every consumer goes through — chat,
   copilot, the idle-session sweep, and (later) loop bodies. It owns:
   - **Two lanes.** `interactive` (a human is watching) up to `C`; `batch`
     (loops, scheduled runs, the sweep) capped at `C-2`, i.e. **1** when `C=3`.
     Batch can never starve a person. `C` is read from the server, never
     hard-coded (§6).
   - **Per-user fairness inside the interactive lane.** Round-robin or a token
     bucket per user, so one person's 10-turn copilot message
     (`WORKFLOW_CHAT_MAX_TURNS`) cannot monopolize the lane. FIFO alone is not
     fair when one request is 10 inferences and another is 1.
   - **Bounded queue + deadline.** Past the bound, fail fast with a real reason
     ("the assistant is busy — N ahead of you") rather than growing a backlog.
   - **Observability**: lane occupancy, queue depth, wait time, per-user LLM
     seconds. Without this you cannot tell §3.1 from a slow GPU.
3. **Stream the copilot** and surface queue position. Perceived latency is most
   of the problem at 3-5 concurrent users; a visible queue is tolerable, a
   frozen page is not.

*Unlocks:* real concurrent copilot use up to the GPU's actual limit, and it
fixes today's whole-app freeze. **Biggest single win in this document.**

### Stage B — make state safe under interleaving

> **Status: built** (branch `llm-async-broker`) —
> `governance_core/store_concurrency.py`, applied to `gateway/workflows.py`'s
> mutators and to the `atomic_write_text` path of all 15 file-backed stores.
> Verified by `_smoke/test_store_concurrency.py` (29 checks), whose first test is
> the cancel bug from §3.2 and fails against the old code.

1. ~~Per-entity async locks~~ → **a per-store `threading.RLock`**, not an
   `asyncio.Lock`. Every mutator is a synchronous function; making them async
   would have meant changing ~40 call sites across the interpreter, workflow_api
   and the five hardcoded workflows. A sync critical section is already atomic
   against the event loop, and the RLock additionally covers a mutation reached
   from a worker thread — which Stage A's `asyncio.to_thread` now makes possible.
2. ~~Mutators take an id~~ → **mutators re-read the stored record and apply their
   change to that**, keeping the existing `(run, **changes)` signature. Same
   correctness, zero call-site churn. Plus `cancelled`/`failed` are **sticky**:
   a late writer can add its steps and artifacts to the record but cannot move it
   out of a verdict someone acted on. `completed` is deliberately not sticky —
   see `STICKY_STATUSES` for why.
3. **Unique temp filenames** per write, plus — a finding from testing, not from
   reading — **per-destination serialization and a bounded rename retry**.
   Concurrent `Path.replace()` onto one path fails with `PermissionError` on
   Windows, which showed up immediately as dropped writes under threads. The
   shared `<file>.tmp` name was only half the problem.
4. **`deferred_save()`** coalesces a unit of work into one write, and the
   interpreter now scopes it to **one node** — so a node costs one write instead
   of two-plus, while each node's outcome is still durable before the next
   begins, which is what resume depends on. Depth is per-task (a `ContextVar`),
   so two concurrent runs cannot extend each other's deferral window.

*Unlocks:* cancel/resume that actually work; a precondition for anything
running in the background.

### Stage C — get long work off the request path

1. **An in-process run queue** with a bounded number of concurrently executing
   runs (start at 2-4). `POST /workflow-runs` returns `202 + runId`; the client
   polls or subscribes.
2. **Automations enqueue instead of executing** (§3.4), with jitter so a shared
   cron minute doesn't stampede.
3. **Orphan recovery on boot** — mark runs left `running` by a restart as failed
   (or resumable), and act on `workflow_health`'s stuck detector instead of only
   reporting it. Depends on the §3.5 / `loopnodedesign.md` §8.3 fix.

*Unlocks:* loops of a useful size; runs that survive a deploy; a UI that shows
progress instead of holding a connection open.

### Stage D — decide the scale-out story (do not skip the decision)

**Today `--number-of-workers 1` is load-bearing.** With module-global caches and
`_LOADED` never invalidated, a second worker or instance gives you two
divergent in-memory views of the same files, each overwriting the other. If
concurrency demand exceeds one process, the file stores must be replaced
(Postgres / Azure Table) with row-level updates and optimistic concurrency —
plus a distributed lock for the singleton sweeps
(`backend/chat.py:318`), a shared run queue, and a shared LLM broker (or
per-request priority pushed down to the model server).

Vertical first is the right call for now: one process with Stages A-C will
comfortably outrun a 3-slot GPU. Just make the choice explicitly, and keep
"single instance" written down as a constraint rather than an accident.

### Stage E — capacity, when the GPU is genuinely the limit

In order of cost:

1. **Reduce demand.** One `llm_transform` over 25 rows instead of 25 calls
   (`loopnodedesign.md` §7.4); `enable_thinking: false` for non-interactive work
   (~2× tokens, `orchestrator.py:520-524`); lower `max_tokens` for structured
   outputs; shorter `WORKFLOW_CHAT_MAX_TURNS` under load.
2. **Split by workload.** Route *interactive* copilot to Claude
   (`USE_LOCAL_LLM=false`, `orchestrator.py:509-510`) and keep the local GPU for
   batch — or the reverse. This is a config flip today, and it is the cheapest
   real capacity increase available.
3. **More slots — only with eyes open.** `--max-mamba-cache-size` 16→32 buys
   3→6 slots but drops the token pool 126,062→~88,478 and breaks the
   full-context guarantee (`deploy.md:145-157`). Only worth it if measurement
   shows real traffic is far under 40K context.
4. **More hardware.** Two configs cannot coexist on this card (~57GB of weights
   on a 48GB slice, `deploy.md:309`).

---

## 5. Design rules for the shared model (apply from Stage A onward)

- **Classify every LLM call.** Interactive vs batch is a property of the caller,
  and it must be explicit at the call site. An unclassified call defaults to
  batch — the safe direction.
- **Keep the shared prompt prefix byte-identical across users.** sglang's prefix
  caching makes a shared system prompt nearly free to re-prefill; a prefix that
  differs per user pays full prefill every turn. **Note the current obstacle:**
  tool specs are built per user from their grant
  (`build_tool_specs(await mcp.list_tools(), grant, ...)`, `orchestrator.py:729`),
  so users with different categories have different prefixes. Options: order the
  prompt stable-part-first (persona and rules before the variable tool list), or
  use a stable tool list for the prompt and let the PDP remain the real boundary
  (it already is — `_govern`, not the prompt, enforces access). The second buys
  more but risks the model proposing tools a user cannot call; measure before
  choosing.
- **Degrade, don't fail.** Under sustained pressure: shed the sweep first, then
  batch, then cap `max_turns`, then queue interactive with visible position.
  Never silently truncate a result — same contract as paginate and loop.
- **Every LLM call gets a deadline** and a bounded retry with jitter. The VM can
  be switched to `training`/`deepseek` with **no request draining**
  (`Qwen3.6_27B/how_to_switch_mode.sh:9-19,46-47`), so "the model is simply gone
  for 10 minutes" is a normal operating state, not an exception.

---

## 5b. Making sglang's prefix cache actually pay

sglang reuses the KV cache of any request whose **token prefix** it has already
seen (RadixAttention, on by default). Prefill is the half of an inference that
scales with prompt length, and our prompts are dominated by a head we re-send on
every single turn — the system prompt plus the tool-schema block. Getting that
head to repeat byte-for-byte is the cheapest throughput win available on a
fixed-size GPU, and it costs nothing but discipline.

**Prefix matching is exact and anchored at position 0.** One byte different at
the front discards the entire cached prefix. So the question is never "is our
prompt similar" — it is "is the head identical".

**Audited, and already correct:**

| Property | Status |
|---|---|
| System prompts | Static module literals (`SYSTEM_PROMPT`, `WORKFLOW_COPILOT_SYSTEM_PROMPT`) — no dates, no interpolation, nothing per-request. |
| Chat history | Append-only; `chat_log.history_for_llm` returns every message and never trims. Turn N+1 is literally turn N's prompt plus more, so the whole conversation so far re-prefills for free. |
| The tool-calling loop | Appends assistant/tool turns and re-sends — each iteration extends the previous prefix rather than rewriting it. Up to 10 inferences per copilot message all share one growing head. |
| `llm_transform` | Puts `instruction` before `input_text`, so a run's fixed instruction is a shared prefix and only the row data differs. This is what will make a loop node's per-row calls cheap. |

**Fixed:** `build_tool_specs` now sorts by tool name. `mcp.list_tools()` makes no
ordering promise, and the tool block renders near the very front of the prompt —
so an unstable order would silently re-prefill the entire head on every request.
The sort makes the block byte-identical for any two requests with the same grant.

**Accepted, not fixed:** tool specs are grant-filtered, so users with different
access have different heads and cannot share (they still share perfectly with
*themselves*, turn to turn, which is the dominant case). The alternative —
sending every user the full catalogue and leaning on the PDP, which is the real
boundary anyway (`_govern`, not the prompt) — would unify the head but invites
the model to propose tools the caller cannot run. Measure before trading a
correctness-shaped property for a performance one.

**How we know it is working.** Two independent signals, both on
`GET /admin/llm-lanes` under `promptCache`:

- `distinctPrefixes` — how many distinct heads this process has produced. It
  should sit near *(number of distinct grant-sets in use) × (2 chat surfaces)*
  and go flat. **If it climbs with traffic, something has made the head
  non-deterministic and every request is paying full prefill.** This is our own
  canary and needs nothing from the server.
- `cacheHitRate` — ground truth from the server's own
  `usage.prompt_tokens_details.cached_tokens`. Reported unconditionally on the
  non-streaming path; on the streaming path it needs
  `GOVERNANCE_LLM_STREAM_USAGE=on`, which is **off by default** because
  `stream_options` is an extra parameter a self-hosted server may reject, and a
  rejected parameter would break chat to gain a metric. Turn it on once
  confirmed against the live server.

**Rules to keep it true.** These are cheap now and expensive to retrofit:

1. Never put anything per-request in a system prompt — no timestamps, no user
   name, no run id. Those belong in the user turn, at the end.
2. Never trim or rewrite history from the front. Summarizing an old conversation
   into a shorter prefix looks like a token saving and is usually a net loss: it
   invalidates the cached head and re-prefills everything behind it. If context
   pressure ever forces compaction, do it at a session boundary (a new
   `session_id`), not mid-conversation.
3. Keep anything newly added to the head sorted and deterministic.
4. `enable_thinking` changes the rendered template, so it changes the head. It is
   env-level and constant — keep it that way rather than per-request.
5. New shared prompt text goes *before* variable text, always.

---

## 6. Read capacity from the server; never hard-code it

Lane sizes derive from the live `max_running_requests`. Read it at startup
(`/get_server_info`) and re-read on reconnect. Note the deployment doc currently
disagrees with itself — the settled table says `--max-mamba-cache-size 16` → **3**
slots, the "Config reference" systemd snippet says 32 → 6 (`deploy.md:138-142`
vs `:191-201`). Resolve that against the live server before sizing anything, and
treat whichever value is live as data, not as a constant in our code.

---

## 7. What this buys, stage by stage

| After | Simultaneous copilot users | Workflow runs | Loop node |
|---|---|---|---|
| Today | 1 (everyone else's app is frozen) | 1 useful | unsafe |
| Stage A | 3-5, degrading to a visible queue | unchanged | LLM body still too slow to hold a request |
| Stage A+B | 3-5, correct cancel/resume | several, safely | tool-call bodies viable |
| Stage A+B+C | 3-5, background progress | bounded queue, survives restarts | full v1 per `loopnodedesign.md` |
| Stage D | limited by the GPU, not the app | horizontal | unchanged |

---

## 8. Measure these before promising numbers

1. **Live `max_running_requests`** (`/get_server_info`) — settles §6.
2. **Per-stream vs aggregate decode at batch 1/2/3** — the §2 table's missing
   ratio. sglang logs `gen throughput (token/s)`; run 1, 2, 3 parallel identical
   requests and record both.
3. **Real copilot turn distribution** — inferences per user message and output
   tokens per inference, from the audit trail. §2's "typical 2-4 turns" is an
   assumption; the whole capacity model rests on it.
4. **Prefill cost with and without a shared prefix** — decides §5's tool-spec
   question with data instead of theory.
5. **Event-loop block time** — instrument it, then watch it go to ~zero after
   Stage A. This is the number that proves §3.1 was the bottleneck.

---

## 9. Sequencing

1. §8.1 and §8.2 — an afternoon, and they set every cap in this document.
   §8.1 is now a config value rather than a code change:
   `GOVERNANCE_LLM_MAX_RUNNING_REQUESTS` (bare alias `MAX_RUNNING_REQUESTS`).
   Setting it wrong is the one way to misconfigure Stage A — too high and we
   queue invisibly inside the model server where we cannot prioritize.
2. ~~**Stage A.**~~ **Done** — see the status note above. What is left from it is
   front-end: show the `assistant_busy` state and queue position instead of a
   generic error, and keep the copilot on the streaming endpoint.
3. ~~**Stage B.**~~ **Done** — see the status note above. The cancel-stays-
   cancelled test exists and passes.
   **Checked, and nothing further is needed:** every other mutable store
   (`approval_store.decide_approval`, `automation_store.update_automation` /
   `mark_run`, `agent_store.update_agent`, `template_store.add_version`,
   `code_plan_store`, `document_review_store`, …) already takes an **id**, looks
   the record up in its module dict, and writes back inside one synchronous
   function — which is the re-read discipline, arrived at by construction.
   `workflows.py` was the sole outlier precisely because it passed `WorkflowRun`
   objects across `await`s. They all received the atomic-write fix; adding a
   `StoreGuard` lock to each would only matter if a mutation were ever reached
   from a worker thread, which none is today. Do it when that changes, not now.
4. **Stage C.** Run queue, automations enqueue, orphan recovery.
5. Loop node v1 (`loopnodedesign.md` §12), which now has the platform it needs.
6. **Stage D** only when one process is genuinely the limit — and as an explicit
   decision with the store rewrite scoped, not as a config change.
