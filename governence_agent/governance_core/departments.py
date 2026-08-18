"""Departments -- an org-unit grouping of categories (Sales, Customer Service, ...).

NOT the pre-2026-07 "department" model design_plan_v2.md describes as renamed away
(that one WAS the access grouping itself, 1:1 with a backend -- see
policy/categories.py's docstring for that history). This is a separate, newer
concept layered on top: a department is just a named SET of categories, used two
places:
  - the public signup form's department dropdown (a user picks one instead of
    typing category ids directly)
  - LIVE grant resolution (policy/resolve.py): a principal's `department` field is
    resolved to its CURRENT category set on every request, not copied once at
    signup -- so editing a department's categories here (or via the admin CRUD API
    once persisted) takes effect for every member immediately, no separate resync
    step. A user's own `categories` field, if any, is additive on top of their
    department's; one-off individual exceptions are expected to go through
    `overrides` instead (see resolve.py).

This is the seed/dev default (mirrors policy/categories.py's CATEGORIES dict);
FilePolicyStore/CosmosPolicyStore persist an admin-editable copy the same way they
already do for categories -- this module is what a fresh store seeds itself from.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Department:
    id: str
    display_name: str
    categories: tuple[str, ...] = field(default_factory=tuple)


# "personal_knowledge" (digest_persoanl_kb.md) is added to every department below
# so every self-service-signed-up user gets their own private knowledge base by
# default -- it holds only the user's own uploaded documents, so unlike every
# other category here it carries no shared-data exposure to grant. Because
# department categories resolve LIVE (see this module's docstring / resolve.py),
# adding it here is retroactive: existing members pick it up on their very next
# request, no backfill script needed. A consumer with no department at all
# (env-seeded Stage-1 consumers, or an admin-created one with no department
# picked) already resolves to allow-all-tools under resolve.py's legacy
# fallback, so it's already covered without appearing in any tuple here.
DEPARTMENTS: dict[str, Department] = {
    "sales": Department("sales", "Sales", ("orders", "personal_knowledge")),
    "customer_service": Department("customer_service", "Customer Service", ("accounts", "orders", "personal_knowledge")),
    "finance": Department("finance", "Finance", ("finance", "personal_knowledge")),
}


def get_department(department_id: str | None) -> Department | None:
    if not department_id:
        return None
    return DEPARTMENTS.get(department_id)


def list_departments() -> list[Department]:
    return list(DEPARTMENTS.values())
