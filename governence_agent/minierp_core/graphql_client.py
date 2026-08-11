"""
Production miniERP GraphQL client (shared).

Single source of the miniERP data-access transport. Extracted from the four
per-backend `mcp-minierp-*/sqlagent/graphql_client.py` copies (which were
byte-identical except that only shipments carried the
`get_sales_order_data_by_order_number` custom resolver — folded in here so the
one client is a superset). All backends now import from this package instead of
their own copy.

Targets the same endpoint the production chatbot uses:
    https://db-api.frontierdental.com/graphql
authenticated via JWT issued by:
    https://db-api.frontierdental.com/authentication/sign-in

Exposes the read operations documented in DB_API_documentation_v1.1.md:
  - find_with_offset_pagination(table, options) -> dict
  - find_with_cursor_pagination(table, options) -> dict
  - get_sales_order_data_by_order_number(value, field) -> dict  (custom join resolver)

Both paginators return the full GraphQL response payload for the operation
({items, page, pageSize, hasMore} for offset; {items, pageSize, hasMore,
nextCursor} for cursor).

Configuration (read from env, typically loaded from .env.local):
  MINIERP_GRAPHQL_URL   default: FD_API or https://db-api.frontierdental.com/graphql
  MINIERP_AUTH_URL      default: FD_API_BASE + /authentication/sign-in
  MINIERP_USERNAME      required for token issuance
  MINIERP_PASSWORD      required for token issuance
  MINIERP_TOKEN         optional: use a pre-issued token (skips sign-in)
  MINIERP_TIMEOUT_SEC   default: 20

The FD_API and FD_API_BASE aliases are supported so local_test can share the
same miniERP endpoint settings used by src/sqlAgent/helper/tool_helper.py.

Token lifecycle:
  - Tokens expire 24h after issuance (per API docs).
  - We cache a token in-process and refresh proactively at the 23h mark.
  - On HTTP 401 we force a refresh and retry once.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx


# ── Errors ────────────────────────────────────────────────────────────────────


class GraphQLConfigError(RuntimeError):
    """Required configuration (URL, credentials) is missing."""


class GraphQLAuthError(RuntimeError):
    """Sign-in failed or returned no token."""


class GraphQLQueryError(RuntimeError):
    """The GraphQL endpoint returned `errors` or a non-2xx status."""


# ── Config ────────────────────────────────────────────────────────────────────

_DEFAULT_GRAPHQL_URL = "https://db-api.frontierdental.com/graphql"
_DEFAULT_AUTH_BASE_URL = "https://db-api.frontierdental.com"
_DEFAULT_AUTH_PATH = "/authentication/sign-in"

# Refresh tokens slightly before the 24h mark so a long-running process
# doesn't pay a 401-retry cost on every call once expired.
_TOKEN_TTL_SEC = 23 * 60 * 60


def _graphql_url() -> str:
    return os.getenv("MINIERP_GRAPHQL_URL") or os.getenv("FD_API") or _DEFAULT_GRAPHQL_URL


def _auth_url() -> str:
    explicit = os.getenv("MINIERP_AUTH_URL")
    if explicit:
        return explicit
    base_url = os.getenv("FD_API_BASE") or _DEFAULT_AUTH_BASE_URL
    return f"{base_url.rstrip('/')}{_DEFAULT_AUTH_PATH}"


def _timeout_sec() -> float:
    return float(os.getenv("MINIERP_TIMEOUT_SEC", "20"))


# ── Credentials + token cache (per credential PROFILE) ──────────────────────────
# Different miniERP domains may require different service accounts: finance uses
# the "administrator" account (AR/AP/GL/PO/Vendor), while orders/accounts/shipments
# use the default account. A *profile* selects the credential set:
#   default   -> MINIERP_TOKEN | MINIERP_USERNAME / MINIERP_PASSWORD
#   "<name>"  -> MINIERP_<NAME>_TOKEN | MINIERP_<NAME>_USERNAME / _PASSWORD
#                (falls back to the default vars if the profile-specific ones are unset)
# Tokens are cached per profile so each account keeps its own JWT.
_token_cache: dict[str, dict[str, Any]] = {}
_token_lock = asyncio.Lock()


def _env_prefix(profile: str | None) -> str:
    if not profile or profile.lower() == "default":
        return "MINIERP_"
    return f"MINIERP_{profile.upper()}_"


def _creds(profile: str | None) -> tuple[str | None, str | None, str | None]:
    """(pre_issued_token, username, password) for a profile; profile-specific vars
    win, else fall back to the default MINIERP_* vars."""
    p = _env_prefix(profile)
    is_default = p == "MINIERP_"
    token = os.getenv(f"{p}TOKEN") or (None if is_default else os.getenv("MINIERP_TOKEN"))
    username = os.getenv(f"{p}USERNAME") or (None if is_default else os.getenv("MINIERP_USERNAME"))
    password = os.getenv(f"{p}PASSWORD") or (None if is_default else os.getenv("MINIERP_PASSWORD"))
    return token, username, password


async def _issue_token(client: httpx.AsyncClient, profile: str | None) -> str:
    pre_issued, username, password = _creds(profile)
    if pre_issued:
        return pre_issued
    if not username or not password:
        raise GraphQLConfigError(
            f"miniERP credentials missing for profile {profile or 'default'!r}: set "
            f"{_env_prefix(profile)}USERNAME/PASSWORD (or {_env_prefix(profile)}TOKEN), "
            "or the default MINIERP_USERNAME/PASSWORD."
        )
    try:
        resp = await client.post(
            _auth_url(),
            json={"username": username, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=_timeout_sec(),
        )
    except httpx.HTTPError as exc:
        raise GraphQLAuthError(f"Sign-in request failed: {exc}") from exc

    if resp.status_code != 200:
        raise GraphQLAuthError(f"Sign-in returned HTTP {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise GraphQLAuthError(f"Sign-in response missing access_token: {body!r}")
    return token


async def _get_token(client: httpx.AsyncClient, profile: str | None, *, force_refresh: bool = False) -> str:
    key = (profile or "default").lower()
    async with _token_lock:
        now = time.time()
        entry = _token_cache.get(key)
        if not force_refresh and entry and entry["token"] and (now - entry["issued_at"]) < _TOKEN_TTL_SEC:
            return entry["token"]
        token = await _issue_token(client, profile)
        _token_cache[key] = {"token": token, "issued_at": now}
        return token


def _reset_token_cache() -> None:
    """Clear all cached tokens. Useful for tests or after a known auth failure."""
    _token_cache.clear()


# ── GraphQL POST ──────────────────────────────────────────────────────────────


async def _post_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    operation_name: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """POST a GraphQL operation and return the `data` payload.

    `profile` selects which miniERP service account authenticates the call.
    Retries once on HTTP 401 with a refreshed token. Raises
    GraphQLQueryError on any non-2xx response or `errors` array
    in the GraphQL body.
    """
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    if operation_name:
        payload["operationName"] = operation_name

    async with httpx.AsyncClient(timeout=_timeout_sec()) as client:
        for attempt in (1, 2):
            token = await _get_token(client, profile, force_refresh=(attempt == 2))
            try:
                resp = await client.post(
                    _graphql_url(),
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
            except httpx.HTTPError as exc:
                raise GraphQLQueryError(f"GraphQL request failed: {exc}") from exc

            if resp.status_code == 401 and attempt == 1:
                # Token may have been revoked or expired earlier than expected.
                continue

            if resp.status_code != 200:
                raise GraphQLQueryError(
                    f"GraphQL HTTP {resp.status_code}: {resp.text[:500]}"
                )

            body = resp.json()
            if body.get("errors"):
                raise GraphQLQueryError(f"GraphQL errors: {body['errors']}")
            data = body.get("data")
            if data is None:
                raise GraphQLQueryError(f"GraphQL response missing data: {body!r}")
            return data

    # Unreachable — both attempts must either return or raise.
    raise GraphQLQueryError("Unexpected GraphQL retry exit")


# ── Public read operations ────────────────────────────────────────────────────


def _format_graphql_value(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            f"{key}: {_format_graphql_value(inner_value)}"
            for key, inner_value in value.items()
        ]
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_format_graphql_value(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _build_offset_query(table: str, options: dict[str, Any]) -> str:
    return f"""query {{
  findWithOffsetPagination(table: {json.dumps(table)}, options: {_format_graphql_value(options)}) {{
    items
    page
    pageSize
    hasMore
  }}
}}"""


def _build_cursor_query(table: str, options: dict[str, Any]) -> str:
    return f"""query {{
  findWithCursorPagination(table: {json.dumps(table)}, options: {_format_graphql_value(options)}) {{
    items
    pageSize
    hasMore
    nextCursor
  }}
}}"""


async def find_with_offset_pagination(
    table: str,
    options: dict[str, Any] | None = None,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Run findWithOffsetPagination against the production miniERP.

    Args:
      table:   Entity name (e.g. "Account", "SOOrder", "Customer").
      options: {select?, where?, orderBy?, page?, pageSize?}.
      profile: miniERP credential profile (default account if None).

    Returns:
      {items: list, page: int, pageSize: int, hasMore: bool}
    """
    if not table or not isinstance(table, str):
        raise ValueError("table must be a non-empty string")
    data = await _post_graphql(_build_offset_query(table, options or {}), profile=profile)
    return data["findWithOffsetPagination"]


