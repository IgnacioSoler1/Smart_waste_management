import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MapContainer, TileLayer, CircleMarker, Marker, Polyline, Popup, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { fetchContainers, fetchRoutes, triggerOptimize } from '../api'
import { usePolling } from '../hooks/usePolling'
import { fillColor, fillClass, formatDistance, formatDuration } from '../helpers'
import type { CircuitSummary, Container, Route, Truck } from '../types'

// Colores distintos para cada camión en el mapa
const ROUTE_COLORS = ['#3b82f6', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6']

// ── Flechas de dirección ───────────────────────────────────────────────────
const ARROW_SPACING_M = 250

function _bearingDeg(p1: [number, number], p2: [number, number]): number {
  const lat1 = (p1[0] * Math.PI) / 180
  const lat2 = (p2[0] * Math.PI) / 180
  const dLon = ((p2[1] - p1[1]) * Math.PI) / 180
  const y = Math.sin(dLon) * Math.cos(lat2)
  const x = Math.cos(lat1) * Math.sin(lat2) - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon)
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360
}

function _distanceM(p1: [number, number], p2: [number, number]): number {
  const R = 6_371_000
  const dLat = ((p2[0] - p1[0]) * Math.PI) / 180
  const dLon = ((p2[1] - p1[1]) * Math.PI) / 180
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((p1[0] * Math.PI) / 180) *
      Math.cos((p2[0] * Math.PI) / 180) *
      Math.sin(dLon / 2) ** 2
  return 2 * R * Math.asin(Math.sqrt(a))
}

