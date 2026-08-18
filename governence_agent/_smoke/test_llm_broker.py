"""Smoke test for gateway/llm_broker.py -- the LLM admission-control layer
(concurrency_and_scale.md Stage A).

Network-independent by construction: the broker's whole job is to manage HOW a
callable is invoked, so a sleep-based fake stands in for the model perfectly and
there is no reason to need a server, a GPU, or credentials.

What matters here, in order of importance:

  1. The event loop STAYS FREE while a blocking (synchronous) LLM call runs.
     This is the bug the broker exists to fix -- before it, one inference froze
     the whole gateway, so nothing else could be served and we could never have
     more than one request in flight no matter how many server slots were free.
     Test 1 is the regression test for that and would fail against the old code.
  2. Capacity and lanes: total in flight never exceeds CAPACITY; batch never
     exceeds CAPACITY-2 (floor 1); interactive keeps its reserved slots even
     while batch work is saturated.
  3. Round-robin fairness BY USER, so one person's 10-turn copilot message
     cannot beat ten other people's first turns.
  4. Bounded queue + deadline -> LLMBusy, never an unbounded backlog.
  5. Streaming: a sync generator is pumped off-loop, deltas arrive in order, the
     slot is held for the whole stream and released after -- including when the
     consumer abandons the stream part-way.
  6. Sync AND async callables both work (existing tests inject plain sync fakes).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))

os.environ["GOVERNANCE_LLM_MAX_RUNNING_REQUESTS"] = "3"

import llm_broker  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


class _Msg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


def blocking_complete(delay=0.2, tracker=None):
    """A stand-in for the real SDK client: genuinely blocks the calling thread,
    exactly like openai's sync client does on a socket read."""
    def complete(messages, tools):
        if tracker is not None:
            tracker.enter()
        time.sleep(delay)
        if tracker is not None:
            tracker.exit()
        return _Msg("done")
    return complete


class Tracker:
    """Records concurrent occupancy so a test can assert a ceiling was respected."""

    def __init__(self):
        self.current = 0
        self.peak = 0
        self._lock = __import__("threading").Lock()

    def enter(self):
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def exit(self):
        with self._lock:
            self.current -= 1


# ── 1. The event loop stays free during a blocking call ───────────────────────

async def test_loop_not_blocked():
    print("\n[1] event loop stays responsive while a blocking LLM call runs")
    llm_broker.reset_for_tests()
    complete = llm_broker.adapt_complete(blocking_complete(delay=0.5))

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    msg = await complete([{"role": "user", "content": "hi"}], None, lane="interactive", user="alice")
    elapsed = time.monotonic() - started
    beat.cancel()

    check("the call returned its result", getattr(msg, "content", "") == "done")
    check("it really took ~the blocking duration", 0.45 <= elapsed < 1.5, f"{elapsed:.2f}s")
    # THE assertion: an unrelated coroutine kept running throughout. Against the
    # pre-broker code this is 0 -- the loop was blocked for the whole inference.
    check("other coroutines kept running during it", ticks >= 20, f"only {ticks} ticks")


# ── 2. Capacity and lane reservation ──────────────────────────────────────────

async def test_capacity_and_lanes():
    print("\n[2] capacity ceiling and batch/interactive lanes")
    llm_broker.reset_for_tests()
    check("capacity comes from the env var", llm_broker.capacity() == 3)
    check("batch lane is capacity-2", llm_broker.batch_capacity() == 1)

    tracker = Tracker()
    complete = llm_broker.adapt_complete(blocking_complete(delay=0.15, tracker=tracker))

    await asyncio.gather(*[
        complete([], None, lane="interactive", user=f"u{i}") for i in range(8)
    ])
    check("never exceeded capacity", tracker.peak <= 3, f"peak={tracker.peak}")
    check("actually ran in parallel (not serialized)", tracker.peak >= 2, f"peak={tracker.peak}")

    batch_tracker = Tracker()
    batch = llm_broker.adapt_complete(blocking_complete(delay=0.15, tracker=batch_tracker))
    await asyncio.gather(*[batch([], None, lane="batch", user=f"b{i}") for i in range(6)])
    check("batch never exceeded its sub-limit", batch_tracker.peak <= 1, f"peak={batch_tracker.peak}")


