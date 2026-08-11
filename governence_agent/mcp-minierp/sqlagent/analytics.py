"""Quaternary tools -- aggregations (client-side rollups; the API has no
aggregate function) and reverse lookups (child -> parent).

Query the raw tables directly via minierp_core (like resolvers.py), using the
raw column names from the miniERP schema:
  soorder: orderNbr, orderDate, orderTotal, customerId(=bAccountId), status, companyId
  soline:  orderNbr, inventoryId, tranDesc, shippedQty, extPrice, customerId, orderDate
  address: bAccountId, city, state, countryId, postalCode, companyId
  baccount: bAccountId, acctCd, acctName, status

Aggregations are bounded by a page cap and report `truncated` when they hit it
(no silent truncation). Every SENSITIVE/PII output key is classified in
policy/manifest.py so the gateway redacts it per grant.
"""
from __future__ import annotations

from minierp_core import find_with_offset_pagination
from sqlagent.orders.index import _company_ids, _json_tool_result
from sqlagent.accounts.index import _gql_baccount_ids_for_acct_cd

_AGG_PAGE_SIZE = 250
_MAX_AGG_PAGES = 20  # cap ~5000 rows/aggregation


def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _month(date_str) -> str | None:
    s = str(date_str or "")
    return s[:7] if len(s) >= 7 else None


async def _paginate(table: str, where: dict, select: dict, *, order_by: dict | None = None,
                    page_size: int = _AGG_PAGE_SIZE, max_pages: int = _MAX_AGG_PAGES):
    """Yield rows across pages up to max_pages. Returns (rows, truncated)."""
    rows: list[dict] = []
    page = 1
    truncated = False
    while True:
        opts = {"select": select, "where": where, "page": page, "pageSize": page_size}
        if order_by:
            opts["orderBy"] = order_by
        result = await find_with_offset_pagination(table, opts)
        items = result.get("items") or []
        rows.extend(items)
        if not result.get("hasMore"):
            break
        page += 1
        if page > max_pages:
            truncated = True
            break
    return rows, truncated


# ── Aggregations ──────────────────────────────────────────────────────────────

async def get_customer_order_summary(customer_id: str, start_date: str = "", end_date: str = "") -> str:
    """Order counts by status, total/average spend, first/last order, month buckets."""
    acct = (customer_id or "").strip()
    if not acct:
        return _json_tool_result(status="missing_identifier", intent="customer_order_summary",
                                 message="A customer_id is required.", missingFields=["customer_id"])
    baccount_ids = await _gql_baccount_ids_for_acct_cd(acct, None)
    if not baccount_ids:
        return _json_tool_result(status="not_found", intent="customer_order_summary",
                                 message=f"No customer account found for {acct}.", customerId=acct)
    where: dict = {"customerId": {"in": baccount_ids}, "companyId": {"in": _company_ids(None)}}
    if start_date:
        where["orderDate"] = {**where.get("orderDate", {}), "gte": start_date}
    if end_date:
        where["orderDate"] = {**where.get("orderDate", {}), "lte": end_date}
    rows, truncated = await _paginate(
        "soorder", where, {"orderNbr": True, "orderDate": True, "orderTotal": True, "status": True})

    by_status: dict[str, int] = {}
    monthly: dict[str, dict] = {}
    grand = 0.0
    dates = []
    for r in rows:
        st = str(r.get("status") or "?")
        by_status[st] = by_status.get(st, 0) + 1
        amt = _f(r.get("orderTotal"))
        grand += amt
        d = r.get("orderDate")
        if d:
            dates.append(str(d))
        mk = _month(d)
        if mk:
            m = monthly.setdefault(mk, {"month": mk, "orderCount": 0, "total": 0.0})
            m["orderCount"] += 1
            m["total"] += amt
    n = len(rows)
    return _json_tool_result(
        status="ok", intent="customer_order_summary", customerId=acct,
        orderCount=n, grandTotal=round(grand, 2),
        averageOrderValue=round(grand / n, 2) if n else 0.0,
        byStatus=by_status,
        firstOrderDate=min(dates) if dates else None,
        lastOrderDate=max(dates) if dates else None,
        monthly=[monthly[k] for k in sorted(monthly)],
        truncated=truncated,
    )


