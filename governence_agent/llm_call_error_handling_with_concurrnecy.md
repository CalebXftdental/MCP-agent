# LLM Call Error Handling with Concurrency-Aware Routing

Status: **design, not yet implemented**. Written 2026-08-26. Closes the gap
already named but never built in `concurrency_and_scale.md:296-299`
("every LLM call gets a deadline and a bounded retry with jitter") and adds
the routing layer needed for the CPU-MoE-for-Home / GPU-for-Copilot /
Sonnet-overflow split.

Companion doc: `Qwen3.6_27B/deploy.md` (local inference deployment — GPU
sglang + CPU Ornith + CPU reranker, all on one VM). Read that before changing
any capacity numbers below; they drift (see deploy.md's own drift note under
"Memory tuning") and must be re-checked against the live systemd units, not
copied from here blind.

---

## 1. Why this design looks the way it does

Four facts from `deploy.md` that shape every decision below — skipping these
leads to a design that looks reasonable and fails in production:

1. **GPU (sglang, port 8000) and CPU (Ornith, port 8092) live on the same VM.**
   They are not independent failure domains. A VM reboot, network partition,
   disk-full, or Azure host issue takes both down together. CPU is a backup
   for *GPU-specific* problems (VRAM OOM, MIG slice issue, sglang crash), not
   for host-level outages. Sonnet (Anthropic, external) is the only
   independent backup either backend has.
2. **Concurrency headroom is thin by design.** GPU `max_running_requests` is
   2 (live) / 3 (documented "settled" config — these have drifted before,
   re-check `systemctl status sglang`'s actual `server_args` before trusting
   either number). CPU Ornith has a hard cap of 4 concurrent slots
   (`--parallel`/`-np` default, confirmed by benchmark: 6 fired → 4 finish
   together, 2 queue). A retry policy that doesn't respect these caps will
   amplify load exactly when a backend is already struggling.
3. **Ornith (CPU MoE) is measured, not assumed, to be unsafe as a silent
   automatic fallback for tool-heavy traffic.** The robustness harness
   (`deploy.md:626-652`, `robustness_sweep_workflow_authoring.py`) scored it
   6/10 vs. Qwen3.8's 10/10, with 4 `TURN_BUDGET_EXHAUSTED` failures
   root-caused to unreliable tool-argument construction specifically under
   the combination of (long system prompt + broad tool menu + real-data
   extraction) — not a general capability deficit. This is exactly the shape
   of the Copilot's real system prompt (`WORKFLOW_COPILOT_SYSTEM_PROMPT`,
   ~29 tools). It must never be an automatic fallback target for Copilot
   traffic, and needs its own validation before being trusted for Home
   traffic too (see §2).
4. **Two silent ("HTTP 200 but wrong") failure signatures are already
   documented as having actually occurred**, not hypothetical:
   - Empty `content` + non-empty `reasoning_content` + `finish_reason:
     "length"` — thinking-mode reasoning consumed the whole `max_tokens`
     budget (`deploy.md:104`).
   - `HTTP 400 exceed_context_size_error` once accumulated multi-turn tool
     context exceeds the deployment's context cap (`deploy.md:578`) — a
     terminal, non-retryable error class, not a transient one.
   Prefill latency is also legitimately variable under MIG contention (15.7s
   vs. 57.6s for near-identical 130K-context requests, `deploy.md:357`) — a
   fixed short timeout will false-positive on requests that are just slow.

---

## 2. Pre-work: validate before routing on assumption

Do these two measurements before wiring automatic routing/failover — they
change what the router is allowed to do, not just how well it performs.

