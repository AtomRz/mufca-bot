import React from 'react'
import { useI18n } from './i18n'

// Class components cannot use hooks, so the text lives in small function components.
function CrashMessage() {
  const { t } = useI18n()
  return (
    <p style={{ color: 'var(--text-dim)', fontSize: 13, marginBottom: 16 }}>
      {t('Something went wrong rendering the dashboard. This is usually a transient rendering glitch — reloading fixes it.')}
    </p>
  )
}

function ReloadButton() {
  const { t } = useI18n()
  return (
    <button className="btn primary" style={{ width: '100%' }} onClick={() => window.location.reload()}>
      {t('Reload')}
    </button>
  )
}

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    console.error('MUFCA dashboard crashed:', error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="login-screen">
          <div className="login-card" style={{ textAlign: 'center' }}>
            <div className="logo" style={{ marginBottom: 16, justifyContent: 'center' }}>
              <span className="pulse" style={{ background: 'var(--short)' }} />
              MUFCA
            </div>
            <CrashMessage />
            {this.state.error && (
              <p style={{ color: 'var(--text-faint)', fontSize: 11, marginBottom: 16, wordBreak: 'break-word', fontFamily: 'var(--font-mono, monospace)' }}>
                {String(this.state.error.message || this.state.error).slice(0, 200)}
              </p>
            )}
            <ReloadButton />
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