async def get_product_sales(inventory_id: str, start_date: str = "", end_date: str = "") -> str:
    """How a product is selling: total quantity, revenue, distinct orders/customers."""
    inv = (inventory_id or "").strip()
    if not inv:
        return _json_tool_result(status="missing_identifier", intent="product_sales",
                                 message="An inventory_id is required.", missingFields=["inventory_id"])
    where: dict = {"inventoryId": inv, "companyId": {"in": _company_ids(None)}}
    if start_date:
        where["orderDate"] = {**where.get("orderDate", {}), "gte": start_date}
    if end_date:
        where["orderDate"] = {**where.get("orderDate", {}), "lte": end_date}
    rows, truncated = await _paginate(
        "soline", where, {"inventoryId": True, "tranDesc": True, "shippedQty": True,
                          "extPrice": True, "customerId": True, "orderNbr": True})
    qty = sum(_f(r.get("shippedQty")) for r in rows)
    revenue = sum(_f(r.get("extPrice")) for r in rows)
    name = next((r.get("tranDesc") for r in rows if r.get("tranDesc")), None)
    return _json_tool_result(
        status="ok" if rows else "not_found", intent="product_sales",
        inventoryId=inv, productName=name,
        totalQuantity=round(qty, 2), totalRevenue=round(revenue, 2),
        orderCount=len({r.get("orderNbr") for r in rows}),
        customerCount=len({r.get("customerId") for r in rows}),
        lineCount=len(rows), truncated=truncated,
    )


# ── Reverse lookups ─────────────────────────────────────────────────────────

async def get_orders_by_product(inventory_id: str, page: int = 1, page_size: int = 25) -> str:
    """Which orders/customers bought a product (reverse of product-in-order)."""
    inv = (inventory_id or "").strip()
    if not inv:
        return _json_tool_result(status="missing_identifier", intent="orders_by_product",
                                 message="An inventory_id is required.", missingFields=["inventory_id"])
    result = await find_with_offset_pagination("soline", {
        "select": {"orderNbr": True, "customerId": True, "shippedQty": True, "extPrice": True, "orderDate": True},
        "where": {"inventoryId": inv, "companyId": {"in": _company_ids(None)}},
        "page": max(1, page), "pageSize": min(200, max(1, page_size)),
    })
    items = result.get("items") or []
    rows = [{"orderNumber": r.get("orderNbr"), "customerId": r.get("customerId"),
             "quantity": r.get("shippedQty"), "lineTotal": r.get("extPrice"),
             "date": r.get("orderDate")} for r in items]
    return _json_tool_result(status="ok" if rows else "not_found", intent="orders_by_product",
                             inventoryId=inv, rows=rows, count=len(rows),
                             hasMore=bool(result.get("hasMore")))


async def get_customers_by_region(country: str = "", state: str = "", city: str = "",
                                  page: int = 1, page_size: int = 25) -> str:
    """Customers in a territory, by country / state / city (reverse via address)."""
    where: dict = {"companyId": {"in": _company_ids(None)}}
    if country.strip():
        where["countryId"] = country.strip()
    if state.strip():
        where["state"] = state.strip()
    if city.strip():
        where["city"] = {"contains": city.strip()}
    if len(where) == 1:
        return _json_tool_result(status="missing_identifier", intent="customers_by_region",
                                 message="Provide at least one of country, state, or city.",
                                 missingFields=["country", "state", "city"])
    addr = await find_with_offset_pagination("address", {
        "select": {"bAccountId": True, "city": True, "state": True, "countryId": True},
        "where": where, "page": max(1, page), "pageSize": min(200, max(1, page_size))})
    items = addr.get("items") or []
    baccount_ids = sorted({r.get("bAccountId") for r in items if r.get("bAccountId")})
    bmap: dict = {}
    if baccount_ids:
        bres = await find_with_offset_pagination("baccount", {
            "select": {"bAccountId": True, "acctCd": True, "acctName": True, "status": True},
            "where": {"bAccountId": {"in": baccount_ids}, "companyId": {"in": _company_ids(None)}},
            "page": 1, "pageSize": max(len(baccount_ids), 10)})
        for b in bres.get("items", []):
            bmap[b.get("bAccountId")] = b
    seen = set()
    customers = []
    for r in items:
        b = bmap.get(r.get("bAccountId"))
        if not b or b.get("bAccountId") in seen:
            continue
        seen.add(b.get("bAccountId"))
        customers.append({"customerId": b.get("acctCd"), "name": b.get("acctName"),
                          "status": b.get("status"), "city": r.get("city"),
                          "state": r.get("state"), "country": r.get("countryId")})
    return _json_tool_result(status="ok" if customers else "not_found", intent="customers_by_region",
                             customers=customers, count=len(customers), hasMore=bool(addr.get("hasMore")))


