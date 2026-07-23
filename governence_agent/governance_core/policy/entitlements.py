"""Per-consumer entitlements -- which data classifications a requester may see.

This is the "censorship per requester" knob: two consumers calling the exact
same tool on the exact same account get different fields back, decided here.

The consumer name is what edge.py derived from the GOVERNANCE_KEY_<NAME> that
authenticated the call (lowercased). Unknown/unlisted consumers fall back to
DEFAULT_LEVELS (least privilege: business data only, no PII, no financials).

Stage 1 keeps this as static in-code config. It is intentionally the same shape
a future Policy Administration Point (dashboard / config store) would write to,
so it can be lifted out without touching the PDP.
"""
from __future__ import annotations

from policy.manifest import INTERNAL, PCI, PII, PUBLIC, SENSITIVE

# Least privilege: only non-sensitive business data unless explicitly granted.
DEFAULT_LEVELS: frozenset[str] = frozenset({PUBLIC, INTERNAL})

# consumer name -> set of classification levels it is entitled to see.
CONSUMER_ENTITLEMENTS: dict[str, frozenset[str]] = {
    # Customer-facing chatbot: full view of the in-scope customer's own data.
    "chatbot": frozenset({PUBLIC, INTERNAL, PII, SENSITIVE}),
    # Outbound email bot: needs contact PII, but not financial figures.
    "email_bot": frozenset({PUBLIC, INTERNAL, PII}),
    # Analytics/reporting agent: financials + business data, never row-level PII.
    "analytics": frozenset({PUBLIC, INTERNAL, SENSITIVE}),
    # Finance team/agent: needs AP/AR/GL financial figures; no customer PII.
    "finance": frozenset({PUBLIC, INTERNAL, SENSITIVE}),
    # Migration fallback key.
    "legacy": DEFAULT_LEVELS,
}


def levels_for(consumer: str | None) -> frozenset[str]:
    return CONSUMER_ENTITLEMENTS.get((consumer or "").lower(), DEFAULT_LEVELS)


def is_known(consumer: str | None) -> bool:
    return (consumer or "").lower() in CONSUMER_ENTITLEMENTS