- **[ ] Re-run `robustness_sweep_workflow_authoring.py`-equivalent against
  Home's actual system prompt and actual tool menu** (`exclude_tools =
  WORKFLOW_ONLY_TOOLS`, i.e. *most* tools are still exposed to Home — this is
  not a trimmed menu). Ornith's failure needed the real combination of long
  prompt + broad tool menu + extraction; a synthetic slimmed-down test would
  under-detect the risk. If Home's tool menu is materially as broad as
  Copilot's, Home inherits the same risk profile Ornith already failed on.
- **[ ] Run the same harness against Sonnet** with the real MCP tool schemas
  and `WORKFLOW_COPILOT_SYSTEM_PROMPT` before treating Sonnet-overflow for
  Copilot as quality-neutral. The Anthropic tool-calling code path
  (`orchestrator.py:780-825`) is wired, but "wired" and "validated on this
  exact prompt/tool-set under load" are different claims.
- **[ ] Consider trimming Home's per-request tool menu** to what's relevant
  to that turn — attacks the documented failure mechanism at the source,
  independent of backend choice. Flagged as an untested lever in
  `deploy.md:656`.

---

## 3. Target architecture

```
request → task classifier (Home / Copilot / nav-help)
        → target lane: Home -> CPU-MoE, Copilot -> GPU
        → admission: try acquire lane slot, SHORT wait (~1-2s, not today's 45s)
            |
            +-- slot acquired AND circuit closed
            |     -> call local model (§4 resilience: retry/backoff/quality-check)
            |         +-- success -> return
            |         +-- retries exhausted, OR mid-turn stuck-loop detected (§6)
            |               -> escalate to Sonnet for the remainder of the turn
            |                  tag: "degraded" (health-driven)
            |
            +-- no slot within short wait, OR circuit open
                  -> route straight to Sonnet
                     tag: "capacity-overflow" (circuit open) or "busy-overflow" (no slot)
                     Sonnet call itself goes through its own admission lane +
                     its own §4 retry/backoff (Anthropic has real transient
                     failure modes too, e.g. 529 overloaded)
                     |
                     +-- Sonnet also unavailable/over its own budget
                           -> last resort: today's behavior (brief queue, then
                              503 + Retry-After, `chat.py:_busy_response`)
