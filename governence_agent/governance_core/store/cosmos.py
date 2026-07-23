"""CosmosPolicyStore -- durable, multi-instance policy store (Cosmos Core/SQL API).

The same write surface as FilePolicyStore, backed by Cosmos containers instead of a
local file -- so per-user policy + the category ("department") default scope persist
durably and are shared across App Service instances (this is what removes the
single-instance limit). Reuses FilePolicyStore's (de)serialization so item shapes match.

Reads serve from an in-memory cache refreshed on a short TTL (and immediately on write),
so the auth hot path (get_by_api_key -> consumers()) stays fast -- no per-request Cosmos
round-trip -- while still picking up other instances' writes within the TTL
(design_plan_v2.md B.2: "loads all consumers, short TTL, refresh on write").

Chat history (chatSessions) is DELIBERATELY NOT part of that cache/refresh cycle.
consumers()/categories()/accessRequests() are small and rarely written, so caching
all of them together and refreshing on the same TTL is cheap; chatSessions grows with
every chat message and never shrinks, so folding it into the same _refresh() would mean
every request that just wants consumers() (i.e. every authenticated request) eventually
pays for a full chat-history scan too. Instead chat reads go straight to Cosmos with
narrow, partition-scoped operations (point read / partition query) that stay cheap
regardless of how much history has accumulated -- see the "chat history" section below.

Containers (created on first run if absent):
  consumers       PK /consumerId    principals (users + agents) -> per-user policy
  categories      PK /id            category templates -> data-domain access grants
  departments     PK /id            org-unit groupings of categories (departments.py) --
                                    same small/rarely-written profile as categories, so
                                    it's part of the shared TTL cache below (unlike chat)
  accessRequests  PK /consumerId    signup + widen-access queue
  config          PK /id            global whitelist (doc id="global")
  chatSessions    PK /consumerId    chat history (see note above -- not TTL-cached)

First run (empty containers) seeds the SAME defaults as the file/local store:
the code category templates (orders/accounts/shipments/finance/analytics) + the
env-seeded admin/viewer/consumers -- then writes them to Cosmos.

Config (env):
  GOVERNANCE_COSMOS_CONNECTION_STRING           (or)
  GOVERNANCE_COSMOS_URL + GOVERNANCE_COSMOS_KEY
  GOVERNANCE_COSMOS_DATABASE        default "governance"
  GOVERNANCE_COSMOS_CACHE_TTL_SEC   default 30
"""
from __future__ import annotations

import os
import time

from azure.cosmos import CosmosClient, PartitionKey
from azure.cosmos import exceptions as cosmos_exceptions

import departments as dept_seed
from policy import categories as cat_seed
from store.base import PolicyStore
from store.file_store import (
    _category_from_dict,
    _category_to_dict,
    _chat_session_from_dict,
    _chat_session_to_dict,
    _consumer_from_dict,
    _consumer_to_dict,
    _department_from_dict,
    _department_to_dict,
)
from store.local import seed_consumers_from_env
from store.models import ChatSession, ConsumerRecord


class CosmosConfigError(RuntimeError):
    """Cosmos connection settings are missing/invalid."""


def _make_client() -> CosmosClient:
    conn = os.getenv("GOVERNANCE_COSMOS_CONNECTION_STRING")
    if conn:
        return CosmosClient.from_connection_string(conn)
    url = os.getenv("GOVERNANCE_COSMOS_URL")
    key = os.getenv("GOVERNANCE_COSMOS_KEY")
    if not (url and key):
        raise CosmosConfigError(
            "Cosmos store needs GOVERNANCE_COSMOS_CONNECTION_STRING, or "
            "GOVERNANCE_COSMOS_URL + GOVERNANCE_COSMOS_KEY."
        )
    return CosmosClient(url, credential=key)


