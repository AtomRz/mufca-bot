import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import ErrorBoundary from './ErrorBoundary.jsx'
import { I18nProvider } from './i18n'
import { ThemeProvider } from './theme'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <I18nProvider>
      <ThemeProvider>
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
      </ThemeProvider>
    </I18nProvider>
  </React.StrictMode>,
)
