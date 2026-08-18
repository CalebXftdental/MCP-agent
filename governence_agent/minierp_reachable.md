# miniERP Reachable-Table Survey — What's Actually Behind the Credential

**Status:** Research/reference, recording what's now built. This started as a
record of direct, read-only probes against production `db-api.frontierdental.com`
(2026-08-17) to answer "what else could a governed tool be built on" beyond the
33 tools live in `mcp-minierp/app.py` at the time. **Eight tools have since
shipped from this survey** (`mcp-minierp/app.py` is now at 38 tools):
`get_po_line_items`, `get_ar_payment_history`, `get_ap_payment_history` (landed
first, see `finalize_stage_1.md` §2), then `get_gl_period_summary`,
`get_invoice_line_items`, `get_bill_line_items`, `get_customer_invoice_history`,
`get_item_movement_history` (landed from §1-§2 of this doc). **CRM tools
(`CROpportunity`/`CRLead`/`CRActivity`, §0 below) were deliberately NOT built**
— explicit decision: leave CRM to `mcp-hubspot` in Stage 2 per `STAGE2_PLAN.md`,
even though §0 found them technically reachable in miniERP too. Everything else
below (Manufacturing, Projects, and the other still-`FORBIDDEN` tables) remains
unbuilt/unavailable.

**Method:** `minierp_core.find_with_offset_pagination(table, {page:1, pageSize:1})`
against each candidate Acumatica DAC name, sequential (not parallel) with a ~0.35s
gap between calls, read-only, no mutations. Two credential profiles exist behind
this same GraphQL endpoint: `default` (the `calsoft` account, used by orders/
accounts/shipments today) and `admin` (the `administrator` account, used by the
finance domain). **Every candidate below was probed under `admin` only**, because
every ambiguous table tested under both profiles in this and the prior session
(`SOShipment`, `SOShipLine`, `SOAdjust`, `INItemXRef`, `EPEmployee`, `SalesPerson`)
came back reachable under `admin` and forbidden under `default` — `admin` is
strictly the broader of the two on this tenant. **This means: nothing below is
confirmed reachable under `default`.** A tool built on any "OK" table below needs
the `admin` profile binding, the same wiring detail called out for the three
tools already shipped — not a new function dropped into the orders/shipments
modules as-is.

A table returning "OK" here means the credential can read it and the tenant has
real rows in it (every OK table below returned ≥1 non-null row — this is live
production data, not an empty-but-permitted table). It does **not** mean a
governed tool should be built on it without the same field-mapping/manifest-
classification rigor already applied to the three shipped tools — this doc is
the input to that decision, not a substitute for it.

---

## 0. Headline finding: the CRM "hard ceiling" doesn't hold for 3 of 4 entities

`STAGE1_README.md` and `finalize_stage_1.md` §0/§2 both state, as a locked
decision: *"`Case`/`CROpportunity`/`CRLead`/`CRActivity` are confirmed `FORBIDDEN`
under every credential probed."* That was true when last checked. **It is no
longer true for three of the four:**

| Table | Result | Notes |
|---|---|---|
| `Case` | **FORBIDDEN** (reconfirmed) | Matches every prior probe. |
| `CROpportunity` | **OK**, real data | 35 fields: `stageId`, `status`, `quotedAmount`, `totalAmount`, `closeDate`, `source`, `leadId`, `parentBAccountId`, ... — this is a live, populated Opportunity pipeline. |
| `CRLead` | **OK**, real data | 11 fields: `status`, `resolution`, `qualificationDate`, `refContactId`, `convertedBy`, ... |
| `CRActivity` | **OK**, real data | 40 fields: `type`, `subject`, `body`, `startDate`/`endDate`, `ownerId`, `priority`, `isPrivate`, ... — notes/calls/tasks logged against a contact or company. |

**Why this matters beyond miniERP:** `STAGE2_PLAN.md` §0/§5 justifies building
`mcp-hubspot` as a *new* backend partly on the premise that CRM data has no path
through miniERP at all. That premise is now only true for `Case`. Opportunities,
leads, and activity notes ARE reachable through the same `admin` credential
already in production use for finance.

