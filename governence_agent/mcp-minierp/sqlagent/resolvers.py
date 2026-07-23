"""Secondary (resolver / entry-point) tools -- turn a human handle into the
canonical keys the primary tools need, and join an order to its addresses.

These are the tools an internal user actually starts from: they rarely know a
customer's internal id, but they know a name, an email, a phone, or an order
number. Ported from the proven generic contact->baccount path in
src/sqlAgent (get_customer_orders_data_helper / get_order_address_details),
onto the shared async minierp_core client.

All output is flat, key-named JSON so the gateway's redaction plan can mask PII
by field name (name/email/phone, street/postalCode) -- see policy/manifest.py.
Resolvers are NOT account-scoped: they are how you FIND a customer_id, so they
cannot require one. They are the enumeration surface, so the gateway meters +
audits them per principal.
"""
from __future__ import annotations

from minierp_core import (
    find_with_offset_pagination,
    get_order_address_data_by_order_number,
)

# Reuse the shared result wrapper (json.dumps with {"source": "miniERP", ...}).
from sqlagent.orders.index import _json_tool_result

_VALID_COMPANIES = {2, 11}


def _companies(company_id) -> list[int]:
    """Company scope for a resolver: explicit input wins, default BOTH companies
    (an internal desk searches across Focus + Frontier), validated to {2,11}."""
    if company_id in (None, "", []):
        return [2, 11]
    raw = company_id if isinstance(company_id, (list, tuple, set)) else [company_id]
    out: list[int] = []
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n in _VALID_COMPANIES:
            out.append(n)
    return out or [2, 11]


def _infer_by(q: str) -> str:
    s = q.strip()
    if "@" in s:
        return "email"
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 7 and len(digits) >= len(s.replace(" ", "").replace("-", "")) - 2:
        return "phone"
    return "name"


def _join_lines(*parts) -> str | None:
    vals = [str(p).strip() for p in parts if p not in (None, "")]
    return ", ".join(vals) if vals else None


async def find_customer(query: str, by: str = "auto", company_id=None,
                        page: int = 1, page_size: int = 10) -> str:
    """Resolve a name / email / phone / acctCd to candidate customers."""
    q = (query or "").strip()
    if not q:
        return _json_tool_result(
            status="missing_identifier", intent="find_customer",
            message="A search query (name, email, phone, or customer id) is required.",
            missingFields=["query"],
        )
    mode = _infer_by(q) if by in (None, "", "auto") else by
    companies = _companies(company_id)

    # acctCd path: straight to the business account.
    if mode == "acctCd":
        res = await find_with_offset_pagination("baccount", {
            "select": {"bAccountId": True, "acctCd": True, "acctName": True,
                       "status": True, "primaryContactId": True},
            "where": {"acctCd": q, "companyId": {"in": companies}},
            "page": page, "pageSize": page_size,
        })
        cands = [{
            "customerId": b.get("acctCd"), "bAccountId": b.get("bAccountId"),
            "name": b.get("acctName"), "email": None, "phone": None,
            "companyId": None, "status": b.get("status"),
        } for b in res.get("items", [])]
        status = "ok" if cands else "not_found"
        return _json_tool_result(status=status, intent="find_customer",
                                 candidates=cands, count=len(cands))

    # name / email / phone -> contact -> business account.
    where = {"companyId": {"in": companies}}
    if mode == "email":
        where["eMail"] = q
    elif mode == "phone":
        where["phone1"] = q
    else:
        where["fullName"] = {"contains": q}

    contacts = await find_with_offset_pagination("contact", {
        "select": {"bAccountId": True, "companyId": True, "fullName": True,
                   "eMail": True, "phone1": True, "contactId": True},
        "where": where, "page": page, "pageSize": page_size,
    })
    citems = contacts.get("items", [])
    if not citems:
        return _json_tool_result(status="not_found", intent="find_customer",
                                 candidates=[], count=0,
                                 message="No customer matched that search.")

    baccount_ids = sorted({c.get("bAccountId") for c in citems if c.get("bAccountId")})
    bmap: dict = {}
    if baccount_ids:
        bres = await find_with_offset_pagination("baccount", {
            "select": {"bAccountId": True, "acctCd": True, "acctName": True, "status": True},
            "where": {"bAccountId": {"in": baccount_ids}, "companyId": {"in": companies}},
            "page": 1, "pageSize": max(len(baccount_ids), 10),
        })
        for b in bres.get("items", []):
            bmap[b.get("bAccountId")] = b

    cands = []
    for c in citems:
        b = bmap.get(c.get("bAccountId"), {})
        cands.append({
            "customerId": b.get("acctCd"),
            "bAccountId": c.get("bAccountId"),
            "name": c.get("fullName") or b.get("acctName"),
            "email": c.get("eMail"),
            "phone": c.get("phone1"),
            "companyId": c.get("companyId"),
            "status": b.get("status"),
        })
    return _json_tool_result(status="ok", intent="find_customer",
                             candidates=cands, count=len(cands))


