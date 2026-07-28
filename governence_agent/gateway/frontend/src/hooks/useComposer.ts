/**
 * The ask box's behavior, shared by Home's hero composer and its pinned-bottom
 * thread composer: draft text, `{blank}` detection and fill-on-click, autogrow,
 * save-to-Quick-asks, and the actual send.
 *
 * Pulled out of the page component on general principle — state and markup
 * belong apart, not because a second page shares it today.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, RefObject } from 'react'
import type { ToastApi } from '../components/ui'
import type { StoredList } from './useStoredList'
import type { UseChatResult } from './useChat'

const PLACEHOLDER = /\{([^}]+)\}/

export interface Blank {
  start: number
  end: number
  name: string
}

/** Exported for QuickAsks, which needs to know a template still has an
 *  unfilled blank before it can be sent outright vs. loaded for editing —
 *  though today every quick ask always goes through `applyTemplate`, never a
 *  direct send, so this mainly documents the shared definition of "blank". */
export function firstBlank(text: string): Blank | null {
  const match = PLACEHOLDER.exec(text)
  if (!match || match.index === undefined) return null
  return { start: match.index, end: match.index + match[0].length, name: match[1] }
}

export interface UseComposerOptions {
  chat: UseChatResult
  toast: ToastApi
  savedAsks: StoredList<string>
}

export interface UseComposerResult {
  draft: string
  setDraft: (value: string) => void
  textareaRef: RefObject<HTMLTextAreaElement | null>
  pendingBlank: Blank | null
  /** Sends `text` if it has no unfilled blank; otherwise loads it into the
   *  draft with the blank selected and warns instead of sending. Used both for
   *  the composer's own submit and for follow-up/quick-ask chips. */
  ask: (text: string) => void
  /** Loads a quick-ask template into the draft with its first blank selected,
   *  without attempting to send it. */
  applyTemplate: (template: string) => void
  saveDraft: () => void
  onKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => void
}

export function useComposer({ chat, toast, savedAsks }: UseComposerOptions): UseComposerResult {
  const [draft, setDraft] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  // Bumped to ask the effect below to focus the composer and select its first
  // blank. A counter rather than a boolean, so two clicks on the same chip both
  // re-select instead of the second being a no-op.
  const [focusTick, setFocusTick] = useState(0)

  useEffect(() => {
    if (focusTick === 0) return
    const el = textareaRef.current
    if (!el) return
    el.focus()
    const blank = firstBlank(el.value)
    if (blank) el.setSelectionRange(blank.start, blank.end)
    else el.setSelectionRange(el.value.length, el.value.length)
  }, [focusTick])

  // Grows with its content up to the max-height in CSS, then scrolls. Reset to
  // 'auto' first, or the height only ever ratchets upward.
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [draft])

  const ask = useCallback(
    (text: string) => {
      const trimmed = text.trim()
      if (!trimmed) return

      const blank = firstBlank(trimmed)
      if (blank) {
        setDraft(trimmed)
        setFocusTick((n) => n + 1)
        toast.warn(`Fill in ${blank.name} before asking`)
        return
      }

      chat.send(trimmed)
      setDraft('')
    },
    [chat, toast],
  )

  const applyTemplate = useCallback((template: string) => {
    setDraft(template)
    setFocusTick((n) => n + 1)
  }, [])

  const saveDraft = useCallback(() => {
    const text = draft.trim()
    if (!text) {
      toast.warn('Type an ask first, then save it')
      return
    }
    savedAsks.add(text)
    toast.success('Saved to Quick asks')
  }, [draft, savedAsks, toast])

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Enter asks, Shift+Enter is a newline — the convention for a
      // multi-line composer. The legacy single-line input had no way to write
      // a two-line question at all.
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        ask(draft)
      }
    },
    [ask, draft],
  )

  return {
    draft,
    setDraft,
    textareaRef,
    pendingBlank: firstBlank(draft),
    ask,
    applyTemplate,
    saveDraft,
    onKeyDown,
  }
}