**Decision (2026-08-17): build nothing on `CROpportunity`/`CRLead`/`CRActivity`
here — CRM stays `mcp-hubspot`'s territory in Stage 2, as originally planned.**
Explicitly chosen despite the technical reachability above; not implementing
against this finding. `STAGE1_README.md`/`finalize_stage_1.md`'s "CRM forbidden"
claim is still technically stale (it's only true for `Case` now), but the
practical outcome — CRM tools live in `mcp-hubspot`, not `mcp-minierp` — is
unchanged from the original plan. Worth fixing the stale wording in those two
docs at some point so a future reader doesn't rediscover this same contradiction
from scratch, but that's a documentation cleanup, not an open architecture
question anymore.

---

## 1. Newly confirmed reachable (admin profile), by module

### Inventory operations

| Table | Fields (count) | What it unlocks |
|---|---|---|
| `INTran` | 112 | **Inventory transaction history** — every receipt/issue/adjustment/transfer, with `inventoryId`, `lotSerialNbr`, `expireDate`, `qty`, `tranType`, `tranDate`, `siteId`/`locationId`, linked back to `soOrderNbr`/`poReceiptNbr`. See §2 — this is the closest thing to real lot/expiry visibility available given `INLotSerStatus` is forbidden. **Shipped as `get_item_movement_history`** (framed as transaction history, not live lot status — see the tool's own docstring). |
| `INSite` | 62 | Warehouse/site master — `siteCd`, `descr`, `active`, address/contact link. Answers "what warehouses do we have." |
| `INLocation` | 35 | Bin/location master within a site — `locationCd`, `descr`, `pickPriority`, `active`. |
| `INItemClass` | 62 | Item class master — `itemClassCd`, `descr`, `lotSerClassId`, `stkItem`, `valMethod`, pricing/planning defaults shared across items in the class. |
| `INUnit` | 18 | UOM conversion table — `fromUnit`/`toUnit`/`unitMultDiv`/`unitRate` per item. Answers "how many EA in a CS" type questions. Not built yet. |

**Confirmed still forbidden:** `INAdjustment`, `INTransfer`, `INPostClass`,
`INKitSpecStkDet`, plus the previously-confirmed `INSiteStatus`,
`INItemXWarehouse`, `INLotSerStatus`, `INLotSerClass`.

### Sales fulfillment / billing

| Table | Fields (count) | What it unlocks |
|---|---|---|
| `SOTaxTran` | 40 | Tax breakdown per sales order line — `taxId`, `taxRate`, `taxableAmt`, `taxAmt`, by `orderNbr`/`lineNbr`. |
| `SOInvoice` | 28 | **The sales-order-to-AR-invoice bridge.** Carries `customerId`, `soOrderNbr`, `refNbr`, `paymentMethodId`, `curyPaymentAmt` in one row. See §2 — this fixes the documented "`ARInvoice` has no customer link" limitation. **Shipped as `get_customer_invoice_history`.** |

**Confirmed still forbidden:** `SOOrderType`, `SOBillingHistory`, `SOPickList`,
`Carrier`.

### Purchasing detail

| Table | Fields (count) | What it unlocks |
|---|---|---|
| `POReceiptLine` | 107 | Line-level PO receipt detail — `receiptNbr`, `receiptQty`, `lotSerialNbr`, `expireDate`, `unitCost`, linked to `poNbr`/`poLineNbr`. Note: `POReceipt` (the header) is still forbidden per the finance module's existing note — this is the line table only, an unusual split. |

**Confirmed still forbidden:** `SOOrderType`, `POOrderType`, `VendorPriceClass`,
`VendorPrice`, `POAccrualHist` (and the previously-confirmed `APBill`,
`VendorClass`, `POReceipt` header).

### AR/AP transaction detail + reference data

| Table | Fields (count) | What it unlocks |
|---|---|---|
| `ARTran` | 179 | **Line-level AR invoice detail** — every billed line, with `inventoryId`, `qty`, `unitPrice`, `extPrice`, `taxCategoryId`, `projectId`, and critically **`salesPersonId`** directly on the row. See §2 — a real path to sales-rep/commission attribution. **Shipped as `get_invoice_line_items`.** |
| `APTran` | 130 | Line-level AP bill detail — mirrors `ARTran` for vendor bills, with `poLineNbr`/`poNbr` linkage back to purchasing. **Shipped as `get_bill_line_items`.** |
| `Terms` | 28 | Payment terms master — `termsId`, `descr`, due-date rules, discount terms. Decodes the `termsId` codes every invoice/vendor tool already returns. |
| `PaymentMethod` | 51 | Payment method master — decodes `paymentMethodId` codes (check, ACH, card, ...). |
| `TaxCategory` | 14 | Tax category master — decodes `taxCategoryId`. |

**Confirmed still forbidden:** `CashAccount`, `Tax`, `TaxZone`, `CustomerClass`.

### GL / company structure

| Table | Fields (count) | What it unlocks |
|---|---|---|
| `GLHistory` | 31 | **Real period-level GL balances** — `finBegBalance`, `finPtdDebit`/`finPtdCredit`, `finYtdBalance`, per `accountId`/`finPeriodId`/`ledgerId`. Directly answers the "GL period summary" gap from `finalize_stage_1.md` §2 — no client-side aggregation over `GLTran` needed. **Shipped as `get_gl_period_summary`.** |
| `FinPeriod` | 29 | Fiscal period master — `finPeriodId`, `startDate`/`endDate`, and per-module closed flags (`apClosed`, `arClosed`, `caClosed`, `inClosed`, `prClosed`). Answers "is this period still open." |
| `Branch` | 48 | Branch/org-unit master. |
| `Company` | 16 | Legal entity master — decodes `companyId` (2/11 today) into real company codes/names. |

### CRM (see §0 — re-opens a locked decision, not just "new tools")

| Table | Fields (count) | Notes |
|---|---|---|
| `CROpportunity` | 35 | Live opportunity pipeline. |
| `CRLead` | 11 | Live lead records. |
| `CRActivity` | 40 | Live activity/notes log. |

**Confirmed still forbidden:** `Case`.

### Confirmed still forbidden elsewhere probed this round

`EPEmployeeClass`, `AMBomItem`/`AMProdItem`/`AMProdOrder` (Manufacturing module —
notable given `InventoryItem`'s `am*`/`usrAS*` custom fields suggested MRP might
be configured; the actual module tables say otherwise, so treat those fields as
vestigial/unused customization, not a live capability), `PMProject`/`PMTask`
(Projects module not accessible either).

---

## 2. Three findings worth prioritizing over a plain "new table, new tool" read

These aren't just new tables — they fix or partially fix limitations the
existing, shipped code explicitly documents as hard schema limits:

1. **`SOInvoice` fixes "ARInvoice has no customer link."** `get_invoice_details`
   and `get_ar_invoices_past_due` both carry a docstring explaining that
   `ARInvoice` has no customer/bAccount field in this schema, so AR aging can't
   be attributed to a customer. `SOInvoice.customerId` + `SOInvoice.refNbr`
   (joins to `ARInvoice.refNbr`) closes that gap — a per-customer AR-aging
   digest becomes possible where it explicitly wasn't before.
2. **`INTran` is a partial answer to the lot/expiry dead end.** `INLotSerStatus`
   (real-time "what's the current status of lot X") is forbidden, confirmed
   twice now. But `INTran` carries `lotSerialNbr` + `expireDate` on every
   inventory movement — receipts, issues, transfers. That's "when was this lot
   received and what's its expiry," reconstructed from transaction history
   rather than a live status table. Weaker than real-time lot tracking (no
   "current qty remaining of this lot"), but a real, honest capability where
   before there was none — should be described to users as transaction history,
   not live lot status, to avoid the "confidently incorrect report" failure
   mode `get_ar_invoices_past_due`'s own docstring warns against.
3. **`ARTran.salesPersonId` is a real path to sales-rep attribution** without
   needing the previously-probed (and admin-only) `SalesPerson`/`EPEmployee`
   tables at all — every billed line already carries who gets credit for it.

---

## 3. What shipped from this survey, and what's still open

All five directly-mapped tools from the original suggestion list are now live
in `mcp-minierp/app.py` (38 tools total), each verified end-to-end against real
production data before landing:

- ✅ `get_gl_period_summary` — `GLHistory`, direct lookup.
- ✅ `get_invoice_line_items` — `ARTran` by `refNbr`, mirrors `get_po_line_items`.
- ✅ `get_bill_line_items` — `APTran` by `refNbr`, same shape.
- ✅ `get_customer_invoice_history` — `SOInvoice.customerId`, the fix in §2.1.
- ✅ `get_item_movement_history` — `INTran` by `inventoryId`, §2.2's lot/expiry
  transaction history, framed as history-not-live-status in its own docstring.

**Still open, not built:**
- `decode_terms` / `decode_payment_method` / `decode_tax_category` — thin
  lookups over `Terms`/`PaymentMethod`/`TaxCategory`. Not built because they're
  mostly useful as supporting joins for other tools' outputs, not clearly
  valuable as standalone user-facing tools — revisit if a real use case shows up.
- `get_item_details` / `get_warehouse_list` / `get_uom_conversions` over
  `InventoryItem`/`INSite`/`INLocation`/`INItemClass`/`INUnit` — reachable,
  not yet built, no specific request for them yet.
- CRM tools (`get_opportunity_summary`, `get_lead_status`, `get_recent_activity`)
  over `CROpportunity`/`CRLead`/`CRActivity` — **deliberately not built.**
  Decision (2026-08-17): CRM stays `mcp-hubspot`'s territory in Stage 2 per
  `STAGE2_PLAN.md`, despite §0's finding that these are technically reachable
  in miniERP too. Not a technical gap — a scope decision.

---

## 4. What this doc does not cover

- **Write access** — every probe here is `findWithOffsetPagination` (read-only).
  Nothing about whether any of these tables accept mutations was tested or is
  in scope for this doc.
- **`default`-profile reachability** — as noted in the Method section, nothing
  above was tested under `default`; assume `admin`-only until proven otherwise.
- **Field-level data quality/completeness** — "OK, real data" means at least one
  non-null row came back for `pageSize:1`, not that the column is populated
  consistently across all rows (e.g. `POLine.poNbr` was found null across every
  sampled row in the prior session's probe — the same kind of surprise could
  exist in any table here and wasn't re-checked column-by-column).
