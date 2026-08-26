# UI Improvement Notes

Goal: make the console feel calmer, tidier, and more elegant (Apple-like) —
simpler hierarchy, less visual noise. **No color/theme changes** — scoped to
spacing, typography, layout structure, motion, and iconography.

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

- **HomePage greeting** (`pages/HomePage.css:163-173`) — the most Apple-like
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

## Root causes

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
(`theme.css:228`), table `<th>` (`DataTable.css:42`), the active nav item
(`AppShell.css:122`), and `DateTimePicker` six separate times. If everything is
emphasized, nothing is.

Target:

- **600** — the one page title (`shell-top-title`). Exactly one per screen.
- **550** — every card, section, modal, and drawer title. They differ by *size*,
  not weight.
- **400/450** — body and table cells.
- **650** stays only on `.ui-eyebrow`: at 11px uppercase, weight is the only
  thing holding it up.

Retire 700 and 800 outright (`NotFoundPage.css:4`, `Avatar.css:7`,
`RequestPicker.css:34`, `CidrInput.css:15`, and the workflow-canvas labels).

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

## Out of scope, but worth saying

The sidebar has **24 destinations in 8 groups**. Apple's calm is mostly *fewer
doors*. No amount of spacing, weight, or iconography work will offset an
information architecture that wide. That's a product conversation, not a
styling pass — but it is the elephant, and this file would be dishonest without
naming it.
