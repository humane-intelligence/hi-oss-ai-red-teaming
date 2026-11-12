type Named = { first_name?: string | null; last_name?: string | null }

export function fullName(user: Named) {
  return [user.first_name, user.last_name].filter(Boolean).join(' ')
}

export function displayName(user: Named & { email: string }) {
  return fullName(user) || user.email
}
