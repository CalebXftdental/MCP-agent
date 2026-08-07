"""Local, in-memory policy store -- the Stage-1 backend (no Cosmos yet).

Seeds from exactly what Stage 1 used, so behavior is unchanged:
  - principals + keys from GOVERNANCE_KEY_<NAME> (+ GOVERNANCE_SHARED_SECRET legacy)
  - per-consumer rate limit from GOVERNANCE_RATE_LIMIT_<NAME> (else default)
  - entitlement levels from policy/entitlements.py

Keys are read in plaintext from env for dev convenience and immediately hashed into
ConsumerRecord.key_hash; the plaintext is not retained. When the Cosmos backend
lands, only this seeding changes -- the interface and the hot path do not.
"""
from __future__ import annotations

import os
import re

from auth.passwords import hash_password
from policy import entitlements
from policy.manifest import ALL_LEVELS
from store.base import PolicyStore
from store.keys import hash_api_key
from store.models import ChatSession, ConsumerRecord

# Dashboard login users have no categories, so they resolve via the "legacy" fallback
# in policy/resolve.py (all_tools=True + this explicit level set) -- NOT via
# entitlements.levels_for(), which is keyed by agent consumer name, not role. Leaving
# this unset (the ConsumerRecord field default is an EMPTY frozenset) silently drops
# every classified field for every tool call, even though the tool call itself is
# allowed -- looks like "found the record but every field is missing", not a denial.
_ADMIN_LEVELS = ALL_LEVELS

_DEFAULT_RATE_LIMIT_PER_HOUR = int(os.getenv("GOVERNANCE_DEFAULT_RATE_LIMIT_PER_HOUR") or "100")


class LocalPolicyStore(PolicyStore):
    def __init__(self, records: list[ConsumerRecord]):
        self._records = records
        # In-memory only, like scope_store.py -- fine for this backend's existing
        # "resets on restart, single instance" contract; File/Cosmos persist chat
        # history durably. Keyed by (session_id, consumer_id), not session_id
        # alone -- see file_store.py's identical note; two different consumers
        # sharing a session_id string must never let one overwrite the other.
        self._chat_sessions: dict[tuple[str, str], ChatSession] = {}

    def consumers(self) -> list[ConsumerRecord]:
        return list(self._records)

    def chat_sessions(self) -> list[ChatSession]:
        return list(self._chat_sessions.values())

    def get_chat_session_for(self, session_id: str, consumer_id: str) -> ChatSession | None:
        return self._chat_sessions.get((session_id, consumer_id))

    def upsert_chat_session(self, session: ChatSession) -> None:
        self._chat_sessions[(session.session_id, session.consumer_id)] = session

    @classmethod
    def from_env(cls) -> "LocalPolicyStore":
        return cls(seed_consumers_from_env())


def seed_consumers_from_env() -> list[ConsumerRecord]:
    """Build the initial consumer set from env (shared by LocalPolicyStore and the
    first-run seed of FilePolicyStore)."""
    records: list[ConsumerRecord] = []
    seen_hashes: set[str] = set()

    for env_name, value in os.environ.items():
        match = re.match(r"^GOVERNANCE_KEY_(.+)$", env_name)
        if not match or not value.strip():
            continue
        suffix = match.group(1)
        name = suffix.lower()
        limit = int(os.getenv(f"GOVERNANCE_RATE_LIMIT_{suffix}") or _DEFAULT_RATE_LIMIT_PER_HOUR)
        # GOVERNANCE_CATEGORY_<NAME>=orders,accounts -> categories list
        cats_raw = (os.getenv(f"GOVERNANCE_CATEGORY_{suffix}") or "").strip()
        categories = [c.strip() for c in cats_raw.split(",") if c.strip()]
        key_hash = hash_api_key(value.strip())
        seen_hashes.add(key_hash)
        records.append(ConsumerRecord(
            consumer_id=name,
            name=name,
            key_hash=key_hash,
            rate_limit_per_hour=limit,
            # Legacy fallback levels apply only when no categories are assigned; a
            # categorized principal resolves its levels from the category templates.
            allowed_levels=entitlements.levels_for(name),
            categories=categories,
            type="agent",
        ))

    # Dashboard login users (no MCP key). Dev seeding convention:
    #   GOVERNANCE_ADMIN_USER / GOVERNANCE_ADMIN_PASSWORD  -> role admin
    #   GOVERNANCE_VIEWER_USER / GOVERNANCE_VIEWER_PASSWORD -> role user
    # In prod these live in Cosmos with the scrypt hash stored directly.
    for role, user_env, pw_env in (
        ("admin", "GOVERNANCE_ADMIN_USER", "GOVERNANCE_ADMIN_PASSWORD"),
        ("user", "GOVERNANCE_VIEWER_USER", "GOVERNANCE_VIEWER_PASSWORD"),
    ):
        uname = (os.getenv(user_env) or "").strip()
        pw = os.getenv(pw_env) or ""
        if uname and pw:
            # Login-only dashboard users (no MCP key, no categories). Tool access is
            # still all_tools=True via the resolve() legacy fallback; the level set
            # below is what actually controls which fields they see -- admin gets
            # everything, the plain "user" (viewer) role gets the same least-privilege
            # default unknown agent consumers get.
            records.append(ConsumerRecord(
                consumer_id=f"login:{uname}",
                name=uname,
                key_hash="",
                role=role,
                type="user",
                login_password_hash=hash_password(pw),
                allowed_levels=_ADMIN_LEVELS if role == "admin" else entitlements.DEFAULT_LEVELS,
            ))

    legacy = (os.getenv("GOVERNANCE_SHARED_SECRET") or "").strip()
    if legacy and hash_api_key(legacy) not in seen_hashes:
        records.append(ConsumerRecord(
            consumer_id="legacy",
            name="legacy",
            key_hash=hash_api_key(legacy),
            rate_limit_per_hour=_DEFAULT_RATE_LIMIT_PER_HOUR,
            allowed_levels=entitlements.levels_for("legacy"),
            type="agent",
        ))

    return records
