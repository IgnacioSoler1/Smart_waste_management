import { useEffect, useMemo, useRef } from 'react'
import { MapContainer, TileLayer, Polyline, CircleMarker, Marker, Popup, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import type { Route, Stop } from '../types'

// ── Flechas de dirección sobre la ruta ────────────────────────────────────
// Distancia en metros entre flechas consecutivas sobre la polilínea.
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
             style="transform:rotate(${deg}deg);display:block;filter:drop-shadow(0 1px 1px rgba(0,0,0,0.4))">
             <polygon points="7,1 13,13 7,10 1,13" fill="${color}" stroke="rgba(0,0,0,0.25)" stroke-width="0.5"/>
           </svg>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7],
  })
}

interface RouteArrowsProps {
  positions: [number, number][]
  color?: string
}

/** Coloca flechas triangulares rotadas cada ARROW_SPACING_M metros a lo largo de la ruta. */
function RouteArrows({ positions, color = '#1e6b3c' }: RouteArrowsProps) {
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

// Montevideo centro
const MVD_CENTER: [number, number] = [-34.9011, -56.1645]
const DEFAULT_ZOOM = 13

// ── Icono del camión ───────────────────────────────────────────────────────
const truckIcon = L.divIcon({
  className: '',
  html: `<div style="
    width:36px; height:36px; border-radius:50%;
    background:#1e6b3c; border:3px solid #fff;
    box-shadow:0 2px 8px rgba(0,0,0,0.4);
    display:flex; align-items:center; justify-content:center;
    font-size:18px; line-height:1;
  ">🚛</div>`,
  iconSize: [36, 36],
  iconAnchor: [18, 18],
})

// ── Icono del depósito ─────────────────────────────────────────────────────
const depotIcon = L.divIcon({
  className: '',
  html: `<div style="
    width:32px; height:32px; border-radius:4px;
    background:#374151; border:2px solid #fff;
    box-shadow:0 2px 6px rgba(0,0,0,0.4);
    display:flex; align-items:center; justify-content:center;
    font-size:16px;
  ">🏭</div>`,
  iconSize: [32, 32],
  iconAnchor: [16, 16],
})

// ── Color por nivel de llenado ─────────────────────────────────────────────
function fillColor(fillLevel: number, emptied: boolean): string {
  if (emptied)        return '#9ca3af'  // gris — ya vaciado
  if (fillLevel > 70) return '#ef4444'  // rojo
  if (fillLevel > 40) return '#f59e0b'  // ámbar
  return '#22c55e'                       // verde
}

// ── Componente interno: ajusta el bounds del mapa a la ruta ───────────────
function FitBounds({ positions }: { positions: [number, number][] }) {
  const map = useMap()
  useEffect(() => {
    if (positions.length > 1) {
      map.fitBounds(L.latLngBounds(positions), { padding: [40, 40] })
    }
  }, [map, positions])
  return null
}

// ─────────────────────────────────────────────────────────
// Props
// ─────────────────────────────────────────────────────────
interface RouteMapProps {
  route: Route | null
  emptiedIds: Set<string>
  truckPosition: [number, number] | null
  selectedStopId?: string | null
  onStopClick?: (stop: Stop) => void
}

// Abre el popup de un marker y centra el mapa en él
function OpenPopup({ markerRef, lat, lng }: { markerRef: React.RefObject<L.CircleMarker | null>; lat: number; lng: number }) {
  const map = useMap()
  useEffect(() => {
    if (markerRef.current) {
      map.setView([lat, lng], map.getZoom(), { animate: true })
      markerRef.current.openPopup()
    }
  }, [markerRef, lat, lng, map])
  return null
}

// ── Marker individual por parada (maneja ref para abrir popup) ────────────
function StopMarker({ stop, emptied, isSelected, onClick }: {
  stop: Stop; emptied: boolean; isSelected: boolean; onClick: () => void
}) {
  const ref = useRef<L.CircleMarker>(null)
  const color = fillColor(stop.fill_level, emptied)

  return (
    <>
      <CircleMarker
        ref={ref}
        center={[stop.latitude, stop.longitude]}
        radius={emptied ? 7 : 10}
        pathOptions={{
          color: '#fff',
          weight: 2,
          fillColor: color,
          fillOpacity: emptied ? 0.5 : 0.9,
        }}
        eventHandlers={{ click: onClick }}
      >
        <Popup>
          <strong>#{stop.sequence} · {stop.container_id}</strong>
          <br />Llenado: <strong>{stop.fill_level}%</strong>
          <br />{stop.demand_kg} kg estimados
          {emptied && <><br /><em>✓ Vaciado</em></>}
        </Popup>
      </CircleMarker>
      {isSelected && (
        <OpenPopup markerRef={ref} lat={stop.latitude} lng={stop.longitude} />
      )}
    </>
  )
}

// ─────────────────────────────────────────────────────────
// Componente principal
// ─────────────────────────────────────────────────────────
export function RouteMap({ route, emptiedIds, truckPosition, selectedStopId, onStopClick }: RouteMapProps) {
  const sortedStops: Stop[] = route
    ? [...route.stops].sort((a, b) => a.sequence - b.sequence)
    : []

  // Polyline: usa geometría real de calles si está disponible,
  // si no, línea recta entre paradas como fallback.
  const hasRoadGeometry = !!(route?.route_geometry && route.route_geometry.length > 1)
  const polylinePoints: [number, number][] = route
    ? hasRoadGeometry
      ? route.route_geometry as [number, number][]
      : [
          [route.depot_lat, route.depot_lon],
          ...sortedStops.map<[number, number]>(s => [s.latitude, s.longitude]),
          [route.depot_lat, route.depot_lon],
        ]
    : []

  // Todos los puntos para fitBounds
  const allPoints: [number, number][] = polylinePoints.length > 0
    ? polylinePoints
    : truckPosition
      ? [truckPosition]
      : [MVD_CENTER]

  return (
    <MapContainer
      center={MVD_CENTER}
      zoom={DEFAULT_ZOOM}
      style={{ width: '100%', height: '100%' }}
      zoomControl={true}
    >
      <TileLayer
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        maxZoom={19}
      />

      {polylinePoints.length > 1 && (
        <>
          <FitBounds positions={allPoints} />

          {/* Línea de la ruta — sólida si tiene geometría real, punteada si es fallback */}
          <Polyline
            positions={polylinePoints}
            pathOptions={{
              color: '#1e6b3c',
              weight: hasRoadGeometry ? 4 : 3,
              opacity: 0.8,
              ...(hasRoadGeometry ? {} : { dashArray: '6 4' }),
            }}
          />

          {/* Flechas de dirección — solo cuando hay geometría real de calles */}
          {hasRoadGeometry && (
            <RouteArrows positions={polylinePoints} color="#1e6b3c" />
          )}

          {/* Depósito */}
          <Marker position={[route!.depot_lat, route!.depot_lon]} icon={depotIcon}>
            <Popup>
              <strong>Depósito</strong>
              <br />Inicio y fin de la ruta
            </Popup>
          </Marker>

          {/* Contenedores */}
          {sortedStops.map((stop) => (
            <StopMarker
              key={stop.container_id}
              stop={stop}
              emptied={emptiedIds.has(stop.container_id)}
              isSelected={stop.container_id === selectedStopId}
              onClick={() => onStopClick?.(stop)}
            />
          ))}
        </>
      )}

      {/* Posición del camión */}
      {truckPosition && (
        <Marker position={truckPosition} icon={truckIcon}>
          <Popup>Tu camión</Popup>
        </Marker>
      )}
    </MapContainer>
  )
}
