# UI kit

Reusable presentational components for the governance console, derived from
`gateway/static/app.html` — the colleague-built dashboard this React app will
replace. **Nothing here is mounted yet.** The barrel is unimported, so the kit
costs zero bytes in the bundle until a panel uses it.

```tsx
import { Card, DataTable, Badge, useToast } from '../components/ui'
```

Import from the barrel (`./components/ui`), not from individual files: the
barrel imports `theme.css` first, and the tokens have to be defined before any
component rule that reads them.

## Why these components

Each one is something the old dashboard already had, repeated by hand. The
counts below are literal occurrences in `app.html`, and they're the reason the
list looks like this rather than like a generic starter kit:

| Component | Replaces | Occurrences |
|---|---|---|
| `ToastProvider` / `useToast` | global `toast(msg, bad)` | 144 |
| `Page` | the page-shell block hand-copied into every page stylesheet | 22 |
| `Card`, `SectionHeader` | `.card`, `.section-head` | 92 |
| `Badge`, `SeverityBar` | `.pill`, `.pill.sev-*`, `.sevbar` | 81 |
| `EmptyState` | `.empty` | 69 |
| `Stat`, `StatGrid` | `.kpi`, `.kpis` | 37 |
| `DataTable` | `.tbl-wrap > table` | 35 |
| `Drawer` | `openDrawer(html)` + `.backdrop` | 12 |
| `BarList` | `_hbars()` | 12 |
| `Chip` | `.qa-chip` | 8 |
| `KeyValue` | `.kv` / `.k` / `.v` | 6 |
| `Donut`, `Legend` | `_donut()`, `_legend()` | 6 |
| `Sparkline` | `_spark()` | 6 |
| `SegmentedControl` | `.seg` | 3 |
| `Field`, `Input`, `Select`, `Textarea` | bare `<label>` + `input{}` | ~all forms |
| `CodeBlock`, `SecretKey` | `<pre class="mono">`, `.key` | — |
| `Avatar` | `.avatar` | — |
| `Spinner`, `TypingDots`, `Skeleton` | `.typing`, `"Loading…"` strings | — |

Deliberately **not** included, because each appears in exactly one panel and
carries panel-specific data shapes: the 24×7 call heatmap (`_heatmap`), the
stacked volume chart (`_stacked`), the workflow graph canvas (`.wf-*`), and the
chat message list (`.msg`). They belong with their panels; if a second panel ever
needs one, move it here then.

## Design decisions

**Tokens, not values.** `theme.css` is the single source of colour, type, space,
radii, shadow, duration, and easing. No component stylesheet contains a raw hex.
That is what makes the dark theme a token swap instead of a second stylesheet.

**Space is a 4px grid (`--ui-s-1` … `--ui-s-8`).** Reach for a token, not a
hand-typed rem. Before these existed each component picked its own value —
`1.05rem`, `0.85rem`, `0.65rem` — so no two panels breathed at the same rate;
the calm comes from sharing one rhythm, not from any single measurement being
ideal.

**Two page widths, not one per page.** `Page` owns the content measure, and it
offers exactly `default` (80rem) and `narrow` (60rem). The console had drifted to
four different caps because every page stylesheet declared its own, so the
content column resized whenever you switched tabs. If a page seems to need a
third width, that's a conversation about the page.

**The brand teal is a fill colour, not a text colour.** `#2FC7BA` is ~2.1:1 on
white. Use `--ui-accent` for fills, indicators, and borders; `--ui-accent-text`
(the darker `#1a9c90`) for anything read as text. The old stylesheet already
worked this way in practice — this just names it.

**Semantic colour is separate from the accent.** `--ui-ok` / `--ui-warn` /
`--ui-danger` / `--ui-info` never shift with the brand, so severity reads
identically in a table pill, a drawer stripe, and a KPI tile. Their values match
`--ok` / `--warn` / `--bad` in `src/index.css`, so this kit and
`ConnectionStatus` agree.