async def test_interactive_not_starved_by_batch():
    print("\n[3] batch work cannot starve an interactive caller")
    llm_broker.reset_for_tests()
    batch = llm_broker.adapt_complete(blocking_complete(delay=0.6))
    quick = llm_broker.adapt_complete(blocking_complete(delay=0.05))

    # Saturate the batch lane, then time how long a person waits.
    batch_tasks = [asyncio.create_task(batch([], None, lane="batch", user=f"b{i}")) for i in range(4)]
    await asyncio.sleep(0.05)
    started = time.monotonic()
    await quick([], None, lane="interactive", user="alice")
    waited = time.monotonic() - started
    for t in batch_tasks:
        t.cancel()
    await asyncio.gather(*batch_tasks, return_exceptions=True)
    # With CAPACITY=3 and batch capped at 1, two slots are always free for people.
    check("interactive was served immediately", waited < 0.35, f"waited {waited:.2f}s")


# ── 4. Per-user fairness ──────────────────────────────────────────────────────

async def test_round_robin_fairness():
    print("\n[4] queued work is round-robin by user, not FIFO")
    llm_broker.reset_for_tests()
    order: list[str] = []

    def make(user):
        def complete(messages, tools):
            time.sleep(0.05)
            order.append(user)
            return _Msg(user)
        return llm_broker.adapt_complete(complete)

    # "greedy" enqueues 5 calls (a 10-turn copilot message's shape) before four
    # other people each enqueue one. FIFO would serve all of greedy's first.
    tasks = []
    for i in range(5):
        tasks.append(asyncio.create_task(make("greedy")([], None, lane="interactive", user="greedy")))
    await asyncio.sleep(0)          # let them queue
    for name in ("bob", "carol", "dave", "erin"):
        tasks.append(asyncio.create_task(make(name)([], None, lane="interactive", user=name)))
    await asyncio.gather(*tasks)

    greedy_in_first_six = order[:6].count("greedy")
    others = {"bob", "carol", "dave", "erin"} & set(order[:6])
    check("greedy did not monopolize the first slots", greedy_in_first_six <= 4,
          f"{greedy_in_first_six} of the first 6 were greedy: {order}")
    check("other users were interleaved early", len(others) >= 2, f"order={order}")
    check("everything eventually ran", len(order) == 9, f"{len(order)} of 9")


# ── 5. Bounded queue and deadline ─────────────────────────────────────────────

async def test_queue_bound_and_timeout():
    print("\n[5] bounded queue and deadline produce LLMBusy, not a backlog")
    llm_broker.reset_for_tests()
    os.environ["GOVERNANCE_LLM_INTERACTIVE_QUEUE_TIMEOUT_SEC"] = "0.3"
    try:
        slow = llm_broker.adapt_complete(blocking_complete(delay=1.2))
        held = [asyncio.create_task(slow([], None, lane="interactive", user=f"u{i}")) for i in range(3)]
        await asyncio.sleep(0.1)
        busy = None
        try:
            await slow([], None, lane="interactive", user="latecomer")
        except llm_broker.LLMBusy as exc:
            busy = exc
        check("a waiter past its deadline raises LLMBusy", busy is not None)
        check("LLMBusy reports its lane", getattr(busy, "lane", None) == "interactive")
        for t in held:
            t.cancel()
        await asyncio.gather(*held, return_exceptions=True)
    finally:
        os.environ.pop("GOVERNANCE_LLM_INTERACTIVE_QUEUE_TIMEOUT_SEC", None)

    llm_broker.reset_for_tests()
    os.environ["GOVERNANCE_LLM_MAX_QUEUE"] = "2"
    try:
        slow = llm_broker.adapt_complete(blocking_complete(delay=0.8))
        held = [asyncio.create_task(slow([], None, lane="interactive", user=f"u{i}")) for i in range(3)]
        await asyncio.sleep(0.05)
        queued = [asyncio.create_task(slow([], None, lane="interactive", user=f"q{i}")) for i in range(2)]
        await asyncio.sleep(0.05)
        rejected = None
        try:
            await slow([], None, lane="interactive", user="overflow")
        except llm_broker.LLMBusy as exc:
            rejected = exc
        check("a full queue rejects rather than growing", rejected is not None)
        for t in held + queued:
            t.cancel()
        await asyncio.gather(*(held + queued), return_exceptions=True)
    finally:
        os.environ.pop("GOVERNANCE_LLM_MAX_QUEUE", None)


