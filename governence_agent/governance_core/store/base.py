"""PolicyStore interface -- what the hot path depends on.

Backends (LocalPolicyStore now, CosmosPolicyStore later) implement `consumers()`;
the shared helpers here (`get_by_api_key`, `by_name`) work against whatever that
returns, so the auth path is identical regardless of backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import departments as _departments_seed
from policy import categories as _categories_seed
from store.keys import hash_api_key
from store.models import ChatSession, ConsumerRecord


class PolicyStore(ABC):
    @abstractmethod
    def consumers(self) -> list[ConsumerRecord]:
        """All registered principals. Backends may cache/refresh internally."""

    @abstractmethod
    def chat_sessions(self) -> list[ChatSession]:
        """All chat sessions, any consumer, any status. Backends may cache/refresh
        internally (same contract as consumers())."""

    def get_category(self, category_id: str | None):
        """Resolve a category template. Default backend serves the code seed
        (policy/categories.py); the Cosmos backend overrides this to read the
        admin-editable `categories` container."""
        return _categories_seed.get_category(category_id)

    def categories(self) -> list:
        return list(_categories_seed.CATEGORIES.values())

    def get_department(self, department_id: str | None):
        """Resolve a department (org-unit grouping of categories, departments.py).
        Default backend serves the code seed; File/Cosmos override to read an
        admin-editable copy -- same contract as get_category above, and this is
        what policy/resolve.py calls LIVE on every request, so an edit here (or via
        the admin CRUD API once persisted) takes effect for every member of that
        department immediately."""
        return _departments_seed.get_department(department_id)

    def departments(self) -> list:
        return list(_departments_seed.DEPARTMENTS.values())

    def upsert_department(self, department) -> None:
        raise NotImplementedError("this policy store is read-only")

    def delete_department(self, department_id: str) -> None:
        raise NotImplementedError("this policy store is read-only")

    def get_by_api_key(self, api_key: str) -> ConsumerRecord | None:
        """Resolve an ACTIVE principal by presented API key (hashed lookup)."""
        if not api_key:
            return None
        wanted = hash_api_key(api_key)
        for record in self.consumers():
            # key_hash is a fixed-length hex digest; dict-style equality is fine here
            # (no timing oracle on a hash comparison of equal-length digests).
            # Skip login-only users (empty key_hash) -- they have no MCP credential.
            if record.active and record.key_hash and record.key_hash == wanted:
                return record
        return None

    def by_name(self, name: str) -> ConsumerRecord | None:
        for record in self.consumers():
            if record.name == name:
                return record
        return None

    def get_by_username(self, username: str) -> ConsumerRecord | None:
        """Resolve a principal with a dashboard login (for the login route).

        Allows `pending` accounts to sign in (they can view their pending status but
        have no grants and no key); `disabled` accounts cannot sign in."""
        if not username:
            return None
        for record in self.consumers():
            if (record.status in ("active", "pending") and record.login_password_hash
                    and record.name == username):
                return record
        return None

    def get_consumer(self, consumer_id: str) -> ConsumerRecord | None:
        for record in self.consumers():
            if record.consumer_id == consumer_id:
                return record
        return None

    def get_chat_session(self, session_id: str) -> ChatSession | None:
        for session in self.chat_sessions():
            if session.session_id == session_id:
                return session
        return None

    def get_chat_session_for(self, session_id: str, consumer_id: str) -> ChatSession | None:
        """Same lookup, but the caller already knows the owner -- lets a backend
        resolve it safely even if a DIFFERENT consumer happens to share this
        exact session_id (e.g. a client-side id collision): Cosmos overrides
        this with a partition-scoped point read; Local/File override it with a
        (session_id, consumer_id)-keyed dict lookup. This default (an
        ownership-filtered scan of chat_sessions()) is only a correctness
        fallback for a hypothetical backend that doesn't override it -- it is
        NOT what Local/File actually use."""
        session = self.get_chat_session(session_id)
        return session if session is not None and session.consumer_id == consumer_id else None

    def list_chat_sessions_for(self, consumer_id: str) -> list[ChatSession]:
        sessions = [s for s in self.chat_sessions() if s.consumer_id == consumer_id]
        sessions.sort(key=lambda s: s.last_active_at, reverse=True)
        return sessions

    def list_open_chat_sessions(self) -> list[ChatSession]:
        return [s for s in self.chat_sessions() if s.status == "open"]

    def upsert_chat_session(self, session: ChatSession) -> None:
        raise NotImplementedError("this policy store does not support chat history")

    # ── Write surface (control plane). Read-only backends raise. ──────────────
    @property
    def writable(self) -> bool:
        return False

    def upsert_consumer(self, record: ConsumerRecord) -> None:
        raise NotImplementedError("this policy store is read-only")

    def delete_consumer(self, consumer_id: str) -> None:
        raise NotImplementedError("this policy store is read-only")

    def upsert_category(self, category) -> None:
        raise NotImplementedError("this policy store is read-only")

    def delete_category(self, category_id: str) -> None:
        raise NotImplementedError("this policy store is read-only")

    def get_whitelist(self) -> list[str]:
        return []

    def set_whitelist(self, cidrs: list[str]) -> None:
        raise NotImplementedError("this policy store is read-only")

    # ── Break-glass controls (incident containment) ───────────────────────────
    def get_controls(self) -> dict:
        """Global runtime controls enforced on the hot path. `paused_agents`
        blocks all agent (API-key) tool calls; `paused_backends` blocks calls to
        the named backends. Humans/admins are unaffected by `paused_agents`."""
        return {"paused_agents": False, "paused_backends": []}

    def set_controls(self, controls: dict) -> None:
        raise NotImplementedError("this policy store is read-only")

    # ── Account / access requests (signup + widen-access queue) ───────────────
    def list_access_requests(self) -> list[dict]:
        return []

    def add_access_request(self, request: dict) -> None:
        raise NotImplementedError("this policy store is read-only")

    def update_access_request(self, request_id: str, fields: dict) -> dict | None:
        raise NotImplementedError("this policy store is read-only")
