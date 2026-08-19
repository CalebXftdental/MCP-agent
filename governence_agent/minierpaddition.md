# miniERP / db-api addition: one generic aggregate endpoint

**Audience:** the db-api maintainer, as a concrete spec for the one new capability we're
requesting. **Ask:** add one new generic aggregate query endpoint to db-api, alongside
the existing `findWithOffsetPagination`/`findWithCursorPagination`. Everything else we
need (bulk/batch filtering, pagination) already exists today — this doc is scoped to the
one real gap.

---

## 1. What we're trying to tackle

Today, every read we do against miniERP goes through `findWithOffsetPagination` /
`findWithCursorPagination` — a generic filter-and-return-rows API over any table. That
covers almost everything we need: single-record lookups, filtered lists, even
multi-record batch lookups via a `where` clause like `{"accountId": {"in": [id1, id2,
...]}}`.

What it can't do is **aggregate**: there's no `SUM`, `COUNT`, `AVG`, `GROUP BY` — nothing
that computes a number across many rows server-side. Any question that needs a total
(revenue for a quarter, spend per customer, invoices per vendor) currently has to be
answered by:

1. Paginating through however many raw rows match the filter,
2. Pulling them all into our own application, and
3. Summing/grouping them ourselves, in Python.

For small, bounded data (e.g. a chart of accounts — a few hundred rows) this works fine
and we're not asking you to change anything for that case. The problem shows up at the
other end of the scale: any aggregation over a genuinely large/transactional table (many
thousands of rows — e.g. summing shipped quantity across every order line in a date
range) runs into a hard ceiling. We can't page through an unbounded number of rows
forever — we cap how many pages we'll fetch, and past that cap we either stop (and the
answer may silently be incomplete) or keep paging (and put a lot more load on a shared,
edge-protected API just to ship rows over the wire that we're going to throw away the
instant we've added them up).

A real aggregate endpoint removes that ceiling entirely: the database computes a sum
over 10 rows or 10 million rows the same way, without ever shipping the raw rows back to
us. That's the one capability we can't get by building more on our side — everything else
in this doc is in service of asking for exactly that, and nothing more.

---

## 2. Why one generic endpoint, not one per table or per question

We could instead ask you to add a bespoke aggregate to each specific API we already use
— a "sum GL balances by account" endpoint, a "total spend by customer" endpoint, and so
on, one at a time as each new question comes up. We don't want to do that, for a few
reasons:

- **The hard part is already built — reuse it.** A flexible, safe filter language
  (equality, `in`, `gte`/`lte`, `startsWith`, ...) already exists in
  `findWithOffsetPagination`. The only genuinely new work is a small, fixed set of
  standard aggregate operators (`sum`/`count`/`avg`/`min`/`max`) sitting on top of that
  *same* filter — not a new query language, not a new permission model, not a new
  table-registration process. If there's a real relational database underneath, this
  maps close to 1:1 onto a native `GROUP BY` + aggregate functions clause.
- **It covers every table we can already reach, automatically — including ones we
  haven't built tools against yet.** Because it reuses the same `table` identifiers and
  the same filter grammar as `findWithOffsetPagination`, anything already queryable
  today is aggregatable the moment this ships, with zero extra work on your side per
  table. A bespoke-per-question design means coming back to you every time we have a new
  business question that happens to need a total — a generic one means we never have to.
- **Small, well-understood surface to build, test, and maintain.** Five aggregate
  operators over an existing filter language is a much smaller, more mechanical piece of
  work to review and keep correct than N bespoke endpoints, each with its own shape,
  each needing its own tests, each a new thing to keep working across future changes.
- **Keeps db-api internally consistent.** Same options-object shape as
  `findWithOffsetPagination` (`table`, `where`, now `groupBy`/`aggregations` instead of
  `select`, plus `page`/`pageSize`) — a natural sibling to the existing endpoint, not a
  second, differently-shaped API style to learn.
- **Better for load on your side, not just ours.** Every one of these questions that
  currently gets answered by pulling N raw rows to sum on our end is N rows of read
  traffic and serialization cost that a single aggregate query avoids entirely — this is
  a win for db-api's own request/bandwidth load, not just a convenience for us.

We did consider the alternative of bypassing db-api entirely for direct database access,
specifically to get real `GROUP BY`. We're deliberately not asking for that: it would
mean giving up the stability and abstraction db-api already provides (table/field
naming, whatever scoping and business-logic awareness already lives in it), taking on
Acumatica's internal schema directly as a new maintenance burden on our side, and
standing up new credential/connection management for a second access path. A single
new endpoint on the API we already trust and already use gets us the one thing we
actually need, without any of that cost.

---

## 3. Proposed shape

```
aggregateWithFilter(table, {
  where: { ...identical clause language findWithOffsetPagination already supports:
            equality, {in: [...]}, {gte: ..., lte: ...}, {startsWith: ...}, etc... },
  groupBy: ["fieldA", "fieldB"],   // optional
  aggregations: [
    { field: "someNumericField", op: "sum",   as: "totalSomeField" },
    { field: "anotherField",     op: "count", as: "rowCount" }
  ],
  page: 1,
  pageSize: 100
})
```

Field by field:

- **`table`** — identical to what `findWithOffsetPagination` already accepts (`Account`,
  `GLHistory`, `SOOrder`, ...). No new registration or permission step: if a table is
  reachable via `find` today, it should be aggregatable the moment this ships.
- **`where`** — identical clause language to the existing endpoint. This is the whole
  point of reusing it rather than inventing something new — same operators, same
  semantics, no new grammar to learn on either side.
- **`groupBy`** — optional list of field names on `table`. Omit it entirely for a single
  grand-total row across every matching record; include it to get one row per unique
  combination of those field values.
- **`aggregations`** — a list of `{field, op, as}`. `op` is one of `sum | count | avg |
  min | max`. `as` names the key the result comes back under. Multiple aggregations in
  one call are fine (e.g. sum of two different numeric fields at once).
- **`page` / `pageSize`** — same pagination discipline as today, applied to the
  **grouped output rows** — relevant when `groupBy` produces a large number of distinct
  groups (e.g. grouping by customer across thousands of customers), not to the raw rows
  being aggregated underneath.

**Response shape:** an array of rows. With no `groupBy`, a single-element array holding
just the requested aggregation aliases. With `groupBy`, one row per group, each
containing that group's field values plus the aggregation aliases.

**One requirement, not an implementation detail to leave implicit: this must respect the
exact same company/tenant scoping `findWithOffsetPagination` already enforces.** If
db-api's underlying data spans more than one company, an aggregate that doesn't carry
that scoping through could sum across data that should never be combined. This needs to
be true from the first version, not patched in after.

---

## 4. Example queries, with explanation

### 4.1 Company-wide financial summary (the motivating case)

Two calls we can already make today, unchanged:

```
// Which accounts are revenue vs. expense — small, bounded (a chart of accounts is a
// few hundred rows at most), already fully supported by findWithOffsetPagination.
findWithOffsetPagination("Account", {
  select: { accountId: true, type: true },
  where: { type: { in: ["Income"] }, active: true, companyId: 1 }
})
// ...and the same again with type: {in: ["Expense"]} for the expense-side ids.
```

Two NEW calls, using the proposed endpoint, one per side:

```
aggregateWithFilter("GLHistory", {
  where: { accountId: { in: [/* revenue account ids from above */] },
           finPeriodId: "202510", companyId: 1 },
  aggregations: [
    { field: "ptdCredit", op: "sum", as: "totalCredit" },
    { field: "ptdDebit",  op: "sum", as: "totalDebit"  }
  ]
})
```

No `groupBy` here — we want one grand total across every matching revenue account for
that period. Repeat with the expense account ids for the expense side. Four calls total
(two existing, two new), each cheap and bounded, and we compute `totalCredit -
totalDebit` ourselves on each side — that's a single subtraction, not something we need
an aggregate operator for.

### 4.2 A grouped breakdown — "which expense accounts are largest this period"

```
aggregateWithFilter("GLHistory", {
  where: { accountId: { in: [/* expense account ids */] },
           finPeriodId: "202510", companyId: 1 },
  groupBy: ["accountId"],
  aggregations: [
    { field: "ptdDebit", op: "sum", as: "totalDebit" }
  ],
  page: 1, pageSize: 50
})
```

Same call shape as 4.1, just with `groupBy: ["accountId"]` added — now it returns one row
per account instead of one grand total. This is the case `groupBy` exists for: the exact
same filter, a different level of granularity in the response, no new endpoint needed to
go from "one number" to "one number per account."

### 4.3 Why this matters beyond the finance example — a large-scale case

```
aggregateWithFilter("SOLine", {
  where: { orderDate: { gte: "2025-01-01", lte: "2025-12-31" }, companyId: 1 },
  groupBy: ["inventoryId"],
  aggregations: [
    { field: "shippedQty", op: "sum", as: "totalShipped" },
    { field: "extPrice",   op: "sum", as: "totalRevenue" }
  ],
  page: 1, pageSize: 100
})
```

This is the shape of question that genuinely can't be answered safely on our side today:
"total quantity shipped and revenue per product, across a full year of order lines"
could easily be tens of thousands of raw rows if we tried to pull and sum them
ourselves — right at or past the pagination cap we have to enforce to avoid an unbounded
fetch. With this endpoint, it's one call, and the answer is exact regardless of how many
line items it's aggregating over, because the rows never have to leave the database to
get summed.

---

## 5. Summary of the ask

One new endpoint, `aggregateWithFilter(table, {where, groupBy, aggregations, page,
pageSize})`, reusing the exact filter grammar `findWithOffsetPagination` already has, a
fixed small set of aggregate operators (`sum`/`count`/`avg`/`min`/`max`), optional
grouping, and the same company/tenant scoping already enforced elsewhere. That's the
whole request — no new table-permission model, no new filter language, no per-question
follow-up asks as we build more tools on top of it.
