/** Toast context and hook, split out from the provider component so importing
 *  `useToast` doesn't drag a component into the module — which is also what
 *  keeps oxlint's react/only-export-components rule satisfied. */

import { createContext, useContext } from 'react'

export type ToastTone = 'neutral' | 'ok' | 'warn' | 'danger'

export interface ToastOptions {
  tone?: ToastTone
  /** Time on screen. `null` pins it until dismissed — use for anything the user
   *  must act on or copy, like a failed save with a reason. */
  durationMs?: number | null
  /** Second line, for the detail behind a short headline. */
  detail?: string
  /** Single inline action, e.g. Undo or Retry. */
  action?: { label: string; onClick: () => void }
}

export interface Toast extends ToastOptions {
  id: number
  message: string
}

export interface ToastApi {
  /** Full control. Returns the id, so it can be dismissed programmatically. */
  show: (message: string, options?: ToastOptions) => number
  success: (message: string, options?: Omit<ToastOptions, 'tone'>) => number
  /** Errors are pinned by default: the legacy `toast(e.message, true)` faded a
   *  failure reason after 2.6s, which is not long enough to read a policy denial
   *  and far too short to copy one into a ticket. */
  error: (message: string, options?: Omit<ToastOptions, 'tone'>) => number
  warn: (message: string, options?: Omit<ToastOptions, 'tone'>) => number
  dismiss: (id: number) => void
}

export const ToastContext = createContext<ToastApi | null>(null)

/** Requires a <ToastProvider> above it. Throws rather than no-oping, because a
 *  silently swallowed error toast hides the failure it was reporting. */
export function useToast(): ToastApi {
  const api = useContext(ToastContext)
  if (!api) throw new Error('useToast must be used inside <ToastProvider>')
  return api
}
