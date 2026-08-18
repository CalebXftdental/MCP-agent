"""Smoke test for the Stage B store safety rails (concurrency_and_scale.md §3.2-§3.3):
governance_core/store_concurrency.py and gateway/workflows.py's rewritten mutators.

No server, no network -- these are properties of the store layer itself.

The bug this exists to prevent, in one sentence: every mutator used to write back
the WorkflowRun object its caller was holding, and `_interpret` holds one such
object across every governed tool call and every LLM call in a run -- so anything
that changed the record in the meantime (a cancel, another step) was silently
overwritten when the caller finally wrote. Test 1 is that exact scenario and it
fails against the old code.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
TMP = ROOT / "_smoke" / ".tmp" / "store-concurrency"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "governance_core"))
os.environ["GOVERNANCE_STATE_DIR"] = str(TMP / "state")

import store_concurrency  # noqa: E402
import workflows  # noqa: E402
from workflow_models import WorkflowStep  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}", detail if detail is not None else "")


def fresh_run(template="t_demo"):
    workflows.reload_for_tests()
    return workflows.new_run(template, "alice", {})


def stored(run_id):
    return workflows.get_run_record(run_id)


def on_disk() -> dict:
    path = Path(os.environ["GOVERNANCE_STATE_DIR"]) / "workflow-runs.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"runs": []}


# ── 1. The cancel bug ─────────────────────────────────────────────────────────

def test_cancel_sticks():
    print("\n[1] a cancel survives a late write from an in-flight run")
    run = fresh_run()
    stale = run                       # what _interpret holds across its awaits

    workflows.cancel_run(run.run_id, actor="admin", reason="user cancelled")
    check("cancel_run marked it cancelled", stored(run.run_id).status == "cancelled")

    # The in-flight pass finishes and writes back its stale copy. Pre-Stage-B this
    # silently resurrected the run as "completed".
    workflows.update_run(stale, status="completed", artifact_ids=["af_1"])
    after = stored(run.run_id)
    check("the run is STILL cancelled", after.status == "cancelled", after.status)
    check("but the late write's other fields did land", after.artifact_ids == ["af_1"], after.artifact_ids)

    # A failure arriving after a cancel must not relabel it either.
    workflows.fail_step(stale, "nope", "boom")
    check("a late failure does not overwrite the cancel", stored(run.run_id).status == "cancelled")

    run2 = fresh_run()
    workflows.update_run(run2, status="completed")
    check("a normal completion is unaffected", stored(run2.run_id).status == "completed")

    # A failure is sticky too -- a late writer must not be able to report success
    # over a run that genuinely failed.
    run3 = fresh_run()
    workflows.update_run(run3, status="failed", error="boom")
    workflows.update_run(run3, status="completed")
    check("a failure cannot be overwritten with success", stored(run3.run_id).status == "failed")

    # But `completed` is NOT sticky, because it is written speculatively (see
    # store_concurrency.STICKY_STATUSES) and a run that pauses again after being
    # marked complete must still be able to say so.
    run4 = fresh_run()
    workflows.update_run(run4, status="completed")
    workflows.update_run(run4, status="approval_required")
    check("a completed run can still move to approval_required",
          stored(run4.run_id).status == "approval_required", stored(run4.run_id).status)


# ── 2. Steps written from stale snapshots ─────────────────────────────────────

def test_steps_not_lost():
    print("\n[2] concurrent step writes do not drop each other")
    run = fresh_run()
    stale = run                       # snapshot taken before either write

    workflows.add_step(run, WorkflowStep(step_id="a", type="tool_call", status="running"))
    # A second writer still holding the pre-`a` snapshot appends its own step.
    workflows.add_step(stale, WorkflowStep(step_id="b", type="tool_call", status="running"))
    ids = [s.step_id for s in stored(run.run_id).steps]
    check("both steps are present", ids == ["a", "b"], ids)

    # Completing a step from a stale snapshot must not delete steps added since.
    workflows.complete_step(stale, "a", {"ok": True})
    after = stored(run.run_id)
    ids = [s.step_id for s in after.steps]
    check("completing from a stale copy keeps every step", ids == ["a", "b"], ids)
    check("the completion actually applied",
          next(s for s in after.steps if s.step_id == "a").status == "completed")
    check("the other step is untouched",
          next(s for s in after.steps if s.step_id == "b").status == "running")


# ── 3. Deferred saves ─────────────────────────────────────────────────────────

def test_deferred_save():
    print("\n[3] deferred_save coalesces a unit of work into one disk write")
    run = fresh_run()
    workflows.store_stats()  # touch
    before = workflows.store_stats()["saves"]

    with workflows.deferred_save():
        for i in range(10):
            workflows.add_step(run, WorkflowStep(step_id=f"s{i}", type="tool_call", status="running"))
            workflows.complete_step(run, f"s{i}", {"i": i})
    after = workflows.store_stats()["saves"]

    check("20 mutations cost exactly 1 write", after - before == 1, f"{after - before} writes")
    check("all 10 steps are in memory", len(stored(run.run_id).steps) == 10)
    disk = next(r for r in on_disk()["runs"] if r["run_id"] == run.run_id)
    check("and all 10 reached the disk", len(disk["steps"]) == 10, len(disk["steps"]))
    check("the deferral was actually counted", workflows.store_stats()["deferredSaves"] >= 19)

    # Nested blocks flush once, at the outermost exit.
    before = workflows.store_stats()["saves"]
    with workflows.deferred_save():
        workflows.add_step(run, WorkflowStep(step_id="n1", type="tool_call", status="running"))
        with workflows.deferred_save():
            workflows.add_step(run, WorkflowStep(step_id="n2", type="tool_call", status="running"))
        check("the inner block did NOT flush", workflows.store_stats()["saves"] == before)
    check("the outer block flushed once", workflows.store_stats()["saves"] - before == 1)

    # A failure partway through must still leave what happened on disk.
    before = workflows.store_stats()["saves"]
    try:
        with workflows.deferred_save():
            workflows.add_step(run, WorkflowStep(step_id="boom", type="tool_call", status="running"))
            raise RuntimeError("iteration 7 blew up")
    except RuntimeError:
        pass
    check("an exception still flushes the work done so far",
          workflows.store_stats()["saves"] - before == 1)
    disk = next(r for r in on_disk()["runs"] if r["run_id"] == run.run_id)
    check("the pre-failure step is durable", any(s["step_id"] == "boom" for s in disk["steps"]))


# ── 4. Atomic writes ──────────────────────────────────────────────────────────

def test_atomic_write():
    print("\n[4] atomic_write_text: unique temp per write, no litter, no partials")
    d = TMP / "atomic"
    d.mkdir(parents=True, exist_ok=True)
    target = d / "data.json"

    store_concurrency.atomic_write_text(target, '{"v":1}')
    check("the file was written", target.read_text(encoding="utf-8") == '{"v":1}')

    # Many threads writing at once: with a shared `<file>.tmp` this is where a
    # half-written document gets renamed into place.
    errors: list[BaseException] = []

    def writer(n):
        try:
            for _ in range(20):
                store_concurrency.atomic_write_text(target, json.dumps({"writer": n, "pad": "x" * 5000}))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("no writer errored", not errors, errors)
    parsed = None
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        check("the file is still valid JSON (never a torn write)", False, exc)
    if parsed is not None:
        check("the file is still valid JSON (never a torn write)", "writer" in parsed, parsed)
    leftovers = [p.name for p in d.iterdir() if ".tmp" in p.name]
    check("no temp files left behind", not leftovers, leftovers)

    # A failed rename must leave the original intact and clean up its temp file.
    # Injected at Path.replace, which is the step that can genuinely fail in
    # production (a held destination, a full disk) and the only one that runs
    # AFTER the temp file exists -- so it is the case where cleanup matters.
    before = target.read_text(encoding="utf-8")
    real_replace = Path.replace
    Path.replace = lambda self, other: (_ for _ in ()).throw(OSError("no space left on device"))
    try:
        raised = None
        try:
            store_concurrency.atomic_write_text(target, '{"v":"should not land"}')
        except OSError as exc:
            raised = exc
        check("the failure is raised, not swallowed", raised is not None)
        check("the original survives a failed write", target.read_text(encoding="utf-8") == before)
        leftovers = [p.name for p in d.iterdir() if ".tmp" in p.name]
        check("a failed write leaves no temp file", not leftovers, leftovers)
    finally:
        Path.replace = real_replace


# ── 5. Threaded hammer on the run store ───────────────────────────────────────

def test_threaded_mutations():
    print("\n[5] the run store holds up under real thread parallelism")
    run = fresh_run()
    errors: list[BaseException] = []

    def worker(n):
        try:
            for i in range(15):
                workflows.add_step(run, WorkflowStep(step_id=f"w{n}_{i}", type="tool_call", status="running"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no worker errored", not errors, errors)
    ids = {s.step_id for s in stored(run.run_id).steps}
    expected = {f"w{n}_{i}" for n in range(6) for i in range(15)}
    check("every one of the 90 steps survived", ids == expected,
          f"{len(ids)} of {len(expected)}; missing {sorted(expected - ids)[:5]}")
    disk = next(r for r in on_disk()["runs"] if r["run_id"] == run.run_id)
    check("the persisted file agrees", len(disk["steps"]) == 90, len(disk["steps"]))


def main():
    test_cancel_sticks()
    test_steps_not_lost()
    test_deferred_save()
    test_atomic_write()
    test_threaded_mutations()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
