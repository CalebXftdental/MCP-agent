# How to build the 3 demo workflows in My Workflow

Covers the three workflows built and proven end-to-end against the live ERP mirror this
session — **Win-Back Radar**, **AP Due-Soon Digest**, **AR Aging Digest** — each three ways:
manually in **Step view**, manually in **Canvas view**, and by asking the **workflow
copilot** to build it for you.

All three follow the same shape: a bulk data-fetch tool → a **Filter** step that applies
the actual business rule → an office artifact → a draft email (never sent). The `filter`
node and the three bulk tools (`get_customer_order_recency`, `get_ap_invoices_due_soon`,
`get_ar_invoices_past_due`) only exist once this session's changes are deployed — see the
project's `DEPLOY.md`.

## Before you start

- Your demo account needs these categories granted (Admin → Consumers → edit → Categories):
  - Win-Back Radar: `analytics`, `office`, `email_draft`
  - AP Due-Soon Digest: `finance`, `office`, `email_draft`
  - AR Aging Digest: `finance`, `office`, `email_draft`
- **Expect an approval pause.** All three digests produce a workbook/PDF classified
  `SENSITIVE`, and if the matched row count crosses the platform's broad-export threshold,
  the run automatically pauses for admin sign-off — no `approval_gate` node needed for this,
  it's `create_excel_report`/`create_pdf_packet`'s own built-in safeguard. This happened for
  both AP and AR in testing. If it happens in your demo: log in as an admin, open
  **Approvals**, approve it, then go back and hit **Resume** on the run — this is a feature
  to narrate ("large sensitive exports always get a human's eyes"), not a bug to route around.

---

## 1. Win-Back Radar

Flags customers in a territory who haven't reordered in over N days, produces a PDF, drafts
a digest email (never sent).

```
trigger (country, state, city, win_back_days)
  → get_customer_order_recency (country/state/city ← trigger)
  → filter: lastOrderDate is older than (days) ← win_back_days
  → create_pdf_packet (tables ← filter's matchedTable)
  → create_email_draft
```

### Step view

1. **My Workflow → New workflow.**
2. Add trigger inputs: `country` / "Country", `state` / "State", `city` / "City",
   `win_back_days` / "Win-back threshold (days)".
3. **+ Add a step** → under `minierp_analytics` → **`get_customer_order_recency`**. Bind
   `country`, `state`, `city` each to "From this workflow's input" → the matching trigger
   key. Leave `page`/`page_size` untouched.
4. **+ Add a step** → **Filter** (Flow control group). Bind "List to filter" → "From an
   earlier step" → the previous step's `customers` output. Add one condition: Field
   `lastOrderDate`, Condition "is older than (days)", Compared to → "From this workflow's
   input" → `win_back_days`.
5. **+ Add a step** → under `office` → **`create_pdf_packet`**. Title (literal):
   `Reorder-Due / Win-Back Radar`. Sections (raw JSON, required field):
   ```json
   [{"heading": "Reorder-Due / Win-Back Radar", "bullets": [
     "Flags customers who haven't reordered recently in this territory.",
     "See the attached table for the full list of flagged accounts."]}]
   ```
   Tables → "From an earlier step" → the Filter step's `matchedTable`. Classification
   (JSON): `["INTERNAL"]`.
6. **+ Add a step** → under `email` → **`create_email_draft`**. `to`: `["account-management@frontierdental.com"]`
   (swap in a real address), subject/body: any text, classification: `["INTERNAL"]`.
7. **Save → Check** (should read "Looks good") **→ Publish.**
8. **Run**: `country` = `US` (or any territory you know has data), leave state/city blank,
   `win_back_days` = `1` to guarantee a hit for the demo, then a realistic number afterward
   (`45`, `60`, ...).

### Canvas view

Same 5 steps, added the same way (**+ Add a step**, same picker) but as free-floating
cards. Instead of picking "From an earlier step" inside each step's form, **drag a wire**
from the source card's output dot to the target card's input dot:
- `get_customer_order_recency` card → drag from its `customers` output pin → drop on the
  Filter card's `input` pin.
- Filter card → drag from its `matchedTable` output pin → drop on `create_pdf_packet`'s
  `tables` pin.
- Trigger card → drag from the `win_back_days` pin → drop on the Filter step's condition
  value (open the Filter card's edit — pencil icon — first to add the condition row, then
  switch its "Compared to" source the same way as Step view; conditions themselves are
  edited inside the step, not drawn as wires).
- `country`/`state`/`city` pins from the Trigger card → the `get_customer_order_recency`
  card's matching input pins.

Click the pencil icon on any card to open/edit its config (same form as Step view); the X
icon removes a step. Save/Check/Publish/Run are the same buttons as Step view.

### Ask the workflow copilot

Open the copilot panel on the My Workflow page and try:

> "Build a workflow that flags customers in a territory who haven't reordered in over N
> days. Let me pick the territory and the day threshold each time I run it. Build a PDF and
> draft an email — don't send it."

The copilot now knows about the `filter` node (taught this session) and should propose
exactly the graph above as a **draft** — open it in the canvas, review, and Publish
yourself; it never publishes on its own.

---

## 2. AP Due-Soon Digest

Flags large AP invoices coming due, across every vendor, into a workbook + draft email.

> **Live-data note:** every AP invoice sampled from the real mirror already has a `payDate`
> set (0 unpaid out of 5000 checked), so a `paid == false` condition will show zero rows
> against this dataset — that's real data, not a bug. The proven demo condition below flags
> **large** invoices (`lineTotal`) instead, which does have real spread. `paid` is still on
> every row if you want to add it as a second condition later.

