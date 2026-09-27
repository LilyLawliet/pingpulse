import { useCallback, useEffect, useState } from 'react'

const KEY = 'pingpulse.theme'

/** 'light' | 'dark' | 'system'. Anything unreadable counts as system. */
function stored() {
  try {
    const value = localStorage.getItem(KEY)
    return value === 'light' || value === 'dark' ? value : 'system'
  } catch {
    return 'system'
  }
}

/** Put the choice on <html>, where the CSS tokens look for it. */
export function applyTheme(choice = stored()) {
  const root = document.documentElement
  if (choice === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', choice)
}

/** Whether the page is currently painted dark, whatever caused it. */
function resolvedDark(choice) {
  if (choice === 'dark') return true
  if (choice === 'light') return false
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}

export function useTheme() {
  const [choice, setChoice] = useState(stored)
  const [dark, setDark] = useState(() => resolvedDark(stored()))

  useEffect(() => {
    applyTheme(choice)
    setDark(resolvedDark(choice))
    if (choice !== 'system' || !window.matchMedia) return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const follow = () => setDark(query.matches)
    query.addEventListener?.('change', follow)
    return () => query.removeEventListener?.('change', follow)
  }, [choice])

  const pick = useCallback((next) => {
    try {
      if (next === 'system') localStorage.removeItem(KEY)
      else localStorage.setItem(KEY, next)
    } catch {
      // Blocked storage: the choice lasts until the page is closed.
    }
    setChoice(next)
  }, [])

  return { choice, dark, pick }
}
