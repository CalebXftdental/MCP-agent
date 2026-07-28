/**
 * Client-side-only format check for the IP allowlist input. The backend
 * (governance_core/store) accepts any string verbatim and never validates —
 * a malformed entry just silently never matches anything at enforcement
 * time (edge.py's `ip_network(..., strict=False)` skips what it can't
 * parse). This exists purely so a typo is caught here instead of quietly
 * doing nothing in production.
 */

const IPV4_OCTET = /^\d{1,3}$/

function isValidIPv4(addr: string): boolean {
  const octets = addr.split('.')
  return octets.length === 4 && octets.every((o) => IPV4_OCTET.test(o) && Number(o) <= 255)
}

/** Loose but effective: real IPv6 addresses are hex groups and colons only,
 *  and this is a format hint, not a full parser. */
function isValidIPv6(addr: string): boolean {
  return /^[0-9a-fA-F:]+$/.test(addr) && addr.includes(':') && addr.split(':').length <= 8
}

/** Returns an error message, or null when the entry looks like a usable
 *  CIDR or bare IP address (a bare address is valid — the backend treats it
 *  as a /32 or /128 at enforcement time, it just doesn't normalize it in
 *  storage). */
export function validateCidr(raw: string): string | null {
  const value = raw.trim()
  if (!value) return 'Enter a CIDR or IP address'

  const slash = value.indexOf('/')
  const addr = slash === -1 ? value : value.slice(0, slash)
  const prefix = slash === -1 ? undefined : value.slice(slash + 1)

  if (prefix !== undefined && !/^\d{1,3}$/.test(prefix)) return 'Prefix must be a number'

  if (addr.includes(':')) {
    if (!isValidIPv6(addr)) return 'Not a valid IPv6 address'
    if (prefix !== undefined && Number(prefix) > 128) return 'IPv6 prefix must be 0–128'
    return null
  }

  if (!isValidIPv4(addr)) return 'Not a valid IPv4 address'
  if (prefix !== undefined && Number(prefix) > 32) return 'IPv4 prefix must be 0–32'
  return null
}
