// Only http(s) URLs are linkable — a `javascript:`/`data:` `reference_url` would be a
// stored-XSS-on-click vector (the backend rejects those on write; this is defense-in-depth
// for any legacy row). Callers render anything else as plain text.
export function isHttpUrl(url: string | null | undefined): url is string {
  return !!url && /^https?:\/\//i.test(url)
}
