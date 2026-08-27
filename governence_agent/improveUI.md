# UI Improvement Notes

Goal: make the console feel calmer, tidier, and more elegant (Apple-like) —
simpler hierarchy, less visual noise — **and usable by someone who does not
work in IT.**

Scope: spacing, typography, layout structure, motion, iconography, **the words
on screen, and how many doors the sidebar offers.** The palette itself stays as
it is; how *often* the accent fires does not (root cause F).

The second half of that goal is new. The first three drafts of this file treated
"elegant" as a purely visual problem and pushed information architecture into an
"out of scope" note at the bottom. That note was right about the elephant and
wrong to walk away from it: for a non-technical user the console's hardest
problem is not that six font weights are in play, it is that five sidebar
entries lead to what looks like the same feature. Phase 4 picks that up, and the
old out-of-scope note is retired.

Frontend root: `governence_agent/gateway/frontend/src/`

The system is well-built: tokenized, accessible, deliberate motion, no raw hex
in any component stylesheet. These are refinements, not a rebuild.

The previous draft of this file listed six per-page issues. Two of them were
already fixed in the code, one located the fix on the wrong component, and one
had been mostly done — see "Closed" at the bottom. More importantly, the three
things doing the most damage to "calm" are not per-page at all: they're missing
system primitives. Those come first now, because they're what makes every
remaining item a one-liner instead of a judgment call.

## Reference points (keep as-is)

- **HomePage greeting** (`pages/HomePage.css:167-177`) — the most Apple-like
  line in the codebase, and worth reading closely: weight **500**, `clamp(1.55rem,
  4vw, 2.05rem)`, tracking `-0.025em`. Big, light, tight. It is *lighter* than
  every card title in the app. That is the model for Phase 1.
- **HomePage layout** — no card chrome around the hero, one elevated composer,
  generous centered whitespace. One focal surface, everything else recedes.
- **AppShell nav active state** (`components/AppShell.css:117-124`) — soft accent
  fill + thin inset stripe instead of a solid block. Restrained, works.
- **Button press physics** (`components/ui/Button.css:49-58`) — 1px hover lift,
  spring-eased press settle. Tactile without being showy.
- **`ui/PlusIcon.tsx`** — the icon contract already exists here: 24 viewBox,
  `currentColor`, round caps, plus a comment on why SVG geometry beats a text
  glyph under transform. Phase 2 extends this rather than inventing a style.

Two more, added for Phase 4 — the app already contains its own best answers to
"how should this read to someone who doesn't work in IT," and they are worth
copying rather than re-deriving:

- **`PlaygroundPage`'s `AI_TASKS`** (`:68-130`) — the strongest non-IT surface in
  the console. Named for the job ("Draft a message", "Compare two customers"),
  not the mechanism; a three-field form instead of a blank prompt; the model's
  jargon confined to `tmpl`, where the user never sees it. Phase 4c's "Do a task"
  launcher is essentially this pattern given more room.
