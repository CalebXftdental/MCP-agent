"""FilePolicyStore -- a writable, JSON-persisted policy store.

The Stage-1 store was read-only (env-seeded). The dashboard control plane needs to
WRITE policy (register consumers, edit departments/grants/limits/whitelist), so this
backend holds the policy in memory and persists every change to a JSON file. On first
run (file absent) it seeds from the same env consumers + the code department templates,
then persists -- so switching to it changes nothing until an admin edits something.

Selected by setting GOVERNANCE_STORE_FILE. The Cosmos backend will implement the same
write surface later; this is the single-instance stand-in until governance Cosmos exists
(a file store obviously doesn't share across replicas -- fine for one instance/dev).
"""
from __future__ import annotations

import json
import os
import store_concurrency
import time
from pathlib import Path

import departments as dept_seed
from departments import Department
from policy import categories as cat_seed
from policy.categories import Category
from store.base import PolicyStore
from store.local import seed_consumers_from_env
from store.models import ChatMessage, ChatSession, ConsumerRecord


# ── serialization ─────────────────────────────────────────────────────────────

def _consumer_to_dict(r: ConsumerRecord) -> dict:
    return {
        "consumer_id": r.consumer_id, "name": r.name, "key_hash": r.key_hash,
        "status": r.status, "login_password_hash": r.login_password_hash,
        "full_name": r.full_name, "department": r.department,
        "rate_limit_per_hour": r.rate_limit_per_hour, "ip_allowlist": list(r.ip_allowlist),
        "allowed_levels": sorted(r.allowed_levels), "categories": list(r.categories),
        "overrides": r.overrides, "role": r.role, "type": r.type,
        "key_created_at": r.key_created_at, "key_rotated_at": r.key_rotated_at,
    }


def _consumer_from_dict(d: dict) -> ConsumerRecord:
    return ConsumerRecord(
        consumer_id=d["consumer_id"], name=d["name"], key_hash=d.get("key_hash", ""),
        status=d.get("status", "active"), login_password_hash=d.get("login_password_hash"),
        full_name=d.get("full_name", ""), department=d.get("department", ""),
        rate_limit_per_hour=d.get("rate_limit_per_hour"), ip_allowlist=list(d.get("ip_allowlist") or []),
        allowed_levels=frozenset(d.get("allowed_levels") or []), categories=list(d.get("categories") or []),
        overrides=d.get("overrides") or {}, role=d.get("role", "user"), type=d.get("type", "agent"),
        key_created_at=d.get("key_created_at"), key_rotated_at=d.get("key_rotated_at"),
    )


def _chat_message_to_dict(m: ChatMessage) -> dict:
    return {"role": m.role, "content": m.content, "ts": m.ts, "tools_used": list(m.tools_used)}


def _chat_message_from_dict(d: dict) -> ChatMessage:
    return ChatMessage(role=d["role"], content=d.get("content", ""), ts=d.get("ts", 0.0),
                       tools_used=list(d.get("tools_used") or []))


def _chat_session_to_dict(s: ChatSession) -> dict:
    return {
        "session_id": s.session_id, "consumer_id": s.consumer_id, "status": s.status,
        "created_at": s.created_at, "last_active_at": s.last_active_at, "closed_at": s.closed_at,
        "summary": s.summary, "messages": [_chat_message_to_dict(m) for m in s.messages],
    }


def _chat_session_from_dict(d: dict) -> ChatSession:
    return ChatSession(
        session_id=d["session_id"], consumer_id=d["consumer_id"], status=d.get("status", "open"),
        created_at=d.get("created_at", 0.0), last_active_at=d.get("last_active_at", 0.0),
        closed_at=d.get("closed_at"), summary=d.get("summary"),
        messages=[_chat_message_from_dict(m) for m in d.get("messages") or []],
    )


def _department_to_dict(d: Department) -> dict:
    return {"id": d.id, "display_name": d.display_name, "categories": list(d.categories)}