```
trigger (days_ahead)
  → get_ap_invoices_due_soon (days_ahead ← trigger)
  → filter: lineTotal is greater than 1000
  → create_excel_report (tables ← filter's matchedTable)
  → create_email_draft
```

### Step view

1. **New workflow.** Trigger input: `days_ahead` / "Due within (days)".
2. **+ Add a step** → under `minierp_finance` → **`get_ap_invoices_due_soon`**. Bind
   `days_ahead` → "From this workflow's input" → `days_ahead`. Leave `company_id`/`page`/
   `page_size` untouched.
3. **+ Add a step** → **Filter**. Bind "List to filter" → "From an earlier step" → the
   previous step's `invoices` output. Condition: Field `lineTotal`, Condition "is greater
   than", Compared to → "Type a value" → `1000`.
4. **+ Add a step** → under `office` → **`create_excel_report`**. Title: `AP Invoices Due
   Soon`. Tables → "From an earlier step" → the Filter step's `matchedTable`.
   Classification: `["INTERNAL", "SENSITIVE"]`.
5. **+ Add a step** → under `email` → **`create_email_draft`**. `to`:
   `["ap-team@frontierdental.com"]`, subject `AP Due-Soon Digest`, any body, classification
   `["INTERNAL"]`.
6. **Save → Check → Publish.**
7. **Run**: `days_ahead` = `60` (bump to `180`/`365` if nothing matches — invoice due dates
   vary).

### Canvas view

Same steps as free-floating cards: drag `get_ap_invoices_due_soon`'s `invoices` pin → the
Filter card's `input` pin; drag the Filter card's `matchedTable` pin → `create_excel_report`'s
`tables` pin. Everything else (condition row, literal values) is edited inside each card via
its pencil icon, same as Step view.

### Ask the workflow copilot

> "Build a workflow that flags large AP invoices due soon, across every vendor, and puts
> them in a workbook. I want to pick the number of days ahead each time I run it. Draft an
> email about it too, don't send it."

The copilot should discover `get_ap_invoices_due_soon` via the real tool catalog, notice
(if it calls the tool to check) that `paid` doesn't discriminate in this data, and propose a
graph filtering on `lineTotal` (or ask you which field/threshold you'd rather use — either is
a reasonable outcome; if it proposes `paid == false`, tell it that field is always true in
this data and ask it to use an amount threshold instead).

---

## 3. AR Aging Digest

Flags AR invoices with an outstanding balance, company-wide, into a workbook + draft email.

> **Schema limit — read this before demoing:** `ARInvoice` has **no customer link field** in
> this ERP's schema (confirmed by direct probe — not a missing feature, a real constraint).
> This digest can total, age, and flag AR invoices, but **cannot** say which customer owes
> what or route anything to an account manager. Frame it as a finance-facing AR-aging report,
> not a "collections queue" — the email draft below already says this explicitly.
>
> Also: `invoiceDate` is a placeholder epoch value (`1900-01-01`) on every sampled row in this
> mirror, so an invoice-age condition won't discriminate anything real today. The proven
> condition below uses `unpaidBalance` instead, which genuinely varies (2792 of 5000 sampled
> rows had a balance > 0).

```
trigger (min_invoice_age_days)
  → get_ar_invoices_past_due (min_invoice_age_days ← trigger)
  → filter: unpaidBalance is greater than 100
  → create_excel_report (tables ← filter's matchedTable)
  → create_email_draft
```

### Step view

1. **New workflow.** Trigger input: `min_invoice_age_days` / "Minimum invoice age (days)".
2. **+ Add a step** → under `minierp_finance` → **`get_ar_invoices_past_due`**. Bind
   `min_invoice_age_days` → "From this workflow's input" → `min_invoice_age_days`.
3. **+ Add a step** → **Filter**. Bind "List to filter" → "From an earlier step" → the
   previous step's `invoices` output. Condition: Field `unpaidBalance`, Condition "is
   greater than", Compared to → "Type a value" → `100`.
4. **+ Add a step** → under `office` → **`create_excel_report`**. Title: `AR Aging Digest`.
   Tables → "From an earlier step" → the Filter step's `matchedTable`. Classification:
   `["INTERNAL", "SENSITIVE"]`.
5. **+ Add a step** → under `email` → **`create_email_draft`**. `to`:
   `["finance-team@frontierdental.com"]`, subject `AR Aging Digest`, body should say plainly
   that rows aren't attributable to a customer (see the note above), classification
   `["INTERNAL"]`.
6. **Save → Check → Publish.**
7. **Run**: `min_invoice_age_days` = `0` (doesn't meaningfully narrow results in this
   dataset today, per the note above — set it anyway since the tool expects it).

### Canvas view

Same as the other two: drag `get_ar_invoices_past_due`'s `invoices` pin → the Filter card's
`input` pin; drag the Filter card's `matchedTable` pin → `create_excel_report`'s `tables`
pin.

### Ask the workflow copilot

> "Build a workflow that flags AR invoices with an outstanding balance, company-wide, into a
> workbook. Let me set the minimum balance each time I run it. Also draft an email about it,
> don't send it."

Per its own rules (DISCOVER, DON'T GUESS), the copilot should call `get_ar_invoices_past_due`
for real, notice there's no customer field, and say so plainly rather than inventing one —
if it doesn't mention that limitation on its own, ask it directly ("can this be routed to a
specific customer?") and it should tell you no and why.
