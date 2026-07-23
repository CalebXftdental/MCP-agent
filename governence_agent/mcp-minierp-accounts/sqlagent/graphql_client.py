"""
Production miniERP GraphQL client (accounts backend).

Generic client -- find_with_offset_pagination / find_with_cursor_pagination
only. No order-specific custom field here; that lives in mcp-minierp-shipments
(get_sales_order_data_by_order_number), since it's a shipment/tracking lookup.

Targets:
    https://db-api.frontierdental.com/graphql
authenticated via JWT issued by:
    https://db-api.frontierdental.com/authentication/sign-in

Configuration (read from env, typically loaded from .env.local):
  MINIERP_GRAPHQL_URL   default: FD_API or https://db-api.frontierdental.com/graphql
  MINIERP_AUTH_URL      default: FD_API_BASE + /authentication/sign-in
  MINIERP_USERNAME      required for token issuance
  MINIERP_PASSWORD      required for token issuance
  MINIERP_TOKEN         optional: use a pre-issued token (skips sign-in)
  MINIERP_TIMEOUT_SEC   default: 20

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


# ── Token cache ───────────────────────────────────────────────────────────────

_token_cache: dict[str, Any] = {"token": None, "issued_at": 0.0}
_token_lock = asyncio.Lock()


async def _issue_token(client: httpx.AsyncClient) -> str:
    pre_issued = os.getenv("MINIERP_TOKEN")
    if pre_issued:
        return pre_issued

    username = os.getenv("MINIERP_USERNAME")
    password = os.getenv("MINIERP_PASSWORD")
    if not username or not password:
        raise GraphQLConfigError(
            "MINIERP_USERNAME and MINIERP_PASSWORD are required (or set "
            "MINIERP_TOKEN to a pre-issued JWT). Configure these in .env.local."
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
        raise GraphQLAuthError(
            f"Sign-in returned HTTP {resp.status_code}: {resp.text[:300]}"
        )

    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise GraphQLAuthError(f"Sign-in response missing access_token: {body!r}")
    return token


async def _get_token(client: httpx.AsyncClient, *, force_refresh: bool = False) -> str:
    async with _token_lock:
        now = time.time()
        if (
            not force_refresh
            and _token_cache["token"]
            and (now - _token_cache["issued_at"]) < _TOKEN_TTL_SEC
        ):
            return _token_cache["token"]

        token = await _issue_token(client)
        _token_cache["token"] = token
        _token_cache["issued_at"] = now
        return token


def _reset_token_cache() -> None:
    """Clear the cached token. Useful for tests or after a known auth failure."""
    _token_cache["token"] = None
    _token_cache["issued_at"] = 0.0


# ── GraphQL POST ──────────────────────────────────────────────────────────────


async def _post_graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    operation_name: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables
    if operation_name:
        payload["operationName"] = operation_name

    async with httpx.AsyncClient(timeout=_timeout_sec()) as client:
        for attempt in (1, 2):
            token = await _get_token(client, force_refresh=(attempt == 2))
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
) -> dict[str, Any]:
    if not table or not isinstance(table, str):
        raise ValueError("table must be a non-empty string")
    data = await _post_graphql(_build_offset_query(table, options or {}))
    return data["findWithOffsetPagination"]


async def find_with_cursor_pagination(
    table: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not table or not isinstance(table, str):
        raise ValueError("table must be a non-empty string")
    data = await _post_graphql(_build_cursor_query(table, options or {}))
    return data["findWithCursorPagination"]
