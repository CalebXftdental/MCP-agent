/**
 * The assistant's tiny markdown dialect, ported from `mdToHtml()` in
 * static/app.html: fenced code, GFM tables, `#`/`##`/`###` headings folded to one
 * visual weight, `-`/`*` lists, and inline `**bold**` / `*em*` / `` `code` ``.
 *
 * Reimplemented as a React tree instead of an HTML string. The legacy version
 * built markup with `esc()` + string concatenation and set it via
 * `innerHTML` — safe there because `esc()` ran on every interpolated value, but
 * the moment this was ported to JSX as `dangerouslySetInnerHTML` it would be one
 * missed `esc()` call away from an XSS hole. Returning React nodes makes that
 * class of bug impossible: text is text, never markup, unless it flows through
 * one of the three tag-producing branches below.
 *
 * Regexes and control flow are kept line-for-line equivalent to the original so
 * the two renderers never disagree on borderline input.
 */

import type { ReactNode } from 'react'

const BOLD = /\*\*(.+?)\*\*/g
const CODE = /`([^`]+)`/g
// (?<![\w.]) is deliberately absent here, matching the legacy regex — this is
// prose emphasis, not the module-reference guard used elsewhere in the app.
const EM = /(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?]|$)/g
const HEADING = /^(#{1,3})\s+(.*)$/
const LIST_ITEM = /^\s*[-*]\s+(.*)$/

/** Splits `**bold**`, `` `code` ``, and `*em*` out of a line of text into React
 *  nodes. Order matters: bold's `**` must be consumed before `*em*`'s single-star
 *  pattern gets a chance at it, exactly as the legacy chain did. */
function inline(text: string, keyPrefix: string): ReactNode[] {
  type Token = { text: string } | { bold: string } | { code: string } | { em: string }
  let tokens: Token[] = [{ text }]

  const split = (
    matcher: RegExp,
    wrap: (text: string) => Token,
  ) => {
    tokens = tokens.flatMap((token) => {
      if (!('text' in token)) return [token]
      const parts: Token[] = []
      let last = 0
      matcher.lastIndex = 0
      let match: RegExpExecArray | null
      while ((match = matcher.exec(token.text))) {
        if (match.index > last) parts.push({ text: token.text.slice(last, match.index) })
        // EM's leading capture group (whitespace/paren before the `*`) belongs
        // outside the emphasis, same as the legacy `$1<em>$2</em>` replacement.
        if (match.length === 3 && match[1] !== undefined) {
          if (match[1]) parts.push({ text: match[1] })
          parts.push(wrap(match[2]))
        } else {
          parts.push(wrap(match[1]))
        }
        last = match.index + match[0].length
      }
      if (last < token.text.length) parts.push({ text: token.text.slice(last) })
      return parts
    })
  }

  split(BOLD, (t) => ({ bold: t }))
  split(CODE, (t) => ({ code: t }))
  split(EM, (t) => ({ em: t }))

  return tokens.map((token, i) => {
    const key = `${keyPrefix}-${i}`
    if ('bold' in token) return <strong key={key}>{token.bold}</strong>
    if ('code' in token) return <code key={key}>{token.code}</code>
    if ('em' in token) return <em key={key}>{token.em}</em>
    return token.text
  })
}

function cells(row: string): string[] {
  return row
    .replace(/^\s*\|/, '')
    .replace(/\|\s*$/, '')
    .split('|')
    .map((c) => c.trim())
}

/** True for a GFM table's separator row: `---|:--:|--` and similar. */
function isSeparatorRow(line: string): boolean {
  return /-/.test(line) && /^[\s:|-]+$/.test(line.trim())
}

export interface MarkdownTable {
  head: string[]
  rows: string[][]
}

export interface MarkdownResult {
  nodes: ReactNode
  /** Every GFM table found, in document order — for the reply's CSV/chart
   *  actions, which the legacy version read back out of the rendered DOM
   *  (`p.querySelector('.mdtable')`). Reading it from the source instead means
   *  those actions work identically whether or not the table happens to still
   *  be mounted. */
  tables: MarkdownTable[]
}

/** Renders the assistant's markdown subset to React nodes, plus the tables found
 *  along the way. */
export function renderMarkdown(source: string): MarkdownResult {
  const lines = (source ?? '').split('\n')
  const blocks: ReactNode[] = []
  const tables: MarkdownTable[] = []
  let listItems: ReactNode[] | null = null
  let i = 0

  const closeList = () => {
    if (listItems) {
      blocks.push(<ul key={`ul-${blocks.length}`}>{listItems}</ul>)
      listItems = null
    }
  }

  while (i < lines.length) {
    const line = lines[i].replace(/\s+$/, '')

    if (/^```/.test(line)) {
      closeList()
      i++
      const code: string[] = []
      while (i < lines.length && !/^```/.test(lines[i])) {
        code.push(lines[i])
        i++
      }
      i++ // closing fence
      blocks.push(
        <pre className="mdcode" key={`code-${blocks.length}`}>
          <code>{code.join('\n')}</code>
        </pre>,
      )
      continue
    }

    if (line.includes('|') && i + 1 < lines.length && isSeparatorRow(lines[i + 1])) {
      closeList()
      const head = cells(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(cells(lines[i]))
        i++
      }
      tables.push({ head, rows })
      const key = `table-${blocks.length}`
      blocks.push(
        <div className="mdtable-wrap" key={key}>
          <table className="mdtable">
            <thead>
              <tr>
                {head.map((h, c) => (
                  <th key={c}>{inline(h, `${key}-h${c}`)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, c) => (
                    <td key={c}>{inline(cell, `${key}-r${r}c${c}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      )
      continue
    }

    const heading = line.match(HEADING)
    if (heading) {
      closeList()
      const key = `h-${blocks.length}`
      blocks.push(<div className="mdh" key={key}>{inline(heading[2], key)}</div>)
      i++
      continue
    }

    const item = line.match(LIST_ITEM)
    if (item) {
      if (!listItems) listItems = []
      listItems.push(<li key={listItems.length}>{inline(item[1], `li-${blocks.length}-${listItems.length}`)}</li>)
      i++
      continue
    }

    closeList()
    const key = `p-${blocks.length}`
    blocks.push(
      line.trim() === '' ? (
        <div className="mdspace" key={key} />
      ) : (
        <div key={key}>{inline(line, key)}</div>
      ),
    )
    i++
  }

  closeList()
  return { nodes: blocks, tables }
}

/** Flattens a table to CSV text, quoting per RFC 4180 — replaces
 *  `tableToCsv(table)`, which read a live `<table>` element; this reads the
 *  parsed structure instead. */
export function tableToCsv(table: MarkdownTable): string {
  const quote = (value: string) => {
    const v = value.trim()
    return /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v
  }
  return [table.head, ...table.rows].map((row) => row.map(quote).join(',')).join('\n')
}

export interface ChartItem {
  k: string
  n: number
}

/** The last numeric column in each row becomes the bar value, matching
 *  `chartFromTable()` — a totals or count column is usually rightmost. */
export function chartFromTable(table: MarkdownTable): ChartItem[] {
  const items: ChartItem[] = []
  for (const row of table.rows) {
    if (row.length === 0) continue
    let value: number | null = null
    for (let c = row.length - 1; c >= 1; c--) {
      const n = Number.parseFloat(row[c].replace(/[^0-9.-]/g, ''))
      if (!Number.isNaN(n)) {
        value = n
        break
      }
    }
    if (value != null) items.push({ k: row[0].trim(), n: value })
  }
  return items
}
