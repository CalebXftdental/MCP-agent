import './SessionBoot.css'

/**
 * Shown for `session.status === 'loading'` — the gap between mount and the
 * first `/dashboard/me` response, which decides whether `App` renders
 * `AppShell` or `LoginPage`.
 *
 * That decision used to have no screen of its own: `'loading'` fell into
 * whichever branch handled "not anonymous," which was `AppShell` — so every
 * fresh visit (and every hard refresh) briefly rendered the full dashboard
 * chrome and a page's worth of content, then yanked it away for the sign-in
 * screen the instant `/dashboard/me` came back 401. A neutral boot screen
 * closes that gap: nobody sees the shell, or the login card, until we
 * actually know which one is correct.
 *
 * Deliberately NOT reused for a re-check after a live session expiry
 * (`useSession`'s `gov:session-expired` handling) — there, the page already
 * on screen stays put during the re-check and only swaps to `LoginPage` once
 * it's conclusively anonymous, which is the better trade when someone was
 * mid-task: no extra flicker for a re-check we're already fairly sure will
 * confirm what just happened.
 */
function SessionBoot() {
  return (
    <div className="session-boot">
      <div className="session-boot-mark">
        <img src="/frontier-mark.png" alt="" />
      </div>
    </div>
  )
}

export default SessionBoot
