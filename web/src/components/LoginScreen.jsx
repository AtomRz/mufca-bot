import React, { useState } from 'react'
import { login } from '../api'
import { useI18n, LanguageSwitcher } from '../i18n'

export default function LoginScreen({ onSuccess }) {
  const { t } = useI18n()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await login(username, password)
      onSuccess()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <div className="logo" style={{ marginBottom: 24 }}>
          <span className="pulse live" />
          MUFCA
        </div>
        <div style={{ position: 'absolute', top: 12, right: 12 }}><LanguageSwitcher /></div>
        {error && <div className="error-banner">{error}</div>}
        <div className="field">
          <label>{t('Username')}</label>
          <input
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
          />
        </div>
        <div className="field">
          <label>{t('Password')}</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        <button className="btn primary" type="submit" disabled={busy} style={{ width: '100%' }}>
          {busy ? t('Signing in…') : t('Sign in')}
        </button>
      </form>
    </div>
  )
}
