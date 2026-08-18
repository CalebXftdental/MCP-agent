# My Agent — Design (Exploratory)

**Status:** Exploratory notes from a single long design conversation (2026-08-17),
carried forward for a future session — NOT a finished spec the way
`digest_persoanl_kb.md` was before it got implemented. A handful of real decisions
got locked along the way (§0), but most of this doc is direction plus explicitly
open questions (§9). Nothing here is built. Don't treat the "decisions locked"
table as license to start coding without another pass — see the banner below.

**Scope:** A third top-level surface alongside "Home Chat" and "My Workflow":
"My Agent" lets a user set a goal/scope/budget once and have an LLM drive tool
calls on its own across multiple steps, unattended — plus a separate, higher-stakes
sub-feature where a local coding agent (Pi) helps a user extend what their agent
can do by generating actual code.

---

> **Read this before implementing anything below.** Same rule as every other
> design doc in this repo (`digest_persoanl_kb.md`, `STAGE2_PLAN.md`,
> `finalize_stage_1.md`): before starting implementation on **any** item here,
> (1) re-verify the relevant claim against the live code — this doc is from a
> single conversation, not a code audit, and less of it was checked against the
> live repo than `digest_persoanl_kb.md` was before its implementation pass; (2)
> ask the user clarifying questions about anything with a real tradeoff, a
> security/privacy implication, or an external dependency; (3) only then start
> writing code. This doc leans harder on (2) than usual — §9 is a long list of
> things that were deliberately left open, not forgotten.

## 0. Decisions locked

