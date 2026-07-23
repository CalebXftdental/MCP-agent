from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ScopeContext:
    customer_id: str | None = None      # acctCd — unique, customer-provided
    b_account_id: str | None = None     # numeric bAccountId if resolved
    company_ids: list = field(default_factory=lambda: [2, 11])
    topic: str = "ORDER"                # per-message, set by topic_classifier
    consecutive_sql_failures: int = 0
    last_pagination: dict = field(default_factory=dict)


# Async-safe context variable inherited by child tasks at creation time.
# Set once per query_agent call; read by the tool gate and inject_scope.
current_scope: ContextVar = ContextVar("current_scope", default=None)
