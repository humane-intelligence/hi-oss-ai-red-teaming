/**
 * A v4 UUID that works on any origin.
 *
 * `crypto.randomUUID` is secure-context-only, so it is absent when the console is reached
 * over a LAN IP rather than localhost (and on browsers predating it). `getRandomValues` is
 * not gated and draws from the same CSPRNG, so the fallback is equally strong —
 * `Math.random()` is deliberately not used, it is not cryptographically secure.
 */
export function uuid(): string {
  if (crypto.randomUUID) return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
