import { useState } from 'react'

export type Theme = 'light' | 'dark'
const KEY = 'red.theme'

// Dark-first: explicit stored choice, else dark.
export function getTheme(): Theme {
  return localStorage.getItem(KEY) === 'light' ? 'light' : 'dark'
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(KEY, theme)
  document.documentElement.classList.toggle('dark', theme === 'dark')
}

export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(getTheme)
  const toggle = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    setTheme(next)
    setThemeState(next)
  }
  return { theme, toggle }
}
