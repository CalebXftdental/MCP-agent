import { useEffect, useState } from 'react'
import type { CSSProperties, FormEvent } from 'react'
import { Button, Dropdown, Field, Input, type DropdownOption } from '../components/ui'
import { ApiError, getDepartments, login, signup, type Department } from '../lib/api'
import type { Session } from '../hooks/useSession'
import './LoginPage.css'

/**
 * Replaces the legacy `_LOGIN_HTML`/`static/signup.html` pair (both now
 * deleted server-side — every signed-out entry point serves the built SPA
 * unconditionally and lets this component decide what to show). One page,
 * two modes, because sign-in and create-account are the same card with
 * different fields, not two different destinations.
 *
 * `/login` and `/signup` are the canonical URLs for the two modes (`pages.py`
 * serves the same shell for both; `initialMode()`/`switchMode()` below are
 * what actually pick and reflect the mode). `/` itself still falls back to
 * this same sign-in view for a session that's anonymous or expired — that's
 * the one legitimate reason to land here without either path — but is no
 * longer meant to be linked to as a login page. `/dashboard` and
 * `/dashboard/signup`, the original pairing, are fully deprecated now: both
 * just redirect server-side (to `/` and `/signup` respectively) rather than
 * rendering anything of their own.
 *
 * Rendered by `App.tsx` in place of `AppShell` whenever `session.status` is
 * `'anonymous'` — not nested inside `AppShell`'s content slot, so it isn't
 * framed by a sidebar advertising panels there's no session to open yet. That
 * same `session.status` flip is also how a mid-session expiry lands back
 * here: `lib/api.ts`'s `request()` fires a `gov:session-expired` event on any
 * 401, `useSession` re-checks itself, and `sessionExpired` is passed through
 * so the sign-in side can say *why* it reappeared instead of doing so
 * silently.
 */

type Mode = 'signin' | 'signup'

function initialMode(): Mode {
  return window.location.pathname === '/signup' ? 'signup' : 'signin'
}

function EyeIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
      <path
        d="M1 10s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <circle cx="10" cy="10" r="2.6" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}

function EyeOffIcon() {
  return (
    <svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true">
      <path
        d="M1 10s3.5-6 9-6c1.7 0 3.2.5 4.5 1.2M19 10s-1 1.8-2.8 3.3M10 16c-5.5 0-9-6-9-6a15 15 0 0 1 3-3.6"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M2 2l16 16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  )
}

interface PasswordFieldProps {
  label: string
  value: string
  onChange: (value: string) => void
  autoComplete: string
  hint?: string
}