# ── Cross-customer analytics (GATED: analytics category only) ─────────────────

async def get_top_customers_by_spend(start_date: str = "", end_date: str = "", limit: int = 10) -> str:
    """Rank customers by total spend over a period. Cross-customer — gated to the
    analytics entitlement (see the analytics category / minierp_analytics backend)."""
    where: dict = {"companyId": {"in": _company_ids(None)}}
    if start_date:
        where["orderDate"] = {**where.get("orderDate", {}), "gte": start_date}
    if end_date:
        where["orderDate"] = {**where.get("orderDate", {}), "lte": end_date}
    rows, truncated = await _paginate(
        "soorder", where, {"customerId": True, "orderTotal": True})
    spend: dict = {}
    for r in rows:
        cid = r.get("customerId")
        if cid is None:
            continue
        agg = spend.setdefault(cid, {"orderCount": 0, "total": 0.0})
        agg["orderCount"] += 1
        agg["total"] += _f(r.get("orderTotal"))
    top = sorted(spend.items(), key=lambda kv: kv[1]["total"], reverse=True)[:max(1, min(100, limit))]
    baccount_ids = [cid for cid, _ in top]
    bmap: dict = {}
    if baccount_ids:
        bres = await find_with_offset_pagination("baccount", {
            "select": {"bAccountId": True, "acctCd": True, "acctName": True},
            "where": {"bAccountId": {"in": baccount_ids}, "companyId": {"in": _company_ids(None)}},
            "page": 1, "pageSize": max(len(baccount_ids), 10)})
        for b in bres.get("items", []):
            bmap[b.get("bAccountId")] = b
    ranked = [{
        "customerId": bmap.get(cid, {}).get("acctCd"),
        "name": bmap.get(cid, {}).get("acctName"),
        "totalSpend": round(agg["total"], 2),
        "orderCount": agg["orderCount"],
    } for cid, agg in top]
    return _json_tool_result(status="ok" if ranked else "not_found", intent="top_customers_by_spend",
                             topCustomers=ranked, count=len(ranked), truncated=truncated)


