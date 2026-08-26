/**
 * Every tab in the console, in one table.
 *
 * Ported from the `ROUTES` object and `buildNav()` in gateway/static/app.html,
 * with the titles and descriptions kept verbatim — they are the page copy, and
 * rewording them mid-port would make the two UIs disagree while both are live.
 *
 * The legacy table also held each panel's render function. Here the registry is
 * data only and `pages/index.tsx` maps a key to a component, so a page can be
 * ported without touching navigation and this module stays free of imports.
 *
 * Icons are emoji throughout. The legacy set mixed emoji with two-letter
 * placeholders (`WF`, `AU`, `KB`, `SQ`, `CP`) for panels added later, so the
 * sidebar read as two different designs stacked on top of each other.
 */

export type RouteKey =
  | 'home'
  | 'playground'
  | 'workflows'
  | 'automations'
  | 'my_workflows'
  | 'knowledge'
  | 'files'
  | 'templates'
  | 'sends'
  | 'calendar'
  | 'access'
  | 'developer'
  | 'history'
  | 'consumers'
  | 'categories'
  | 'department-admin'
  | 'requests'
  | 'pending-signups'
  | 'approvals'
  | 'agents'
  | 'code-plans'
  | 'monitor'
  | 'alerts'
  | 'security'
  | 'whitelist'

export interface RouteDef {
  key: RouteKey
  title: string
  /** Sits under the page title in the topbar. */
  description: string
  icon: string
  /** Hidden from non-admins in the sidebar AND refused by `resolveRoute`. The
   *  server enforces the same rule on every admin endpoint — this is only so the
   *  UI doesn't offer a door that won't open. */
  admin?: boolean
}

export const ROUTES: Record<RouteKey, RouteDef> = {
  home: {
    key: 'home',
    // Home IS the assistant now — the composer here answers through the same
    // governed chat the old Assistant tab did, so that tab was retired rather
    // than kept as a second door to the same conversation.
    title: 'Home',
    description: 'Ask about customers, orders, shipments, and invoices — in plain language.',
    icon: '🏠',
  },
  playground: {
    key: 'playground',
    title: 'AI Playground',
    description:
      'Guided AI tasks — draft messages, summarize accounts, and surface insights, all through the governed assistant.',
    icon: '🪄',
  },
  workflows: {
    key: 'workflows',
    title: 'Workflows',
    description: 'Run governed office workflows that turn data into reports, decks, and files.',
    icon: '⚙️',
  },
  automations: {
    key: 'automations',
    title: 'Automations',
    description: 'Recurring governed workflows with deterministic local due-run execution.',
    icon: '⏱️',
  },
  my_workflows: {
    key: 'my_workflows',
    title: 'My Workflow',
    description:
      'Build a custom automation by connecting governed tools, approval gates, and AI steps — no code required.',
    icon: '🧩',
  },
  knowledge: {
    key: 'knowledge',
    title: 'Knowledge',
    description: 'Local document ingestion, search, and citation-backed answers.',
    icon: '📚',
  },
  files: {
    key: 'files',
    title: 'Files',
    description: 'Generated artifacts, classifications, and downloads.',
    icon: '📁',
  },
  templates: {
    key: 'templates',
    title: 'Templates',
    description: 'Reusable report, deck, email, calendar, prompt, and workflow templates.',
    icon: '📄',
  },
  sends: {
    key: 'sends',
    title: 'Send Queue',
    description: 'Approval-gated email delivery queue and connector handoff status.',
    icon: '📧',
  },
  calendar: {
    key: 'calendar',
    title: 'Calendar',
    description: 'Draft invite artifacts and approval-gated calendar connector queue.',
    icon: '📅',
  },
  access: {
    key: 'access',
    title: 'My Access',
    description: 'Your status, granted data domains, and API key.',
    icon: '🔑',
  },
  developer: {
    key: 'developer',
    title: 'Developer',
    description: 'Your API key, an MCP connection snippet, and a playground to test governed tools.',
    icon: '🧰',
  },
  history: {
    key: 'history',
    // Broader than the legacy panel, which was conversations only: this page now
    // also holds the recent-activity table and the pinned answers that used to
    // sit on Home. Conversations themselves are not ported yet, and the page
    // links across to the current console for them.
    title: 'History',
    description: 'Your recent governed calls and the answers you pinned from Home.',
    icon: '🕘',
  },
  consumers: {
    key: 'consumers',
    title: 'Consumers',
    description: 'Principals (people & agents), their categories, and API keys.',
    icon: '👥',
    admin: true,
  },
  categories: {
    key: 'categories',
    title: 'Categories',
    description: 'Data-domain templates: which backend, tools, and sensitivity levels each grants.',
    icon: '🗂️',
    admin: true,
  },
  'department-admin': {
    key: 'department-admin',
    title: 'Departments',
    description:
      'Org-unit groupings of categories, used by self-signup. Editing a department’s categories updates every member’s access immediately — no per-user resync.',
    icon: '🏢',
    admin: true,
  },
  requests: {
    key: 'requests',
    title: 'Access Requests',
    description: 'Approve signups and access asks; review denied-attempt suggestions.',
    icon: '📥',
    admin: true,
  },
  'pending-signups': {
    key: 'pending-signups',
    title: 'Pending Signups',
    description: 'New accounts that verified their email, waiting on admin approval before they can sign in.',
    icon: '🆕',
    admin: true,
  },
  approvals: {
    key: 'approvals',
    title: 'Approvals',
    description: 'External sends and other high-impact actions, waiting for an admin decision.',
    icon: '✅',
    admin: true,
  },
  agents: {
    key: 'agents',
    title: 'Agents',
    description:
      'Autonomous agent identities with allowed workflow templates and schedule constraints.',
    icon: '🤖',
    admin: true,
  },
  'code-plans': {
    key: 'code-plans',
    title: 'Code Plans',
    description: 'Read-only opencode planning, repository reviews, and workflow template drafts.',
    icon: '💻',
    admin: true,
  },
  monitor: {
    key: 'monitor',
    title: 'Monitor',
    description:
      'Live security overview — call volume, authorization, sensitive access, and every governed tool call.',
    icon: '📊',
    admin: true,
  },
  alerts: {
    key: 'alerts',
    title: 'Alerts',
    description:
      'Security incidents — enumeration, denial bursts, and anomalies grouped per principal for triage.',
    icon: '🚨',
    admin: true,
  },
  security: {
    key: 'security',
    title: 'Security',
    description:
      'Backend health and API-key hygiene — dormant keys, unused grants, unrotated credentials.',
    icon: '🔒',
    admin: true,
  },
  whitelist: {
    key: 'whitelist',
    title: 'IP Allowlist',
    description: 'Global CIDR allowlist enforced at the MCP edge.',
    icon: '🛡️',
    admin: true,
  },
}

