/**
 * Types out each word in `words` one character at a time, holds, then deletes
 * and moves on — the classic "typewriter" cycling effect, used for the
 * composer's placeholder. Pure text in, text out: the caller decides where it
 * gets rendered (a placeholder attribute has no room for markup or a blinking
 * cursor element).
 */

import { useEffect, useRef, useState } from 'react'

export interface UseTypewriterOptions {
  words: string[]
  /** Pauses on the current frame instead of advancing — used to stop the
   *  effect once it would be invisible anyway (composer has a draft). */
  enabled?: boolean
  typingMs?: number
  deletingMs?: number
  holdMs?: number
  startDelayMs?: number
}

const REDUCED_MOTION_QUERY = '(prefers-reduced-motion: reduce)'

export function useTypewriter({
  words,
  enabled = true,
  typingMs = 45,
  deletingMs = 25,
  holdMs = 1700,
  startDelayMs = 300,
}: UseTypewriterOptions): string {
  const [text, setText] = useState('')
  // Survives pause/resume (draft typed then cleared) so the effect restarts
  // the SAME word rather than skipping ahead.
  const wordIndexRef = useRef(0)

  useEffect(() => {
    if (!enabled || words.length === 0) return

    if (typeof window !== 'undefined' && window.matchMedia?.(REDUCED_MOTION_QUERY).matches) {
      setText(words[wordIndexRef.current % words.length])
      return
    }

    let cancelled = false
    let timer: ReturnType<typeof setTimeout>

    const step = (phase: 'typing' | 'deleting', charIndex: number) => {
      if (cancelled) return
      const word = words[wordIndexRef.current % words.length]

      if (phase === 'typing') {
        setText(word.slice(0, charIndex))
        if (charIndex < word.length) {
          timer = setTimeout(() => step('typing', charIndex + 1), typingMs)
        } else {
          timer = setTimeout(() => step('deleting', charIndex), holdMs)
        }
        return
      }

      const nextIndex = charIndex - 1
      setText(word.slice(0, nextIndex))
      if (nextIndex > 0) {
        timer = setTimeout(() => step('deleting', nextIndex), deletingMs)
      } else {
        wordIndexRef.current += 1
        timer = setTimeout(() => step('typing', 0), typingMs)
      }
    }

    timer = setTimeout(() => step('typing', 0), startDelayMs)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, words])

  return text
}
