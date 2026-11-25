// Lightweight i18n for the dashboard (no external dependency).
//
// Convention: the ENGLISH TEXT is the key. `t('Save')` returns 'Save' in English and
// the translation from the locale files in other languages; a missing translation
// silently falls back to the English text, so a half-translated panel still works.
//
// Locale files live in ./locales/<Name>.js, one per component, so they can be edited
// independently. Each exports `{ ru: { 'English text': 'Translation' }, ka: { ... } }`.
// A value may also be a function `(params) => string` for plural-dependent strings.
//
// Interpolation: `t('Showing {n} of {total}', { n: 5, total: 20 })`.
// Only the user-visible UI is translated; backend messages, report text, log lines,
// identifiers and code stay English.

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

export const LANGUAGES = [
  { code: 'en', label: 'EN', name: 'English', locale: 'en-US' },
  { code: 'ru', label: 'RU', name: 'Русский', locale: 'ru-RU' },
  { code: 'ka', label: 'KA', name: 'ქართული', locale: 'ka-GE' },
]

const STORAGE_KEY = 'mufca_lang'
const DEFAULT_LANG = 'en'

// Merge every locales/*.js file into one dictionary per language.
const DICT = { ru: {}, ka: {} }
const modules = import.meta.glob('./locales/*.js', { eager: true })
for (const mod of Object.values(modules)) {
  const data = mod.default || {}
  for (const lang of Object.keys(DICT)) {
    if (data[lang]) Object.assign(DICT[lang], data[lang])
  }
}

function detectInitialLang() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && LANGUAGES.some((l) => l.code === saved)) return saved
  } catch (_) { /* storage unavailable: fall through to browser language */ }
  const nav = (typeof navigator !== 'undefined' && navigator.language || '').toLowerCase()
  if (nav.startsWith('ru')) return 'ru'
  if (nav.startsWith('ka')) return 'ka'
  return DEFAULT_LANG
}

function interpolate(text, params) {
  if (!params) return text
  return text.replace(/\{(\w+)\}/g, (m, k) => (params[k] !== undefined && params[k] !== null ? String(params[k]) : m))
}

export function translate(lang, text, params) {
  if (typeof text !== 'string') return text
  const entry = lang === 'en' ? undefined : DICT[lang]?.[text]
  if (typeof entry === 'function') return entry(params || {})
  return interpolate(entry !== undefined ? entry : text, params)
}

const I18nContext = createContext({
  lang: DEFAULT_LANG,
  locale: 'en-US',
  setLang: () => {},
  t: (text, params) => translate('en', text, params),
})

export function I18nProvider({ children }) {
  const [lang, setLangState] = useState(detectInitialLang)

  const setLang = useCallback((code) => {
    if (!LANGUAGES.some((l) => l.code === code)) return
    setLangState(code)
    try { localStorage.setItem(STORAGE_KEY, code) } catch (_) { /* per-session only */ }
  }, [])

  useEffect(() => { document.documentElement.lang = lang }, [lang])

  const value = useMemo(() => {
    const locale = LANGUAGES.find((l) => l.code === lang)?.locale || 'en-US'
    return { lang, locale, setLang, t: (text, params) => translate(lang, text, params) }
  }, [lang, setLang])

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

// const { t, lang, locale } = useI18n()
export function useI18n() {
  return useContext(I18nContext)
}

// Compact EN / RU / KA switcher used in the top bar and on the login screen.
export function LanguageSwitcher({ style }) {
  const { lang, setLang } = useI18n()
  return (
    <select
      className="lang-switch"
      value={lang}
      onChange={(e) => setLang(e.target.value)}
      aria-label="Language"
      title="Language"
      style={style}
    >
      {LANGUAGES.map((l) => (
        <option key={l.code} value={l.code}>{l.label}</option>
      ))}
    </select>
  )
}
