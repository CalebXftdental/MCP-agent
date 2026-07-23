"""The PDP entrypoint: decide().

Pure function. Given the requester, the (canonical) tool, its args, and the
resolved session scope, return a Verdict:

  - deny  -> the gateway returns a typed refusal, never calls the backend
  - allow -> the gateway calls the backend, then applies verdict.redaction_plan

Deny reasons in Stage 1:
  - "unknown_tool"           the tool is not in the manifest
  - "missing_customer_scope" an account-scoped tool with no customer in scope

Allow always carries a redaction_plan (possibly empty): for every classified
field the requester is NOT entitled to see, a RedactField with the level's
default action. This is where per-requester censorship is computed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from policy import entitlements, manifest
from policy.redaction import RedactField
from policy.resolve import EffectiveGrant


@dataclass(frozen=True)
class Scope:
    customer_id: str | None = None

    @property
    def verified(self) -> bool:
        return bool(self.customer_id)


@dataclass
class Verdict:
    allowed: bool
    intent: str = "governed_call"
    reason: str = ""
    message: str = ""
    missing_fields: list[str] = field(default_factory=list)
    redaction_plan: list[RedactField] = field(default_factory=list)
    # Risk metadata copied from the tool's ToolPolicy (manifest.py §7.3) so callers
    # (the broad-export/approval checks in gateway/app.py) don't have to re-look up
    # the policy themselves -- decide() is the one place that already has it.
    risk: str = ""
    approval_required: bool = False
    max_rows_without_approval: int | None = None


def decide(
    consumer: str | None,
    canonical_tool: str,
    args: dict,
    scope: Scope,
    levels: frozenset[str] | None = None,
    grant: EffectiveGrant | None = None,
) -> Verdict:
    """Decide allow/deny + redaction plan.

    `grant` is the principal's resolved EffectiveGrant (from policy.resolve, via the
    request's ConsumerRecord + its categories). When present it drives BOTH:
      - tool-level authorization (deny-by-default: an ungranted tool/backend is denied), and
      - the data-classification levels used for redaction.
    `levels` is the older, tool-authz-free path (still used by unit tests / storeless
    callers); if neither is given, fall back to the static entitlements table."""
    policy = manifest.get(canonical_tool)
    if policy is None:
        return Verdict(
            allowed=False,
            reason="unknown_tool",
            message="This tool is not available.",
        )

    # Tool-level authorization (deny-by-default) -- "which MCP, which part of it".
    if grant is not None and not grant.allows_tool(policy.backend, canonical_tool):
        return Verdict(
            allowed=False,
            intent=policy.intent,
            reason="not_granted",
            message="Your access does not include this tool.",
        )

    if policy.account_scoped and not scope.verified:
        return Verdict(
            allowed=False,
            intent=policy.intent,
            reason="missing_customer_scope",
            message="A verified customer ID is required before I can look up account data.",
            missing_fields=["customerId"],
        )

    if grant is not None:
        allowed_levels = grant.levels_for(policy.backend)
    elif levels is not None:
        allowed_levels = levels
    else:
        allowed_levels = entitlements.levels_for(consumer)
    plan = [
        RedactField(field=field_name, action=manifest.default_action(level))
        for field_name, level in policy.fields.items()
        if level not in allowed_levels
    ]
    return Verdict(
        allowed=True, intent=policy.intent, redaction_plan=plan,
        risk=policy.risk, approval_required=policy.approval_required,
        max_rows_without_approval=policy.max_rows_without_approval,
    )