async def paginate_all(
    table: str,
    options: dict[str, Any],
    *,
    profile: str | None = None,
    page_size: int = 250,
    max_pages: int = 20,
) -> tuple[list[dict[str, Any]], bool]:
    """Page findWithOffsetPagination to exhaustion (or `max_pages`), returning
    the combined item list and whether it was truncated.

    Every cross-record/"bulk" analytics tool (mcp-minierp/sqlagent/analytics.py's
    order-recency/top-spenders tools, sqlagent/finance/index.py's due-soon/past-due
    tools, ...) needs this exact "loop pages, cap at N, flag truncated" shape --
    before this it was copy-pasted per domain module. Callers still own their own
    `select`/`where`/`orderBy` (passed via `options`, same shape
    find_with_offset_pagination itself takes); this only owns the paging loop.
    Never silently truncates without saying so -- `truncated=True` means there was
    more data than `max_pages` covered.
    """
    rows: list[dict[str, Any]] = []
    page = max(1, int(options.get("page") or 1))
    truncated = False
    while True:
        result = await find_with_offset_pagination(table, {**options, "page": page, "pageSize": page_size}, profile=profile)
        rows.extend(result.get("items") or [])
        if not result.get("hasMore"):
            break
        page += 1
        if page > max_pages:
            truncated = True
            break
    return rows, truncated