class CosmosPolicyStore(PolicyStore):
    def __init__(self, client: CosmosClient | None = None):
        client = client or _make_client()
        db = client.create_database_if_not_exists(os.getenv("GOVERNANCE_COSMOS_DATABASE", "governance"))
        self._consumers_c = db.create_container_if_not_exists("consumers", PartitionKey(path="/consumerId"))
        self._categories_c = db.create_container_if_not_exists("categories", PartitionKey(path="/id"))
        self._departments_c = db.create_container_if_not_exists("departments", PartitionKey(path="/id"))
        self._requests_c = db.create_container_if_not_exists("accessRequests", PartitionKey(path="/consumerId"))
        self._config_c = db.create_container_if_not_exists("config", PartitionKey(path="/id"))
        self._chat_c = db.create_container_if_not_exists("chatSessions", PartitionKey(path="/consumerId"))

        self._ttl = int(os.getenv("GOVERNANCE_COSMOS_CACHE_TTL_SEC", "30"))
        self._consumers: dict[str, ConsumerRecord] = {}
        self._categories: dict = {}
        self._departments: dict = {}
        self._whitelist: list[str] = []
        self._requests: list[dict] = []
        self._loaded_at = 0.0

        self._refresh(force=True)
        if not self._consumers and not self._categories:
            self._seed()
        elif not self._departments:
            # departments (departments.py) was added after this store already had
            # consumers/categories -- the "empty store" seed gate above never fires
            # for an already-populated deployment, so back-fill just this piece.
            for department in dept_seed.DEPARTMENTS.values():
                self.upsert_department(department)

    # ── cache ───────────────────────────────────────────────────────────────
    def _refresh(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._loaded_at) < self._ttl:
            return
        self._consumers = {
            d["consumer_id"]: _consumer_from_dict(d) for d in self._consumers_c.read_all_items()
        }
        self._categories = {d["id"]: _category_from_dict(d) for d in self._categories_c.read_all_items()}
        self._departments = {d["id"]: _department_from_dict(d) for d in self._departments_c.read_all_items()}
        self._requests = [dict(d) for d in self._requests_c.read_all_items()]
        try:
            cfg = self._config_c.read_item("global", partition_key="global")
        except cosmos_exceptions.CosmosResourceNotFoundError:
            cfg = None
        self._whitelist = list((cfg or {}).get("cidrs") or [])
        self._controls = dict((cfg or {}).get("controls") or {})
        self._loaded_at = now

    # ── reads (cache-served, TTL-refreshed) ───────────────────────────────────
    def consumers(self) -> list[ConsumerRecord]:
        self._refresh()
        return list(self._consumers.values())

    def get_category(self, category_id: str | None):
        self._refresh()
        return self._categories.get(category_id) if category_id else None

    def categories(self) -> list:
        self._refresh()
        return list(self._categories.values())

    def get_department(self, department_id: str | None):
        self._refresh()
        return self._departments.get(department_id) if department_id else None

    def departments(self) -> list:
        self._refresh()
        return list(self._departments.values())

    def get_whitelist(self) -> list[str]:
        self._refresh()
        return list(self._whitelist)

    def get_controls(self) -> dict:
        self._refresh()
        c = self._controls or {}
        return {"paused_agents": bool(c.get("paused_agents")),
                "paused_backends": list(c.get("paused_backends") or [])}

    # ── chat history (uncached, not part of _refresh -- see module docstring) ──
    def chat_sessions(self) -> list[ChatSession]:
        """Every session, any consumer. Full, uncached container scan -- fine for an
        occasional admin/export use, NOT for a per-request path (that's what
        get_chat_session_for / list_chat_sessions_for / list_open_chat_sessions are
        for; they narrow the read instead)."""
        return [_chat_session_from_dict(d) for d in self._chat_c.read_all_items()]

    def get_chat_session_for(self, session_id: str, consumer_id: str) -> ChatSession | None:
        """O(1) regardless of history size: a single point read keyed by (id,
        partition key), not a scan."""
        try:
            return _chat_session_from_dict(
                self._chat_c.read_item(item=session_id, partition_key=consumer_id)
            )
        except cosmos_exceptions.CosmosResourceNotFoundError:
            return None

    def get_chat_session(self, session_id: str) -> ChatSession | None:
        """Owner unknown (e.g. an admin opening a transcript by id alone) -- a
        cross-partition query, but only by that one id, not a full scan. Low
        frequency (dashboard viewing), unlike get_chat_session_for."""
        items = list(self._chat_c.query_items(
            query="SELECT * FROM c WHERE c.session_id = @sid",
            parameters=[{"name": "@sid", "value": session_id}],
            enable_cross_partition_query=True,
        ))
        return _chat_session_from_dict(items[0]) if items else None

    def list_chat_sessions_for(self, consumer_id: str) -> list[ChatSession]:
        """Single-partition query -- stays cheap as OTHER users' history grows;
        one user's own history growing without bound is a separate, not-yet-needed
        concern (pagination/retention), not addressed here."""
        items = self._chat_c.query_items(
            query="SELECT * FROM c WHERE c.consumerId = @cid",
            parameters=[{"name": "@cid", "value": consumer_id}],
            partition_key=consumer_id,
        )
        sessions = [_chat_session_from_dict(d) for d in items]
        sessions.sort(key=lambda s: s.last_active_at, reverse=True)
        return sessions

    def list_open_chat_sessions(self) -> list[ChatSession]:
        """Cross-partition, but filtered server-side to status='open' -- as total
        history grows, closed sessions (the vast majority over time) are excluded
        from the scan entirely, so this stays cheap. Called only by the sweep
        (every GOVERNANCE_CHAT_SWEEP_INTERVAL_SEC), never a per-request path."""
        items = self._chat_c.query_items(
            query="SELECT * FROM c WHERE c.status = 'open'",
            enable_cross_partition_query=True,
        )
        return [_chat_session_from_dict(d) for d in items]

    def upsert_chat_session(self, session: ChatSession) -> None:
        self._chat_c.upsert_item(
            {**_chat_session_to_dict(session), "id": session.session_id, "consumerId": session.consumer_id}
        )

    @property
    def writable(self) -> bool:
        return True

    # ── writes (Cosmos + cache) ────────────────────────────────────────────────
    def upsert_consumer(self, record: ConsumerRecord) -> None:
        self._consumers_c.upsert_item(
            {**_consumer_to_dict(record), "id": record.consumer_id, "consumerId": record.consumer_id}
        )
        self._consumers[record.consumer_id] = record

    def delete_consumer(self, consumer_id: str) -> None:
        try:
            self._consumers_c.delete_item(consumer_id, partition_key=consumer_id)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            pass
        self._consumers.pop(consumer_id, None)

    def upsert_category(self, category) -> None:
        self._categories_c.upsert_item({**_category_to_dict(category)})  # dict has "id" (= PK)
        self._categories[category.id] = category

    def delete_category(self, category_id: str) -> None:
        try:
            self._categories_c.delete_item(category_id, partition_key=category_id)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            pass
        self._categories.pop(category_id, None)

    def upsert_department(self, department) -> None:
        self._departments_c.upsert_item({**_department_to_dict(department)})  # dict has "id" (= PK)
        self._departments[department.id] = department

    def delete_department(self, department_id: str) -> None:
        try:
            self._departments_c.delete_item(department_id, partition_key=department_id)
        except cosmos_exceptions.CosmosResourceNotFoundError:
            pass
        self._departments.pop(department_id, None)

    def set_whitelist(self, cidrs: list[str]) -> None:
        wl = [c.strip() for c in cidrs if c and c.strip()]
        # Preserve controls (both live in the one "global" config doc).
        self._config_c.upsert_item({"id": "global", "cidrs": wl, "controls": self._controls or {}})
        self._whitelist = wl

    def set_controls(self, controls: dict) -> None:
        ctrl = {"paused_agents": bool(controls.get("paused_agents")),
                "paused_backends": [b for b in (controls.get("paused_backends") or []) if b]}
        self._config_c.upsert_item({"id": "global", "cidrs": self._whitelist, "controls": ctrl})
        self._controls = ctrl

    def list_access_requests(self) -> list[dict]:
        self._refresh()
        return [dict(r) for r in self._requests]

    def add_access_request(self, request: dict) -> None:
        doc = {**request, "id": request["id"], "consumerId": request.get("consumer_id", "_none")}
        self._requests_c.upsert_item(doc)
        self._requests.append(dict(request))

    def update_access_request(self, request_id: str, fields: dict) -> dict | None:
        for r in self._requests:
            if r.get("id") == request_id:
                r.update(fields)
                self._requests_c.upsert_item(
                    {**r, "id": r["id"], "consumerId": r.get("consumer_id", "_none")}
                )
                return dict(r)
        return None

    # ── first-run seed (mirrors FilePolicyStore._seed) ──────────────────────────
    def _seed(self) -> None:
        for record in seed_consumers_from_env():
            self.upsert_consumer(record)
        for category in cat_seed.CATEGORIES.values():
            self.upsert_category(category)
        for department in dept_seed.DEPARTMENTS.values():
            self.upsert_department(department)
        raw = (os.getenv("GOVERNANCE_IP_ALLOWLIST") or "").strip()
        wl = [c.strip() for c in raw.split(",") if c.strip()]
        if wl:
            self.set_whitelist(wl)
        self._loaded_at = time.time()
