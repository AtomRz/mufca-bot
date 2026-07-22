import React from 'react'

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
            <p style={{ color: 'var(--text-dim)', fontSize: 13, marginBottom: 16 }}>
              Something went wrong rendering the dashboard. This is usually a transient
              rendering glitch — reloading fixes it.
            </p>
            <button className="btn primary" style={{ width: '100%' }} onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
