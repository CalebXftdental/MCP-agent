/** Client-side file download via an object URL — `downloadText()` from
 *  static/app.html, unchanged: create a blob, click a detached anchor, revoke. */
export function downloadText(filename: string, text: string, mimeType = 'text/plain'): void {
  const anchor = document.createElement('a')
  anchor.href = URL.createObjectURL(new Blob([text], { type: mimeType }))
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(anchor.href)
}
