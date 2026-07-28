import { Button } from '../ui'
import type { UseComposerResult } from '../../hooks/useComposer'
import { useTypewriter } from '../../hooks/useTypewriter'
import './Composer.css'

/**
 * The ask box itself — presentational only; all behavior lives in
 * `useComposer`. Shared verbatim by Home's hero and pinned-bottom composer, so
 * the two can never drift apart.
 */

export interface ComposerProps {
  composer: UseComposerResult
  busy: boolean
  placeholder?: string
  /** Seeded example asks to cycle through as a typewriter effect, replacing
   *  `placeholder` while the draft is empty. Opt-in (Home's hero only) — a
   *  pinned follow-up composer mid-thread doesn't want its placeholder
   *  rewriting itself under the user's eye. */
  typewriterExamples?: string[]
  autoFocus?: boolean
  /** Unique per mount so two composers on screen at once (shouldn't normally
   *  happen, but Home's hero/thread branches are mutually exclusive renders
   *  of the SAME instance) never collide on the label's `htmlFor`. */
  idPrefix?: string
}

function Composer({
  composer,
  busy,
  placeholder = 'e.g. Show recent orders for Acme Dental',
  typewriterExamples,
  autoFocus = true,
  idPrefix = 'ask',
}: ComposerProps) {
  const inputId = `${idPrefix}-input`

  const isEmpty = composer.draft.length === 0
  const typed = useTypewriter({
    words: typewriterExamples ?? [],
    enabled: Boolean(typewriterExamples?.length) && isEmpty,
  })
  const effectivePlaceholder = typewriterExamples?.length ? typed : placeholder

  return (
    <form
      className="chat-composer"
      onSubmit={(e) => {
        e.preventDefault()
        composer.ask(composer.draft)
      }}
    >
      <label className="ui-sr-only" htmlFor={inputId}>
        Ask the governed assistant
      </label>
      <textarea
        id={inputId}
        ref={composer.textareaRef}
        className="chat-composer-input"
        value={composer.draft}
        onChange={(e) => composer.setDraft(e.target.value)}
        onKeyDown={composer.onKeyDown}
        placeholder={effectivePlaceholder}
        rows={1}
        autoComplete="off"
        autoFocus={autoFocus}
        spellCheck
      />

      <div className="chat-composer-foot">
        <p className="chat-composer-hint">
          {composer.pendingBlank ? (
            <span className="chat-composer-blank">
              Replace <code>{`{${composer.pendingBlank.name}}`}</code> before asking
            </span>
          ) : (
            <>
              <kbd>Enter</kbd> to ask · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
            </>
          )}
        </p>
        <div className="chat-composer-actions">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={composer.saveDraft}
            title="Save this ask to Quick asks"
            aria-label="Save this ask to Quick asks"
          >
            ★
          </Button>
          <Button
            type="submit"
            disabled={composer.draft.trim().length === 0 || busy}
            loading={busy}
            icon="↑"
          >
            Ask
          </Button>
        </div>
      </div>
    </form>
  )
}

export default Composer
