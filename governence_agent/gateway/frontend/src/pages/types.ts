/** The contract every page implements. Kept in its own module so `routes.ts`
 *  stays import-free and pages don't import each other to get the type. */

import type { Session } from '../hooks/useSession'
import type { RouteDef, RouteKey } from './routes'

export interface PageProps {
  /** This page's own registry entry — title, description, icon, admin flag. */
  route: RouteDef
  session: Session
  /** Tab navigation. Pages never touch `location` directly, so swapping the
   *  hash router for React Router later is a change in one file. */
  navigate: (key: RouteKey) => void
}
