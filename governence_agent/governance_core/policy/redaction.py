"""Apply a redaction plan to a structured (already-parsed) backend result.

A plan is a list of RedactField(field_name, action). The result is walked
recursively; any dict key whose name matches a planned field is redacted in
place with the planned action:

  - "drop": remove the key entirely
  - "mask": replace the value with a shape-preserving placeholder (emails keep
            their domain, other strings keep a leading character, numbers -> "***")

Matching is by field NAME anywhere in the structure (top-level or inside
records[] lists), which is why manifest field names must be specific
(email, phone, total, grandTotal, street, postalCode, ...). The walker returns
the redacted object plus the set of field names it actually touched, for audit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RedactField:
    field: str
    action: str  # "drop" | "mask"


def _mask_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "***"
    if isinstance(value, (int, float)):
        return "***"
    if isinstance(value, str):
        if not value:
            return value
        if "@" in value:  # email: keep first char + domain
            local, _, domain = value.partition("@")
            head = local[0] if local else ""
            return f"{head}***@{domain}"
        return value[0] + "***" if len(value) > 1 else "***"
    if isinstance(value, list):
        return ["***" for _ in value]
    return "***"


def apply(plan: list[RedactField], obj: Any) -> tuple[Any, list[str]]:
    """Return (redacted_obj, sorted list of field names actually redacted)."""
    if not plan:
        return obj, []
    action_by_field = {r.field: r.action for r in plan}
    touched: set[str] = set()

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            for key, value in node.items():
                action = action_by_field.get(key)
                if action == "drop":
                    touched.add(key)
                    continue
                if action == "mask":
                    touched.add(key)
                    out[key] = _mask_value(value)
                    continue
                out[key] = _walk(value)
            return out
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    return _walk(obj), sorted(touched)
