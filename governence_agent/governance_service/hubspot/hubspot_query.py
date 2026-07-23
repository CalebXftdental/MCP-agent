from __future__ import annotations

import asyncio
import json
import os
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import re
import requests

HUBSPOT_COMPANY_SEARCH_URL = os.getenv(
    "HUBSPOT_COMPANY_SEARCH_URL",
    "https://api.hubapi.com/crm/objects/2026-03/companies/search",
).strip()
_CONTACTS_PATH = Path(__file__).with_name("contacts.json")
# Old phone-only directory, used as a fallback when contacts.json is unavailable.
_PHONEBOOK_PATH = Path(__file__).with_name("phonebook.json")
_REQUEST_TIMEOUT_S = float((os.getenv("HUBSPOT_REQUEST_TIMEOUT_S") or "15").strip())
_CONTACTS_BLOB_TIMEOUT_S = float(
    (os.getenv("CONTACTS_BLOB_TIMEOUT_S") or os.getenv("PHONEBOOK_BLOB_TIMEOUT_S") or "15").strip()
)
_DEFAULT_CONTACTS_BLOB_NAME = "contacts.json"
_CONTACTS_CACHE: list[dict[str, str]] | None = None
# Name matching here is a deterministic fuzzy-ratio match only (SequenceMatcher).
# The original chatbot-side implementation used an LLM to disambiguate fuzzy
# names; that dependency (chat_primitives/model_manager, and therefore an LLM
# API key) is deliberately NOT carried into this credential-broker service —
# the governance agent should hold DB/CRM credentials only, not LLM credentials.


def lookup_sales_rep_sync(customer_id: str) -> dict[str, Any]:
    normalized_customer_id = (customer_id or "").strip()
    if not normalized_customer_id:
        return {
            "status": "missing_customer_id",
            "customer_id": "",
            "message": "Customer ID is required for a sales rep lookup.",
        }

    token = _hubspot_access_token()
    if not token:
        return {
            "status": "hubspot_not_configured",
            "customer_id": normalized_customer_id,
            "message": "HubSpot access token is not configured.",
        }

    try:
        payload = _fetch_company_search_results(normalized_customer_id, token)
    except requests.HTTPError as exc:
        return {
            "status": "hubspot_http_error",
            "customer_id": normalized_customer_id,
            "message": f"HubSpot request failed: {exc}",
        }
    except requests.RequestException as exc:
        return {
            "status": "hubspot_request_error",
            "customer_id": normalized_customer_id,
            "message": f"HubSpot request failed: {exc}",
        }
    except ValueError as exc:
        return {
            "status": "hubspot_parse_error",
            "customer_id": normalized_customer_id,
            "message": f"HubSpot response could not be parsed: {exc}",
        }

    salesperson_name = _extract_salesperson_name(payload)
    if not salesperson_name:
        return {
            "status": "not_found",
            "customer_id": normalized_customer_id,
            "message": "No sales rep was found for that Customer ID.",
        }

    contact_entry = _match_contact_entry(salesperson_name, _load_contacts())
    return {
        "status": "success",
        "customer_id": normalized_customer_id,
        "salesperson_name": salesperson_name,
        "phone": contact_entry.get("phone") if contact_entry else None,
        "email": contact_entry.get("email") if contact_entry else None,
        "matched_contact_name": contact_entry.get("name") if contact_entry else None,
    }


async def lookup_sales_rep(customer_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(lookup_sales_rep_sync, customer_id)


def customer_exists_in_hubspot_sync(customer_id: str) -> bool:
    """Return True if customer_id matches any company in HubSpot."""
    normalized = (customer_id or "").strip()
    if not normalized:
        return False
    token = _hubspot_access_token()
    if not token:
        return False
    try:
        payload = _fetch_company_search_results(normalized, token)
        return bool(payload.get("results"))
    except Exception:
        return False


async def customer_exists_in_hubspot(customer_id: str) -> bool:
    return await asyncio.to_thread(customer_exists_in_hubspot_sync, customer_id)


async def lookup_sales_rep_by_name(salesperson_name: str) -> dict[str, Any]:
    """Resolve a sales rep from a (possibly fuzzy) name the user supplied.

    Deterministic fuzzy match against the directory (SequenceMatcher ratio).
    Returns one of:
      - status="success"        single confident match (with phone/email)
      - status="not_found_name" no plausible match in the directory
    """
    query = (salesperson_name or "").strip()
    if not query:
        return {
            "status": "missing_name",
            "message": "A sales rep name is required for a name lookup.",
        }

    contacts = await asyncio.to_thread(_load_contacts)
    entry = _match_contact_entry(query, contacts)
    if not entry:
        return {
            "status": "not_found_name",
            "query_name": query,
            "message": "No sales rep in the directory matched that name.",
        }
    return {
        "status": "success",
        "query_name": query,
        "salesperson_name": entry.get("name"),
        "phone": entry.get("phone") or None,
        "email": entry.get("email") or None,
        "matched_contact_name": entry.get("name"),
        "lookup_by": "name",
    }


def _hubspot_access_token() -> str:
    for key in ("HUBSPOT_ACCESS_TOKEN", "hubspot_access_token"):
        value = (os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _fetch_company_search_results(customer_id: str, token: str) -> dict[str, Any]:
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "baccount_acctcd",
                        "operator": "EQ",
                        "value": customer_id,
                    }
                ]
            }
        ],
        "limit": 1,
        "properties": ["salesperson_name"],
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        HUBSPOT_COMPANY_SEARCH_URL,
        json=payload,
        headers=headers,
        timeout=_REQUEST_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


