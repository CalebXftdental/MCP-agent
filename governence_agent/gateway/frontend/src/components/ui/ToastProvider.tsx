import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ToastContext, type Toast, type ToastApi, type ToastOptions } from './toast-context'
import './ToastProvider.css'

/**
 * Replaces the legacy global `toast(msg, bad)`, which wrote into one shared
 * `<div id="toast">`: a second call inside 2.6s overwrote the first, so a burst
 * of failures showed only the last one. Here they stack.
 *
 * Mount once near the app root:
 *
 *   <ToastProvider><App /></ToastProvider>
 *
 * then anywhere below: `const toast = useToast(); toast.error(e.message)`.
 */

export interface ToastProviderProps {
  children: ReactNode
  /** Cap on visible toasts. Older ones drop off the top. */
  max?: number
  corner?: 'bottom-right' | 'bottom-left' | 'top-right' | 'top-left'
}

const DEFAULT_MS = 3200

/** A pending auto-dismiss. `remaining` is what's left of the original duration;
 *  `startedAt` is when the current run began, so pausing can subtract the part
 *  already elapsed. */
interface Countdown {
  timer: ReturnType<typeof setTimeout>
  remaining: number
  startedAt: number
}

function ToastProvider({ children, max = 4, corner = 'bottom-right' }: ToastProviderProps) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)
  const countdowns = useRef(new Map<number, Countdown>())

  const dismiss = useCallback((id: number) => {
    const countdown = countdowns.current.get(id)
    if (countdown) {
      clearTimeout(countdown.timer)
      countdowns.current.delete(id)
    }
    setToasts((list) => list.filter((t) => t.id !== id))
  }, [])

  const arm = useCallback(
    (id: number, ms: number) => {
      countdowns.current.set(id, {
        timer: setTimeout(() => dismiss(id), ms),
        remaining: ms,
        startedAt: Date.now(),
      })
    },
    [dismiss],
  )

  const show = useCallback(
    (message: string, options: ToastOptions = {}) => {
      const id = nextId.current++
      const toast: Toast = { id, message, tone: 'neutral', durationMs: DEFAULT_MS, ...options }

      setToasts((list) => [...list, toast].slice(-max))
      if (toast.durationMs != null) arm(id, toast.durationMs)
      return id
    },
    [arm, max],
  )

  // Hovering the stack pauses every countdown and resumes with the time that was
  // actually left. The CSS progress bar pauses on :hover too, so the bar and the
  // timer stay in agreement — pausing only the bar would leave a toast
  // disappearing while its indicator still showed time remaining.
  const pauseAll = useCallback(() => {
    const now = Date.now()
    for (const [id, countdown] of countdowns.current) {
      clearTimeout(countdown.timer)
      countdowns.current.set(id, {
        ...countdown,
        remaining: Math.max(0, countdown.remaining - (now - countdown.startedAt)),
      })
    }
  }, [])

  const resumeAll = useCallback(() => {
    for (const [id, countdown] of countdowns.current) arm(id, countdown.remaining)
  }, [arm])

  useEffect(() => {
    const pending = countdowns.current
    return () => {
      for (const { timer } of pending.values()) clearTimeout(timer)
      pending.clear()
    }
  }, [])

  // Stable across renders so a component can depend on `toast` in an effect
  // without re-running it every render.
  const api = useMemo<ToastApi>(
    () => ({
      show,
      dismiss,
      success: (message, options) => show(message, { ...options, tone: 'ok' }),
      warn: (message, options) => show(message, { ...options, tone: 'warn' }),
      error: (message, options) => show(message, { durationMs: null, ...options, tone: 'danger' }),
    }),
    [show, dismiss],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      {/* One live region for the whole stack. `polite` yields to whatever the
          user is doing; errors escalate to `assertive` per toast below. */}
      <div
        className={`ui-toasts ui-toasts--${corner}`}
        role="region"
        aria-label="Notifications"
        onPointerEnter={pauseAll}
        onPointerLeave={resumeAll}
        onFocusCapture={pauseAll}
        onBlurCapture={resumeAll}
      >
        {toasts.map((toast) => (
          <output
            key={toast.id}
            className={`ui-toast ui-toast--${toast.tone}`}
            aria-live={toast.tone === 'danger' ? 'assertive' : 'polite'}
          >
            <div className="ui-toast-text">
              <p className="ui-toast-message">{toast.message}</p>
              {toast.detail && <p className="ui-toast-detail">{toast.detail}</p>}
            </div>

            {toast.action && (
              <button
                type="button"
                className="ui-toast-action"
                onClick={() => {
                  toast.action?.onClick()
                  dismiss(toast.id)
                }}
              >
                {toast.action.label}
              </button>
            )}

            <button
              type="button"
              className="ui-toast-close"
              onClick={() => dismiss(toast.id)}
              aria-label="Dismiss notification"
            >
              <span aria-hidden="true">✕</span>
            </button>

            {/* Depleting bar, so an auto-dismissing toast shows how long is left
                instead of vanishing without warning. */}
            {toast.durationMs != null && (
              <span
                className="ui-toast-timer"
                style={{ animationDuration: `${toast.durationMs}ms` }}
                aria-hidden="true"
              />
            )}
          </output>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export default ToastProvider
