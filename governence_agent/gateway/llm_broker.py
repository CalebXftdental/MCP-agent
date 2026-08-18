"""The single point every LLM call in this process goes through.

Two problems this exists to solve, both documented in concurrency_and_scale.md:

1. **Blocking the event loop.** Every LLM client we build is SYNCHRONOUS
   (orchestrator.py's `default_llm_complete`/`default_llm_stream` -- openai's
   `OpenAI`/`AzureOpenAI` and anthropic's `Anthropic`), and every one of them was
   called directly from an `async def`. A blocking socket read on the event loop
   stalls the WHOLE process -- during one inference the gateway cannot read or
   write any socket, so a second user's request is not "waiting for the GPU", it
   is not being served at all. That is why we could never have more than one
   request in flight, and therefore could never use more than one of the model
   server's concurrent slots no matter how many were free.

   Fixed here by running the sync callable off-loop (`asyncio.to_thread` for a
   one-shot, a pump thread + queue bridge for a stream) rather than by rewriting
   the three client builders against their async SDK equivalents. Deliberate:
   the Azure and Anthropic branches have no credentials in any test environment
   (see requirements.txt's note -- anthropic is import-checked only), so leaving
   their bodies byte-identical and changing only HOW they are invoked keeps the
   untestable paths untouched.

2. **Sharing a small, fixed pool of slots fairly.** With USE_LOCAL_LLM=true
   everything -- chat, the workflow copilot, the idle-session summarizer, and
   workflow `llm_transform` steps -- shares one self-hosted sglang server whose
   `max_running_requests` is 3 (Qwen3.6_27B/deploy.md). Two lanes keep batch work
   from starving a human:

     interactive  a person is watching (chat, copilot)   -- up to CAPACITY
     batch        nobody is watching (workflow steps,    -- up to CAPACITY-2,
                  scheduled runs, summaries)                floor 1

   Both lanes draw from one global semaphore, so the two limits compose rather
   than sum: total in flight never exceeds CAPACITY, batch never exceeds its own
   sub-limit, and therefore at CAPACITY=3 an interactive caller can always get at
   least 2 slots no matter what batch work is running.

Fairness inside the interactive lane is round-robin BY USER, not FIFO: one
copilot message is up to GOVERNANCE_WORKFLOW_CHAT_MAX_TURNS (10) separate
inferences, so plain FIFO would let one person's single message beat ten other
people's first turns. Waiters queue per user and slots are handed out by
rotating through users.

Capacity is read from the environment, never hard-coded -- see CAPACITY_ENV.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable

# The model server's own `max_running_requests`. Deliberately an env var rather
# than a constant: it is a property of the deployed server (sglang's
# --max-mamba-cache-size decides it, NOT --mem-fraction-static or
# --context-length), it differs per backend, and the deployment doc's own two
# statements of it disagree -- so the live value is configuration, not code.
# `MAX_RUNNING_REQUESTS` is accepted as a bare alias because that is the name the
# App Service setting uses.
CAPACITY_ENV = "GOVERNANCE_LLM_MAX_RUNNING_REQUESTS"
_CAPACITY_ALIAS = "MAX_RUNNING_REQUESTS"
_DEFAULT_CAPACITY = 3

INTERACTIVE = "interactive"
BATCH = "batch"
_LANES = (INTERACTIVE, BATCH)


class LLMBusy(RuntimeError):
    """No slot became free within this call's deadline, or the queue is full.

    Raised instead of queueing without bound. Carries the numbers a caller needs
    to tell a user something true ("N ahead of you") rather than a bare failure.
    """

    def __init__(self, message: str, *, lane: str, queued: int, waited_sec: float = 0.0):
        super().__init__(message)
        self.lane = lane
        self.queued = queued
        self.waited_sec = waited_sec


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def capacity() -> int:
    raw = os.getenv(CAPACITY_ENV) or os.getenv(_CAPACITY_ALIAS) or ""
    try:
        value = int(raw.strip() or _DEFAULT_CAPACITY)
    except ValueError:
        return _DEFAULT_CAPACITY
    return value if value > 0 else _DEFAULT_CAPACITY


def batch_capacity(total: int | None = None) -> int:
    """How many slots batch work may hold at once: CAPACITY-2, floor 1.

    The -2 is the reservation that makes the interactive guarantee real. At the
    current CAPACITY=3 that is 1 batch slot and 2 always available to a person;
    at CAPACITY=1 (a degenerate config) batch and interactive simply share the
    single slot rather than deadlocking.
    """
    total = capacity() if total is None else total
    return max(1, total - 2)


# ── The lane scheduler ────────────────────────────────────────────────────────

class _Waiter:
    __slots__ = ("future", "user", "lane", "queued_at")

    def __init__(self, future: asyncio.Future, user: str, lane: str):
        self.future = future
        self.user = user
        self.lane = lane
        self.queued_at = time.monotonic()


class _Broker:
    """Admission control for one process. Not thread-safe by design -- every
    method runs on the event loop; only the work ITSELF is handed to threads."""

    def __init__(self) -> None:
        self._in_flight: dict[str, int] = {INTERACTIVE: 0, BATCH: 0}
        # user -> FIFO of waiters, per lane. An OrderedDict so "next user" is a
        # rotation rather than a scan, which is what makes this round-robin.
        self._queues: dict[str, OrderedDict[str, deque[_Waiter]]] = {
            INTERACTIVE: OrderedDict(), BATCH: OrderedDict(),
        }
        self._served: dict[str, int] = {INTERACTIVE: 0, BATCH: 0}
        self._rejected: dict[str, int] = {INTERACTIVE: 0, BATCH: 0}
        self._wait_total: dict[str, float] = {INTERACTIVE: 0.0, BATCH: 0.0}
        self._wait_max: dict[str, float] = {INTERACTIVE: 0.0, BATCH: 0.0}

    # -- capacity checks ------------------------------------------------------

    def _total_in_flight(self) -> int:
        return self._in_flight[INTERACTIVE] + self._in_flight[BATCH]

    def _has_room(self, lane: str) -> bool:
        if self._total_in_flight() >= capacity():
            return False
        if lane == BATCH and self._in_flight[BATCH] >= batch_capacity():
            return False
        return True

    def queued(self, lane: str | None = None) -> int:
        lanes = _LANES if lane is None else (lane,)
        return sum(len(q) for ln in lanes for q in self._queues[ln].values())

    # -- acquire / release ----------------------------------------------------

    async def acquire(self, lane: str, user: str, *, timeout_sec: float) -> float:
        """Wait for a slot. Returns how long we waited. Raises LLMBusy."""
        lane = lane if lane in _LANES else BATCH  # unclassified -> the safe direction
        if self._has_room(lane) and not self._queues[lane]:
            self._in_flight[lane] += 1
            return 0.0

        max_queue = _int_env("GOVERNANCE_LLM_MAX_QUEUE", 32)
        if self.queued(lane) >= max_queue:
            self._rejected[lane] += 1
            raise LLMBusy(
                f"the assistant is at capacity ({self.queued(lane)} already waiting)",
                lane=lane, queued=self.queued(lane),
            )

        waiter = _Waiter(asyncio.get_running_loop().create_future(), user or "(anonymous)", lane)
        self._queues[lane].setdefault(waiter.user, deque()).append(waiter)
        try:
            await asyncio.wait_for(waiter.future, timeout=timeout_sec)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._drop(waiter)
            # A slot may have been handed to us in the same tick the timeout
            # fired; if so it is ours and must be given back, not leaked.
            if waiter.future.done() and not waiter.future.cancelled() and waiter.future.exception() is None:
                self.release(lane)
            waited = time.monotonic() - waiter.queued_at
            self._rejected[lane] += 1
            raise LLMBusy(
                f"the assistant did not free up within {timeout_sec:.0f}s",
                lane=lane, queued=self.queued(lane), waited_sec=waited,
            ) from None
        waited = time.monotonic() - waiter.queued_at
        self._wait_total[lane] += waited
        self._wait_max[lane] = max(self._wait_max[lane], waited)
        return waited

    def _drop(self, waiter: _Waiter) -> None:
        bucket = self._queues[waiter.lane].get(waiter.user)
        if bucket is None:
            return
        try:
            bucket.remove(waiter)
        except ValueError:
            pass
        if not bucket:
            self._queues[waiter.lane].pop(waiter.user, None)

    def release(self, lane: str) -> None:
        lane = lane if lane in _LANES else BATCH
        self._in_flight[lane] = max(0, self._in_flight[lane] - 1)
        self._served[lane] += 1
        # Interactive first: a freed slot goes to a person before it goes to a
        # workflow step, which is the whole point of having lanes.
        for candidate in (INTERACTIVE, BATCH):
            while self._has_room(candidate) and self._promote(candidate):
                pass

    def _promote(self, lane: str) -> bool:
        """Hand a slot to the next user in the rotation. False if nobody waits."""
        queues = self._queues[lane]
        for user in list(queues.keys()):
            bucket = queues[user]
            # Rotate BEFORE serving so the next release picks a different user.
            queues.move_to_end(user)
            while bucket:
                waiter = bucket.popleft()
                if not bucket:
                    queues.pop(user, None)
                if waiter.future.done():   # cancelled while queued
                    continue
                self._in_flight[lane] += 1
                waiter.future.set_result(True)
                return True
        return False

    # -- observability --------------------------------------------------------

    def snapshot(self) -> dict:
        total = capacity()
        return {
            "capacity": total,
            "capacitySource": CAPACITY_ENV if os.getenv(CAPACITY_ENV) else (
                _CAPACITY_ALIAS if os.getenv(_CAPACITY_ALIAS) else "default"),
            "batchCapacity": batch_capacity(total),
            "inFlight": {"total": self._total_in_flight(), **{ln: self._in_flight[ln] for ln in _LANES}},
            "queued": {ln: self.queued(ln) for ln in _LANES},
            "queuedByUser": {
                ln: {user: len(q) for user, q in self._queues[ln].items()} for ln in _LANES
            },
            "served": dict(self._served),
            "rejected": dict(self._rejected),
            "avgWaitSec": {
                ln: round(self._wait_total[ln] / self._served[ln], 3) if self._served[ln] else 0.0
                for ln in _LANES
            },
            "maxWaitSec": {ln: round(self._wait_max[ln], 3) for ln in _LANES},
        }


_BROKER = _Broker()


# ── Prefix-cache accounting ───────────────────────────────────────────────────
#
# sglang reuses the KV cache of any request whose token prefix it has already
# seen (RadixAttention). Our prompts are built to exploit that: the system prompt
# is a static literal, the tool block is sorted (build_tool_specs), and chat
# history is append-only -- so turn N+1 of a conversation is literally turn N's
# prompt plus more, and the whole shared head is free to re-prefill.
#
# That property is easy to lose by accident and invisible when you do (the only
# symptom is a slower prefill), so it is measured two ways:
#
#   * `_PREFIXES` -- how many DISTINCT (system prompt + tool list) heads this
#     process has produced. This should stay close to the number of distinct
#     grant-sets in use, times the two chat surfaces. If it climbs with traffic,
#     something is making the head non-deterministic and every request is paying
#     full prefill.
#   * `record_usage` -- what the SERVER says it reused, when it tells us
#     (`usage.prompt_tokens_details.cached_tokens`). This is ground truth.
_PREFIXES: dict[str, int] = {}
_PREFIX_CAP = 200          # stop growing the map; the count is what matters
_PROMPT_TOKENS = 0
_CACHED_PROMPT_TOKENS = 0
_USAGE_SAMPLES = 0


def prefix_signature(messages, tools) -> str:
    """A short hash of the part of the prompt that SHOULD be identical across
    requests: the system message plus the tool names, in order."""
    system = ""
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        system = str(messages[0].get("content") or "")
    names = []
    for spec in tools or []:
        fn = spec.get("function", spec) if isinstance(spec, dict) else {}
        names.append(str(fn.get("name", "")))
    raw = system + "\x00" + ",".join(names)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def note_prefix(messages, tools) -> None:
    key = prefix_signature(messages, tools)
    if key in _PREFIXES:
        _PREFIXES[key] += 1
    elif len(_PREFIXES) < _PREFIX_CAP:
        _PREFIXES[key] = 1


def record_usage(usage) -> None:
    """Fold one response's token usage into the cache-hit counters.

    `prompt_tokens_details.cached_tokens` is the OpenAI-compatible field sglang
    reports prefix-cache reuse in. Every access is defensive: not every backend
    or version returns it, and a missing field must never break a chat turn."""
    global _PROMPT_TOKENS, _CACHED_PROMPT_TOKENS, _USAGE_SAMPLES
    if usage is None:
        return
    try:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    except (TypeError, ValueError):
        return
    if prompt_tokens <= 0:
        return
    _PROMPT_TOKENS += prompt_tokens
    _CACHED_PROMPT_TOKENS += min(cached, prompt_tokens)
    _USAGE_SAMPLES += 1


def cache_snapshot() -> dict:
    hit_rate = round(_CACHED_PROMPT_TOKENS / _PROMPT_TOKENS, 4) if _PROMPT_TOKENS else None
    return {
        # Distinct prompt heads produced. Compare against how many grant-sets are
        # actually in use -- a number that climbs with traffic means the head is
        # not stable and prefix caching is doing nothing.
        "distinctPrefixes": len(_PREFIXES),
        "prefixCapped": len(_PREFIXES) >= _PREFIX_CAP,
        "requestsPerPrefix": sorted(_PREFIXES.values(), reverse=True)[:10],
        # Server-reported reuse. None means this backend never told us.
        "promptTokens": _PROMPT_TOKENS,
        "cachedPromptTokens": _CACHED_PROMPT_TOKENS,
        "cacheHitRate": hit_rate,
        "usageSamples": _USAGE_SAMPLES,
    }


def snapshot() -> dict:
    return {**_BROKER.snapshot(), "promptCache": cache_snapshot()}


def reset_for_tests() -> None:
    global _BROKER, _PROMPT_TOKENS, _CACHED_PROMPT_TOKENS, _USAGE_SAMPLES
    _BROKER = _Broker()
    _PREFIXES.clear()
    _PROMPT_TOKENS = 0
    _CACHED_PROMPT_TOKENS = 0
    _USAGE_SAMPLES = 0


def _timeout_for(lane: str) -> float:
    """How long a caller waits for a slot before giving up. Interactive is short
    (a person is watching a spinner); batch is long (nobody is, and a workflow
    step would rather wait than fail)."""
    if lane == INTERACTIVE:
        return _float_env("GOVERNANCE_LLM_INTERACTIVE_QUEUE_TIMEOUT_SEC", 45.0)
    return _float_env("GOVERNANCE_LLM_BATCH_QUEUE_TIMEOUT_SEC", 300.0)


# ── Running the work off the event loop ───────────────────────────────────────

_ADAPTED = "_llm_broker_adapted"


def adapt_complete(fn: Callable | None) -> Callable | None:
    """Wrap a one-shot `complete(messages, tools)` into an awaitable, lane-managed
    `complete(messages, tools, *, lane, user)`. Idempotent.

    A sync `fn` is run in a worker thread; an `async def fn` is awaited directly.
    Accepting both is what lets every existing test keep injecting a plain sync
    fake (`llm_complete=lambda messages, tools: ...`) unchanged while production
    uses the real blocking SDK client.
    """
    if fn is None or getattr(fn, _ADAPTED, False):
        return fn

    async def complete(messages, tools, *, lane: str = BATCH, user: str = "", **extra):
        timeout = _timeout_for(lane)
        note_prefix(messages, tools)
        await _BROKER.acquire(lane, user, timeout_sec=timeout)
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(messages, tools, **extra)
            return await asyncio.to_thread(fn, messages, tools, **extra)
        finally:
            _BROKER.release(lane)

    setattr(complete, _ADAPTED, True)
    return complete


def adapt_stream(fn: Callable | None) -> Callable | None:
    """Wrap a `stream(messages, tools)` sync generator into a lane-managed ASYNC
    generator. Idempotent.

    The sync generator is pumped in one worker thread that pushes each delta onto
    the loop via `call_soon_threadsafe`; the async side just drains a queue. The
    slot is held for the whole stream and released in `finally`, including when
    the consumer abandons the generator part-way (which sets `stop` so the pump
    breaks out and the underlying HTTP stream is closed by the generator's own
    cleanup).
    """
    if fn is None or getattr(fn, _ADAPTED, False):
        return fn

    async def stream(messages, tools, *, lane: str = BATCH, user: str = "", **extra):
        timeout = _timeout_for(lane)
        note_prefix(messages, tools)
        await _BROKER.acquire(lane, user, timeout_sec=timeout)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        done = object()
        stop = threading.Event()

        def pump():
            try:
                for item in fn(messages, tools, **extra):
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except BaseException as exc:  # noqa: BLE001 -- re-raised on the loop side
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        worker = threading.Thread(target=pump, name="llm-stream-pump", daemon=True)
        worker.start()
        try:
            while True:
                item = await queue.get()
                if item is done:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            _BROKER.release(lane)

    setattr(stream, _ADAPTED, True)
    return stream


async def run(fn: Callable, *args, lane: str = BATCH, user: str = "", **kwargs) -> Any:
    """Escape hatch for a one-off LLM-shaped call that isn't the standard
    `complete(messages, tools)` signature. Same lane accounting."""
    await _BROKER.acquire(lane, user, timeout_sec=_timeout_for(lane))
    try:
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)
    finally:
        _BROKER.release(lane)