**Both themes, and the toggle wins.** Tokens are redefined under
`@media (prefers-color-scheme: dark)` and again under `:root[data-theme="dark"]`
and `:root[data-theme="light"]`, so an explicit toggle overrides the OS
preference in both directions. Nothing sets `data-theme` yet — add it on
`<html>` when a theme switcher exists.

**Animation is purposeful, and always optional.** Four durations and three
easings, applied where movement carries meaning: table rows stagger in reading
order, a segmented thumb slides so you see which direction you moved, KPI numbers
count up *only when they change* (these panels poll — a tile animating 0 → 4,182
on every refresh is noise), toast countdowns pause when you hover them. Every
entrance animation is dropped under `prefers-reduced-motion: reduce`, not merely
shortened; spinners become pulsing rings rather than freezing mid-rotation.

**Accessibility fixes carried in deliberately.** These are behaviour changes from
the old UI, not just restyling:

- `Drawer` traps Tab, closes on Escape, locks background scroll, and returns
  focus to whatever opened it. The legacy drawer had a permanent
  `aria-hidden="true"` in its markup, so its contents were hidden from screen
  readers even while open.
- `DataTable` rows are keyboard-activatable. Legacy clickable rows had
  `cursor:pointer` and a click handler and nothing else, making the detail drawer
  mouse-only.
- `Field` wires `htmlFor`, `aria-describedby`, and `aria-invalid`, so an error is
  announced when focus reaches the control it belongs to.
- `SegmentedControl` is a `radiogroup` with roving tabindex and arrow keys, not
  four separate tab stops.
- `Chip`'s remove button is a sibling of the chip, not nested inside it —
  nested buttons are invalid HTML and the inner one was unreachable.
- `SecretKey` masks the credential until revealed, and can copy without
  revealing. The legacy `.key` printed a freshly rotated key straight into the
  page.

## Gotchas when porting panels

- **`.pill.warn` was red.** In the old CSS `warn` used the error red and
  `pending` carried the amber, so `denied` and `critical` rendered identically to
  a real warning. Tones here are named for meaning: a legacy `.pill.warn` becomes
  `tone="danger"`, and `.pill.pending` becomes `tone="warn"`.
- **`toast(msg, true)` → `toast.error(msg)`**, and errors now stay until
  dismissed. 2.6 seconds was not enough to read a policy denial, let alone copy
  one into a ticket. Pass `durationMs` to override.
- **`DataTable` needs a real `rowKey`.** Index keys break on polled tables that
  reorder — React reuses the wrong row's DOM.
- **Class names are all `ui-`-prefixed.** The legacy stylesheet styles bare
  `button`, `input`, `table`, and `label` selectors, so an unprefixed kit would be
  restyled by it wherever the two coexist. Every component also restates the
  properties that stylesheet sets, so the kit renders correctly even inside a page
  that still loads it.
- **The font is Inter, loaded from a CDN — and that is a live constraint.**
  `index.html` pulls Inter variable from `fonts.googleapis.com`. The in-between
  weights the kit uses (550, 650) only render on a variable font, so if that
  request fails — restricted egress, offline deployment — the app falls back to
  Segoe UI / SF and the type hierarchy flattens. `display=optional` keeps a cold
  load from re-flowing, but the CDN dependency itself is unresolved; self-hosting
  the woff2 is the fix. (An earlier version of this file claimed the kit used no
  webfont. That stopped being true and the reasoning behind it still stands.)

## When mounting starts

1. `<ToastProvider>` goes near the root, above anything calling `useToast()` —
   the hook throws without it rather than silently no-oping, since a swallowed
   error toast hides the failure it was reporting.
2. Keep `src/index.css` as-is. It defines the unprefixed `--text` / `--bg` /
   `--ok` family that `ConnectionStatus` uses; `theme.css` only adds `--ui-*`
   names, so the two do not collide.
3. Port one panel at a time. The legacy paths stay registered under the bare
   prefix (see `gateway/backend/__init__.py`), so `static/app.html` keeps working
   while React panels come online against `/backend`.
