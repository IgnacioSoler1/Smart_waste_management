import { useState } from 'react'
import { Login } from './pages/Login'
import { MapPage } from './pages/Map'
import type { Session } from './types'

const SESSION_KEY = 'sw-driver-session'

function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}

function saveSession(session: Session | null): void {
  if (session) {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session))
  } else {
    localStorage.removeItem(SESSION_KEY)
  }
}

export default function App() {
  const [session, setSession] = useState<Session | null>(loadSession)

  const handleLogin = (s: Session) => {
    saveSession(s)
    setSession(s)
  }

  const handleLogout = () => {
    saveSession(null)
    setSession(null)
  }

  if (!session) {
    return <Login onLogin={handleLogin} />
  }

  return <MapPage session={session} onLogout={handleLogout} />
}
