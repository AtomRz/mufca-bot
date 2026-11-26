// Light / dark theme for the dashboard (no external dependency).
//
// The theme is the `data-theme` attribute on <html> ('dark' | 'light'); every colour in
// styles.css is a CSS variable, and :root[data-theme='light'] overrides the palette.
// Dark stays the default. The choice is persisted in localStorage; index.html applies it
// before React renders so a light-theme user doesn't get a dark flash on load.
//
// Canvas / lightweight-charts colours can't use CSS variables — ChartPanel reads `theme`
// from useTheme() and picks its palette from there.

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { useI18n } from './i18n'

const STORAGE_KEY = 'mufca_theme'
const DEFAULT_THEME = 'dark'

function detectInitialTheme() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch (_) { /* storage unavailable: fall back to the default */ }
  return DEFAULT_THEME
}

const ThemeContext = createContext({ theme: DEFAULT_THEME, setTheme: () => {}, toggleTheme: () => {} })

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(detectInitialTheme)

  const setTheme = useCallback((value) => {
    if (value !== 'light' && value !== 'dark') return
    setThemeState(value)
    try { localStorage.setItem(STORAGE_KEY, value) } catch (_) { /* per-session only */ }
  }, [])

  const toggleTheme = useCallback(() => setTheme(theme === 'dark' ? 'light' : 'dark'), [theme, setTheme])

  useEffect(() => { document.documentElement.dataset.theme = theme }, [theme])

  const value = useMemo(() => ({ theme, setTheme, toggleTheme }), [theme, setTheme, toggleTheme])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

// const { theme, toggleTheme } = useTheme()
export function useTheme() {
  return useContext(ThemeContext)
}

// Sun / moon toggle, placed next to the language switcher (top bar and login screen).
// The icon shows the theme you will switch TO.
export function ThemeSwitcher({ style }) {
  const { theme, toggleTheme } = useTheme()
  const { t } = useI18n()
  const toLight = theme === 'dark'
  const label = toLight ? t('Switch to light theme') : t('Switch to dark theme')
  return (
    <button
      type="button"
      className="theme-switch"
      onClick={toggleTheme}
      aria-label={label}
      title={label}
      style={style}
    >
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {toLight ? (
          <>
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
          </>
        ) : (
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        )}
      </svg>
    </button>
  )
}
