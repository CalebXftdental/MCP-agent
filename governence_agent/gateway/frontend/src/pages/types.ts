/** The contract every page implements. Kept in its own module so `routes.ts`
 *  stays import-free and pages don't import each other to get the type. */

import type { Session } from '../hooks/useSession'
import type { RouteDef, RouteKey } from './routes'

export interface PageProps {
  /** This page's own registry entry — title, description, icon, admin flag. */
  route: RouteDef
  session: Session
  /** Tab navigation. Pages never touch `location` directly, so swapping the
   *  hash router for React Router later is a change in one file. An optional
   *  second argument appends `/<param>` to the hash (see `routeParam` below). */
  navigate: (key: RouteKey, param?: string) => void
  /** The segment after this tab in the URL, if any (`#/home/<id>` → `id`).
   *  Null on a bare `#/home`. Currently only Home reads this, to open a
   *  specific conversation deep-linked from History's preview list — a
   *  generic name rather than `conversationId` since any page could grow its
   *  own use for a URL-carried id later. */
  routeParam: string | null
}
