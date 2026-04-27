// ─────────────────────────────────────────────────────────
// Tipos compartidos — SmartWaste Driver
// ─────────────────────────────────────────────────────────

/** Parada individual dentro de una ruta. Devuelta por GET /circuits/{id}/route */
export interface Stop {
  sequence: number
  container_id: string
  latitude: number
  longitude: number
  fill_level: number   // 0–100
  demand_kg: number
}

/** Ruta activa de un circuito, devuelta por GET /circuits/{id}/route */
export interface Route {
  route_id: string
  truck_id: string
  circuit_id: string
  status: 'active' | 'superseded'
  created_at: string           // ISO 8601
  stops: Stop[]
  total_distance_m: number
  total_duration_s: number
  depot_lat: number
  depot_lon: number
  solver: string
  solver_status: string
  route_geometry?: [number, number][]
}

/** Mensaje recibido por WebSocket cuando hay una nueva ruta */
export interface WsRouteUpdate {
  type: 'route_update'
  circuit_id: string
  route_id: string
  truck_id: string
  stops: number
  distance_km: number
  duration_min: number
  message: string
}

/** Estado de sesión del conductor (persiste en localStorage) */
export interface Session {
  truckId: string
  circuitId: string
}

/** Estado de la conexión WebSocket */
export type WsStatus = 'connecting' | 'open' | 'closed' | 'error'