export interface NavGroup {
  /** Null for the ungrouped first item, matching the legacy sidebar. */
  label: string | null
  keys: RouteKey[]
}

/** Sidebar order and grouping, from `buildNav()`. Admin groups are filtered by
 *  role at render time rather than being a separate list, so adding an admin tab
 *  is one line here. */
export const NAV_GROUPS: NavGroup[] = [
  { label: null, keys: ['home'] },
  { label: 'Ask & Automate', keys: ['playground', 'workflows', 'automations', 'my_workflows'] },
  { label: 'Content', keys: ['knowledge', 'files', 'templates', 'sends', 'calendar'] },
  { label: 'My Account', keys: ['access', 'developer', 'history'] },
  { label: 'Access Control', keys: ['consumers', 'categories', 'department-admin'] },
  { label: 'Requests & Approvals', keys: ['requests', 'pending-signups', 'approvals'] },
  { label: 'Automation Admin', keys: ['agents', 'code-plans'] },
  { label: 'Monitoring & Security', keys: ['monitor', 'alerts', 'security', 'whitelist'] },
]

export const DEFAULT_ROUTE: RouteKey = 'home'

function isRouteKey(value: string): value is RouteKey {
  return Object.hasOwn(ROUTES, value)
}

/**
 * Turns an arbitrary tab string into a route this principal may actually open.
 * Unknown keys and admin tabs requested by a non-admin both fall back to Home,
 * which is what the legacy `currentKey()` and `go()` did between them.
 *
 * @param role null while the session is still loading — treated as non-admin, so
 *             an admin tab is not briefly rendered and then yanked away.
 */
export function resolveRoute(raw: string | null | undefined, role: string | null): RouteKey {
  const key = (raw ?? '').replace(/^\/+/, '').split('/')[0]
  // `/dashboard/admin` was the old entry point for the admin area.
  if (key === 'admin') return role === 'admin' ? 'consumers' : DEFAULT_ROUTE
  if (!isRouteKey(key)) return DEFAULT_ROUTE
  return ROUTES[key].admin && role !== 'admin' ? DEFAULT_ROUTE : key
}

/**
 * Whether `raw` names a real destination at all — the bare root, the `admin`
 * alias, or a real tab key — regardless of whether this role can actually
 * open it. That last case (a real, admin-gated tab requested by a non-admin)
 * still silently lands on Home via `resolveRoute` above, same as always —
 * that's an access decision, not a wrong URL, so it's deliberately not what
 * this reports. This is only for telling "a route that doesn't exist" apart
 * from "Home," which `resolveRoute` alone can't do since it always returns
 * *some* valid key.
 */
export function isKnownPath(raw: string): boolean {
  const key = (raw ?? '').replace(/^\/+/, '').split('/')[0]
  return key === '' || key === 'admin' || isRouteKey(key)
}

/** Nav groups with every tab this role can't reach removed, and now-empty groups
 *  dropped — so a non-admin sees no stray group headings. */
export function visibleNav(role: string | null): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    ...group,
    keys: group.keys.filter((key) => !ROUTES[key].admin || role === 'admin'),
  })).filter((group) => group.keys.length > 0)
}
