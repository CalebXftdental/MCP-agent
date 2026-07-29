import './FileTypeIcon.css'

/**
 * A colour-coded "page with a folded corner" glyph, monogrammed by artifact
 * type — the same idea real file managers use (Drive, Office) so a grid of
 * files reads at a glance without a thumbnail render. SVG-drawn (matching
 * `PlusIcon`'s reasoning) rather than an icon font, and coloured from the
 * kit's own semantic tokens rather than invented hex values, so a file grid
 * agrees with the rest of the console's palette in both themes.
 */

export interface FileTypeIconProps {
  type: string
  size?: number
}

interface TypeMeta {
  label: string
  color: string
}

const TYPE_META: Record<string, TypeMeta> = {
  word: { label: 'DOC', color: 'var(--ui-info)' },
  excel: { label: 'XLS', color: 'var(--ui-ok)' },
  powerpoint: { label: 'PPT', color: 'var(--ui-warn)' },
  pdf: { label: 'PDF', color: 'var(--ui-danger)' },
  email_draft: { label: 'MAIL', color: 'var(--ui-info)' },
  calendar_invite: { label: 'ICS', color: 'var(--ui-accent-600)' },
  json: { label: '{ }', color: 'var(--ui-text-faint)' },
  text: { label: 'TXT', color: 'var(--ui-text-faint)' },
}

const DEFAULT_META: TypeMeta = { label: 'FILE', color: 'var(--ui-text-faint)' }

function FileTypeIcon({ type, size = 40 }: FileTypeIconProps) {
  const meta = TYPE_META[type] ?? DEFAULT_META

  return (
    <svg
      className="ui-filetype-icon"
      width={size}
      height={size * 1.18}
      viewBox="0 0 34 40"
      role="img"
      aria-label={`${meta.label} file`}
    >
      <path
        d="M4 2h15l11 11v23a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z"
        fill={`color-mix(in srgb, ${meta.color} 13%, var(--ui-surface))`}
        stroke={meta.color}
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M19 2v9a2 2 0 0 0 2 2h9" fill="none" stroke={meta.color} strokeWidth="1.6" strokeLinejoin="round" />
      <text x="17" y="29" textAnchor="middle" fontSize="6.6" fontWeight="750" fill={meta.color}>
        {meta.label}
      </text>
    </svg>
  )
}

export default FileTypeIcon
