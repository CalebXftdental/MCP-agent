"""Effective-policy resolution (design_plan_v2.md B.3).

Turns a principal (its set of categories + per-principal overrides) into the concrete
grant the PDP enforces: which tools it may call per backend, and which data
classification levels it may see per backend. Resolved LIVE at decision time, so a
category-template change takes effect immediately for every principal holding it.

Precedence (deterministic):
  1. Start from the UNION across the principal's categories: for each category add its
     backend's granted tools and its level set.
  2. Apply overrides.grantTools / grantLevels (admin-approved additions), per backend.
  3. Apply overrides.denyTools / denyLevels -- deny always wins.
  4. Levels are a SET (union minus denies), not a linear ceiling (PII and SENSITIVE
     are orthogonal).
  5. Anything not granted after the above is denied (deny-by-default) -- enforced by
     the PDP via allows_tool() / levels_for().

Department (departments.py): a principal's `department`, if set, contributes its
CURRENT category set live, every call -- not a copy taken at signup. Editing a
department's categories therefore takes effect for every member immediately, with
no separate resync step; this is the same "resolved live, not copied" property
categories already have for the tools/levels they grant. It's additive with any
categories the record ALSO holds directly (e.g. an admin-granted individual extra
on top of a department baseline); one-off exceptions are expected to go through
`overrides` instead. An unresolvable department id (deleted, or a store lag) fails
closed exactly like an unknown category -- it contributes nothing, and does NOT
count as "no department" for the legacy-fallback check below (a principal that
names a department is never treated as a category-less legacy principal, even if
that department can't currently be resolved).

Legacy / dev fallback: a principal with NO categories, NO department, AND no
overrides (e.g. the env-seeded consumers from Stage 1) resolves to allow-all-tools +
its explicit `allowed_levels`, preserving pre-category behavior. An UNKNOWN category
id contributes nothing (fail closed).

`overrides` shape (per backend):
  { "<backend>": { "grantTools": [...], "denyTools": [...],
                   "grantLevels": [...], "denyLevels": [...] } }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from policy import manifest


@dataclass
class EffectiveGrant:
    all_tools: bool = False                                    # legacy: no categories -> allow all
    tools_by_backend: dict[str, set[str]] = field(default_factory=dict)
    levels_by_backend: dict[str, frozenset[str]] = field(default_factory=dict)
    default_levels: frozenset[str] = frozenset()               # legacy: levels for any backend

    def allows_tool(self, backend: str, canonical_tool: str) -> bool:
        if self.all_tools:
            return True
        return canonical_tool in self.tools_by_backend.get(backend, set())

    def levels_for(self, backend: str) -> frozenset[str]:
        return self.levels_by_backend.get(backend, self.default_levels)


def resolve(record, get_category: Callable[[str], object],
            get_department: Callable[[str], object] | None = None) -> EffectiveGrant:
    """`record` is duck-typed (ConsumerRecord): .categories, .department, .overrides,
    .allowed_levels, .role. `get_department` is optional (defaults to None, i.e. no
    department contribution) so callers/tests that only care about categories
    (e.g. _smoke/test_categories.py) don't need to pass one.

    role="admin" is an unconditional full-access bypass -- every tool, every
    classification level, regardless of whatever categories/department/
    overrides also happen to be set on the record. Deliberately checked
    FIRST, before any of that is even read: an admin's access must not
    silently narrow just because they (or their department) also picked up
    a category for organizational/bookkeeping reasons. This is the only
    place `role` is ever consulted for tool-calling access -- everywhere
    else (rate limits, IP allowlist, break-glass pause, disable) still
    applies to an admin exactly like anyone else; this bypass is scoped to
    the PDP grant only."""
    if getattr(record, "role", None) == "admin":
        return EffectiveGrant(all_tools=True, default_levels=manifest.ALL_LEVELS)

    category_ids = list(getattr(record, "categories", None) or [])
    department_id = getattr(record, "department", None) or ""
    if department_id and get_department is not None:
        department = get_department(department_id)
        if department is not None:  # live top-up, not a copy -- see module docstring
            category_ids = list(dict.fromkeys(category_ids + list(department.categories)))
    overrides: dict = getattr(record, "overrides", None) or {}

    # Legacy / dev: no categories, no department, and no overrides -> allow all
    # tools, explicit levels.
    if not category_ids and not department_id and not overrides:
        return EffectiveGrant(all_tools=True, default_levels=getattr(record, "allowed_levels", frozenset()))

    tools_by_backend: dict[str, set[str]] = {}
    levels_by_backend: dict[str, set[str]] = {}

    # 1. Union across the principal's categories (each category maps to one backend).
    for cid in category_ids:
        category = get_category(cid)
        if category is None:  # unknown category contributes nothing (fail closed)
            continue
        backend = category.backend
        base_tools = manifest.tools_for_backend(backend) if category.tools == "*" else set(category.tools)
        tools_by_backend.setdefault(backend, set()).update(base_tools)
        levels_by_backend.setdefault(backend, set()).update(category.levels)

    # 2-3. Per-backend overrides: grants add, denies win.
    if isinstance(overrides, dict):
        for backend, ov in overrides.items():
            tools = tools_by_backend.setdefault(backend, set())
            tools.update(ov.get("grantTools", []))
            tools.difference_update(ov.get("denyTools", []))
            levels = levels_by_backend.setdefault(backend, set())
            levels.update(ov.get("grantLevels", []))
            levels.difference_update(ov.get("denyLevels", []))

    return EffectiveGrant(
        tools_by_backend={b: set(t) for b, t in tools_by_backend.items()},
        levels_by_backend={b: frozenset(l) for b, l in levels_by_backend.items()},
    )
