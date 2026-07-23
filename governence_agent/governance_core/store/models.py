"""Store record types.

ConsumerRecord is the resolved view of a principal (agent or user) that the hot
path needs: identity, credential hash, status, rate limit, IP allowlist, and the
data-classification levels it is entitled to.

`allowed_levels` is the legacy/fallback level set used only when a principal has no
categories (the env-seeded Stage 1 consumers). A principal's real grant is resolved
live from its `categories` (each = one data domain/backend) + `overrides`
(design_plan_v2.md B.3, policy/resolve.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConsumerRecord:
    consumer_id: str
    name: str                       # consumer identity used in audit/ctx (e.g. "chatbot")
    key_hash: str                   # SHA-256 hash of the MCP API key (never the key); "" for login-only users
    status: str = "active"          # active | pending | disabled
    login_password_hash: str | None = None   # scrypt hash of the dashboard login password (users only)
    full_name: str = ""             # display name (self-service users); "" falls back to `name` in the UI
    department: str = ""            # signup_departments id this user picked, if self-service (UI-only; the real grant is `categories`)
    rate_limit_per_hour: int | None = None
    ip_allowlist: list[str] = field(default_factory=list)
    allowed_levels: frozenset[str] = frozenset()   # legacy/fallback when no categories
    categories: list[str] = field(default_factory=list)   # data-domain categories (union grant)
    overrides: dict = field(default_factory=dict)  # per-backend grant/deny overrides (see resolve.py)
    role: str = "user"              # user | admin
    type: str = "agent"             # agent | user
    key_created_at: float | None = None   # epoch when an API key was first issued for this principal
    key_rotated_at: float | None = None   # epoch when the API key was last (re)issued -> credential age

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class ChatMessage:
    role: str                       # "user" | "assistant"
    content: str
    ts: float
    tools_used: list[str] = field(default_factory=list)   # informational only (assistant turns)


@dataclass(frozen=True)
class ChatSession:
    """One chat conversation (one browser tab's worth), scoped to the consumer that
    owns it. `messages` holds only user/assistant turns (not the raw tool-call/
    tool-response wire messages) -- enough to replay as LLM history and to read back
    as a transcript, without re-feeding old tool mechanics into a new turn.

    Idle-expiry: a session past GOVERNANCE_CHAT_IDLE_SEC since `last_active_at` is
    closed (status="closed", `summary` filled in) by the background sweep in
    chat_log.py; a request that reaches an already-idle session forks a new one
    rather than reusing stale context (see chat_log.resolve_session_id)."""
    session_id: str
    consumer_id: str
    status: str = "open"            # open | closed
    created_at: float = 0.0
    last_active_at: float = 0.0
    closed_at: float | None = None
    summary: str | None = None
    messages: list[ChatMessage] = field(default_factory=list)
