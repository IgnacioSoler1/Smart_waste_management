// ─────────────────────────────────────────────────────────
// Tipos — SmartWaste Dashboard
// ─────────────────────────────────────────────────────────

export interface Container {
  container_id: string
  circuit_id: string
  latitude: number
  longitude: number
  fill_level: number        // 0–100
  needs_collection: boolean
  status: string
  shift: string
  last_reading?: string     // ISO 8601
}

export interface CircuitSummary {
  circuit_id: string
  shift: string
  total_containers: number
  needs_collection: number
  avg_fill_level: number
  fill_below_30: number
  fill_30_60: number
  fill_60_80: number
  fill_above_80: number
}

export interface Stop {
  sequence: number
  container_id: string
  latitude: number
  longitude: number
  fill_level: number
  demand_kg: number
}

export interface Route {
  route_id: string
  truck_id: string
  circuit_id: string
  status: 'active' | 'superseded'
  created_at: string
  stops: Stop[]
  total_distance_m: number
  total_duration_s: number
  depot_lat: number
  depot_lon: number
  solver: string
  solver_status: string
  route_geometry?: [number, number][]
}

export interface Truck {
  truck_id: string
  status: string
  circuit_id?: string
  latitude?: number
  longitude?: number
  capacity_kg?: number
}

export interface RouteComparisonTotals {
  baseline_distance_km: number
  optimized_distance_km: number
  distance_saved_km: number
  avg_distance_improvement_pct: number
  baseline_duration_min: number
  optimized_duration_min: number
  duration_saved_min: number
  avg_duration_improvement_pct: number
  baseline_stops: number
  optimized_stops: number
  stops_skipped: number
}

export interface CircuitComparison {
  circuit_id: string
  baseline_distance_km: number
  optimized_distance_km: number
  distance_improvement_pct: number
  baseline_duration_min: number
  optimized_duration_min: number
  duration_improvement_pct: number
  baseline_stops: number
  optimized_stops: number
}

export interface RouteComparison {
  circuits_with_routes: number
  totals: RouteComparisonTotals
  by_circuit: CircuitComparison[]
}

// ─────────────────────────────────────────────────────────
// Analytics
// ─────────────────────────────────────────────────────────

export interface AnalyticsSummaryData {
  total_readings: number
  containers_reporting: number
  avg_fill_level: number
  containers_overflowing: number
  containers_underutilized: number
  battery_alerts: number
  temperature_alerts: number
}

export interface CircuitAnalytics {
  circuit_id: string
  zone: string
  shift: string
  avg_fill_level: number
  max_fill_level: number
  median_fill_level: number
  containers_reporting: number
  overflow_count: number
  avg_battery: number
  avg_fill_rate_pct_per_hour: number
  predicted_hours_to_full: number
}

export interface HourlyPattern {
  hour: number
  avg_fill_level: number
  containers: number
}

export interface Hotspot {
  circuit_id: string
  avg_fill_level: number
  overflow_count: number
}

export interface BatteryAlert {
  container_id: string
  circuit_id: string
  min_battery: number
}

export interface TemperatureAlert {
  container_id: string
  circuit_id: string
  temperature: number
  lat: number
  lon: number
}

export interface Prediction {
  container_id: string
  circuit_id: string
  current_fill: number
  fill_rate_pct_per_hour: number
  predicted_hours_to_80: number
}

export interface RouteEfficiencyCircuit {
  circuit_id: string
  zone: string
  shift: string
  baseline_distance_km: number
  optimized_distance_km: number
  distance_improvement_pct: number
  baseline_duration_min: number
  optimized_duration_min: number
  duration_improvement_pct: number
  baseline_stops: number
  optimized_stops: number
  stops_skipped: number
}

export interface RouteEfficiencyByZone {
  zone: string
  circuits: number
  avg_distance_improvement_pct: number
  total_baseline_km: number
  total_optimized_km: number
  total_saved_km: number
}

export interface RouteEfficiencyByShift {
  shift: string
  circuits: number
  avg_distance_improvement_pct: number
  total_baseline_km: number
  total_optimized_km: number
  total_saved_km: number
}

export interface RouteEfficiencySummary {
  circuits_with_routes: number
  avg_distance_improvement_pct: number
  total_distance_saved_km: number
  avg_duration_improvement_pct: number
  total_duration_saved_min: number
  total_stops_skipped: number
}

export interface RouteEfficiency {
  summary: RouteEfficiencySummary
  by_circuit: RouteEfficiencyCircuit[]
  by_zone: RouteEfficiencyByZone[]
  by_shift: RouteEfficiencyByShift[]
  top_improving: RouteEfficiencyCircuit[]
  needs_attention: RouteEfficiencyCircuit[]
}

export interface AnalyticsResponse {
  generated_at: string
  date: string
  summary: AnalyticsSummaryData
  by_circuit: CircuitAnalytics[]
  by_zone: { zone: string; avg_fill_level: number; containers: number }[]
  by_shift: { shift: string; avg_fill_level: number; circuits: number }[]
  hourly_pattern: HourlyPattern[]
  hotspots: Hotspot[]
  heatmap_data: [number, number, number][]
  battery_alerts: BatteryAlert[]
  temperature_alerts: TemperatureAlert[]
  predictions: Prediction[]
  route_efficiency?: RouteEfficiency
}

export interface TrendPoint {
  circuit_id: string
  date: string
  avg_fill_level: number
}

export interface RouteEfficiencyTrend {
  circuit_id: string
  date: string
  distance_improvement_pct: number
  duration_improvement_pct: number
  distance_saved_km: number
  duration_saved_min: number
  baseline_distance_km: number
  optimized_distance_km: number
  stops_skipped: number
  routes: number
}

export type View = 'map' | 'circuit' | 'kpis' | 'analytics'