async def get_customer_order_recency(country: str = "", state: str = "", city: str = "",
                                     page: int = 1, page_size: int = 25) -> str:
    """Cross-customer order recency by territory -- order count and last-order date
    per customer (a win-back / reorder-due signal). Cross-customer -- gated to the
    analytics entitlement, same as get_top_customers_by_spend.

    Territory resolution is identical to get_customers_by_region (address ->
    baccount join); the per-customer rollup (count/total/first/last order date) is
    the same aggregation get_customer_order_summary applies to one account, just
    grouped across everyone matched on this page instead of filtered to one.

    country/state/city are normalized to "" before use (not just relying on the
    str="" default): a "My Workflow" graph binds each to its own trigger input,
    and a run that only supplies country resolves the other two via
    _resolve_binding to a real `None` (workflow_graph_interpreter.py's
    _resolve_binding returns run.inputs.get(path), which is None for an absent
    key) rather than falling back to this function's own default -- a bound-but-
    unfilled arg is a real None on the wire, not a missing kwarg."""
    country, state, city = country or "", state or "", city or ""
    where: dict = {"companyId": {"in": _company_ids(None)}}
    if country.strip():
        where["countryId"] = country.strip()
    if state.strip():
        where["state"] = state.strip()
    if city.strip():
        where["city"] = {"contains": city.strip()}
    if len(where) == 1:
        return _json_tool_result(status="missing_identifier", intent="customer_order_recency",
                                 message="Provide at least one of country, state, or city.",
                                 missingFields=["country", "state", "city"])
    addr = await find_with_offset_pagination("address", {
        "select": {"bAccountId": True, "city": True, "state": True, "countryId": True},
        "where": where, "page": max(1, page), "pageSize": min(200, max(1, page_size))})
    items = addr.get("items") or []
    baccount_ids = sorted({r.get("bAccountId") for r in items if r.get("bAccountId")})
    if not baccount_ids:
        return _json_tool_result(status="not_found", intent="customer_order_recency",
                                 customers=[], count=0, hasMore=False)

    bres = await find_with_offset_pagination("baccount", {
        "select": {"bAccountId": True, "acctCd": True, "acctName": True, "status": True},
        "where": {"bAccountId": {"in": baccount_ids}, "companyId": {"in": _company_ids(None)}},
        "page": 1, "pageSize": max(len(baccount_ids), 10)})
    bmap = {b.get("bAccountId"): b for b in bres.get("items", [])}

    # Phone: NOT on baccount itself -- it lives on Contact (phone1). The
    # "obvious" unambiguous path, baccount.primaryContactId, is confirmed by
    # live sampling to be essentially UNPOPULATED in this data (0/50 sampled
    # accounts had one set) -- so this joins directly on Contact.bAccountId
    # instead, one bulk query for every account on this page, not a call per
    # customer. Most accounts have MULTIPLE contact rows here (confirmed by
    # sampling: 25/27), sometimes with genuinely different phone numbers, and
    # nothing in this schema designates one as authoritative when
    # primaryContactId is absent -- silently picking one would be exactly the
    # kind of confident-but-wrong guess this project's governance design
    # explicitly avoids elsewhere (e.g. never inferring an ambiguous status
    # code's meaning). The one deterministic, defensible tie-break available
    # is contactId order (lowest = earliest-created contact on the account);
    # this is a best-effort "a" phone number for outreach, not a verified
    # "the customer's primary contact" -- described as such below and in the
    # tool's own manifest description.
    contact_res = await find_with_offset_pagination("contact", {
        "select": {"contactId": True, "bAccountId": True, "phone1": True},
        "where": {"bAccountId": {"in": baccount_ids}},
        "page": 1, "pageSize": max(len(baccount_ids) * 5, 50)})
    contacts_by_account: dict = {}
    for c in contact_res.get("items", []):
        contacts_by_account.setdefault(c.get("bAccountId"), []).append(c)
    phone_by_account: dict = {}
    for bid, contacts in contacts_by_account.items():
        with_phone = sorted((c for c in contacts if c.get("phone1")), key=lambda c: c.get("contactId") or 0)
        phone_by_account[bid] = with_phone[0]["phone1"] if with_phone else None

    rows, truncated = await _paginate(
        "soorder", {"customerId": {"in": baccount_ids}, "companyId": {"in": _company_ids(None)}},
        {"customerId": True, "orderDate": True, "orderTotal": True})
    agg: dict = {}
    for r in rows:
        cid = r.get("customerId")
        if cid is None:
            continue
        a = agg.setdefault(cid, {"orderCount": 0, "total": 0.0, "dates": []})
        a["orderCount"] += 1
        a["total"] += _f(r.get("orderTotal"))
        d = r.get("orderDate")
        if d:
            a["dates"].append(str(d))

    customers = []
    for bid in baccount_ids:
        b = bmap.get(bid)
        if not b:
            continue
        a = agg.get(bid, {"orderCount": 0, "total": 0.0, "dates": []})
        customers.append({
            "customerId": b.get("acctCd"), "name": b.get("acctName"), "status": b.get("status"),
            "orderCount": a["orderCount"], "grandTotal": round(a["total"], 2),
            "firstOrderDate": min(a["dates"]) if a["dates"] else None,
            "lastOrderDate": max(a["dates"]) if a["dates"] else None,
            # None whenever the account has no contact on file with a phone
            # number at all -- a real, expected outcome, not a bug. When it IS
            # set, it's the lowest-contactId contact that has a phone1, a
            # best-effort pick (see the note above this function's contact
            # lookup), not a verified "primary" contact.
            "phone": phone_by_account.get(bid),
        })
    return _json_tool_result(status="ok" if customers else "not_found", intent="customer_order_recency",
                             customers=customers, count=len(customers),
                             hasMore=bool(addr.get("hasMore")), truncated=truncated)