function PasswordField({ label, value, onChange, autoComplete, hint }: PasswordFieldProps) {
  const [show, setShow] = useState(false)
  return (
    <Field label={label} required hint={hint}>
      {(props) => (
        <div className="login-password-wrap">
          <Input
            {...props}
            type={show ? 'text' : 'password'}
            required
            autoComplete={autoComplete}
            value={value}
            onChange={(e) => onChange(e.target.value)}
            className="login-password-input"
          />
          <button
            type="button"
            className="login-password-toggle"
            onClick={() => setShow((s) => !s)}
            aria-label={show ? 'Hide password' : 'Show password'}
          >
            {show ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        </div>
      )}
    </Field>
  )
}

function errorMessage(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : 'Could not reach the gateway. Is it running?'
}

interface LoginPageProps {
  session: Session
}

function LoginPage({ session }: LoginPageProps) {
  const [mode, setMode] = useState<Mode>(initialMode)

  const [departments, setDepartments] = useState<Department[]>([])
  const [deptState, setDeptState] = useState<'loading' | 'ready' | 'error'>('loading')

  const [signinUsername, setSigninUsername] = useState('')
  const [signinPassword, setSigninPassword] = useState('')
  const [signinError, setSigninError] = useState<string | null>(null)
  const [signinBusy, setSigninBusy] = useState(false)

  const [fullName, setFullName] = useState('')
  const [signupUsername, setSignupUsername] = useState('')
  const [signupPassword, setSignupPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [departmentId, setDepartmentId] = useState('')
  const [signupError, setSignupError] = useState<string | null>(null)
  const [signupBusy, setSignupBusy] = useState(false)

  useEffect(() => {
    let live = true
    getDepartments()
      .then((result) => {
        if (!live) return
        setDepartments(result.departments ?? [])
        setDeptState('ready')
      })
      .catch(() => {
        if (!live) return
        setDeptState('error')
      })
    return () => {
      live = false
    }
  }, [])

  function switchMode(next: Mode) {
    setMode(next)
    // Always the canonical /login or /signup from here on, even if this page
    // was reached some other way (e.g. /dashboard, for a session that expired
    // mid-use) -- once someone's actively choosing a mode, the address bar
    // should reflect the real entry point for it.
    const path = next === 'signup' ? '/signup' : '/login'
    if (window.location.pathname !== path) window.history.replaceState(null, '', path)
  }

  async function submitSignIn(e: FormEvent) {
    e.preventDefault()
    const username = signinUsername.trim()
    if (!username || !signinPassword) {
      setSigninError('Enter your username and password.')
      return
    }
    setSigninBusy(true)
    setSigninError(null)
    try {
      await login(username, signinPassword)
      window.history.replaceState(null, '', '/')
      session.refresh()
    } catch (cause) {
      setSigninError(errorMessage(cause))
    } finally {
      setSigninBusy(false)
    }
  }

  async function submitSignUp(e: FormEvent) {
    e.preventDefault()
    const name = fullName.trim()
    const username = signupUsername.trim()
    if (!name || !username || !signupPassword || !departmentId) {
      setSignupError('Fill in every field and choose a department.')
      return
    }
    if (signupPassword !== confirmPassword) {
      setSignupError("Passwords don't match.")
      return
    }
    setSignupBusy(true)
    setSignupError(null)
    try {
      await signup({ full_name: name, username, password: signupPassword, department: departmentId })
      window.history.replaceState(null, '', '/')
      session.refresh()
    } catch (cause) {
      setSignupError(errorMessage(cause))
    } finally {
      setSignupBusy(false)
    }
  }

  const departmentOptions: DropdownOption[] = departments.map((d) => ({ value: d.id, label: d.display_name }))
  const departmentPlaceholder =
    deptState === 'loading' ? 'Loading departments…' : deptState === 'error' ? "Couldn't load departments" : 'Choose a department…'

  return (
    <div className="login-screen">
      <div className="login-panel login-panel--brand" aria-hidden="true">
        <div className="login-grid-texture" />
        <div className="login-blob login-blob--a" />
        <div className="login-blob login-blob--b" />
        <div className="login-blob login-blob--c" />
        <div className="login-brand-content">
          <div className="login-mark-tile">
            <img className="login-mark" src="/frontier-mark.png" alt="" />
          </div>
          <p className="login-brand-eyebrow">Frontier Dental</p>
          <h1 className="login-brand-title">Frontier MCP Workspace</h1>
          <p className="login-brand-tagline">
            One governed door into every backend, tool, and workflow your team touches.
          </p>
          <div className="login-stat-row">
            <div className="login-stat-chip" style={{ '--ui-i': 0 } as CSSProperties}>
              <span className="login-stat-value">100%</span>
              <span className="login-stat-label">Calls audited</span>
            </div>
            <div className="login-stat-chip" style={{ '--ui-i': 1 } as CSSProperties}>
              <span className="login-stat-value">Live</span>
              <span className="login-stat-label">Policy checks</span>
            </div>
            <div className="login-stat-chip" style={{ '--ui-i': 2 } as CSSProperties}>
              <span className="login-stat-value">Gated</span>
              <span className="login-stat-label">Sensitive actions</span>
            </div>
          </div>
        </div>
      </div>

      <div className="login-panel login-panel--form">
        <div className="login-card-wrap">
          <div className="login-card" key={mode}>
            <img className="login-card-logo" src="/frontier-mark.png" alt="Frontier MCP Workspace" />

            {mode === 'signin' ? (
              <>
                <h2 className="login-card-title">Sign in</h2>
                <p className="login-card-sub">Sign in to the control plane.</p>

                {session.sessionExpired && (
                  <p className="login-banner login-banner--warn" role="status">
                    Your session expired — sign in again to continue.
                  </p>
                )}

                <form className="login-form" onSubmit={submitSignIn} noValidate>
                  <Field label="Username" required>
                    {(props) => (
                      <Input
                        {...props}
                        required
                        autoFocus
                        autoComplete="username"
                        value={signinUsername}
                        onChange={(e) => setSigninUsername(e.target.value)}
                      />
                    )}
                  </Field>

                  <PasswordField
                    label="Password"
                    value={signinPassword}
                    onChange={setSigninPassword}
                    autoComplete="current-password"
                  />

                  {signinError && (
                    <p className="login-banner login-banner--danger" role="alert" aria-live="polite">
                      {signinError}
                    </p>
                  )}

                  <Button type="submit" block loading={signinBusy}>
                    Sign in
                  </Button>
                </form>

                <p className="login-switch">
                  No account?{' '}
                  <button type="button" className="login-switch-link" onClick={() => switchMode('signup')}>
                    Create one
                  </button>
                </p>
              </>
            ) : (
              <>
                <h2 className="login-card-title">Create your account</h2>
                <p className="login-card-sub">Join your team's governed workspace.</p>

                <form className="login-form" onSubmit={submitSignUp} noValidate>
                  <Field label="Full name" required>
                    {(props) => (
                      <Input
                        {...props}
                        required
                        autoFocus
                        autoComplete="name"
                        value={fullName}
                        onChange={(e) => setFullName(e.target.value)}
                      />
                    )}
                  </Field>

                  <Field label="Username" required>
                    {(props) => (
                      <Input
                        {...props}
                        required
                        autoComplete="username"
                        value={signupUsername}
                        onChange={(e) => setSignupUsername(e.target.value)}
                      />
                    )}
                  </Field>

                  <Field label="Department" required hint="Sets what you can access — an admin can widen it later.">
                    {(props) => (
                      <Dropdown
                        {...props}
                        value={departmentId}
                        onChange={setDepartmentId}
                        options={departmentOptions}
                        placeholder={departmentPlaceholder}
                        disabled={deptState !== 'ready'}
                      />
                    )}
                  </Field>

                  <PasswordField
                    label="Password"
                    value={signupPassword}
                    onChange={setSignupPassword}
                    autoComplete="new-password"
                  />

                  <PasswordField
                    label="Confirm password"
                    value={confirmPassword}
                    onChange={setConfirmPassword}
                    autoComplete="new-password"
                  />

                  {signupError && (
                    <p className="login-banner login-banner--danger" role="alert" aria-live="polite">
                      {signupError}
                    </p>
                  )}

                  <Button type="submit" block loading={signupBusy}>
                    Create account
                  </Button>
                </form>

                <p className="login-switch">
                  Already have an account?{' '}
                  <button type="button" className="login-switch-link" onClick={() => switchMode('signin')}>
                    Sign in
                  </button>
                </p>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export default LoginPage
