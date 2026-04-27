import { useState, useEffect, useCallback } from 'react'
import type { Route } from '../types'

const API_URL = import.meta.env.VITE_API_URL ?? ''

interface UseRouteResult {
  route: Route | null
  loading: boolean
  error: string | null
  refetch: () => void
}

/**
 * Obtiene la ruta activa de un circuito desde la REST API.
 * Se re-ejecuta automáticamente cuando cambia circuitId.
 * Expone refetch() para que el componente la llame al recibir
 * un route_update por WebSocket.
 */
export function useRoute(circuitId: string | null): UseRouteResult {
  const [route, setRoute]     = useState<Route | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState<string | null>(null)

  const fetchRoute = useCallback(async () => {
    if (!circuitId) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_URL}/circuits/${encodeURIComponent(circuitId)}/route`)
      if (res.status === 404) {
        setRoute(null)
        return
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      // La API devuelve { routes: [...] } con todas las rutas activas del circuito.
      // El driver solo muestra la primera ruta (su camión).
      if (data.routes && Array.isArray(data.routes)) {
        setRoute(data.routes.length > 0 ? (data.routes[0] as Route) : null)
      } else {
        // Fallback: formato antiguo (Route directa)
        setRoute(data as Route)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [circuitId])

  useEffect(() => {
    fetchRoute()
  }, [fetchRoute])

  return { route, loading, error, refetch: fetchRoute }
}
