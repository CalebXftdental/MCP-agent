"""Suspicious-activity heuristics over the audit log, for the Monitor panel.

Computed LIVE at view time from whatever's in the recent audit window --
deliberately NOT stamped into the audit record at call time, so retuning a
threshold never touches the real governed-call hot path (gateway/app.py's
_govern never imports this module and is completely unaffected by it).

Four independent signals. A call is flagged "suspicious" if it falls within
the triggering window of ANY signal for its consumer; `reasons` says which.
Everything else is "safe" (green). This is a monitoring aid, not a PDP --
it never denies or alters a call, only how Monitor colors it after the fact.

Signal 1, customer enumeration, is the one this system's own design already
depends on: row-scope is deliberately unrestricted (any employee may look up
any customer; subject/tool-level grants are what's enforced) -- "how many
distinct customers is this principal touching" is the documented guard
against that being abused, and until now nothing actually watched it.

Starter thresholds below are deliberately conservative placeholders -- retune
after watching real traffic; that's the whole point of computing this live
rather than baking a verdict into the audit record forever.
"""
from __future__ import annotations

from collections import defaultdict

ENUMERATION_WINDOW_SEC = 15 * 60
ENUMERATION_DISTINCT_CUSTOMERS = 5   # >5 distinct customer_ids by one consumer in the window

DENIAL_WINDOW_SEC = 15 * 60
DENIAL_COUNT = 3                     # >3 not_granted-style denials by one consumer in the window

BURST_WINDOW_SEC = 5 * 60
BURST_CALL_COUNT = 20                # >20 calls (any status) by one consumer in the window

ERROR_WINDOW_SEC = 15 * 60
ERROR_REPEAT_COUNT = 5               # >5 errors on the SAME tool by one consumer in the window


def _ts(c: dict) -> float:
    return c.get("ts") or 0


def _sliding_flag(items: list[dict], window_sec: float, threshold: int,
                  flagged: dict, reason_fmt) -> None:
    """For each item (already sorted by ts), look back `window_sec` within
    `items` itself; if the count in that window exceeds `threshold`, flag
    every item in the window (not just the one that tipped it over)."""
    for c in items:
        start = _ts(c) - window_sec
        window = [x for x in items if start <= _ts(x) <= _ts(c)]
        if len(window) > threshold:
            reason = reason_fmt(len(window))
            for x in window:
                flagged[id(x)].append(reason)


def _flag_enumeration(group: list[dict], flagged: dict) -> None:
    scoped = [c for c in group if c.get("customer_id")]
    for c in scoped:
        start = _ts(c) - ENUMERATION_WINDOW_SEC
        window = [x for x in scoped if start <= _ts(x) <= _ts(c)]
        distinct = {x["customer_id"] for x in window}
        if len(distinct) > ENUMERATION_DISTINCT_CUSTOMERS:
            reason = f"{len(distinct)} distinct customers in {ENUMERATION_WINDOW_SEC // 60}min"
            for x in window:
                flagged[id(x)].append(reason)


def _flag_denials(group: list[dict], flagged: dict) -> None:
    denials = [c for c in group if c.get("type") == "denied"]
    _sliding_flag(denials, DENIAL_WINDOW_SEC, DENIAL_COUNT, flagged,
                 lambda n: f"{n} access denials in {DENIAL_WINDOW_SEC // 60}min")


def _flag_burst(group: list[dict], flagged: dict) -> None:
    _sliding_flag(group, BURST_WINDOW_SEC, BURST_CALL_COUNT, flagged,
                 lambda n: f"{n} calls in {BURST_WINDOW_SEC // 60}min")


def _flag_errors(group: list[dict], flagged: dict) -> None:
    by_tool: dict[str, list[dict]] = defaultdict(list)
    for c in group:
        if c.get("status") == "error":
            by_tool[c.get("tool") or ""].append(c)
    for tool, errs in by_tool.items():
        _sliding_flag(errs, ERROR_WINDOW_SEC, ERROR_REPEAT_COUNT, flagged,
                     lambda n, tool=tool: f"{n} errors on {tool} in {ERROR_WINDOW_SEC // 60}min")


def classify(calls: list[dict]) -> list[dict]:
    """`calls`: call/denied audit records (any order). Returns NEW dicts (input
    untouched) each with `suspicious` (bool) and `reasons` (list[str]) added."""
    by_consumer: dict[str, list[dict]] = defaultdict(list)
    for c in calls:
        by_consumer[c.get("consumer") or "unknown"].append(c)

    flagged: dict[int, list[str]] = defaultdict(list)
    for group in by_consumer.values():
        group.sort(key=_ts)
        _flag_enumeration(group, flagged)
        _flag_denials(group, flagged)
        _flag_burst(group, flagged)
        _flag_errors(group, flagged)

    out = []
    for c in calls:
        reasons = sorted(set(flagged.get(id(c), [])))
        out.append({**c, "suspicious": bool(reasons), "reasons": reasons})
    return out
