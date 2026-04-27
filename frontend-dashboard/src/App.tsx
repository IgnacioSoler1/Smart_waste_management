import { useCallback, useState } from 'react'
import { fetchAnalyticsSummary, fetchCircuits, fetchRouteComparison, fetchTrucks } from './api'
import { usePolling } from './hooks/usePolling'
import { MapView } from './pages/MapView'
import { CircuitView } from './pages/CircuitView'
import { KPIsView } from './pages/KPIsView'
import { AnalyticsView } from './pages/AnalyticsView'
import type { View } from './types'

const NAV: { key: View; label: string; icon: string }[] = [
  { key: 'map', label: 'Mapa', icon: '\u{1F5FA}' },
  { key: 'circuit', label: 'Circuitos', icon: '\u{1F6E3}' },
  { key: 'kpis', label: 'KPIs', icon: '\u{1F4CA}' },
  { key: 'analytics', label: 'Analytics', icon: '\u{1F9EA}' },
]

const VIEW_TITLES: Record<View, string> = {
  map: 'Vista Mapa — Montevideo',
  circuit: 'Detalle de Circuito',
  kpis: 'KPIs Operativos',
  analytics: 'Analytics Historicos',
}

export default function App() {
  const [view, setView] = useState<View>('map')

  const circuitsFetcher = useCallback(() => fetchCircuits(), [])
  const trucksFetcher = useCallback(() => fetchTrucks(), [])
  const comparisonFetcher = useCallback(() => fetchRouteComparison(), [])
  const analyticsFetcher = useCallback(() => fetchAnalyticsSummary(), [])

  const circuits = usePolling(circuitsFetcher)
  const trucks = usePolling(trucksFetcher)
  const comparison = usePolling(comparisonFetcher)
  const analyticsData = usePolling(analyticsFetcher, [], { paused: view !== 'analytics' })

  const hasError = circuits.error || trucks.error

  return (
    <div className="layout">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="sidebar-logo">
          SmartWaste
          <span>Dashboard Operaciones</span>
        </div>
        <nav className="sidebar-nav">
          {NAV.map((n) => (
            <button
              key={n.key}
              className={view === n.key ? 'active' : ''}
              onClick={() => setView(n.key)}
            >
              <span className="icon">{n.icon}</span>
              {n.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
          {circuits.data
            ? `${circuits.data.length} circuitos cargados`
            : 'Cargando...'}
        </div>
      </aside>

      {/* Main */}
      <main className="main">
        <header className="header">
          <h1>{VIEW_TITLES[view]}</h1>
          <span className={`status ${hasError ? 'err' : 'ok'}`}>
            {hasError ? 'Error de conexion' : 'Conectado — polling 30s'}
          </span>
        </header>

        {hasError && (
          <div className="error-banner">
            {circuits.error || trucks.error}
          </div>
        )}

        <div className="content">
          {view === 'map' && (
            <MapView
              circuits={circuits.data ?? []}
              trucks={trucks.data ?? []}
              loading={circuits.loading}
            />
          )}
          {view === 'circuit' && (
            <CircuitView
              circuits={circuits.data ?? []}
              trucks={trucks.data ?? []}
            />
          )}
          {view === 'kpis' && (
            <KPIsView
              circuits={circuits.data ?? []}
              trucks={trucks.data ?? []}
              comparison={comparison.data ?? null}
            />
          )}
          {view === 'analytics' && (
            <AnalyticsView
              analytics={analyticsData.data ?? null}
              loading={analyticsData.loading}
            />
          )}
        </div>
      </main>
    </div>
  )
}