async def find_with_cursor_pagination(
    table: str,
    options: dict[str, Any] | None = None,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Run findWithCursorPagination against the production miniERP.

    Args:
      table:   Entity name.
      options: {select?, orderBy (required by API), pageSize?, cursor?}.
      profile: miniERP credential profile (default account if None).

    Returns:
      {items: list, pageSize: int, hasMore: bool, nextCursor: str | None}
    """
    if not table or not isinstance(table, str):
        raise ValueError("table must be a non-empty string")
    data = await _post_graphql(_build_cursor_query(table, options or {}), profile=profile)
    return data["findWithCursorPagination"]


async def get_sales_order_data_by_order_number(
    value: str,
    *,
    field: str = "orderNumber",
    profile: str | None = None,
) -> dict[str, Any]:
    """Run the production shipment/tracking lookup used by src/sqlAgent.

    The DB API exposes this as a custom GraphQL field. src builds it as:
      getSalesOrderDataByOrderNumber(orderNumber: "...") or
      getSalesOrderDataByOrderNumber(shipmentNumber: "...")
    """
    if field not in {"orderNumber", "shipmentNumber"}:
        raise ValueError("field must be orderNumber or shipmentNumber")
    query = f"""query {{
  getSalesOrderDataByOrderNumber({field}: {json.dumps(value)}) {{
    items
  }}
}}"""
    data = await _post_graphql(query, {}, operation_name=None, profile=profile)
    return data["getSalesOrderDataByOrderNumber"]


async def get_order_address_data_by_order_number(order_number: str, *, profile: str | None = None) -> dict[str, Any]:
    """Run the production order-address lookup used by src/sqlAgent.

    The DB API exposes this as a custom GraphQL field that joins a sales order
    to its bill-to / ship-to addresses:
      getOrderAddressDataByOrderNumber(orderNumber: "...")
    """
    query = f"""query {{
  getOrderAddressDataByOrderNumber(orderNumber: {json.dumps(order_number)}) {{
    items
  }}
}}"""
    data = await _post_graphql(query, {}, operation_name=None, profile=profile)
    return data["getOrderAddressDataByOrderNumber"]
