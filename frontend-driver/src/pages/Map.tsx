import { useState, useCallback, useEffect, useRef } from 'react'
import { RouteMap } from '../components/RouteMap'
import { StopList } from '../components/StopList'
import { useRoute } from '../hooks/useRoute'
import { useWebSocket } from '../hooks/useWebSocket'
import type { Session, Stop, WsRouteUpdate, WsStatus } from '../types'

interface MapPageProps {
  session: Session
  onLogout: () => void
}

// ── Indicador de estado WS ─────────────────────────────────────────────────
const WS_DOT: Record<WsStatus, { color: string; label: string }> = {
  connecting: { color: '#f59e0b', label: 'Conectando…' },
  open:       { color: '#22c55e', label: 'Conectado' },
  closed:     { color: '#9ca3af', label: 'Reconectando…' },
  error:      { color: '#ef4444', label: 'Sin conexión' },
}

// ── Componente ─────────────────────────────────────────────────────────────
export function MapPage({ session, onLogout }: MapPageProps) {
  const { truckId, circuitId } = session

  // Ruta desde REST API
  const { route, loading, error, refetch } = useRoute(circuitId)

  // Contenedores vaciados en esta sesión (estado local)
  const [emptiedIds, setEmptiedIds] = useState<Set<string>>(new Set())

  // Contenedor seleccionado en el mapa (para highlight en la lista)
  const [highlightedId, setHighlightedId] = useState<string | null>(null)

  // Posición del camión: inicialmente en el depósito de la ruta
  const [truckPosition, setTruckPosition] = useState<[number, number] | null>(null)
  useEffect(() => {
    if (route && !truckPosition) {
      setTruckPosition([route.depot_lat, route.depot_lon])
    }
  }, [route, truckPosition])

  // Panel lateral abierto/cerrado en móvil
  const [sidebarOpen, setSidebarOpen] = useState(false)

  // Toast para notificaciones WS
  const [toast, setToast] = useState<string | null>(null)
  const toastTimer        = useRef<ReturnType<typeof setTimeout>>()

  const showToast = useCallback((msg: string) => {
    setToast(msg)
    clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 5_000)
  }, [])

  // WebSocket
  const handleWsMessage = useCallback((data: unknown) => {
    const msg = data as WsRouteUpdate
    if (msg.type === 'route_update' && msg.circuit_id === circuitId) {
      // Nueva ruta calculada — refetch y notificar
      refetch()
      showToast(`🗺️ Nueva ruta: ${msg.stops} paradas · ${msg.distance_km} km`)
    }
  }, [circuitId, refetch, showToast])

  const { status: wsStatus, send: wsSend } = useWebSocket(truckId, circuitId, handleWsMessage)

  // Vaciar contenedor
  const handleEmpty = useCallback((stop: Stop) => {
    if (emptiedIds.has(stop.container_id)) return

    wsSend({ action: 'container_emptied', container_id: stop.container_id })

    setEmptiedIds(prev => new Set([...prev, stop.container_id]))

    // Mover el camión a la posición del contenedor vaciado
    setTruckPosition([stop.latitude, stop.longitude])
  }, [emptiedIds, wsSend])

  const dot = WS_DOT[wsStatus]

  return (
    <div style={styles.root}>
      {/* ── Topbar ────────────────────────────────────────── */}
      <header style={styles.topbar}>
        <div style={styles.topbarLeft}>
          <span style={styles.topbarTitle}>♻️ SmartWaste</span>
          <span style={styles.topbarSub}>{truckId} · {circuitId}</span>
        </div>

        <div style={styles.topbarRight}>
          {/* Indicador WS */}
          <div style={styles.wsDot(dot.color)} title={dot.label} />

          {/* Toggle sidebar en móvil */}
          <button
            style={styles.sidebarToggle}
            onClick={() => setSidebarOpen(o => !o)}
            title="Ver paradas"
            aria-label="Ver lista de paradas"
          >
            {sidebarOpen ? '✕' : '☰'}
          </button>

          {/* Logout */}
          <button style={styles.logoutBtn} onClick={onLogout} title="Salir">
            Salir
          </button>
        </div>
      </header>

      {/* ── Layout principal ───────────────────────────────── */}
      <div style={styles.body}>
        {/* Mapa */}
        <div style={styles.mapArea}>
          <RouteMap
            route={route}
            emptiedIds={emptiedIds}
            truckPosition={truckPosition}
            selectedStopId={highlightedId}
            onStopClick={(stop) => {
              setHighlightedId(stop.container_id)
              setSidebarOpen(true)
            }}
          />
        </div>

        {/* Sidebar / panel de paradas */}
        <div style={styles.sidebar(sidebarOpen)}>
          <StopList
            route={route}
            loading={loading}
            error={error}
            emptiedIds={emptiedIds}
            highlightedId={highlightedId}
            onStopSelect={(stop) => setHighlightedId(stop.container_id)}
            onEmpty={handleEmpty}
          />
        </div>
      </div>

      {/* ── Toast ─────────────────────────────────────────── */}
      {toast && (
        <div style={styles.toast}>
          {toast}
        </div>
      )}
    </div>
  )
}

