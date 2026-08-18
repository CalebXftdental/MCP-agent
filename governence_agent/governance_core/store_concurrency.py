"""Shared safety rails for the file-backed stores (concurrency_and_scale.md Stage B).

Every store in this codebase is the same shape: a module-global dict cached in
memory, rewritten to one JSON file in full on every mutation. That was safe for
exactly one reason -- nothing in the process ever ran in parallel, because the
single blocking LLM call on the event loop serialized everything (see
gateway/llm_broker.py's docstring for that story). Stage A removed that
accidental serialization on purpose. These three primitives are what make the
stores safe now that real interleaving is possible:

`atomic_write_text`
    The stores all wrote to a FIXED temp name (`<file>.tmp`) before `replace()`.
    Two writers therefore interleave inside the same temp file and one of them
    renames a half-written document into place. A unique name per write removes
    the shared resource entirely.

`StoreGuard.lock`
    A plain `threading.RLock`, deliberately NOT an `asyncio.Lock`. Every store
    mutator here is a synchronous function, and making them async would mean
    changing ~40 call sites across the interpreter, workflow_api and the five
    hardcoded workflows. A sync critical section is already atomic with respect
    to the event loop; the RLock additionally makes it correct if a mutation is
    ever reached from a worker thread (which Stage A's `asyncio.to_thread` now
    makes possible in principle), and costs nothing uncontended.

`StoreGuard.deferred`
    One `_save()` per unit of work instead of one per field change. A workflow
    run writes a step record at least twice per node (running -> completed) and
    each write re-serializes EVERY run in the file, so the cost of a run grows
    with the history of every run before it. Deferring is what makes a loop node
    (loopnodedesign.md) survivable at all.

The rule these encode, and the one thing a future store must not break: a
mutator reads the CURRENT stored record inside the guard and applies its change
to that -- never to a copy the caller has been holding across an `await`. See
gateway/workflows.py's mutators for the reference implementation.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


# One lock per destination file. Two writers to the SAME store file have nothing
# to gain from overlapping -- last-write-wins either way -- and on Windows they
# actively hurt: `os.replace` onto a path another thread is simultaneously
# replacing fails with PermissionError (ERROR_ACCESS_DENIED), which surfaced as
# real dropped writes the first time this module was tested under threads.
# Bounded in size by the number of store files, which is a fixed small set.
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()

# Even holding our own lock, an external holder (Windows Defender, the search
# indexer, a dev editor watching the file) can keep the destination open for a
# few milliseconds. Retry briefly before giving up rather than losing the write.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SEC = 0.02


def _path_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
        return lock


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8", newline: str | None = None) -> None:
    """Write `text` to `path` atomically, via a temp file UNIQUE to this write.

    `Path.replace` is atomic on both POSIX and Windows, so a reader sees either
    the whole old file or the whole new one -- but only if no other writer is
    using the same temp path, which is why the uuid is here rather than the
    `<file>.tmp` every store used to share. Writes to one destination are also
    serialized (see `_PATH_LOCKS`) and the rename retried, because concurrent
    replaces onto the same path fail outright on Windows.

    The temp file is removed if anything fails, so a failed write leaves the
    original intact and no litter behind.

    `newline` is passed through for the two knowledge stores, which write with
    `newline=""` to keep their content byte-stable across platforms.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        tmp.write_text(text, encoding=encoding, newline=newline)
        with _path_lock(path):
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    tmp.replace(path)
                    return
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(_REPLACE_BACKOFF_SEC * (attempt + 1))
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class StoreGuard:
    """One store's mutation lock plus its save-coalescing state.

    Usage in a store module:

        _GUARD = StoreGuard("workflow-runs")

        def _save() -> None:
            if _GUARD.defer_save():        # inside a deferred block -> mark dirty
                return
            ...actually write...

        def mutate(...):
            with _GUARD.lock:
                current = _RECORDS.get(key)      # re-read, never trust the caller
                ...apply change to `current`...
                _save()
    """

    __slots__ = ("name", "lock", "_depth", "_dirty", "_saves", "_deferred_saves")

    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.RLock()
        # Depth is PER TASK (and per thread), not global. With one global counter,
        # two runs whose deferred blocks overlap would nest -- run A's steps would
        # stay unwritten until run B's block closed, so a crash could lose work
        # that finished long before. A ContextVar keeps each caller's deferral
        # window bounded by its own block, which is the only window it controls.
        self._depth: ContextVar[int] = ContextVar(f"store_defer_{name}", default=0)
        self._dirty = False        # store-wide: the FILE owes a write
        self._saves = 0            # writes that actually hit the disk
        self._deferred_saves = 0   # writes coalesced away by a deferred block

    def defer_save(self) -> bool:
        """True if the caller should SKIP writing because a deferred block is
        open. Records that a write is owed so the block flushes it on exit."""
        with self.lock:
            if self._depth.get() > 0:
                self._dirty = True
                self._deferred_saves += 1
                return True
            self._saves += 1
            self._dirty = False
            return False

    @contextmanager
    def deferred(self, save):
        """Coalesce every `_save()` inside this block into one write on exit.

        Re-entrant: nested blocks in the same task flush once, at the outermost
        exit. The flush runs even when the block exits by exception -- a run that
        failed halfway must still leave its completed steps on disk, since the
        whole point of the record is to say how far it got.

        A flush writes the WHOLE store, so another task's still-open block may be
        flushed early by this one. That is harmless (the file is always written
        in full from current memory) and strictly safer than deferring longer.
        """
        token = self._depth.set(self._depth.get() + 1)
        try:
            yield
        finally:
            self._depth.reset(token)
            with self.lock:
                should_flush = self._depth.get() == 0 and self._dirty
            # Called with the lock released and this task's depth already back to
            # 0, so the store's own `_save()` takes the normal path (and does its
            # own accounting) rather than seeing an open block and re-deferring.
            if should_flush:
                save()

    def stats(self) -> dict:
        return {
            "store": self.name,
            "saves": self._saves,
            "deferredSaves": self._deferred_saves,
            "deferDepth": self._depth.get(),
        }

    def reset_stats(self) -> None:
        self._saves = 0
        self._deferred_saves = 0


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# The subset a late writer must never be able to move a record OUT of. Both are
# verdicts someone acted on: a human cancelled the run, or it genuinely failed.
# A stale in-flight pass finishing afterwards and reporting "completed" erases
# that verdict, which is exactly the bug in concurrency_and_scale.md §3.2.
#
# "completed" is deliberately NOT sticky. It is not a verdict anyone acts on, and
# it is written speculatively in places (workflows.resume_run marks a run
# completed before its continuation runs), so freezing it would break legitimate
# flows to defend against a transition that is harmless anyway -- a later
# "failed" after a completion is informative, not a regression.
STICKY_STATUSES = frozenset({"cancelled", "failed"})


def is_terminal(status: str | None) -> bool:
    return (status or "") in TERMINAL_STATUSES


def is_sticky(status: str | None) -> bool:
    return (status or "") in STICKY_STATUSES


def guard_enabled() -> bool:
    """Escape hatch for diagnosing a suspected deadlock in production without a
    redeploy: GOVERNANCE_STORE_GUARD=off reverts to the pre-Stage-B behaviour of
    writing on every mutation. Never set this while anything runs concurrently."""
    return os.getenv("GOVERNANCE_STORE_GUARD", "on").strip().lower() not in ("0", "false", "no", "off")
