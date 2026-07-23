"""Policy store -- the single source the control plane writes and the data plane reads.

Stage 1 of the store (design_plan_v2.md Part D step 1): a store interface with a
LOCAL in-memory backend seeded from today's env/config, so Stage 1 behavior is
unchanged. A Cosmos backend drops in later behind the same interface, selected by
`get_store()` when governance Cosmos is provisioned.

Everything on the hot path (edge auth, PDP) reads through `get_store()`.
"""
from __future__ import annotations

import os

from store.base import PolicyStore
from store.local import LocalPolicyStore
from store.models import ConsumerRecord

_store: PolicyStore | None = None


def get_store() -> PolicyStore:
    """Return the configured policy store (cached process-wide).

    Selection: if a Cosmos connection is configured (GOVERNANCE_COSMOS_URL), use it;
    otherwise the local env-seeded backend. Cosmos backend lands in a later step --
    until then only the local backend exists, so this always returns LocalPolicyStore.
    """
    global _store
    if _store is None:
        # Precedence: Cosmos (durable, multi-instance) > file (single-instance
        # persistent) > local (in-memory, env-seeded).
        if os.getenv("GOVERNANCE_COSMOS_URL") or os.getenv("GOVERNANCE_COSMOS_CONNECTION_STRING"):
            from store.cosmos import CosmosPolicyStore
            _store = CosmosPolicyStore()
        elif os.getenv("GOVERNANCE_STORE_FILE"):
            from store.file_store import FilePolicyStore
            _store = FilePolicyStore(os.getenv("GOVERNANCE_STORE_FILE"))
        else:
            _store = LocalPolicyStore.from_env()
    return _store


def reset_store() -> None:
    """Drop the cached store (tests / after a config change)."""
    global _store
    _store = None


__all__ = ["PolicyStore", "LocalPolicyStore", "ConsumerRecord", "get_store", "reset_store"]