async def resolve_contact(query: str, by: str = "auto",
                          page: int = 1, page_size: int = 10) -> str:
    """Resolve an email / phone / name to the people (contacts) behind it."""
    q = (query or "").strip()
    if not q:
        return _json_tool_result(
            status="missing_identifier", intent="resolve_contact",
            message="A search query (email, phone, or name) is required.",
            missingFields=["query"],
        )
    mode = _infer_by(q) if by in (None, "", "auto") else by
    where = {"companyId": {"in": [2, 11]}}
    if mode == "email":
        where["eMail"] = q
    elif mode == "phone":
        where["phone1"] = q
    else:
        where["fullName"] = {"contains": q}

    res = await find_with_offset_pagination("contact", {
        "select": {"contactId": True, "bAccountId": True, "companyId": True,
                   "fullName": True, "title": True, "eMail": True, "phone1": True},
        "where": where, "page": page, "pageSize": page_size,
    })
    people = [{
        "contactId": c.get("contactId"), "bAccountId": c.get("bAccountId"),
        "companyId": c.get("companyId"), "name": c.get("fullName"),
        "title": c.get("title"), "email": c.get("eMail"), "phone": c.get("phone1"),
    } for c in res.get("items", [])]
    status = "ok" if people else "not_found"
    return _json_tool_result(status=status, intent="resolve_contact",
                             contacts=people, count=len(people))


async def get_order_addresses(order_number: str) -> str:
    """Get the bill-to / ship-to addresses for a sales order by order number."""
    on = (order_number or "").strip()
    if not on:
        return _json_tool_result(
            status="missing_identifier", intent="order_addresses",
            message="An order_number is required.", missingFields=["order_number"],
        )
    res = await get_order_address_data_by_order_number(on)
    items = res.get("items", []) if isinstance(res, dict) else []
    if not items:
        return _json_tool_result(status="not_found", intent="order_addresses",
                                 addresses=[], message="No address found for that order.")
    addresses = []
    for it in items:
        addresses.append({
            "orderNumber": on,
            "shipping": {
                "street": _join_lines(it.get("shipAddressLine1"), it.get("shipAddressLine2"), it.get("shipAddressLine3")),
                "city": it.get("shipCity"), "state": it.get("shipState"),
                "postalCode": it.get("shipPostalCode"), "country": it.get("shipCountryId"),
            },
            "billing": {
                "street": _join_lines(it.get("billAddressLine1"), it.get("billAddressLine2"), it.get("billAddressLine3")),
                "city": it.get("billCity"), "state": it.get("billState"),
                "postalCode": it.get("billPostalCode"), "country": it.get("billCountryId"),
            },
        })
    return _json_tool_result(status="ok", intent="order_addresses",
                             addresses=addresses, count=len(addresses))