- **`AccessPage`'s denial table** (`:236-266`) — the column header is literally
  **"What you tried to do"**, the tool id is demoted to small mono underneath,
  and the fix is a one-press "Request access" button in the row. That is the
  right instinct on the highest-stakes screen a non-technical user reaches: the
  one they land on because something was already refused. (`:261`'s stray "no
  category" is the single lapse — Phase 4b.)

## Root causes

**A, B, C, and E are closed** (Phase 0) and are kept as the diagnosis that
produced it, not as live findings. Read their line references as historical:
`theme.css:92-111` in A, for instance, now points at the space scale Phase 0
*added* there — the fix, not the problem it describes. E is likewise done, and
re-verified here: `LoginPage.css` no longer contains `font-weight: 1200`, and
both brand elements now read `var(--ui-mono)` (`:183`, `:191`). The two `#ffffff`
still in that file (`:44`, `:64`) are decorative radial-gradient stops, not the
token violation E flagged — leave them.

**D is half-closed**: `display=optional` shipped, the CDN dependency did not
(Phase 5.3). **F, G, and H are open**, and are new in this draft.

### A. There is no spacing scale

`components/ui/theme.css` tokenizes color, type, radius, shadow, duration, and
easing — and not space (`theme.css:92-111` jumps straight from shape to motion).
So every component hand-picks its own values: `1.05rem`, `1.15rem`, `0.85rem`,
`0.7rem`, `0.65rem`, `0.55rem`, `0.45rem`, `0.4rem`, `0.3rem`, `0.15rem`,
`0.1rem`. Nothing lands on a shared grid, so no two panels breathe at the same
rate.

Apple's calm comes from a strict 4/8pt rhythm, not from removing borders. This
is the actual root cause behind most of what the previous draft called
"nesting" and "border noise."

### B. The page shell is copy-pasted 22 times

Every page stylesheet opens with the same eight lines — `flex column`,
`gap: 1rem`, `padding: 1.5rem 1.75rem 3rem`, a `max-width`, and a 640px padding
override. Twenty-two copies. Any change to page rhythm currently means editing
twenty-two files, which is precisely why the per-page fixes in the last draft
looked expensive.

### C. The content column changes width on every tab switch

Because each of those 22 copies picked its own cap, there are five distinct
measures in play:

| Cap | Pages |
|---|---|
| 84rem | Monitor, Files, Knowledge |
| 80rem | 15 pages (the de facto default) |
| 72rem | Playground |
| 60rem | Developer, Calendar |
| 48rem | Placeholder (deliberate) |

Clicking Monitor → Approvals → Playground → Calendar resizes the content column
three times. This is invisible in any single screenshot — which is why a
page-by-page review missed it — and obvious in thirty seconds of real use.
Nothing reads less like Apple than a layout that will not hold still.

### D. The typographic hierarchy is one CDN request from collapsing

`gateway/frontend/index.html:13-18` loads Inter variable from
`fonts.googleapis.com` with `display=swap`. Every in-between weight the UI
relies on (550, 650) renders correctly *only* under a variable font. In an
egress-restricted deployment — plausible for a governance console — the fetch
fails silently, Segoe UI takes over, and the entire hierarchy flattens.
`display=swap` also guarantees a FOUT on every cold load.

`components/ui/README.md:115` still asserts *"Fonts stay system. No webfont: the
console is internal, must work offline, and a font CDN would be one more origin
to allow."* That was true once and is now stale — and the reasoning in it is
still correct, which is the point.

### E. LoginPage is the least disciplined file, and the first screen anyone sees

`pages/LoginPage.css` breaks the no-raw-values rule the rest of the kit holds:

- `:178` — `font-weight: 1200`. Outside CSS's valid 1–1000 range, so the
  declaration is **invalid and dropped**; the brand title currently renders at
  inherited weight. This is a live bug, not a style preference.
- `:181`, `:189` — raw `font-family: monospace` instead of `var(--ui-mono)`,
  both missing their trailing semicolon.
- `:180` — hardcoded `#ffffff` instead of a token.

### F. The accent fires on hover, everywhere, and so signals nothing

`border-color: var(--ui-accent)` appears **24 times across 20 files.** That count
on its own is misleading, though, and an earlier version of this section used it
to argue for a palette change. Broken down by the state it responds to:

| State | Uses | Verdict |
|---|---|---|
| **Selection** — `[data-selected]` | `PickableAccessCard.css:34,91`, `Chip.css:88`, `WorkflowCatalogTile.css:35`, `WorkflowCanvas.css` | **Correct.** "This one is chosen" is exactly what a brand accent is for. |
| **Focus** — `:focus` / `:focus-within` | `Field.css:92`, `Composer.css:23`, `Card.css:31`, `Dropdown.css:30`, `DateTimePicker.css:29`, `CodeBlock.css:34` | **Correct.** Deliberate, and `Field.css:88-89` documents why it is a border rather than an outline. |
| **Hover** | `Card.css:24`, `Stat.css:112`, `Chip.css:15`, `Button.css:87`, `NavHelpBubble.css:31`, `WorkflowInfoIcon.css:28`, `PlaygroundPage.css:71` | **The actual problem.** |

Only the last row is overuse. Brushing the pointer across a page currently lights
things up in brand teal that the user has neither chosen nor focused — so by the
time the accent means "selected," it has already been spent on "the mouse passed
over this." Apple and Cohere are near-monochrome precisely so the one accent
still lands when it appears.

`AccessCard.css:24-28` already does the right thing — hover moves
`--ui-border-strong`, no accent. Make that the rule and the other seven follow.

### G. The copy is written by the people who built it

The page description is the first sentence a non-technical user reads under
every title, and `pages/routes.ts` writes them in the vocabulary of the
implementation:

- `:89` — "Recurring governed workflows with deterministic local due-run execution."
- `:120` — "Approval-gated email delivery queue and connector handoff status."
- `:126` — "Draft invite artifacts and approval-gated calendar connector queue."
- `:108` — "Generated artifacts, classifications, and downloads."

"Deterministic local due-run execution" is three pieces of jargon in four words,
describing a page that means *"workflows that run on a schedule."*

It is not only `routes.ts`. `components/chat/Composer.tsx:54` labels the main ask
box, for screen readers, "Ask the governed assistant." `pages/AccessPage.tsx:261`
prints a literal **"no category"** to an end user in the denials table — on the
one screen someone visits *because* something already went wrong for them.

The nav group labels have the same problem: **"Content"** (`routes.ts:254`) is an
information-architecture term, not a thing anyone came here to do.

### H. Two doors are open that should not be

- **`Developer` is not admin-gated.** `routes.ts:135-140` carries no
  `admin: true`, so every ordinary user gets a sidebar entry offering an API key
  and an MCP connection snippet. It also duplicates the API key already on My
  Access (`:132`), so the one user who *does* want it is offered it twice.
- **The unported dot is nearly retired.** `pages/index.ts:47-72` registers 24 of
  26 routes; only `agents` and `code-plans` still fall through to
  `PlaceholderPage`, and both are admin-only. So `AppShell.tsx:180-186`'s dimming
  and dot — plus the `isPorted` plumbing behind it — now render for two admin
  tabs and no one else. Worth deleting once those two land, not before.

## Plan

### Phase 0 — make the system enforce calm — **DONE**

Mechanical, near-zero visual risk, and it converts every later item from a
judgment call into a one-liner.

1. ✅ Space tokens in `theme.css` on a 4px grid — `--ui-s-1` (4px) through
   `--ui-s-8` (64px) — plus `--ui-page-max` / `--ui-page-max-narrow`.
2. ✅ `components/ui/PageShell.tsx` owns the shell; all 23 duplicated blocks
   deleted. Named `PageShell`, not `Page`, because `PageProps` in
   `pages/types.ts` is already the routed-page contract and two `Page*` types in
   one file would be a trap. Widths collapse from five (48/60/72/80/84rem) to
   two: `default` 80rem, `narrow` 60rem (Developer, Calendar, Placeholder).
   Playground passes `layout="bare"` and keeps its own two-column grid.
3. ✅ `LoginPage.css` — `font-weight: 1200` → `700` (it was invalid, so the
   brand title had been rendering at inherited weight), raw `monospace` →
   `var(--ui-mono)` on both brand elements, redundant `#ffffff` dropped (the
   panel already sets white).
4. ✅ `display=swap` → `display=optional`, so a cold load no longer re-flows
   every heading. **The CDN dependency itself is still open** — self-hosting the
   woff2 is the robust fix and needs a call on adding a binary asset to the repo.
   `ui/README.md`'s stale "fonts stay system" claim is corrected.

Also folded in, because Phase 0 would otherwise have introduced a visible
mismatch: `.shell-top`, `.home`, `.home-thread-*`, and `.chat-log` insets moved
from the off-grid `1.75rem` to `--ui-s-5`, so the topbar title, the composer, and
card content all sit on one left edge.

Visual deltas to expect: page inline inset 28px → 24px everywhere; Monitor /
Files / Knowledge narrow 84rem → 80rem; Playground 72rem → 80rem; Placeholder
48rem → 60rem; the Login brand title becomes bold.

Verified: `tsc -b`, `oxlint`, and `vite build` all clean.

### Phase 1 — typography

Reduce **six** weights in active use (500 / 550 / 600 / 650 / 700 / 800) to
three, and let size and tracking carry hierarchy the way HomePage already does.

Weight 650 currently appears ~35 times and therefore signals nothing: card
titles (`Card.css:73`), section titles (`Card.css:140`), modal titles
(`Modal.css:79`), drawer titles (`Drawer.css:75`), eyebrows
(`theme.css:255`), table `<th>` (`DataTable.css:42`), the active nav item
(`AppShell.css:122`), and `DateTimePicker` six separate times. If everything is
emphasized, nothing is.

Target:

- **600** — the one page title (`shell-top-title`). Exactly one per screen.
- **550** — every card, section, modal, and drawer title. They differ by *size*,
  not weight.
- **400/450** — body and table cells.
- **650** stays only on `.ui-eyebrow`: at 11px uppercase, weight is the only
  thing holding it up.

Retire 700 and 800 outright. Grepped fresh, that is eleven declarations, more
than the four the earlier draft listed: `NotFoundPage.css` (800), `Avatar.css`,
`CidrInput.css`, `RequestPicker.css`, `PickableAccessCard.css`,
`ChatMessageBubble.css`, `WorkflowAlertBadge.css`, `StepFlow.css`,
`WorkflowCanvasLegend.css`, and **`LoginPage.css` three times**.

That last one needs a deliberate call rather than a blind sweep: Phase 0 item 3
*set* one of those 700s, replacing the invalid `font-weight: 1200` that had been
silently dropped. So Phase 1 is about to re-open a line Phase 0 just closed —
correctly, since 700 was chosen then only as "a valid weight," not as a
considered place in a three-weight system. Login is also the first screen anyone
sees, so it is the one file where the brand title arguably earns an exception.
Decide it explicitly; do not let a find-and-replace decide it.

### Phase 2 — the icon set

The largest visual payoff, and stylistically pre-decided by `PlusIcon.tsx`.

83 emoji occurrences across 22 files. The concentration is small enough to be
tractable: `pages/routes.ts` (24 nav icons), `components/workflow/stepMeta.ts:7-27`
(9), `PlaygroundPage.tsx` (8), `WorkflowCatalogTile.tsx` (7),
`chat/MessageActions.tsx` (6) — that is 54 of the 83 in five files.

Emoji render inconsistently across OS and browser (stroke weight, style, even
color) and fight the single-weight typographic system. It is the most visible
"not quite Apple" tell in the app.

Approach: one `<Icon name="…">` component backed by a name→path record,
following PlusIcon's contract — 24 viewBox, `currentColor`, round caps and
joins, **stroke 1.75** (2.5 reads heavy at the 1.2rem nav size). Then
`routes.ts` changes `icon: '🏠'` → `icon: 'home'` and the type narrows from
`string` to the icon-name union, so a missing icon becomes a compile error.

Do the 24 nav icons first: one file, and the result shows on every screen.

### Phase 3 — panel-stack tightening

Now cheap, because Phase 0 supplied the vocabulary.

`WorkflowsPage` stacks four full-width bordered Cards at a uniform `gap: 1rem`
(health strip → catalog → run form → recent runs). Individually calm; together
it reads as a list of report widgets rather than one screen.

- Demote the health strip from a full titled+described `Card` to a slim
  border-less inline stat row above the catalog. It is four numbers.
  (`WorkflowsPage.tsx:316` also carries `className="workflows-health"`, which
  has no matching CSS rule — dead class, remove it.)
- Tighten the gap between the run-form and recent-runs cards, since they are
  causally linked: you just ran something, here is where it shows up.
- Drop the remaining `translateY(-1px)` on `WorkflowCatalogTile.css:26`; in a
  dense grid of 7.5rem tiles, every tile you brush past popping reads as
  jittery. Keep the border/shadow change.

### Phase 4 — non-IT usability

The half of the goal the earlier drafts skipped. Ordered cheapest-first, because
the first two items are an afternoon and carry most of the gain.

**4a. Close the two open doors** (root cause H). Add `admin: true` to the
`developer` route, or fold its contents into My Access behind a `<details>` — it
is one line either way, and it removes an API key from the sidebar of every
non-technical user.

**4b. Rewrite the user-facing copy** (root cause G). Roughly eight strings:

| `routes.ts` | Rewrite |
|---|---|
| "Recurring governed workflows with deterministic local due-run execution." | "Workflows that run on a schedule." |
| "Approval-gated email delivery queue and connector handoff status." | "Emails waiting to be approved and sent." |
| "Draft invite artifacts and approval-gated calendar connector queue." | "Meeting invites waiting for approval." |
| "Generated artifacts, classifications, and downloads." | "Files the assistant made for you." |
| "Your status, granted data domains, and API key." | "What you can see — and how to ask for more." |

Plus `Composer.tsx:54` "Ask the governed assistant" → "Ask a question", and
`AccessPage.tsx:261`'s "no category" → something that tells the user what to do
instead. Group label "Content" → "Your files and documents."

Leave the admin-only descriptions (`whitelist`'s CIDR language, `consumers`'
"principals") alone. Admins are the audience there and the precision is worth
more than the plainness.

**4c. Collapse the five automation doors.** The one that matters, and the one
that needs a product decision rather than a patch.

A non-admin sees **14 destinations** (`visibleNav` over `NAV_GROUPS`), and five
of them are the same idea wearing different names:

> AI Playground · Workflows · Workflow Store · Automations · My Workflow

The distinction between them is architectural, not something a user came here
holding. Ask an office manager which one drafts a payment reminder and they will
guess. Proposed:

| Today | Proposed |
|---|---|
| AI Playground, Workflows | **Do a task** — one launcher: Playground's guided cards on top, the workflow catalog below |
| My Workflow, Automations, Workflow Store | **Automations** — three tabs *inside* one page (Mine / Scheduled / Browse) |

Five doors → two, no backend change; `routes.ts` and `pages/index.ts` already
make the registry side of this cheap. No amount of spacing or weight work
substitutes for it.

**4d. Give the nav helper a label.** `NavHelpBubble` is the best non-IT idea in
the app — a floating "where do I find X" assistant — and it renders as a bare 🧭
with an `aria-label` and no visible text (`NavHelpBubble.tsx:143-151`). The users
who need it most are exactly the ones who will not click an unlabeled emoji.
Ship it as a labeled pill ("Need help?") that collapses to the icon after first
use.

**4e. First-run orientation.** A new user lands on Home facing an empty
composer. Add a dismissible three-step card in the hero, first session only —
*ask a question → your files land in Files → if you're refused, request access.*
`useStoredList` already has the persistence pattern.

**4f. Auto-select quick-ask blanks.** Clicking a chip inserts
`Find the customer {name or email}` and expects the user to infer that the braces
are theirs to replace. `Composer.tsx:72-76` guards this well — but a guard is a
worse fix than not needing one. On insert, select the first blank's text range so
the next keystroke replaces it. Two lines in `useComposer`.

### Phase 5 — finish the visual system

Three loose ends, all cheap, all visible.

1. **Scope the accent to selection and focus** (root cause F). Move the seven
   *hover* uses to `--ui-border-strong`, matching `AccessCard.css:24-28`. Leave
   every selection and focus use alone.
2. **Wire the theme toggle that already exists.** `theme.css:179-239` defines
   both themes under `:root[data-theme=…]` so an explicit choice beats the OS in
   both directions — and **nothing in the app ever sets the attribute.** Grepped:
   `data-theme` appears only inside CSS selectors (`theme.css`, `AppShell.css:67`,
   `Avatar.css:23`, `Tooltip.css:26`) and `ui/README.md:81`, which says outright
   "Nothing sets `data-theme` yet." The hard half is done; the switch is missing.
3. **Self-host the Inter woff2.** Still open from Phase 0, and it is a
   correctness issue rather than a taste one: the hierarchy Phase 1 establishes
   depends on weights 550/650, which exist **only** on the variable font. One
   blocked egress request and the whole system silently flattens to Segoe UI.
   ~110KB in the repo settles it permanently.

## Suggested order

Phase numbers are chronological, not priority — 4a/4b are far cheaper than
Phase 2 and land more value per hour. Recommended sequence:

1. **Phase 1** (typography) — biggest aesthetic gain per hour; mechanical.
2. **Phase 4a + 4b** (admin gate, copy) — an afternoon, immediate non-IT gain.
3. **Phase 2** (icons, nav's 24 first) — the most visible "not quite Apple" tell.
4. **Phase 5** (accent, toggle, font) — cheap, and 5.3 protects Phase 1's work.
5. **Phase 4c** (the IA consolidation) — biggest single win; needs a product call.
6. **Phase 3**, then 4d–4f.

## The governing principle

The previous draft treated elegance as *subtraction of borders*. The sharper
rule, and it generalizes to all 22 pages:

> **One focal surface per screen. Exactly one card may carry a shadow; the rest
> are border-only or bare.**

HomePage already obeys this (one elevated composer). WorkflowsPage violates it
four ways. Implementing it needs one small `Card` API addition — an
`elevation="flat"` variant — rather than page-by-page surgery, and it subsumes
both of the nesting/panel-stack items from the last draft.

## Closed

Verified against the code; do not re-do these.

- **Card-in-card nesting on My Access** — already fixed.
  `pages/AccessPage.css:127-130` sets `.access-group { border: 0 }`, with a
  comment stating verbatim that "boxing the section too was a border around a
  border." The `<details>` carries both classes and `.access-group` wins on
  source order, so it is two framing levels, not three.
  `.workflows-detail-step` likewise has no border — just a sunken tint, which is
  already the one-framing-device rule.
- **Default `DataTable` to `flush`** — do not do this. `flush` is a **`Card`**
  prop (`ui/Card.tsx:22`), not DataTable's, so the fix was aimed at the wrong
  component; and it is already applied in 22 places, including both examples the
  last draft cited (`ApprovalsPage.tsx:185`, `AccessPage.tsx:402`). The tables
  that lack it, such as `AccessPage.tsx:429`, are *siblings* of other card
  content — flushing those would push the table edge-to-edge while the picker
  above it stayed inset, i.e. it would introduce the misalignment it was meant
  to remove.
- **Uniform hover-lift across all tiles** — mostly already scoped.
  `AccessCard.css:24-28` has no `translateY` and uses `--ui-border-strong`, not
  accent. `WorkflowCatalogTile.css:22-27` is `-1px` and border-strong. Only
  `Card.css:24-28` and `Stat.css:112-116` use `-2px` + accent, and those are the
  large singular surfaces where it belongs. The one pixel that remains is folded
  into Phase 3.

## Promoted out of "out of scope"

This section used to say the sidebar breadth was the elephant, that no amount of
spacing or iconography would offset it, and that it was a product conversation
rather than a styling pass. All three claims still hold. The conclusion drawn
from them — leave it alone — did not, so the item is now **Phase 4c** and this
section records the correction rather than repeating the note.

Two corrections to the numbers it quoted, both counted fresh against
`NAV_GROUPS`:

- It said **24 destinations in 8 groups**. It is **26** in 8 — 1 + 5 + 5 + 3 + 3
  + 3 + 2 + 4. The count drifted as `workflow_store` and `my_workflows` landed.
- More to the point, 26 is the *admin* number and was never the right one to
  quote here. A non-admin sees **14**, and that is the figure Phase 4c has to
  move, because the non-technical user is by definition never an admin.

## Still genuinely out of scope

- **The palette.** `--ui-accent` stays `#2FC7BA`. Phase 5.1 changes how often it
  fires, not what it is.
- **The workflow canvas** (`components/workflow/`). It is a node editor for
  people building automations, and it should be judged as a power tool. Phase 2's
  icon work touches `stepMeta.ts`; nothing else here applies to it.
- **The admin console's density.** Monitor, Alerts, Security, and Consumers are
  dense on purpose. Their audience reads dense tables for a living.