def _extract_salesperson_name(payload: dict[str, Any]) -> str | None:
    for result in payload.get("results") or []:
        properties = result.get("properties") or {}
        salesperson_name = str(properties.get("salesperson_name") or "").strip()
        if salesperson_name:
            return salesperson_name
    return None


def _load_contacts() -> list[dict[str, str]]:
    global _CONTACTS_CACHE
    if _CONTACTS_CACHE is not None:
        return _CONTACTS_CACHE

    entries = _load_blob_json(_contacts_blob_name(), _contacts_blob_url())
    if entries is None:
        entries = _load_local_json(_CONTACTS_PATH)

    if not entries:
        print("contacts.json unavailable; falling back to phonebook.json (phone numbers only).")
        entries = _load_blob_json(_phonebook_blob_name(), _phonebook_blob_url())
        if entries is None:
            entries = _load_local_json(_PHONEBOOK_PATH)

    _CONTACTS_CACHE = entries or []
    return _CONTACTS_CACHE


def _load_blob_json(blob_name: str, blob_url: str) -> list[dict[str, str]] | None:
    try:
        blob_text = _download_blob_text_via_sdk(blob_name)
        if not blob_text and blob_url:
            with urlopen(blob_url, timeout=_CONTACTS_BLOB_TIMEOUT_S) as response:
                blob_text = response.read().decode("utf-8").strip()
        if not blob_text:
            return None
        raw = json.loads(blob_text)
        return [entry for entry in raw if isinstance(entry, dict)]
    except Exception as exc:
        print(f"blob load for {blob_name!r} failed: {exc}")
        return None


def _load_local_json(path: Path) -> list[dict[str, str]] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        return [entry for entry in raw if isinstance(entry, dict)]
    except Exception as exc:
        print(f"local contact file {path.name} load failed: {exc}")
        return None


def _download_blob_text_via_sdk(blob_name: str) -> str | None:
    connection_string = _blob_connection_string()
    container_name = _blob_container_name()
    if not (connection_string and container_name and blob_name):
        return None

    try:
        from azure.storage.blob import BlobServiceClient
    except Exception:
        return None

    client = BlobServiceClient.from_connection_string(connection_string)
    blob_client = client.get_blob_client(container=container_name, blob=blob_name)
    data = blob_client.download_blob().readall()
    return data.decode("utf-8").strip()


def _blob_connection_string() -> str:
    return (
        (os.getenv("CONTACTS_BLOB_CONNECTION_STRING") or "").strip()
        or (os.getenv("PHONEBOOK_BLOB_CONNECTION_STRING") or "").strip()
        or (os.getenv("PASS1_KB_BLOB_CONNECTION_STRING") or "").strip()
        or (os.getenv("BLOB_CONNECTION_STRING") or "").strip()
    )


def _blob_container_name() -> str:
    return (
        (os.getenv("CONTACTS_BLOB_CONTAINER") or "").strip()
        or (os.getenv("PHONEBOOK_BLOB_CONTAINER") or "").strip()
        or (os.getenv("PASS1_KB_BLOB_CONTAINER") or "").strip()
    )


def _contacts_blob_name() -> str:
    return (
        (os.getenv("CONTACTS_BLOB_NAME") or "").strip()
        or (os.getenv("CONTACTS_KB_BLOB_NAME") or "").strip()
        or (os.getenv("contacts_kb") or "").strip()
        or _DEFAULT_CONTACTS_BLOB_NAME
    )


def _contacts_blob_url() -> str:
    return (
        (os.getenv("CONTACTS_BLOB_URL") or "").strip()
        or (os.getenv("CONTACTS_KB_BLOB_URL") or "").strip()
    )


def _phonebook_blob_name() -> str:
    return (
        (os.getenv("PHONEBOOK_BLOB_NAME") or "").strip()
        or (os.getenv("PHONEBOOK_KB_BLOB_NAME") or "").strip()
        or (os.getenv("PHONEBOOK_KB") or "").strip()
        or (os.getenv("phonebook_kb") or "").strip()
        or "phonebook.json"
    )


def _phonebook_blob_url() -> str:
    return (
        (os.getenv("PHONEBOOK_BLOB_URL") or "").strip()
        or (os.getenv("PHONEBOOK_KB_BLOB_URL") or "").strip()
    )


def _match_contact_entry(
    salesperson_name: str,
    contacts: list[dict[str, str]],
) -> dict[str, str] | None:
    target = _normalize_name(salesperson_name)
    if not target:
        return None

    exact_match = next(
        (
            entry
            for entry in contacts
            if _normalize_name(str(entry.get("name") or "")) == target
        ),
        None,
    )
    if exact_match:
        return exact_match

    best_match = None
    best_score = 0.0
    for entry in contacts:
        candidate_name = _normalize_name(str(entry.get("name") or ""))
        if not candidate_name:
            continue
        score = SequenceMatcher(None, target, candidate_name).ratio()
        if score > best_score:
            best_score = score
            best_match = entry
    return best_match if best_score >= 0.86 else None


def _normalize_name(value: str) -> str:
    lowered = (value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "", lowered)