// ── Estilos ────────────────────────────────────────────────────────────────
const SIDEBAR_W = '340px'

const styles = {
  root: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100dvh',
    background: '#f9fafb',
    overflow: 'hidden',
  } satisfies React.CSSProperties,

  topbar: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '0 14px',
    height: '48px',
    background: '#052e16',
    color: '#fff',
    flexShrink: 0,
    zIndex: 10,
    boxShadow: '0 1px 4px rgba(0,0,0,0.3)',
  } satisfies React.CSSProperties,

  topbarLeft: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '1px',
  } satisfies React.CSSProperties,

  topbarTitle: {
    fontSize: '14px',
    fontWeight: 700,
    letterSpacing: '-0.01em',
  } satisfies React.CSSProperties,

  topbarSub: {
    fontSize: '10px',
    opacity: 0.7,
  } satisfies React.CSSProperties,

  topbarRight: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
  } satisfies React.CSSProperties,

  wsDot: (color: string): React.CSSProperties => ({
    width: '10px',
    height: '10px',
    borderRadius: '50%',
    background: color,
    flexShrink: 0,
    boxShadow: `0 0 6px ${color}`,
    transition: 'background 0.3s ease',
  }),

  sidebarToggle: {
    background: 'rgba(255,255,255,0.12)',
    border: 'none',
    color: '#fff',
    borderRadius: '6px',
    width: '32px',
    height: '32px',
    fontSize: '16px',
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    // Solo visible en móvil (≤ 768 px) — ocultamos en desktop via media query approach:
    // En un proyecto real usaríamos CSS modules; aquí lo mostramos siempre.
  } satisfies React.CSSProperties,

  logoutBtn: {
    background: 'rgba(255,255,255,0.12)',
    border: 'none',
    color: '#fff',
    borderRadius: '6px',
    padding: '0 10px',
    height: '32px',
    fontSize: '12px',
    cursor: 'pointer',
    fontWeight: 600,
  } satisfies React.CSSProperties,

  body: {
    flex: 1,
    display: 'flex',
    overflow: 'hidden',
    position: 'relative' as const,
  } satisfies React.CSSProperties,

  mapArea: {
    flex: 1,
    minWidth: 0,
    position: 'relative' as const,
    zIndex: 0,
  } satisfies React.CSSProperties,

  sidebar: (open: boolean): React.CSSProperties => ({
    width: SIDEBAR_W,
    flexShrink: 0,
    position: 'absolute' as const,
    top: 0,
    right: 0,
    bottom: 0,
    zIndex: 10,
    transform: open ? 'translateX(0)' : `translateX(${SIDEBAR_W})`,
    transition: 'transform 0.25s ease',
    boxShadow: open ? '-4px 0 20px rgba(0,0,0,0.3)' : 'none',
  }),

  toast: {
    position: 'fixed' as const,
    bottom: '20px',
    left: '50%',
    transform: 'translateX(-50%)',
    background: '#052e16',
    color: '#fff',
    padding: '10px 20px',
    borderRadius: '99px',
    fontSize: '13px',
    fontWeight: 600,
    boxShadow: '0 4px 20px rgba(0,0,0,0.3)',
    zIndex: 100,
    maxWidth: 'calc(100vw - 40px)',
    textAlign: 'center' as const,
    animation: 'fadeInUp 0.2s ease',
  } satisfies React.CSSProperties,
}