# ── 6. Streaming ──────────────────────────────────────────────────────────────

async def test_streaming():
    print("\n[6] streaming: pumped off-loop, ordered, slot released after")
    llm_broker.reset_for_tests()

    class Delta:
        def __init__(self, content):
            self.content = content
            self.tool_calls = None

    def sync_stream(messages, tools):
        for i in range(5):
            time.sleep(0.05)
            yield Delta(f"tok{i}")

    stream = llm_broker.adapt_stream(sync_stream)

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    got = [d.content async for d in stream([], None, lane="interactive", user="alice")]
    beat.cancel()

    check("all deltas arrived in order", got == [f"tok{i}" for i in range(5)], got)
    check("the loop stayed free between deltas", ticks >= 10, f"only {ticks} ticks")
    check("the slot was released after the stream", llm_broker.snapshot()["inFlight"]["total"] == 0,
          llm_broker.snapshot())

    # Abandoning a stream part-way must still release the slot.
    async def abandon():
        async for _ in stream([], None, lane="interactive", user="bob"):
            break
    await abandon()
    await asyncio.sleep(0.3)   # let the pump thread notice `stop` and unwind
    check("an abandoned stream releases its slot too",
          llm_broker.snapshot()["inFlight"]["total"] == 0, llm_broker.snapshot())

    errors = []

    def failing_stream(messages, tools):
        yield Delta("first")
        raise RuntimeError("upstream died")

    failing = llm_broker.adapt_stream(failing_stream)
    try:
        async for _ in failing([], None, lane="batch", user="x"):
            pass
    except RuntimeError as exc:
        errors.append(str(exc))
    check("an error inside the generator propagates to the caller", errors == ["upstream died"], errors)
    check("and its slot is released", llm_broker.snapshot()["inFlight"]["total"] == 0)


# ── 7. Sync + async callables, idempotent adaptation ──────────────────────────

async def test_adapt_shapes():
    print("\n[7] sync and async callables, adaptation is idempotent")
    llm_broker.reset_for_tests()

    async def async_complete(messages, tools):
        await asyncio.sleep(0.01)
        return _Msg("async-ok")

    adapted = llm_broker.adapt_complete(async_complete)
    msg = await adapted([], None, lane="batch", user="x")
    check("an async callable is awaited directly", getattr(msg, "content", "") == "async-ok")

    twice = llm_broker.adapt_complete(adapted)
    check("adapting twice returns the same wrapper", twice is adapted)

    sync_msg = await llm_broker.adapt_complete(blocking_complete(delay=0.01))([], None, lane="batch", user="x")
    check("a plain sync fake still works unchanged", getattr(sync_msg, "content", "") == "done")
    check("None passes through", llm_broker.adapt_complete(None) is None)

    snap = llm_broker.snapshot()
    check("snapshot reports capacity and its source", snap["capacity"] == 3
          and snap["capacitySource"] == llm_broker.CAPACITY_ENV, snap)
    check("snapshot ends with nothing in flight", snap["inFlight"]["total"] == 0, snap)


