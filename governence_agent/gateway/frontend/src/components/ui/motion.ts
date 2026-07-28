/** Motion helpers shared by the kit. CSS covers most animation; these three
 *  cover the cases it can't: reading the user's preference from JS, animating a
 *  number, and keeping an element mounted long enough to animate out. */

import { useEffect, useRef, useState } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

/** Live `prefers-reduced-motion`. Live, not read-once: the OS setting can change
 *  while the app is open, and a stale `true` would freeze every animation. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => typeof matchMedia === 'function' && matchMedia(QUERY).matches,
  )

  useEffect(() => {
    if (typeof matchMedia !== 'function') return
    const mq = matchMedia(QUERY)
    const onChange = () => setReduced(mq.matches)
    setReduced(mq.matches)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  return reduced
}

export interface CountUpOptions {
  /** Total animation time. */
  durationMs?: number
  /** Skip the animation (already-seen values, dense tables). */
  disabled?: boolean
  /** Decimal places to keep while interpolating. */
  decimals?: number
}

/**
 * Counts from the previous value to `value`. Returns `value` unchanged when the
 * user prefers reduced motion, when disabled, or on the very first render — a
 * KPI that ticks up from 0 every time a poll returns is noise, so only genuine
 * changes animate.
 */
export function useCountUp(value: number, options: CountUpOptions = {}): number {
  const { durationMs = 650, disabled = false, decimals = 0 } = options
  const reduced = useReducedMotion()
  const still = disabled || reduced || !Number.isFinite(value)

  const [shown, setShown] = useState(value)
  const fromRef = useRef(value)
  const firstRef = useRef(true)

  useEffect(() => {
    if (still) {
      fromRef.current = value
      setShown(value)
      return
    }
    // First paint lands on the real number; only later changes animate.
    if (firstRef.current) {
      firstRef.current = false
      fromRef.current = value
      setShown(value)
      return
    }

    const from = fromRef.current
    const delta = value - from
    if (delta === 0) return

    const round = 10 ** decimals
    let frame = 0
    let start: number | null = null

    const step = (now: number) => {
      start ??= now
      const t = Math.min(1, (now - start) / durationMs)
      // Ease-out cubic: fast commit, gentle settle — matches --ui-ease-out.
      const eased = 1 - (1 - t) ** 3
      setShown(Math.round((from + delta * eased) * round) / round)
      if (t < 1) frame = requestAnimationFrame(step)
      else fromRef.current = value
    }

    frame = requestAnimationFrame(step)
    return () => cancelAnimationFrame(frame)
  }, [value, still, durationMs, decimals])

  return still ? value : shown
}

export type PresenceState = 'entering' | 'open' | 'leaving'

/**
 * Keeps a component mounted through its exit animation.
 *
 * React unmounts the moment `open` flips false, which kills any CSS exit
 * transition. This holds `mounted` true for `exitMs` longer and exposes the
 * phase for `data-state`, so one element can animate both ways.
 */
export function usePresence(open: boolean, exitMs = 200) {
  const reduced = useReducedMotion()
  const [mounted, setMounted] = useState(open)
  const [state, setState] = useState<PresenceState>(open ? 'open' : 'leaving')

  useEffect(() => {
    if (open) {
      setMounted(true)
      setState('entering')
      // Next frame, so the entering styles are actually painted before the
      // transition to 'open' — setting both in one tick animates nothing.
      const frame = requestAnimationFrame(() => setState('open'))
      return () => cancelAnimationFrame(frame)
    }

    setState('leaving')
    const timer = setTimeout(() => setMounted(false), reduced ? 0 : exitMs)
    return () => clearTimeout(timer)
    // `mounted` is deliberately not a dependency: it changes as a RESULT of this
    // effect, and re-running would restart the entrance at 'entering' one frame
    // after it reached 'open', which reads as a flicker.
  }, [open, exitMs, reduced])

  return { mounted, state }
}