| # | Decision | Choice |
|---|---|---|
| Build approach | convert the workflow-graph engine into an agent engine vs. build a new sibling surface | **New sibling surface.** The deterministic graph engine is valuable *because* it's not dynamically steerable — that's what makes it auditable and cheap to reason about for the business processes it already covers well. Folding streaming/interrupt/dynamic-tool-choice into that same engine would weaken that guarantee for everything, not just the new use case. Share the governance/audit/tool-execution plumbing (`_govern` already doesn't care who decided to call a tool); don't share the execution model. |
| Identity/scoping for an agent run | new concept vs. reuse existing | **Reuse `agent_store.py`'s `AgentProfile`**, extended with goal/allowed-categories/budget fields — it already has the right shape (a governed, rate-limited identity restricted to an allow-list), just currently scoped to "which workflow templates" rather than "which categories/tools + what goal." |
| Code-generation authorization (Pi + local LLM, end-user-facing) | default-on for everyone vs. admin-granted opt-in | **Admin-granted opt-in per user**, at least to start. Explicitly NOT the same default-on treatment `personal_knowledge` got — that was safe to default-on because it only ever touches the user's own private data; code generation/execution is a categorically different risk class. Mirrors the existing `analytics` category pattern (seeded, nobody has it until an admin grants it). |
| Code-generation review gate | automated verification only vs. human technical review required | **A technical person (admin/designated reviewer) must review the generated code (or at minimum its test results) before it can run for real.** Not just the requesting user clicking approve — a non-technical user can't meaningfully evaluate generated code, so that "approval" wouldn't be a real gate. |
| Coding-agent harness (Pi vs. opencode) | keep opencode (existing precedent) vs. switch to Pi for this specifically | **Pi**, specifically because this is backed by a local, smaller (27B-class) model. Benchmark data found this session: on the *identical* model, Pi outperformed opencode because opencode's system prompt runs 10K+ tokens vs. Pi's under 1,000 — local models are more sensitive to prompt bloat than large hosted ones. `STAGE2_PLAN.md` §8 already flagged Pi as worth a second look "in case a specific gap in opencode ever makes it worth it" — this is that gap. |
| Coding-agent's backing model | hosted frontier model vs. self-hosted local model | **Self-hosted Qwen3.8-27B-FP8** (the user deployed this the same day this conversation happened, replacing the previously-deployed Qwen3.6-27B). Already running, zero additional infra cost. See §7.3 for the benchmark reasoning and its caveats. |

---

## 1. Terminology: what "agent" already means here vs. what's new

Worth being precise about before designing anything, because there's a real naming
collision already in the codebase:

- **`agent_store.py`'s `AgentProfile`** (confirmed live, read this session) is a
  governed *identity* a scheduled automation run executes under — `consumer_id`,
  `allowed_template_ids`, `max_runs_per_day`. It does not make decisions; it's a
  permission scope, closer to a service account than to an LLM loop.
- **The workflow-graph engine** (`workflow_graph_interpreter.py`) is deliberately
  NOT agentic — `finalize_stage_1.md` §6.1 already rejected "let the LLM re-derive
  the logic at runtime" in favor of a fixed, human-authored graph, specifically
  because a predictable execution path beats an LLM improvising the same logic
  every run. This was a considered choice, not an oversight, and North's own
  "loops and branching as native builder primitives" framing independently landed
  on the same conclusion.
- **Home chat and the workflow copilot are ALREADY agents**, in the actual
  technical sense (an LLM dynamically deciding which tool to call next, in a
  loop) — both run the same `run_chat`/`execute_tool` tool-calling loop in
  `orchestrator.py`, just with different system prompts. Nobody thinks of them as
  "agents" only because a human types every turn.
- **What "My Agent" actually adds**, therefore, is not reasoning capability —
  that already exists, twice — it's **removing the human-per-turn dependency**:
  a goal/scope/budget set once, then the LLM drives every step on its own.

## 2. Why a new sibling surface, not a workflow-engine conversion

Covered in §0's first row. The mental model: Home Chat (human drives every turn),
My Workflow (human wires the sequence once, in advance), My Agent (human sets a
goal/scope/budget once, then the LLM drives every turn on its own) — same
underlying `_govern`/audit/tool-execution machinery under all three, differing
only in who or what decides "what happens next."

## 3. Properties an agent run needs, and what already exists

Checked against the live code this session, not assumed:

- **Streaming** — already solved, just not wired up for this. Chat already has
  real SSE streaming (`run_chat_stream`, `text/event-stream` in
  `gateway/backend/chat.py`). `workflow_graph_interpreter.py`'s `run_graph` has
  **zero** streaming today — confirmed by reading it — it runs to completion and
  returns a dict. An agent run should reuse the chat-streaming machinery, not
  invent new infrastructure.
- **Chatable / steerable mid-run** — mostly already solved. The tool-calling loop
  already interleaves reasoning and tool calls turn-by-turn; what's new is
  decoupling "advance to the next step" from "wait for a human to type the next
  message" — i.e., it keeps going on its own, but should still accept a message
  if one arrives.
- **Interruptable** — the genuinely new, hard piece. Confirmed: no cancel/abort/
  pause exists anywhere in `workflow_graph_interpreter.py` today, and nothing
  equivalent exists for the chat loop either (a chat "run" only ever pauses
  because it's waiting on the human, not because it was interrupted mid-tool-call).
  This needs a live channel into an already-running async task — a run-scoped
  flag or queue the loop checks between steps. Plumbing problem, not a prompting
  problem. **No design chosen yet** — see §9.
- **"Free to shift" (dynamic tool selection)** — the actual agent-reasoning core.
  This is the reused orchestrator loop, budgeted (see §4).

## 4. Safety: the guardrail that disappears

Both existing "agents" (home chat, workflow copilot) have never needed a hard
step/tool-call budget because a human is always the one advancing each turn —
human patience is the de facto runaway-loop protection today, **by accident, not
by design**. The moment "My Agent" can advance on its own between turns, that
accidental safety net is gone. This makes `finalize_stage_1.md` §5's existing open
item — "confirm whether a per-run tool-call budget enforcement exists today" —
load-bearing rather than a nice-to-have: it needs a real answer, and probably a
real hard cap with `truncated: true` semantics (same convention the `loop`/
`paginate` node design in `finalize_stage_1.md` §3.2 already established), before
any unattended agent execution ships.

## 5. Authoring UX — direction only, not detailed

Resisted the instinct to build a second graph canvas for this. A workflow graph
is honest about being a fixed sequence, which is why a non-engineer can build one.
An agent's whole point is that the sequence isn't fixed, so a canvas would be
lying about what's actually happening. Direction: reuse the existing workflow
copilot's *chat* pattern as the authoring surface itself — describe the goal in
plain language, pick allowed categories/tools (reusing category grants as the
scope mechanism, same as everywhere else in this system), set a budget and a
trigger. A form/conversation, not a canvas. **Not wireframed or detailed further
this session** — see §9.

## 6. CLI — deferred, not because it's a bad idea but because it's premature

Covered at length in conversation; summary for whoever picks this up:

- **No CLI needed at the Home Chat / My Workflow level.** Those users have the
  right interface already (chat UI for non-technical staff), and MCP already
  gives any script/agent a programmatic, audited way to reach the same governed
  endpoints — a CLI wouldn't add monitoring capability that doesn't already exist
  at the `_govern` chokepoint.
- **Once real unattended agents exist, an internal ops/devtool CLI becomes a
  genuinely different, narrower value proposition** — triggering a run on demand,
  streaming its live tool-call trace, killing a runaway one, replaying a past
  run's audit trail. That's closer to a devtool than a product surface.
- **Recommendation: don't build it ahead of need.** Revisit once there's a first
  real unattended agent running and the team is reaching for `curl`/ad hoc
  scripts repeatedly to manage it — design the CLI against that actual pain, not
  a guess at its shape now.
- Correction worth keeping: the CLI's value was never really "making LLM calls
  easier" (that part is already trivial, one HTTP call) — it's agent-*run*
  observability and control.

## 7. End-user-facing code generation (Pi + local LLM) — the higher-stakes sub-feature

Confirmed scope this session: this is meant to be **end-user-facing** (a
workspace user gets help building their own agent), not an internal engineering
devtool. That's a materially bigger decision than anything else in this doc —
`STAGE2_PLAN.md` §8 already scoped an equivalent capability (`mcp-code`'s real
read/write/bash execution mode) as admin/developer-only, specifically because
arbitrary code generation/execution is a bigger risk surface than anything
governed today. Making the equivalent thing available to regular staff is a
deliberate expansion beyond that existing rule, not a natural extension of it —
flagging this explicitly rather than letting it read as a small feature.

### 7.1 Design principles locked this session

- **Never let generated code run directly/immediately as part of a live agent.**
  Same propose → preview → approval_gate → audit → commit shape already used for
  `email_send_external` and planned for the Acumatica write pilot
  (`STAGE2_PLAN.md` §4).
- **Generated code never bypasses `_govern`.** It only ever calls the same
  already-classified, already-audited tool surface everything else uses — no raw
  filesystem/network/shell access. A genuinely new primitive it needs gets a real
  manifest entry with a risk tier, like every other tool in this system, not a
  side door.
- **Narrow the task the model is asked to do.** Not "design and write an agent" —
  "fill in a well-typed function body against an explicit input/output contract."
  This is the tractable version of the task, and it's what makes automatic
  verification possible before a human ever sees it.
- **Verification-heavy harness, not a bigger prompt.** Auto-run generated code
  against test cases in a sandbox; reject/retry automatically; only surface
  passing candidates to the human reviewer. This is the mechanism behind the
  benchmark jump described in §7.3 below, not incidental to it.
- **Isolated execution**, reusing `STAGE2_PLAN.md` §8's existing principle for
  `mcp-code`'s real-execution mode: run against an isolated worktree per session,
  never the live system directly. **Open tension, not resolved:** that principle
  was designed for code-*editing* tasks (mutating a repo). "My Agent" custom
  tools plausibly need to call *real* governed tools (ERP, knowledge, etc.), not
  just read/write files in a sandbox — how isolation reconciles with needing real
  tool access is genuinely unresolved. See §9.

### 7.2 Authorization and review gate

Both locked this session (§0): admin-granted opt-in per user, not default-on; a
technical reviewer must look at generated code (or at minimum its test results)
before it runs for real, not just the requesting user's own approval.

### 7.3 Harness and model choice, with caveats

- **Pi over opencode** for this specifically, because of local-model fit (§0).
  `STAGE2_PLAN.md` §8 already named Pi as an SDK-mode/RPC-mode embeddable
  alternative "in case a specific gap in opencode ever makes it worth a second
  look" — local-model prompt-overhead sensitivity is that gap. **Not decided:**
  SDK mode vs. RPC mode for embedding Pi — see §9.
- **Model: self-hosted Qwen3.8-27B-FP8.** Benchmark figures found this session
  (Alibaba's own model-card numbers — **not independently replicated, standard
  caveat applies**): Qwen3.6-27B (the previous deployment) reported 73–77 on
  SWE-bench Verified, ~50–53 on SWE-bench Pro, and 59.3 on Terminal-Bench 2.0
  (reported as matching Claude Opus 4.5 on that specific benchmark). The same
  model reportedly reached 90% on SWE-bench Verified with "an engineered agent
  stack" — a ~15-point jump from harness quality alone, no model change, which is
  the concrete evidence behind §7.1's "verification-heavy harness" principle.
  Qwen3.8-27B (released 2026-08-14, three days before this conversation, same
  architecture family) reportedly improves SWE-bench Pro to 61.7. **No
  operational track record yet** given how recent the release is — the user
  deployed it to production the same day as this conversation, replacing 3.6,
  without a side-by-side validation pass. Worth watching, not necessarily wrong.

### 7.4 Operational loose end from the model swap

`appservice.settings.json`'s `GOVERNANCE_CHAT_MODEL` (and the matching env var)
hardcodes the path `/home/azureuser/models/Qwen3.6-27B-FP8`. Whether the actual
3.8 deployment reused that exact path or landed under a different one was
**not verified this session** — the user declined an endpoint check mid-session
and has since deallocated the VM. If the path changed, the chat lane's config
needs updating to match, or the self-hosted lane breaks. Check this before
relying on either the chat lane or anything built on top of it (including
everything in this doc).

---

## 9. Open questions to carry into a future session

Explicitly not resolved — carried forward on purpose, per the user's request,
rather than papered over with a default:

- **Interrupt mechanism**: how does a live human message actually reach an
  in-flight autonomous run? (Polling flag? Queue the loop checks between steps?
  A websocket push the loop awaits on?) No design chosen.
- **Budget defaults**: what's the actual per-run step/tool-call cap, and what
  happens when it's hit (hard stop with `truncated: true`, ask-to-continue,
  something else)? Ties directly to `finalize_stage_1.md` §5's still-open
  "does a per-run tool-call budget exist today at all" question — needs an answer
  before any unattended execution ships, not just for My Agent.
- **Authoring UX detail**: the "conversational form, not a canvas" direction
  (§5) is a direction, not a spec — no wireframe, no field list, no decision on
  how scope/budget get set inside that conversation.
- **CLI**: deliberately deferred (§6) — no commitment on if/when to build it,
  revisit only once a real unattended agent exists and the pain is felt directly.
- **Qwen3.8-27B validation**: deployed already, but no side-by-side comparison
  against 3.6 was actually run — worth doing before leaning on it for anything
  higher-stakes than what 3.6 was already trusted for.
- **`GOVERNANCE_CHAT_MODEL` path reconciliation** (§7.4) — unverified, blocking
  confirmation that the chat lane (and therefore everything built on it) still
  works post-swap.
- **What counts as a "new primitive"** when generated code needs a capability
  beyond existing governed tools — not detailed how that request gets triaged
  into a real manifest entry, or by whom.
- **Lifecycle of a generated custom tool**: is it a permanent, versioned,
  reusable artifact once approved (like an artifact in `artifact_store.py`), or
  single-use/ephemeral per agent? Not decided — affects whether it needs its own
  store, versioning, and revocation story.
- **Sandboxing vs. real tool access tension** (§7.1's "open tension, not
  resolved") — `mcp-code`'s isolated-worktree model doesn't obviously extend to
  code that needs to call live governed tools, not just edit files. This is
  probably the single hardest unresolved design question in this whole doc.
- **Pi embedding mode**: SDK mode vs. RPC mode (`STAGE2_PLAN.md` §8 mentions
  both as options) — not decided.
