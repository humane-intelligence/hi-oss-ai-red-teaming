// Where the bearer access token lives between page loads. localStorage is fine for a
// personal, local-only console; revisit if this ever ships. `/api/v1/auth/refresh`
// exists, but nothing here calls it yet — the refresh token rides the OIDC fragment
// unused, ready for whenever that's wired up.
const KEY = 'red.access_token'

export const tokenStore = {
  get: () => localStorage.getItem(KEY),
  set: (token: string) => localStorage.setItem(KEY, token),
  clear: () => localStorage.removeItem(KEY),
}
