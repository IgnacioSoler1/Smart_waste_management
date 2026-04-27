import { useCallback, useEffect, useMemo, useState } from 'react'
import { MapContainer, TileLayer, CircleMarker, Marker, Popup, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { fetchAllContainers } from '../api'
import { usePolling } from '../hooks/usePolling'
import { fillColor } from '../helpers'
import type { CircuitSummary, Container, Truck } from '../types'

// Truck icon (SVG data URI)
const truckIcon = new L.Icon({
  iconUrl: 'data:image/svg+xml,' + encodeURIComponent(`
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="32" height="32">
      <rect x="1" y="6" width="15" height="10" rx="2" fill="#3b82f6" stroke="#1e3a5f" stroke-width="1"/>
      <rect x="16" y="9" width="7" height="7" rx="1" fill="#60a5fa" stroke="#1e3a5f" stroke-width="1"/>
      <circle cx="6" cy="17" r="2" fill="#222" stroke="#555" stroke-width="0.5"/>
      <circle cx="19" cy="17" r="2" fill="#222" stroke="#555" stroke-width="0.5"/>
    </svg>
  `),
  iconSize: [32, 32],
  iconAnchor: [16, 16],
  popupAnchor: [0, -16],
})

// Montevideo center
const MVD_CENTER: [number, number] = [-34.88, -56.17]

function FitBounds({ containers }: { containers: Container[] }) {
  const map = useMap()
  useEffect(() => {
    if (containers.length === 0) return
    const bounds = L.latLngBounds(
      containers.map((c) => [c.latitude, c.longitude] as [number, number]),
    )
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [30, 30] })
  }, [containers, map])
  return null
}

interface Props {
  circuits: CircuitSummary[]
  trucks: Truck[]
  loading: boolean
}

type FillFilter = '__all__' | 'low' | 'medium' | 'high' | 'full'
type ShiftFilter = '__all__' | 'morning' | 'afternoon' | 'night'

const FILL_FILTERS: { key: FillFilter; label: string; color: string; min: number; max: number }[] = [
  { key: '__all__', label: 'Todos los niveles',  color: '', min: 0,  max: 101 },
  { key: 'low',    label: 'Bajo (< 30%)',        color: '#22c55e', min: 0,  max: 30  },
  { key: 'medium', label: 'Medio (30–60%)',      color: '#eab308', min: 30, max: 60  },
  { key: 'high',   label: 'Alto (60–80%)',        color: '#f97316', min: 60, max: 80  },
  { key: 'full',   label: 'Lleno (> 80%)',        color: '#ef4444', min: 80, max: 101 },
]

const SHIFT_FILTERS: { key: ShiftFilter; label: string }[] = [
  { key: '__all__',   label: 'Todos los turnos' },
  { key: 'morning',   label: 'Mañana (morning)' },
  { key: 'afternoon', label: 'Tarde (afternoon)' },
  { key: 'night',     label: 'Noche (night)' },
]

export function MapView({ circuits, trucks, loading }: Props) {
  const [filterCircuit, setFilterCircuit] = useState<string>('__all__')
  const [filterFill, setFilterFill] = useState<FillFilter>('__all__')
  const [filterShift, setFilterShift] = useState<ShiftFilter>('__all__')

  const circuitIds = useMemo(
    () =>
      filterCircuit === '__all__'
        ? circuits.map((c) => c.circuit_id)
        : [filterCircuit],
    [circuits, filterCircuit],
  )

  const containersFetcher = useCallback(
    () => fetchAllContainers(circuitIds),
    [circuitIds],
  )
  const { data: containers } = usePolling<Container[]>(containersFetcher, [circuitIds])

  const displayContainers = useMemo(() => {
    let all = containers ?? []
    if (filterShift !== '__all__') {
      all = all.filter((c) => c.shift === filterShift)
    }
    if (filterFill === '__all__') return all
    const f = FILL_FILTERS.find((ff) => ff.key === filterFill)!
    return all.filter((c) => {
      const level = c.fill_level ?? 0
      return level >= f.min && level < f.max
    })
  }, [containers, filterFill, filterShift])

  const filteredTrucks = useMemo(
    () =>
      filterCircuit === '__all__'
        ? trucks
        : trucks.filter((t) => t.circuit_id === filterCircuit),
    [trucks, filterCircuit],
  )

  if (loading && !containers) {
    return <div className="loading">Cargando mapa...</div>
  }

  return (
    <div className="map-wrapper">
      <MapContainer center={MVD_CENTER} zoom={12} zoomControl={true}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
          url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
        />

        <FitBounds containers={displayContainers} />

        {displayContainers.map((c) => (
          <CircleMarker
            key={c.container_id}
            center={[c.latitude, c.longitude]}
            radius={6}
            pathOptions={{
              fillColor: fillColor(c.fill_level ?? 0),
              fillOpacity: 0.85,
              color: '#000',
              weight: 0.5,
            }}
          >
            <Popup>
              <strong>{c.container_id}</strong><br />
              Circuito: {c.circuit_id}<br />
              Nivel: {Math.round(c.fill_level ?? 0)}%<br />
              {c.needs_collection && <em>Necesita recoleccion</em>}
            </Popup>
          </CircleMarker>
        ))}

        {filteredTrucks
          .filter((t) => t.latitude && t.longitude)
          .map((t) => (
            <Marker
              key={t.truck_id}
              position={[t.latitude!, t.longitude!]}
              icon={truckIcon}
            >
              <Popup>
                <strong>{t.truck_id}</strong><br />
                Estado: {t.status}<br />
                {t.circuit_id && <>Circuito: {t.circuit_id}</>}
              </Popup>
            </Marker>
          ))}
      </MapContainer>

      {/* Controls overlay */}
      <div className="map-controls">
        <select
          value={filterCircuit}
          onChange={(e) => setFilterCircuit(e.target.value)}
        >
          <option value="__all__">Todos los circuitos</option>
          {circuits.map((c) => (
            <option key={c.circuit_id} value={c.circuit_id}>
              {c.circuit_id} ({c.total_containers})
            </option>
          ))}
        </select>
        <select
          value={filterShift}
          onChange={(e) => setFilterShift(e.target.value as ShiftFilter)}
        >
          {SHIFT_FILTERS.map((s) => (
            <option key={s.key} value={s.key}>{s.label}</option>
          ))}
        </select>
        <select
          value={filterFill}
          onChange={(e) => setFilterFill(e.target.value as FillFilter)}
        >
          {FILL_FILTERS.map((f) => (
            <option key={f.key} value={f.key}>{f.label}</option>
          ))}
        </select>
        <div className="map-counter">
          {displayContainers.length.toLocaleString()} contenedores
        </div>
      </div>

      {/* Legend */}
      <div className="map-legend">
        <div className="row">
          <span className="dot" style={{ background: '#22c55e' }} />
          {'< 30% — Bajo'}
        </div>
        <div className="row">
          <span className="dot" style={{ background: '#eab308' }} />
          30–60% — Medio
        </div>
        <div className="row">
          <span className="dot" style={{ background: '#f97316' }} />
          60–80% — Alto
        </div>
        <div className="row">
          <span className="dot" style={{ background: '#ef4444' }} />
          {'> 80% — Lleno'}
        </div>
      </div>
    </div>
  )
}
