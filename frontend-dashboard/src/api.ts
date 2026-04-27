// ─────────────────────────────────────────────────────────
// API client — SmartWaste Dashboard
// ─────────────────────────────────────────────────────────

import type { AnalyticsResponse, CircuitSummary, Container, Route, RouteComparison, RouteEfficiencyTrend, TrendPoint, Truck } from './types'

const BASE = import.meta.env.VITE_API_URL

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`)
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`)
  return res.json() as Promise<T>
}

export async function fetchCircuits(): Promise<CircuitSummary[]> {
  const data = await get<{ circuits: CircuitSummary[] }>('/circuits')
  return data.circuits
}

export async function fetchContainers(circuitId: string): Promise<Container[]> {
  const data = await get<{ containers: Container[] }>(
    `/circuits/${encodeURIComponent(circuitId)}/containers`,
  )
  return data.containers
}

export async function fetchAllContainers(
  circuitIds: string[],
): Promise<Container[]> {
  const batches = await Promise.all(circuitIds.map(fetchContainers))
  return batches.flat()
}

export async function fetchRoutes(circuitId: string): Promise<Route[]> {
  try {
    const data = await get<{ routes: Route[] }>(
      `/circuits/${encodeURIComponent(circuitId)}/route`,
    )
    return data.routes
  } catch {
    return []
  }
}

export async function fetchTrucks(): Promise<Truck[]> {
  const data = await get<{ trucks: Truck[] }>('/trucks')
  return data.trucks
}

export async function fetchRouteComparison(): Promise<RouteComparison> {
  return get<RouteComparison>('/routes/comparison')
}

export async function triggerOptimize(circuitId: string): Promise<void> {
  const res = await fetch(
    `${BASE}/optimize/${encodeURIComponent(circuitId)}`,
    { method: 'POST' },
  )
  if (!res.ok) throw new Error(`Optimize failed: ${res.status}`)
}

export async function fetchAnalyticsSummary(): Promise<AnalyticsResponse> {
  return get<AnalyticsResponse>('/analytics/summary')
}

export async function fetchAnalyticsTrends(
  circuitId?: string,
  days = 30,
): Promise<TrendPoint[]> {
  const params = new URLSearchParams()
  if (circuitId) params.set('circuit_id', circuitId)
  params.set('days', String(days))
  const data = await get<{ trends: TrendPoint[] }>(`/analytics/trends?${params}`)
  return data.trends
}

export async function fetchRouteEfficiencyTrends(
  circuitId?: string,
  days = 30,
): Promise<RouteEfficiencyTrend[]> {
  const params = new URLSearchParams()
  if (circuitId) params.set('circuit_id', circuitId)
  params.set('days', String(days))
  const data = await get<{ trends: RouteEfficiencyTrend[] }>(`/analytics/route-efficiency-trends?${params}`)
  return data.trends
}