function _makeArrowIcon(deg: number, color: string): L.DivIcon {
  return L.divIcon({
    className: '',
    html: `<svg width="14" height="14" viewBox="0 0 14 14"
             style="transform:rotate(${deg}deg);display:block;filter:drop-shadow(0 1px 1px rgba(0,0,0,0.5))">
             <polygon points="7,1 13,13 7,10 1,13" fill="${color}" stroke="rgba(0,0,0,0.25)" stroke-width="0.5"/>
           </svg>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  })
}

function RouteArrows({ positions, color }: { positions: [number, number][]; color: string }) {
  const arrows = useMemo(() => {
    if (positions.length < 2) return []
    const result: Array<{ pos: [number, number]; deg: number; key: string }> = []
    let accumulated = 0
    for (let i = 1; i < positions.length; i++) {
      accumulated += _distanceM(positions[i - 1], positions[i])
      if (accumulated >= ARROW_SPACING_M) {
        result.push({ pos: positions[i], deg: _bearingDeg(positions[i - 1], positions[i]), key: `${i}` })
        accumulated = 0
      }
    }
    return result
  }, [positions])

  return (
    <>
      {arrows.map(({ pos, deg, key }) => (
        <Marker
          key={key}
          position={pos}
          icon={_makeArrowIcon(deg, color)}
          interactive={false}
          zIndexOffset={-100}
        />
      ))}
    </>
  )
}

// ── Animación de recorrido ─────────────────────────────────────────────────

/** Velocidades de animación disponibles en m/s. */
const ANIM_SPEEDS: { label: string; value: number }[] = [
  { label: 'Lenta',  value: 100 },
  { label: 'Normal', value: 300 },
  { label: 'Rápida', value: 700 },
]

/** Icono del camión animado: círculo con flecha interior rotada según bearing. */
function _makeTruckAnimIcon(deg: number, color: string): L.DivIcon {
  return L.divIcon({
    className: '',
    html: `<div style="width:32px;height:32px;filter:drop-shadow(0 2px 4px rgba(0,0,0,0.6))">
             <svg width="32" height="32" viewBox="0 0 32 32">
               <circle cx="16" cy="16" r="14" fill="${color}" stroke="white" stroke-width="2.5"/>
               <g transform="rotate(${deg},16,16)">
                 <polygon points="16,5 23,24 16,20 9,24" fill="white"/>
               </g>
             </svg>
           </div>`,
    iconSize: [32, 32],
    iconAnchor: [16, 16],
  })
}

/**
 * Avanza `distMeters` metros a lo largo del array de posiciones.
 * Devuelve el nuevo segIdx + t (0..1 dentro del segmento) y si llegó al final.
 */
function _advance(
  positions: [number, number][],
  segIdx: number,
  t: number,
  distMeters: number,
): { segIdx: number; t: number; done: boolean } {
  let remaining = distMeters
  let si = segIdx
  let ti = t

  while (si < positions.length - 1) {
    const segLen = _distanceM(positions[si], positions[si + 1])
    const distLeft = segLen > 0 ? (1 - ti) * segLen : 0

    if (remaining <= distLeft) {
      return { segIdx: si, t: ti + (segLen > 0 ? remaining / segLen : 0), done: false }
    }
    remaining -= distLeft
    si++
    ti = 0
    if (si >= positions.length - 1) {
      return { segIdx: positions.length - 2, t: 1, done: true }
    }
  }
  return { segIdx: positions.length - 2, t: 1, done: true }
}

interface AnimatedRouteProps {
  positions: [number, number][]
  totalDistM: number
  color: string
  playing: boolean
  speed: number        // m/s
  animKey: number      // incrementar para forzar reset
  onProgress: (pct: number) => void
  onComplete: () => void
}

/**
 * Renderiza un marcador de camión animado que recorre las `positions` en orden.
 * Usa la API imperativa de Leaflet (.setLatLng / .setIcon) para no re-renderizar
 * el árbol React en cada frame de la animación.
 */
function AnimatedRoute({
  positions, totalDistM, color, playing, speed, animKey, onProgress, onComplete,
}: AnimatedRouteProps) {
  const map = useMap()
  const markerRef    = useRef<L.Marker | null>(null)
  const rafRef       = useRef<number>(0)
  const stateRef     = useRef<{ segIdx: number; t: number; coveredM: number; done: boolean }>(
    { segIdx: 0, t: 0, coveredM: 0, done: false },
  )
  // Guardamos las posiciones en un ref para que el loop de animación y el
  // efecto de creación del marcador no dependan de la prop `positions` de forma
  // reactiva. Así, los re-renders del padre (ej. actualización de animPct cada
  // frame o polling de 30s) no destruyen ni reinician el marcador.
  const positionsRef  = useRef<[number, number][]>(positions)
  const totalDistMRef = useRef<number>(totalDistM)
  const onCompleteRef = useRef(onComplete)
  const onProgressRef = useRef(onProgress)

  // Actualizar refs en cada render sin disparar efectos
  useEffect(() => { onCompleteRef.current = onComplete }, [onComplete])
  useEffect(() => { onProgressRef.current = onProgress }, [onProgress])
  // positions y totalDistM solo se "bloquean" al inicio de cada animación
  // (cuando animKey cambia). Actualizarlos en otras circunstancias no
  // tiene efecto sobre el marcador ya creado.
  positionsRef.current  = positions
  totalDistMRef.current = totalDistM

  // Crear/destruir marcador. Solo se dispara cuando cambia animKey (reset
  // explícito) o color/map — nunca por un re-render normal del padre.
  useEffect(() => {
    const pts = positionsRef.current
    if (pts.length < 2) return
    stateRef.current = { segIdx: 0, t: 0, coveredM: 0, done: false }

    const marker = L.marker(pts[0] as L.LatLngExpression, {
      icon: _makeTruckAnimIcon(0, color),
      zIndexOffset: 1000,
      interactive: false,
    }).addTo(map)
    markerRef.current = marker

    return () => {
      cancelAnimationFrame(rafRef.current)
      marker.remove()
      markerRef.current = null
    }
  // `positions` y `totalDistM` quedan fuera de las deps a propósito:
  // los leemos desde los refs para que los re-renders del padre no recreen el marcador.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, color, animKey])

  // Loop de animación: arranca/pausa según `playing`.
  useEffect(() => {
    cancelAnimationFrame(rafRef.current)
    if (!playing || !markerRef.current || stateRef.current.done) return

    let prevTs: DOMHighResTimeStamp | null = null

    function frame(ts: DOMHighResTimeStamp) {
      if (!markerRef.current || stateRef.current.done) return
      if (!prevTs) prevTs = ts
      const dt = Math.min((ts - prevTs) / 1000, 0.05)  // cap 50ms para evitar saltos
      prevTs = ts

      const pts = positionsRef.current
      const { segIdx, t, coveredM } = stateRef.current
      const next = _advance(pts, segIdx, t, speed * dt)

      // Interpolar posición exacta dentro del segmento actual
      const p1 = pts[next.segIdx]
      const p2 = pts[Math.min(next.segIdx + 1, pts.length - 1)]
      const tt  = Math.min(next.t, 1)
      const lat = p1[0] + (p2[0] - p1[0]) * tt
      const lon = p1[1] + (p2[1] - p1[1]) * tt
      const deg = _bearingDeg(p1, p2)

      const newCoveredM = coveredM + speed * dt
      stateRef.current = { ...next, coveredM: newCoveredM }

      markerRef.current.setLatLng([lat, lon])
      markerRef.current.setIcon(_makeTruckAnimIcon(deg, color))

      const pct = Math.min(100, Math.round((newCoveredM / totalDistMRef.current) * 100))
      onProgressRef.current(pct)

      if (next.done) {
        stateRef.current.done = true
        onCompleteRef.current()
        return
      }
      rafRef.current = requestAnimationFrame(frame)
    }

    rafRef.current = requestAnimationFrame(frame)
    return () => cancelAnimationFrame(rafRef.current)
  // `positions` y `totalDistM` quedan fuera de las deps a propósito (leemos desde refs).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing, speed, color])

  return null
}

// ── Tipos y props ──────────────────────────────────────────────────────────

type AnimState = 'idle' | 'playing' | 'paused' | 'done'

interface Props {
  circuits: CircuitSummary[]
  trucks: Truck[]
}

type OptimizeStatus =
  | { state: 'idle' }
  | { state: 'sending' }
  | { state: 'waiting'; attempt: number }
  | { state: 'done'; message: string; ok: boolean }

function routePositions(route: Route): [number, number][] {
  if (route.route_geometry) return route.route_geometry
  if (!route.stops) return []
  const pts: [number, number][] = []
  if (route.depot_lat && route.depot_lon) {
    pts.push([route.depot_lat, route.depot_lon])
  }
  for (const s of route.stops) {
    pts.push([s.latitude, s.longitude])
  }
  if (route.depot_lat && route.depot_lon) {
    pts.push([route.depot_lat, route.depot_lon])
  }
  return pts
}

export function CircuitView({ circuits, trucks }: Props) {
  const [selected, setSelected] = useState<string>(
    circuits[0]?.circuit_id ?? '',
  )
  const [optStatus, setOptStatus] = useState<OptimizeStatus>({ state: 'idle' })

  // ── Estado de animación de recorrido ──────────────────────────────────
  const [animState,    setAnimState]    = useState<AnimState>('idle')
  const [animSpeed,    setAnimSpeed]    = useState<number>(ANIM_SPEEDS[1].value)
  const [animKey,      setAnimKey]      = useState<number>(0)
  const [animPct,      setAnimPct]      = useState<number>(0)
  const animDoneCountRef = useRef<number>(0)

  // Al cambiar de circuito, resetear animación
  useEffect(() => {
    setAnimState('idle')
    setAnimKey(k => k + 1)
    setAnimPct(0)
    animDoneCountRef.current = 0
  }, [selected])

  function handleAnimPlay() {
    if (animState === 'done') {
      animDoneCountRef.current = 0
      setAnimPct(0)
      setAnimKey(k => k + 1)
    }
    setAnimState('playing')
  }

  function handleAnimPause() { setAnimState('paused') }

  function handleAnimReset() {
    setAnimState('idle')
    setAnimKey(k => k + 1)
    setAnimPct(0)
    animDoneCountRef.current = 0
  }

  function handleRouteComplete(totalRoutes: number) {
    animDoneCountRef.current += 1
    if (animDoneCountRef.current >= totalRoutes) {
      setAnimState('done')
    }
  }

  const containersFetcher = useCallback(
    () => (selected ? fetchContainers(selected) : Promise.resolve([])),
    [selected],
  )
  const routesFetcher = useCallback(
    () => (selected ? fetchRoutes(selected) : Promise.resolve([])),
    [selected],
  )

  const { data: containers } = usePolling<Container[]>(containersFetcher, [selected])
  const { data: routes, refresh: refreshRoutes } = usePolling<Route[]>(
    routesFetcher, [selected], { paused: animState !== 'idle' },
  )

  const circuitSummary = useMemo(
    () => circuits.find((c) => c.circuit_id === selected),
    [circuits, selected],
  )

  const assignedTrucks = useMemo(
    () => trucks.filter((t) => t.circuit_id === selected),
    [trucks, selected],
  )

  const mapCenter = useMemo<[number, number]>(() => {
    if (!containers || containers.length === 0) return [-34.88, -56.17]
    const bounds = L.latLngBounds(
      containers.map((c) => [c.latitude, c.longitude] as [number, number]),
    )
    const center = bounds.getCenter()
    return [center.lat, center.lng]
  }, [containers])

  // Aggregate stats across all routes
  const routeStats = useMemo(() => {
    if (!routes || routes.length === 0) return null
    const totalStops = routes.reduce((s, r) => s + (r.stops?.length ?? 0), 0)
    const totalDistance = routes.reduce((s, r) => s + (r.total_distance_m ?? 0), 0)
    const totalDuration = routes.reduce((s, r) => s + (r.total_duration_s ?? 0), 0)
    const solver = routes[0]?.solver ?? '—'
    const solverStatus = routes[0]?.solver_status ?? '—'
    const createdAt = routes[0]?.created_at ?? ''
    return { totalStops, totalDistance, totalDuration, solver, solverStatus, createdAt, count: routes.length }
  }, [routes])

  const handleOptimize = async () => {
    if (!selected) return

    // Check if there are containers needing collection
    const needsCollection = circuitSummary?.needs_collection ?? 0
    if (needsCollection < 5) {
      setOptStatus({
        state: 'done',
        ok: false,
        message: needsCollection === 0
          ? 'No hay contenedores que necesiten recoleccion en este circuito. El optimizador requiere al menos 5.'
          : `Solo ${needsCollection} contenedor(es) necesitan recoleccion. El optimizador requiere al menos 5.`,
      })
      return
    }

    setOptStatus({ state: 'sending' })
    try {
      await triggerOptimize(selected)
    } catch (err) {
      setOptStatus({
        state: 'done',
        ok: false,
        message: `Error al enviar: ${err instanceof Error ? err.message : String(err)}`,
      })
      return
    }

    // Poll for the new route (Lambda runs async, typically 5-15s)
    setOptStatus({ state: 'waiting', attempt: 1 })
    const routesBefore = routes?.map((r) => r.route_id) ?? []

    for (let attempt = 1; attempt <= 6; attempt++) {
      await new Promise((r) => setTimeout(r, 5000))
      setOptStatus({ state: 'waiting', attempt: attempt + 1 })

      try {
        const newRoutes = await fetchRoutes(selected)
        const hasNew = newRoutes.some((r) => !routesBefore.includes(r.route_id))
        if (hasNew) {
          refreshRoutes()
          const totalStops = newRoutes.reduce((s, r) => s + (r.stops?.length ?? 0), 0)
          setOptStatus({
            state: 'done',
            ok: true,
            message: `Ruta optimizada: ${newRoutes.length} camion(es), ${totalStops} paradas — solver: ${newRoutes[0]?.solver} (${newRoutes[0]?.solver_status})`,
          })
          return
        }
      } catch {
        // keep polling
      }
    }

    setOptStatus({
      state: 'done',
      ok: false,
      message: 'La optimizacion fue disparada pero no se genero una nueva ruta. Puede que el circuito no tenga suficientes contenedores pendientes, o que la Lambda haya fallado. Revisa los logs.',
    })
  }

  if (circuits.length === 0) {
    return <div className="loading">Cargando circuitos...</div>
  }

  const optimizing = optStatus.state === 'sending' || optStatus.state === 'waiting'

  return (
    <div className="circuit-view">
      {/* Header with selector */}
      <div className="circuit-header">
        <select
          value={selected}
          onChange={(e) => { setSelected(e.target.value); setOptStatus({ state: 'idle' }) }}
        >
          {circuits.map((c) => (
            <option key={c.circuit_id} value={c.circuit_id}>
              {c.circuit_id} — {c.total_containers} cont. — avg {c.avg_fill_level}%
            </option>
          ))}
        </select>
        <button
          className="btn"
          onClick={handleOptimize}
          disabled={optimizing || !selected}
        >
          {optStatus.state === 'sending'
            ? 'Enviando...'
            : optStatus.state === 'waiting'
              ? `Esperando resultado (${optStatus.attempt}/6)...`
              : 'Optimizar ruta'}
        </button>
      </div>

      {/* Optimize feedback */}
      {optStatus.state === 'done' && (
        <div
          className={optStatus.ok ? 'optimize-success' : 'optimize-warning'}
          style={{
            padding: '10px 16px',
            marginBottom: 16,
            borderRadius: 8,
            fontSize: 13,
            background: optStatus.ok ? 'rgba(34,197,94,0.12)' : 'rgba(234,179,8,0.12)',
            color: optStatus.ok ? 'var(--green)' : 'var(--yellow)',
            border: `1px solid ${optStatus.ok ? 'rgba(34,197,94,0.3)' : 'rgba(234,179,8,0.3)'}`,
          }}
        >
          {optStatus.message}
        </div>
      )}

      {/* ── Controles de animación de recorrido ── */}
      {routes && routes.length > 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap',
          padding: '10px 16px', marginBottom: 12, borderRadius: 12,
          background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)',
        }}>
          {/* Botón play/pause principal */}
          <button
            onClick={animState === 'playing' ? handleAnimPause : handleAnimPlay}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 8,
              padding: '8px 18px', borderRadius: 999, border: 'none', cursor: 'pointer',
              fontSize: 13, fontWeight: 600, letterSpacing: '0.01em',
              background: animState === 'playing'
                ? 'rgba(249,115,22,0.18)'
                : 'linear-gradient(135deg, #3b82f6 0%, #6366f1 100%)',
              color: animState === 'playing' ? '#fb923c' : '#fff',
              boxShadow: animState === 'playing'
                ? '0 0 0 1px rgba(249,115,22,0.35)'
                : '0 0 12px rgba(99,102,241,0.35), 0 0 0 1px rgba(99,102,241,0.4)',
              transition: 'all 0.2s ease',
              outline: 'none',
            }}
          >
            {animState === 'playing' ? (
              /* Pause icon */
              <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor">
                <rect x="2" y="1" width="4" height="12" rx="1.5"/>
                <rect x="8" y="1" width="4" height="12" rx="1.5"/>
              </svg>
            ) : animState === 'done' ? (
              /* Replay icon */
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="1 4 1 10 7 10"/>
                <path d="M3.51 15a9 9 0 1 0 .49-3.36"/>
              </svg>
            ) : (
              /* Play icon */
              <svg width="13" height="13" viewBox="0 0 13 13" fill="currentColor">
                <path d="M2 1.5 L11.5 6.5 L2 11.5 Z"/>
              </svg>
            )}
            {animState === 'playing'  ? 'Pausar'
             : animState === 'paused' ? 'Continuar'
             : animState === 'done'   ? 'Repetir'
             : 'Animar recorrido'}
          </button>

          {/* Botón reset */}
          {animState !== 'idle' && (
            <button
              onClick={handleAnimReset}
              title="Reiniciar animación"
              style={{
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                width: 34, height: 34, borderRadius: 999, border: '1px solid rgba(255,255,255,0.12)',
                background: 'rgba(255,255,255,0.05)', color: 'rgba(255,255,255,0.55)',
                cursor: 'pointer', transition: 'all 0.2s ease', outline: 'none',
              }}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="3" width="18" height="18" rx="2"/>
              </svg>
            </button>
          )}

          {/* Selector de velocidad */}
          <div style={{
            display: 'flex', gap: 2, marginLeft: 4,
            background: 'rgba(255,255,255,0.05)', borderRadius: 999,
            padding: '3px',
          }}>
            {ANIM_SPEEDS.map((s) => (
              <button
                key={s.value}
                onClick={() => setAnimSpeed(s.value)}
                style={{
                  padding: '4px 11px', borderRadius: 999, border: 'none', cursor: 'pointer',
                  fontSize: 12, fontWeight: 600, outline: 'none',
                  transition: 'all 0.15s ease',
                  background: animSpeed === s.value ? 'rgba(99,102,241,0.5)' : 'transparent',
                  color: animSpeed === s.value ? '#c7d2fe' : 'rgba(255,255,255,0.4)',
                  boxShadow: animSpeed === s.value ? '0 0 0 1px rgba(99,102,241,0.5)' : 'none',
                }}
              >
                {s.label}
              </button>
            ))}
          </div>

          {/* Barra de progreso */}
          {animState !== 'idle' && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: 8, flex: 1, minWidth: 160 }}>
              <div style={{ flex: 1, height: 6, borderRadius: 3, background: 'rgba(255,255,255,0.1)', overflow: 'hidden' }}>
                <div style={{
                  width: `${animPct}%`, height: '100%', borderRadius: 3,
                  background: animState === 'done' ? '#22c55e' : '#3b82f6',
                  transition: 'width 0.3s ease',
                }}/>
              </div>
              <span style={{ fontSize: 12, color: 'rgba(255,255,255,0.55)', whiteSpace: 'nowrap' }}>
                {animState === 'done' ? '✓ Completado' : `${animPct}%`}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Stats cards */}
      <div className="stats-row">
        <div className="card">
          <div className="card-label">Contenedores</div>
          <div className="card-value">{circuitSummary?.total_containers ?? '—'}</div>
        </div>
        <div className="card">
          <div className="card-label">Necesitan recoleccion</div>
          <div className="card-value" style={{ color: 'var(--orange)' }}>
            {circuitSummary?.needs_collection ?? '—'}
          </div>
        </div>
        <div className="card">
          <div className="card-label">Fill promedio</div>
          <div className="card-value">{circuitSummary?.avg_fill_level ?? '—'}%</div>
        </div>
        <div className="card">
          <div className="card-label">Turno</div>
          <div className="card-value" style={{ fontSize: 18 }}>
            {circuitSummary?.shift || '—'}
          </div>
        </div>
        <div className="card">
          <div className="card-label">Camiones asignados</div>
          <div className="card-value" style={{ fontSize: 16 }}>
            {assignedTrucks.length > 0
              ? assignedTrucks.map((t) => t.truck_id).join(', ')
              : 'Sin asignar'}
          </div>
        </div>
      </div>

      {/* Route info — aggregated across all trucks */}
      {routeStats && (
        <div className="stats-row">
          <div className="card">
            <div className="card-label">Distancia total</div>
            <div className="card-value">{formatDistance(routeStats.totalDistance)}</div>
            {routeStats.count > 1 && (
              <div className="card-sub">{routeStats.count} camiones</div>
            )}
          </div>
          <div className="card">
            <div className="card-label">Duracion estimada</div>
            <div className="card-value">{formatDuration(routeStats.totalDuration)}</div>
          </div>
          <div className="card">
            <div className="card-label">Paradas totales</div>
            <div className="card-value">{routeStats.totalStops}</div>
          </div>
          <div className="card">
            <div className="card-label">Solver</div>
            <div className="card-value" style={{ fontSize: 14 }}>
              {routeStats.solver} ({routeStats.solverStatus})
            </div>
            <div className="card-sub">{new Date(routeStats.createdAt).toLocaleString('es-UY')}</div>
          </div>
        </div>
      )}

      {/* Per-truck breakdown */}
      {routes && routes.length > 1 && (
        <div className="stats-row">
          {routes.map((r, i) => (
            <div className="card" key={r.route_id} style={{ borderLeft: `3px solid ${ROUTE_COLORS[i % ROUTE_COLORS.length]}` }}>
              <div className="card-label">{r.truck_id}</div>
              <div className="card-value" style={{ fontSize: 16 }}>
                {r.stops?.length ?? 0} paradas
              </div>
              <div className="card-sub">
                {formatDistance(r.total_distance_m)} — {formatDuration(r.total_duration_s)}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Map + table grid */}
      <div className="circuit-grid">
        <div className="circuit-map">
          <MapContainer center={mapCenter} zoom={14} zoomControl={true} key={selected}>
            <TileLayer
              attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
              url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
            />
            {(containers ?? []).map((c) => (
              <CircleMarker
                key={c.container_id}
                center={[c.latitude, c.longitude]}
                radius={7}
                pathOptions={{
                  fillColor: fillColor(c.fill_level ?? 0),
                  fillOpacity: 0.85,
                  color: '#000',
                  weight: 0.5,
                }}
              >
                <Popup>
                  <strong>{c.container_id}</strong><br />
                  Nivel: {Math.round(c.fill_level ?? 0)}%
                </Popup>
              </CircleMarker>
            ))}
            {/* Draw each truck's route in a different color with direction arrows */}
            {(routes ?? []).map((r, i) => {
              const positions = routePositions(r)
              if (positions.length < 2) return null
              const color = ROUTE_COLORS[i % ROUTE_COLORS.length]
              const hasRoadGeometry = !!r.route_geometry
              const totalRoutes = (routes ?? []).length
              return (
                <>
                  <Polyline
                    key={r.route_id}
                    positions={positions}
                    pathOptions={{
                      color,
                      weight: 3,
                      opacity: animState !== 'idle' ? 0.35 : 0.8,
                    }}
                  />
                  {hasRoadGeometry && animState === 'idle' && (
                    <RouteArrows key={`arrows-${r.route_id}`} positions={positions} color={color} />
                  )}
                  {animState !== 'idle' && (
                    <AnimatedRoute
                      key={`anim-${r.route_id}-${animKey}`}
                      positions={positions}
                      totalDistM={r.total_distance_m ?? 1}
                      color={color}
                      playing={animState === 'playing'}
                      speed={animSpeed}
                      animKey={animKey}
                      onProgress={setAnimPct}
                      onComplete={() => handleRouteComplete(totalRoutes)}
                    />
                  )}
                </>
              )
            })}
          </MapContainer>
        </div>

        <div className="table-wrapper">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>Nivel</th>
                <th>Estado</th>
                <th>Lat</th>
                <th>Lon</th>
              </tr>
            </thead>
            <tbody>
              {(containers ?? []).map((c) => (
                <tr key={c.container_id}>
                  <td>{c.container_id}</td>
                  <td>
                    <span className={`fill-badge ${fillClass(c.fill_level ?? 0)}`}>
                      {Math.round(c.fill_level ?? 0)}%
                    </span>
                  </td>
                  <td>{c.needs_collection ? 'Recolectar' : 'OK'}</td>
                  <td>{c.latitude?.toFixed(5)}</td>
                  <td>{c.longitude?.toFixed(5)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
