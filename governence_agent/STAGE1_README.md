# Stage 1 — Governance Gateway + miniERP domain backends

Implements the federating-gateway design (see `design_plan.md`). All callers talk
MCP to the **gateway**; the gateway authenticates them, runs the deterministic
PDP, resolves session scope, calls the owning **backend** over MCP, redacts the
result per requester, and audits.

As of 2026-07 the miniERP tool surface is split into four domain backends
instead of one — a backend is a *data domain* (orders, accounts, shipments,
finance), not a department's private server; multiple departments (finance,
sales, customer_service, shipping, ...) grant themselves tools across several
of these backends (see `governance_core/policy/departments.py`).

```
caller (MCP client, per-consumer key)
        │  Streamable HTTP  →  gateway/              :8020  /mcp   (PEP: edge auth, PDP, redaction, audit)
        │                          │  MCP client
        │                          ├─▶ mcp-minierp-orders/    :8021 /mcp  (SOOrder/SOLine/InventoryItem)
        │                          ├─▶ mcp-minierp-finance/   :8022 /mcp  (AR/AP/GL/PO/Vendor)
        │                          ├─▶ mcp-minierp-accounts/  :8023 /mcp  (BAccount/Customer/Contact/Address)
        │                          └─▶ mcp-minierp-shipments/ :8024 /mcp  (shipment/tracking lookups)
        │                                     │
        │                                     ▼
        │                          db-api.frontierdental.com (GraphQL)
```

## Layout

| Path | What it is |
|---|---|
| `governance_core/` | Shared plane: `edge.py` (Layer-1 auth/rate/IP/size), `audit.py`, `request_context.py`, `scope_store.py`, and `policy/` (PDP). |
| `governance_core/policy/` | `manifest.py` (tool → backend/scope/field-classifications), `entitlements.py` (consumer → allowed levels), `departments.py` (department → backend/tool/level grants), `decision.py` (`decide()`), `redaction.py` (apply plan). |
| `gateway/` | `app.py` (FastMCP PEP + namespaced `minierp_<domain>_*` tools + `_govern` pipeline), `backends.py` (MCP client pool), `static/dashboard.html`. |
| `mcp-minierp-orders/` | Sales orders, order lines, order totals (`SOOrder`/`SOLine`/`InventoryItem`), plus the BAccount resolution every account-scoped tool needs. |
| `mcp-minierp-accounts/` | Contacts, addresses, customer billing profile (`BAccount`/`Customer`/`Contact`/`Address`). |
| `mcp-minierp-shipments/` | Shipment/tracking lookups by order or shipment number — duplicates a minimal slice of order-header/BAccount resolution (needed for ownership checks + status enrichment), same pattern as `mcp-minierp-finance`'s own BAccount resolution. |
| `mcp-minierp-finance/` | AP/AR invoices, vendors, PO headers, GL account transactions (`APInvoice`/`ARInvoice`/`Vendor`/`POOrder`/`Account`/`GLTran`). |
| `_smoke/` | `test_policy.py` (offline PDP+redaction), `mock_minierp.py` + `e2e_client.py` (full E2E — predates the domain split; tool names there need updating to match). |

## Run locally

```powershell
# one-time
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r gateway\requirements.txt

# backends (real miniERP): copy each <backend>\.env.local.example -> .env.local, fill MINIERP_USERNAME/PASSWORD
cd mcp-minierp-orders;    ..\.venv\Scripts\python.exe app.py   # :8021
cd mcp-minierp-finance;   ..\.venv\Scripts\python.exe app.py   # :8022
cd mcp-minierp-accounts;  ..\.venv\Scripts\python.exe app.py   # :8023
cd mcp-minierp-shipments; ..\.venv\Scripts\python.exe app.py   # :8024

# gateway: copy gateway\.env.local.example -> .env.local (dev consumer keys are prefilled)
cd gateway;      ..\.venv\Scripts\python.exe app.py      # :8020, dashboard at /dashboard
```

## Verify

```powershell
.\.venv\Scripts\python.exe _smoke\test_policy.py          # offline checks, no servers needed -- tool names need updating post-split

# full E2E against a mock backend (no miniERP creds needed):
$env:MINIERP_ORDERS_PORT="8021"; $env:MINIERP_ORDERS_ENV_FILE="NUL"
Start-Process .venv\Scripts\python.exe _smoke\mock_minierp.py
# then start the gateway with the dev keys and:
.\.venv\Scripts\python.exe _smoke\e2e_client.py
```

`_smoke/` predates the 2026-07 domain split and still references the single
`minierp` backend and un-namespaced-by-domain tool names — needs an update pass
before it's trustworthy again; not yet done.

## Entitlements (Stage 1, in `policy/entitlements.py`)

| Consumer | PUBLIC | INTERNAL | PII (name/email/phone/street) | SENSITIVE (totals) |
|---|:-:|:-:|:-:|:-:|
| `chatbot` | ✓ | ✓ | ✓ | ✓ |
| `email_bot` | ✓ | ✓ | ✓ | — (dropped) |
| `analytics` | ✓ | ✓ | — (masked) | ✓ |
| `finance` | ✓ | ✓ | — | ✓ |
| unknown / `legacy` | ✓ | ✓ | — | — |

Consumer identity = the `GOVERNANCE_KEY_<NAME>` that authenticated the call, or
(once a principal has a `department`) the department's grant in
`policy/departments.py`, which is the more specific and more commonly-used path.

## Known follow-ups (not Stage 1)

- **Chatbot migration.** The chatbot still points at the old `governance_service`
  and uses the un-namespaced tool names / old text result shapes. Re-point it at
  the gateway and update tool names to the new namespaced form (e.g.
  `minierp_orders_get_customer_orders`) + parse structured JSON.
- **Order-number/invoice-number/PO-number tools aren't ownership-gated.**
  `get_order_details`, `get_product_details_in_order`, `get_shipping_by_order`,
  `get_invoice_details`, `get_ap_invoice_details`, `get_po_order_status` are
  non-account-scoped — anyone who knows the number sees it. This matches the
  *current* governance_service behavior for the order-number tools; tightening
  it is a deliberate later decision, not a silent change.
- **`get_invoice_details` has no ownership check by construction, not by
  choice.** `ARInvoice` has no customer/bAccount link field in this schema at
  all (confirmed by probe: `customerId`/`bAccountId`/`customerID`/`custId` all
  rejected as invalid columns) — there is no customer-scoped AR-balance rollup
  tool for this reason.
- **CRM/marketing backend not built.** `Case`/`CROpportunity`/`CRLead`/
  `CRActivity` are confirmed `FORBIDDEN` under every credential probed so far
  (including an `administrator`-level account) — needs an access grant from
  whoever administers `db-api.frontierdental.com` before there's anything real
  to build `mcp-minierp-crm` against.
- **In-memory state.** `scope_store`, `edge` rate limits, and the `audit` ring
  buffer are per-instance (POC). Move to Redis + a durable audit sink before >1 replica.
- **Prod networking.** Backends must be reachable only from the gateway (VNet /
  private endpoint / mTLS) — see `design_plan.md` §10.
- `governance_service/` is retained until the chatbot cuts over, then deleted.