# ── 8. Prefix-cache accounting ────────────────────────────────────────────────

async def test_prefix_cache_accounting():
    print("\n[8] prompt-prefix stability and cache-hit accounting")
    llm_broker.reset_for_tests()

    SYS = [{"role": "system", "content": "you are a governed assistant"}]
    tools_a = [{"type": "function", "function": {"name": "find_customer"}},
               {"type": "function", "function": {"name": "list_orders"}}]

    sig1 = llm_broker.prefix_signature(SYS + [{"role": "user", "content": "hello"}], tools_a)
    sig2 = llm_broker.prefix_signature(SYS + [{"role": "user", "content": "something else entirely"}], tools_a)
    check("the signature ignores the user turn (only the shared head matters)", sig1 == sig2)

    # The whole reason build_tool_specs sorts: a different ORDER is a different
    # prompt from position ~0, so it re-prefills the entire system+tools block.
    sig3 = llm_broker.prefix_signature(SYS, list(reversed(tools_a)))
    check("a reordered tool list IS a different prefix", sig1 != sig3)

    sig4 = llm_broker.prefix_signature(
        [{"role": "system", "content": "a different persona"}], tools_a)
    check("a different system prompt is a different prefix", sig1 != sig4)

    # Turns of one conversation must land on ONE prefix, not one per turn.
    complete = llm_broker.adapt_complete(blocking_complete(delay=0.01))
    convo = list(SYS)
    for i in range(5):
        convo.append({"role": "user", "content": f"turn {i}"})
        await complete(convo, tools_a, lane="interactive", user="alice")
        convo.append({"role": "assistant", "content": f"reply {i}"})
    snap = llm_broker.snapshot()["promptCache"]
    check("5 turns of one conversation = 1 distinct prefix", snap["distinctPrefixes"] == 1, snap)
    check("and they are counted against it", snap["requestsPerPrefix"][:1] == [5], snap)

    # A second user with the same grant shares the head; a narrower grant does not.
    await complete(list(SYS) + [{"role": "user", "content": "hi"}], tools_a, lane="interactive", user="bob")
    check("a second user with the SAME tools reuses the prefix",
          llm_broker.snapshot()["promptCache"]["distinctPrefixes"] == 1)
    await complete(list(SYS) + [{"role": "user", "content": "hi"}],
                   tools_a[:1], lane="interactive", user="carol")
    check("a user with a narrower grant is a new prefix (expected, not a bug)",
          llm_broker.snapshot()["promptCache"]["distinctPrefixes"] == 2)

    # Server-reported reuse.
    class Details:
        cached_tokens = 900

    class Usage:
        prompt_tokens = 1000
        completion_tokens = 50
        prompt_tokens_details = Details()

    llm_broker.record_usage(Usage())
    snap = llm_broker.snapshot()["promptCache"]
    check("cache hit rate is computed from server usage", snap["cacheHitRate"] == 0.9, snap)
    check("prompt tokens accumulate", snap["promptTokens"] == 1000, snap)

    class BareUsage:
        prompt_tokens = 500
        prompt_tokens_details = None

    llm_broker.record_usage(BareUsage())
    snap = llm_broker.snapshot()["promptCache"]
    check("a backend that reports no cached_tokens still counts as prompt tokens",
          snap["promptTokens"] == 1500 and snap["cachedPromptTokens"] == 900, snap)
    llm_broker.record_usage(None)
    llm_broker.record_usage(object())
    check("junk usage is ignored, never raised",
          llm_broker.snapshot()["promptCache"]["promptTokens"] == 1500)


async def main():
    await test_loop_not_blocked()
    await test_capacity_and_lanes()
    await test_interactive_not_starved_by_batch()
    await test_round_robin_fairness()
    await test_queue_bound_and_timeout()
    await test_streaming()
    await test_adapt_shapes()
    await test_prefix_cache_accounting()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