def _department_from_dict(d: dict) -> Department:
    return Department(id=d["id"], display_name=d.get("display_name", d["id"]),
                      categories=tuple(d.get("categories") or []))


def _category_to_dict(c: Category) -> dict:
    return {
        "id": c.id, "display_name": c.display_name, "backend": c.backend,
        "tools": ("*" if c.tools == "*" else sorted(c.tools)),
        "levels": sorted(c.levels), "data_domains": list(c.data_domains),
    }


def _category_from_dict(d: dict) -> Category:
    return Category(
        id=d["id"], display_name=d.get("display_name", d["id"]), backend=d["backend"],
        tools=("*" if d.get("tools") == "*" else frozenset(d.get("tools") or [])),
        levels=frozenset(d.get("levels") or []), data_domains=list(d.get("data_domains") or []),
    )


class FilePolicyStore(PolicyStore):
    def __init__(self, path: str):
        self._path = Path(path)
        self._consumers: dict[str, ConsumerRecord] = {}
        self._categories: dict[str, Category] = {}
        self._departments: dict[str, Department] = {}
        self._whitelist: list[str] = []
        self._requests: list[dict] = []
        # Keyed by (session_id, consumer_id), NOT session_id alone -- two
        # different consumers whose clients end up sending the same session_id
        # string (e.g. the sessionStorage-collision bug fixed 2026-07: testing
        # two accounts in one browser tab) must never let one silently
        # overwrite the other's conversation. Cosmos gets this for free from
        # its partition key; this backend needs it explicit.
        self._chat_sessions: dict[tuple[str, str], ChatSession] = {}
        if self._path.exists():
            self._load()
            dirty = False
            # Per-department category backfill -- same reasoning as
            # CosmosPolicyStore's (see its docstring comment): additive only,
            # adds category ids the code lists that a persisted department is
            # missing (e.g. "personal_knowledge" added to every department
            # after some stores already existed), never drops one.
            for dept in dept_seed.DEPARTMENTS.values():
                existing = self._departments.get(dept.id)
                if existing is None:
                    self._departments[dept.id] = dept
                    dirty = True
                else:
                    missing = [c for c in dept.categories if c not in existing.categories]
                    if missing:
                        self._departments[dept.id] = Department(
                            id=existing.id, display_name=existing.display_name,
                            categories=tuple(existing.categories) + tuple(missing),
                        )
                        dirty = True
            # Per-category backfill (not gated on "categories empty"): new categories
            # (e.g. "office", added for artifact editing) get defined in code after
            # deployments already have a persisted store, so an all-or-nothing seed
            # check like departments' would never add them. Only fill in IDs missing
            # entirely -- never overwrite a category an admin has already edited.
            for cat in cat_seed.CATEGORIES.values():
                if cat.id not in self._categories:
                    self._categories[cat.id] = cat
                    dirty = True
            if dirty:
                self._persist()
        else:
            self._seed()
            self._persist()

    # ── reads ─────────────────────────────────────────────────────────────────
    def consumers(self) -> list[ConsumerRecord]:
        return list(self._consumers.values())

    def get_category(self, category_id: str | None):
        return self._categories.get(category_id) if category_id else None

    def categories(self) -> list:
        return list(self._categories.values())

    def get_department(self, department_id: str | None):
        return self._departments.get(department_id) if department_id else None

    def departments(self) -> list:
        return list(self._departments.values())

    def get_whitelist(self) -> list[str]:
        return list(self._whitelist)

    def get_controls(self) -> dict:
        c = self._controls or {}
        return {"paused_agents": bool(c.get("paused_agents")),
                "paused_backends": list(c.get("paused_backends") or []),
                "paused_consumers": list(c.get("paused_consumers") or []),
                "paused_categories": {cid: list(cats) for cid, cats in (c.get("paused_categories") or {}).items() if cats}}

    def chat_sessions(self) -> list[ChatSession]:
        return list(self._chat_sessions.values())

    def get_chat_session_for(self, session_id: str, consumer_id: str) -> ChatSession | None:
        return self._chat_sessions.get((session_id, consumer_id))

    @property
    def writable(self) -> bool:
        return True

    # ── writes ────────────────────────────────────────────────────────────────
    def upsert_consumer(self, record: ConsumerRecord) -> None:
        self._consumers[record.consumer_id] = record
        self._persist()

    def delete_consumer(self, consumer_id: str) -> None:
        self._consumers.pop(consumer_id, None)
        self._persist()

    def upsert_category(self, category: Category) -> None:
        self._categories[category.id] = category
        self._persist()

    def delete_category(self, category_id: str) -> None:
        self._categories.pop(category_id, None)
        self._persist()

    def upsert_department(self, department: Department) -> None:
        self._departments[department.id] = department
        self._persist()

    def delete_department(self, department_id: str) -> None:
        self._departments.pop(department_id, None)
        self._persist()

    def set_whitelist(self, cidrs: list[str]) -> None:
        self._whitelist = [c.strip() for c in cidrs if c and c.strip()]
        self._persist()

    def set_controls(self, controls: dict) -> None:
        raw_categories = controls.get("paused_categories") or {}
        self._controls = {"paused_agents": bool(controls.get("paused_agents")),
                          "paused_backends": [b for b in (controls.get("paused_backends") or []) if b],
                          "paused_consumers": [c for c in (controls.get("paused_consumers") or []) if c],
                          "paused_categories": {cid: [c for c in (cats or []) if c]
                                                for cid, cats in raw_categories.items() if cats}}
        self._persist()

    def list_access_requests(self) -> list[dict]:
        return [dict(r) for r in self._requests]

    def add_access_request(self, request: dict) -> None:
        self._requests.append(dict(request))
        self._persist()

    def update_access_request(self, request_id: str, fields: dict) -> dict | None:
        for r in self._requests:
            if r.get("id") == request_id:
                r.update(fields)
                self._persist()
                return dict(r)
        return None

    def upsert_chat_session(self, session: ChatSession) -> None:
        self._chat_sessions[(session.session_id, session.consumer_id)] = session
        self._persist()

    # ── persistence ───────────────────────────────────────────────────────────
    def _seed(self) -> None:
        for r in seed_consumers_from_env():
            self._consumers[r.consumer_id] = r
        for cat in cat_seed.CATEGORIES.values():
            self._categories[cat.id] = cat
        for dept in dept_seed.DEPARTMENTS.values():
            self._departments[dept.id] = dept
        raw = (os.getenv("GOVERNANCE_IP_ALLOWLIST") or "").strip()
        self._whitelist = [c.strip() for c in raw.split(",") if c.strip()]
        self._controls = {}

    def _load(self) -> None:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self._consumers = {d["consumer_id"]: _consumer_from_dict(d) for d in data.get("consumers", [])}
        self._categories = {d["id"]: _category_from_dict(d) for d in data.get("categories", [])}
        self._departments = {d["id"]: _department_from_dict(d) for d in data.get("departments", [])}
        self._whitelist = list(data.get("whitelist") or [])
        self._controls = dict(data.get("controls") or {})
        self._requests = list(data.get("access_requests") or [])
        self._chat_sessions = {
            (d["session_id"], d["consumer_id"]): _chat_session_from_dict(d)
            for d in data.get("chat_sessions", [])
        }

    def _persist(self) -> None:
        data = {
            "version": int(time.time()),
            "consumers": [_consumer_to_dict(r) for r in self._consumers.values()],
            "categories": [_category_to_dict(c) for c in self._categories.values()],
            "departments": [_department_to_dict(d) for d in self._departments.values()],
            "whitelist": self._whitelist,
            "controls": self._controls,
            "access_requests": self._requests,
            "chat_sessions": [_chat_session_to_dict(s) for s in self._chat_sessions.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        store_concurrency.atomic_write_text(self._path, json.dumps(data, indent=2))