```

Two distinct triggers feed the same fallback path — **capacity** (busy, a
healthy backend just has no free slot) and **health** (circuit open,
something is actually broken). Tag them differently in telemetry (§7): they
mean different things operationally. Busy = "need more capacity or traffic
is bursty"; circuit-open = "something is down, go look."

**Confidence differs by task, act accordingly:**
- Home → Sonnet overflow: close to a free win — Sonnet is strictly the
  stronger backup for the intentionally-lighter tier. Safe to enable
  aggressively once §2's Home-specific validation is done.
- Copilot → Sonnet overflow: a genuine safety valve for capacity, but its
  *quality* needs the §2 measurement before being trusted under real load,
  not assumed from "Sonnet is generally strong."
- Home/Copilot → **local CPU MoE is never an automatic fallback target for
  Copilot traffic**, regardless of capacity pressure. Fact 3 in §1 stands
  until a harness run says otherwise.

---

## 4. Stage 1 — local-call resilience (per backend: GPU, CPU-MoE, and Sonnet)

Applies uniformly to every LLM callable, including Sonnet's own lane — Sonnet
is not exempt from needing retry/circuit-breaker treatment just because it's
the fallback target.

**Detect** — classify every response/exception into three buckets:

| Class | Examples | Action |
|---|---|---|
| Transient/retryable | connection refused/reset (e.g. mid `systemctl restart`, `RestartSec=5`), timeout, 502/503, Anthropic 529 | retry, bounded |
| Terminal/non-retryable | `400 exceed_context_size_error`, malformed-request 4xx | do NOT retry — identical failure guaranteed; surface immediately (trim context / structured error) |
| Silent/semantic ("200 but wrong") | empty `content` + `finish_reason=="length"` + non-empty `reasoning_content`; (defense-in-depth) degenerate repeated-token output | treat as failure; recover differently, see below |

**Recover:**
- Transient: exponential backoff + jitter, small cap (2-3 attempts). Timeout
  per attempt scales with request size (floor + per-1K-token allowance)
  rather than one fixed number — the documented prefill variance means a
  fixed short timeout will false-positive on legitimately slow requests.
- Silent/semantic (reasoning-truncation case): a blind identical-params retry
  is close to useless at `temperature=0` (the Copilot's setting) — it will
  likely truncate at the same point again. Retry once with an adjusted
  request instead: bump `max_tokens`, or set `chat_template_kwargs:
  {"enable_thinking": false}`.

**Isolate:**
- Retries re-enter `llm_broker`'s existing admission control (acquire a slot
  again) — never bypass it, or retries silently create load the broker was
  built to bound.
- Per-backend circuit breaker (new state, lives next to `_BROKER` in
  `llm_broker.py:268`, same in-process pattern). Threshold on
  consecutive-failures / failure-rate in a short rolling window. Open ->
  fail fast with **zero retries**, go straight to fallback — don't let every
  concurrent request independently burn 2-3 retries against a backend
  that's already known to be down on a 2-4-slot server. Half-open probe
  after a short cooldown (seconds, matching `RestartSec=5`'s typical
  self-heal window); require a couple of clean probes before fully closing
  to avoid flapping.

**Degrade / surface, don't hide:**
- Flag fallback responses (SSE field / response metadata + a counter in
  telemetry, §7) rather than silently absorbing them.
- Once the circuit closes again, stop routing *new* turns to the recovered
  backend, but let any turn already mid-conversation on Sonnet finish there
  — tool-calling formats differ between OpenAI-style (`orchestrator.py:637,
  664, 986, 1012`) and Anthropic-style (`orchestrator.py:780, 802-825`)
  clients; switching backends mid-turn risks feeding the wrong-shaped tool
  history into the wrong-shaped client.

---

## 5. Concurrency-aware admission routing (new)

This is the mechanism behind the user's "over-concurrency ones get sent to
Sonnet" proposal, built as **admission-time routing**, not failure-time
fallback:

- Extend `llm_broker`'s lane model from today's `INTERACTIVE`/`BATCH`
  (`llm_broker.py:169` `acquire(lane, user, timeout_sec)`) to be
  backend-scoped: `HOME_CPU`, `COPILOT_GPU`, `SONNET_OVERFLOW`, each with its
  own known capacity ceiling (`capacity()`/`batch_capacity()`,
  `llm_broker.py:102-120`, currently GPU-wide — needs a CPU-MoE-specific
  ceiling and a Sonnet ceiling added).
- On a new request: attempt admission to the primary lane with a **short**
  wait (~1-2s) instead of today's interactive queue timeout
  (`GOVERNANCE_LLM_INTERACTIVE_QUEUE_TIMEOUT_SEC=45s`). No slot within that
  window -> immediately route to `SONNET_OVERFLOW` rather than making the
  user wait out the full 45s queue before ever finding out.
- Retire the "queue-then-503" behavior (`chat.py:_busy_response`,
  `nav_help.py:_busy_payload`) to a **last-resort** step only, used when
  Sonnet's own lane is also saturated/unavailable — not the first response
  to local busy-ness.
- Fairness: per-user FIFO ordering that the broker already does
  (`llm_broker.py`'s `OrderedDict[str, deque[_Waiter]]`) must still apply
  before a request is judged "no slot" — a user shouldn't jump the queue
  into Sonnet ahead of another user who was already about to get a local
  slot.

Why admission-time beats failure-time here: with 2-4 local slots, a request
that queues 45s and then 503s is 45s of dead time for nothing. Knowing
within ~1-2s that there's no slot and handing off to Sonnet immediately is
both faster for the user and protects the local backend from the
retry-storm risk in §4 (overflowing at admission means nothing is stacking
retries on an already-saturated backend).

---

## 6. Semantic (mid-turn) circuit breaker — targeted at the documented failure

The Ornith failure signature (`deploy.md:646`) was specific: repeatedly
calling the same tool with degenerate/empty args while its own reasoning
text says "I need to stop doing this" — never fixing it, burning the full
`_MAX_TOOL_TURNS`/`WORKFLOW_CHAT_MAX_TURNS` budget
(`orchestrator.py:52,58,870,1119`) getting there.

This is detectable *during* a turn, faster and more precisely than the
aggregate failure-rate circuit breaker in §4:

- In the tool-calling loop (call sites at `orchestrator.py:890, 924, 933,
  1156, 1167`, all funneling through `execute_tool`,
  `orchestrator.py:438-457`), track the (tool name, args) of consecutive
  calls within the current turn.
- If the same tool fires N times in a row with identical or degenerate
  (empty-collection) args, treat it as a live stuck-loop, not a transient
  tool error: abort the local attempt for the rest of that turn and hand off
  to Sonnet immediately, rather than waiting for turn-budget exhaustion.
- This is backend-agnostic logic (works the same whether the local backend
  is CPU-MoE or GPU) but is the specific mitigation for exactly the failure
  mode already measured — build it regardless of which backend ends up
  serving Home.

---

## 7. Observability (extends existing endpoint, doesn't add a new one)

Extend `admin_security._admin_llm_lanes` (`admin_security.py:120`, wired at
`backend/__init__.py:154` as `GET /admin/llm-lanes`) — already the
observability surface for `llm_broker.snapshot()` — with:

- Per-lane (`HOME_CPU`, `COPILOT_GPU`, `SONNET_OVERFLOW`) in-flight/capacity,
  matching the new lane model in §5.
- Circuit-breaker state per backend (closed/open/half-open, time since last
  trip).
- Overflow counters, split by cause: `busy-overflow` (no slot, healthy
  backend) vs. `degraded-overflow` (circuit open / retries exhausted /
  stuck-loop detected). These must stay distinguishable — they drive
  different follow-up actions (capacity planning vs. incident investigation).
- Mid-turn stuck-loop trigger count (§6), separately from the aggregate
  circuit breaker trips (§4), since it's a different detector.

**This telemetry is also the answer to the 96GB VRAM question** — don't
decide that from first principles:

- Run for 1-2 weeks of real traffic once the counters exist.
- Low, occasional Copilot `busy-overflow` rate (say <5%, mostly during known
  bursts) -> current GPU concurrency is fine; Sonnet absorbing the tail is
  cheaper than doubling GPU spend.
- Frequent/sustained `busy-overflow` -> real evidence for the upgrade. Size
  it with the same math the deploy doc already used to pick 3-concurrent/
  40,960 at 48GB (`N × context_length ≤ max_total_num_tokens`,
  `deploy.md:145-157`) — 96GB roughly doubles the token-capacity ceiling,
  re-derive the safe concurrency from that, don't guess.
- Compare against the *measured* Sonnet-overflow API cost from the same
  window, not a hypothetical, before comparing to the GPU upgrade's ~2x cost
  (`deploy.md:671`, NC144lds_xl vs. g7e.2xlarge).

---

## 8. Config additions

New env vars, consistent with existing `GOVERNANCE_LLM_*` naming
(`orchestrator.py:577-589`, `llm_broker.py:102-120,370-376`):

| Var | Purpose | Suggested default |
|---|---|---|
| `GOVERNANCE_LLM_MAX_RETRIES` | Stage-1 bounded retry cap, per backend | `2` |
| `GOVERNANCE_LLM_RETRY_BASE_DELAY_SEC` | backoff base (jittered) | `0.5` |
| `GOVERNANCE_LLM_CIRCUIT_FAILURE_THRESHOLD` | consecutive/rate trip point | tune from measurement |
| `GOVERNANCE_LLM_CIRCUIT_COOLDOWN_SEC` | half-open probe delay | `10` (matches `RestartSec=5`-ish recovery) |
| `GOVERNANCE_LLM_ADMISSION_WAIT_SEC` | short admission wait before overflow (§5) | `1.5` |
| `GOVERNANCE_LLM_STUCKLOOP_REPEAT_THRESHOLD` | consecutive identical/degenerate tool calls before mid-turn breaker trips (§6) | `3` |
| `GOVERNANCE_LLM_SONNET_OVERFLOW_ENABLED` | master switch, per task type (Home / Copilot independently) | `true` for Home, gated on §2 for Copilot |
| `GOVERNANCE_LLM_SONNET_LANE_CAPACITY` | Sonnet's own admission ceiling (§5) | tune against Anthropic rate limits |

---

## 9. Where this lives in the code

- Retry/circuit-breaker wrapper: new logic in `llm_broker.py` alongside
  `adapt_complete`/`adapt_stream` (`llm_broker.py:384-460`) — keeps
  `orchestrator.py`'s `complete()`/`stream()` builders (lines 637-997,
  780-825) unchanged; resilience stays in the one module already
  responsible for adapting LLM callables.
- Lane/capacity model changes: `llm_broker.py:102-265` (`capacity()`,
  `batch_capacity()`, `_Broker`).
- Admission-time overflow routing: new function, called from
  `chat.py`/`nav_help.py` before today's `_BROKER.acquire(...)` call sites
  (`chat.py:54,135-136,186-187,242-243`; `nav_help.py:123-155`).
- Mid-turn stuck-loop detector: `orchestrator.py`'s tool-calling loops
  (~`orchestrator.py:870-942` non-streaming, `~1119-1187` streaming), reading
  from the same tool-call history the loop already tracks.
- Telemetry: `admin_security.py:120` (`_admin_llm_lanes`).

## 10. Testing

Add `_smoke/test_llm_resilience.py`, alongside the existing
`_smoke/test_llm_broker.py` and `_smoke/test_tool_call_resilience.py`,
covering:

- Transient error recovers via retry (per backend).
- Persistent failure opens the circuit and triggers Sonnet failover, tagged
  `degraded-overflow`.
- Local lane at capacity routes to Sonnet within the short admission window,
  tagged `busy-overflow`, without waiting the old 45s.
- Empty-content-with-reasoning-truncation gets the corrective retry
  (adjusted params), not a blind identical retry.
- Mid-turn repeated-degenerate-tool-call pattern trips the stuck-loop
  breaker before `_MAX_TOOL_TURNS`/`WORKFLOW_CHAT_MAX_TURNS` exhaustion.
- Sonnet's own lane, when saturated, falls through to the last-resort
  queue-then-503 behavior rather than failing silently.

Extend `test_workflow_chatbot_separation.py`'s existing assertion (Home vs.
Copilot use different system prompts/tool sets) to also assert they resolve
to different primary lanes under this design.
