import type { CSSProperties, KeyboardEvent, ReactNode } from 'react'
import { Skeleton } from './Skeleton'
import './DataTable.css'

/**
 * One table for the 35 in the old dashboard. They were all the same thing —
 * `.tbl-wrap` > `<table>` with an uppercase head, a hover row, and a
 * `<tr><td colspan=N class="empty">` for both loading and empty — written out
 * long-hand each time, with the colspan hardcoded and therefore wrong wherever
 * a column was later added.
 *
 * Three fixes beyond the DRY:
 *   - colspan comes from `columns.length`, so it can't drift.
 *   - loading renders skeleton rows at full height instead of a one-line
 *     "Loading…", so the page below doesn't jump when data lands.
 *   - clickable rows are keyboard-reachable. In the old UI they carried
 *     `cursor:pointer` and a click handler and nothing else, so the drawer that
 *     opened on row click was mouse-only.
 *
 * Row click uses tabIndex + Enter/Space on the `<tr>` rather than an ARIA grid:
 * a full grid pattern would take over arrow keys and break native table
 * navigation for screen readers, which is a worse trade for a read-only list.
 * When a row's action is anything more than "show me this", put a real Button in
 * a cell instead.
 */

export interface Column<T> {
  /** Stable identity for the column, used as the React key. */
  key: string
  header: ReactNode
  /** Cell content. Defaults to `String(row[key])` when omitted. */
  render?: (row: T, index: number) => ReactNode
  /** Any CSS length, applied to the header cell. */
  width?: string
  align?: 'left' | 'right' | 'center'
  /** Monospace, for ids, tool names, and paths. */
  mono?: boolean
  /** Monospace + tabular numerals + right aligned, so digits column up. */
  numeric?: boolean
  /** Muted text, for timestamps and secondary detail. */
  muted?: boolean
  /** Never wrap. Use sparingly — it forces horizontal scroll. */
  nowrap?: boolean
  /** Header text for a column whose visible header is empty (an actions
   *  column). Rendered screen-reader-only so the column is still announced. */
  srHeader?: string
}

export interface DataTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  /** Stable key per row. Index is a fallback, not a default — polled tables
   *  reorder, and index keys make React reuse the wrong row's DOM. */
  rowKey: (row: T, index: number) => string
  loading?: boolean
  /** How many skeleton rows to show. Match the usual page size. */
  skeletonRows?: number
  /** Shown when `rows` is empty and not loading. */
  empty?: ReactNode
  onRowClick?: (row: T, index: number) => void
  /** Marks the row currently shown in a drawer or detail pane. */
  isRowActive?: (row: T, index: number) => boolean
  /** Describes the table to screen readers. Visually hidden. */
  caption?: string
  /** Tighter rows, for long audit lists. */
  dense?: boolean
  /** Header sticks while the body scrolls. Needs a bounded `maxHeight`. */
  stickyHeader?: boolean
  /** Any CSS length. Turns the wrapper into a vertical scroll region. */
  maxHeight?: string
  /** Strip under the table — counts, pagination, "showing 200 of N". */
  footer?: ReactNode
}

function cellClass<T>(col: Column<T>): string {
  return [
    col.numeric ? 'ui-td--num' : '',
    col.mono && !col.numeric ? 'ui-td--mono' : '',
    col.muted ? 'ui-td--muted' : '',
    col.nowrap ? 'ui-td--nowrap' : '',
    col.align ? `ui-td--${col.align}` : '',
  ]
    .filter(Boolean)
    .join(' ')
}

function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  skeletonRows = 5,
  empty,
  onRowClick,
  isRowActive,
  caption,
  dense = false,
  stickyHeader = false,
  maxHeight,
  footer,
}: DataTableProps<T>) {
  const onKeyDown = (e: KeyboardEvent<HTMLTableRowElement>, row: T, index: number) => {
    if (!onRowClick) return
    // Space scrolls the page by default, and the row is the thing being
    // activated, so both keys are consumed here.
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onRowClick(row, index)
    }
  }

  return (
    <div
      className={`ui-tbl-wrap ${stickyHeader ? 'ui-tbl-wrap--sticky' : ''}`}
      style={maxHeight ? { maxHeight, overflowY: 'auto' } : undefined}
    >
      <table
        className={`ui-tbl ${dense ? 'ui-tbl--dense' : ''} ${onRowClick ? 'ui-tbl--rows-clickable' : ''}`}
        aria-busy={loading || undefined}
      >
        {caption && <caption className="ui-sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                scope="col"
                style={col.width ? { width: col.width } : undefined}
                className={col.align ? `ui-td--${col.align}` : col.numeric ? 'ui-td--right' : ''}
              >
                {col.srHeader ? <span className="ui-sr-only">{col.srHeader}</span> : col.header}
              </th>
            ))}
          </tr>
        </thead>

        <tbody>
          {loading &&
            Array.from({ length: skeletonRows }, (_, i) => (
              <tr key={`sk-${i}`} className="ui-tbl-skeleton-row">
                {columns.map((col, c) => (
                  <td key={col.key}>
                    <Skeleton
                      index={i * columns.length + c}
                      height="0.7rem"
                      width={c === 0 ? '70%' : c === columns.length - 1 ? '40%' : '55%'}
                    />
                  </td>
                ))}
              </tr>
            ))}

          {!loading && rows.length === 0 && (
            <tr className="ui-tbl-empty-row">
              <td colSpan={columns.length}>{empty ?? <span>No data</span>}</td>
            </tr>
          )}

          {!loading &&
            rows.map((row, i) => (
              <tr
                key={rowKey(row, i)}
                className="ui-tbl-row"
                style={{ '--ui-i': i } as CSSProperties}
                data-active={isRowActive?.(row, i) || undefined}
                tabIndex={onRowClick ? 0 : undefined}
                onClick={onRowClick ? () => onRowClick(row, i) : undefined}
                onKeyDown={onRowClick ? (e) => onKeyDown(e, row, i) : undefined}
              >
                {columns.map((col) => (
                  <td key={col.key} className={cellClass(col)}>
                    {col.render ? col.render(row, i) : String((row as Record<string, unknown>)[col.key] ?? '—')}
                  </td>
                ))}
              </tr>
            ))}
        </tbody>
      </table>

      {footer && <div className="ui-tbl-foot">{footer}</div>}
    </div>
  )
}

export default DataTable
