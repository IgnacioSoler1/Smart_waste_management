import { useState } from 'react'
import type { Session } from '../types'

// Camiones de prueba. En producción vendrían del endpoint GET /trucks.
const SAMPLE_TRUCKS = [
  'CAM-001', 'CAM-002', 'CAM-003', 'CAM-004', 'CAM-005',
  'CAM-006', 'CAM-007', 'CAM-008', 'CAM-009', 'CAM-010',
]

interface LoginProps {
  onLogin: (session: Session) => void
}

export function Login({ onLogin }: LoginProps) {
  const [truckId, setTruckId]     = useState(SAMPLE_TRUCKS[0])
  const [circuitId, setCircuitId] = useState('')
  const [error, setError]         = useState('')

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!circuitId.trim()) {
      setError('Ingresá el ID del circuito asignado.')
      return
    }
    setError('')
    onLogin({ truckId, circuitId: circuitId.trim() })
  }

  return (
    <div style={styles.page}>
      <div style={styles.card}>
        {/* Logo / branding */}
        <div style={styles.logoArea}>
          <div style={styles.logoIcon}>♻️</div>
          <h1 style={styles.logoTitle}>SmartWaste</h1>
          <p style={styles.logoSub}>App del Conductor · Montevideo</p>
        </div>

        <form onSubmit={handleSubmit} style={styles.form}>
          {/* Camión */}
          <div style={styles.field}>
            <label htmlFor="truck" style={styles.label}>Camión</label>
            <select
              id="truck"
              value={truckId}
              onChange={e => setTruckId(e.target.value)}
              style={styles.select}
            >
              {SAMPLE_TRUCKS.map(t => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>

          {/* Circuito */}
          <div style={styles.field}>
            <label htmlFor="circuit" style={styles.label}>ID de circuito</label>
            <input
              id="circuit"
              type="text"
              value={circuitId}
              onChange={e => setCircuitId(e.target.value)}
              placeholder="Ej: A_DU_0101"
              autoCapitalize="characters"
              spellCheck={false}
              style={styles.input}
            />
            <span style={styles.hint}>
              Tu supervisor te indicó el código de circuito.
            </span>
          </div>

          {error && <p style={styles.error}>{error}</p>}

          <button type="submit" style={styles.btn}>
            Iniciar turno →
          </button>
        </form>
      </div>
    </div>
  )
}

// ── Estilos ────────────────────────────────────────────────────────────────

const styles: Record<string, React.CSSProperties> = {
  page: {
    minHeight: '100dvh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: 'linear-gradient(135deg, #052e16 0%, #14532d 50%, #166534 100%)',
    padding: '24px 16px',
  },
  card: {
    width: '100%',
    maxWidth: '360px',
    background: '#fff',
    borderRadius: '16px',
    boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
    padding: '32px 28px',
  },
  logoArea: {
    textAlign: 'center',
    marginBottom: '28px',
  },
  logoIcon: {
    fontSize: '48px',
    lineHeight: 1,
    marginBottom: '8px',
  },
  logoTitle: {
    fontSize: '26px',
    fontWeight: 800,
    color: '#052e16',
    margin: 0,
    letterSpacing: '-0.03em',
  },
  logoSub: {
    fontSize: '13px',
    color: '#6b7280',
    marginTop: '4px',
  },
  form: {
    display: 'flex',
    flexDirection: 'column',
    gap: '18px',
  },
  field: {
    display: 'flex',
    flexDirection: 'column',
    gap: '6px',
  },
  label: {
    fontSize: '13px',
    fontWeight: 600,
    color: '#374151',
  },
  select: {
    padding: '10px 12px',
    borderRadius: '8px',
    border: '1.5px solid #d1d5db',
    fontSize: '15px',
    color: '#111827',
    background: '#fff',
    appearance: 'auto',
    cursor: 'pointer',
    outline: 'none',
  },
  input: {
    padding: '10px 12px',
    borderRadius: '8px',
    border: '1.5px solid #d1d5db',
    fontSize: '15px',
    color: '#111827',
    outline: 'none',
  },
  hint: {
    fontSize: '11px',
    color: '#9ca3af',
  },
  error: {
    fontSize: '13px',
    color: '#dc2626',
    background: '#fee2e2',
    border: '1px solid #fca5a5',
    borderRadius: '6px',
    padding: '8px 12px',
    margin: 0,
  },
  btn: {
    padding: '12px',
    borderRadius: '8px',
    border: 'none',
    background: '#1e6b3c',
    color: '#fff',
    fontSize: '15px',
    fontWeight: 700,
    cursor: 'pointer',
    letterSpacing: '0.02em',
    marginTop: '4px',
    transition: 'background 0.15s ease',
  },
}
