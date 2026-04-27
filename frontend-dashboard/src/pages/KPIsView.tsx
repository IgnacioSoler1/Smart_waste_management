import { useMemo } from 'react'
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  Cell,
  PieChart,
  Pie,
} from 'recharts'
import type { CircuitSummary, RouteComparison, Truck } from '../types'

interface Props {
  circuits: CircuitSummary[]
  trucks: Truck[]
  comparison: RouteComparison | null
}

export function KPIsView({ circuits, trucks, comparison }: Props) {
  const totalContainers = useMemo(
    () => circuits.reduce((s, c) => s + c.total_containers, 0),
    [circuits],
  )

  const totalNeedsCollection = useMemo(
    () => circuits.reduce((s, c) => s + c.needs_collection, 0),
    [circuits],
  )

  const avgFill = useMemo(() => {
    if (circuits.length === 0) return 0
    const sum = circuits.reduce((s, c) => s + c.avg_fill_level * c.total_containers, 0)
    return totalContainers > 0 ? Math.round(sum / totalContainers) : 0
  }, [circuits, totalContainers])

  const activeTrucks = useMemo(
    () => trucks.filter((t) => t.status === 'active' || t.status === 'en_route').length,
    [trucks],
  )

  // Fill level distribution — uses per-container bucket counts from the API
  const fillDistribution = useMemo(() => {
    let low = 0, medium = 0, high = 0, full = 0
    for (const c of circuits) {
      low    += c.fill_below_30 ?? 0
      medium += c.fill_30_60    ?? 0
      high   += c.fill_60_80    ?? 0
      full   += c.fill_above_80 ?? 0
    }
    return [
      { name: '< 30%',  value: low,    color: '#22c55e' },
      { name: '30-60%', value: medium, color: '#eab308' },
      { name: '60-80%', value: high,   color: '#f97316' },
      { name: '> 80%',  value: full,   color: '#ef4444' },
    ]
  }, [circuits])

  // Stats by shift
  const byShift = useMemo(() => {
    const groups: Record<string, { shift: string; circuits: number; containers: number; needsCollection: number; avgFill: number }> = {}
    for (const c of circuits) {
      const key = c.shift || 'sin_turno'
      if (!groups[key]) {
        groups[key] = { shift: key, circuits: 0, containers: 0, needsCollection: 0, avgFill: 0 }
      }
      groups[key].circuits += 1
      groups[key].containers += c.total_containers
      groups[key].needsCollection += c.needs_collection
    }
    for (const g of Object.values(groups)) {
      const shiftCircuits = circuits.filter((c) => (c.shift || 'sin_turno') === g.shift)
      const totalFill = shiftCircuits.reduce((s, c) => s + c.avg_fill_level * c.total_containers, 0)
      g.avgFill = g.containers > 0 ? Math.round(totalFill / g.containers) : 0
    }
    return Object.values(groups).sort((a, b) => a.shift.localeCompare(b.shift))
  }, [circuits])

  // Top 10 circuits by needs_collection
  const topCircuits = useMemo(
    () =>
      [...circuits]
        .sort((a, b) => b.needs_collection - a.needs_collection)
        .slice(0, 10)
        .map((c) => ({
          name: c.circuit_id.length > 16 ? c.circuit_id.slice(-12) : c.circuit_id,
          fullName: c.circuit_id,
          pendientes: c.needs_collection,
          total: c.total_containers,
          avgFill: c.avg_fill_level,
        })),
    [circuits],
  )

  if (circuits.length === 0) {
    return <div className="loading">Cargando KPIs...</div>
  }

  return (
    <div className="kpis-view">
      {/* Summary cards */}
      <div className="cards">
        <div className="card">
          <div className="card-label">Total contenedores</div>
          <div className="card-value">{totalContainers.toLocaleString()}</div>
          <div className="card-sub">{circuits.length} circuitos</div>
        </div>
        <div className="card">
          <div className="card-label">Necesitan recoleccion</div>
          <div className="card-value" style={{ color: '#f97316' }}>
            {totalNeedsCollection.toLocaleString()}
          </div>
          <div className="card-sub">
            {totalContainers > 0
              ? `${Math.round((totalNeedsCollection / totalContainers) * 100)}% del total`
              : '—'}
          </div>
        </div>
        <div className="card">
          <div className="card-label">Fill promedio</div>
          <div className="card-value">{avgFill}%</div>
        </div>
        <div className="card">
          <div className="card-label">Camiones activos</div>
          <div className="card-value" style={{ color: '#3b82f6' }}>
            {activeTrucks}
          </div>
          <div className="card-sub">{trucks.length} total</div>
        </div>
      </div>

      {/* Charts row */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, padding: '0 24px' }}>
        {/* Top 10 circuits bar chart */}
        <div className="chart-section">
          <h3>Top 10 circuitos — Contenedores pendientes</h3>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={topCircuits} margin={{ top: 5, right: 20, bottom: 60, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
              <XAxis
                dataKey="name"
                tick={{ fill: '#8b8fa3', fontSize: 11 }}
                angle={-45}
                textAnchor="end"
              />
              <YAxis tick={{ fill: '#8b8fa3', fontSize: 12 }} />
              <Tooltip
                contentStyle={{ background: '#222535', border: '1px solid #2e3248', borderRadius: 8 }}
                labelStyle={{ color: '#e4e6ed' }}
                formatter={(value: number, name: string) => [value, name === 'pendientes' ? 'Pendientes' : name]}
                labelFormatter={(label: string) => {
                  const item = topCircuits.find((c) => c.name === label)
                  return item ? item.fullName : label
                }}
              />
              <Bar dataKey="pendientes" radius={[4, 4, 0, 0]}>
                {topCircuits.map((entry, i) => (
                  <Cell
                    key={i}
                    fill={entry.avgFill >= 80 ? '#ef4444' : entry.avgFill >= 60 ? '#f97316' : '#eab308'}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>

        {/* Fill distribution pie */}
        <div className="chart-section">
          <h3>Distribucion por nivel de llenado</h3>
          <ResponsiveContainer width="100%" height={300}>
            <PieChart>
              <Pie
                data={fillDistribution}
                cx="50%"
                cy="50%"
                innerRadius={60}
                outerRadius={100}
                dataKey="value"
                label={({ name, percent }) =>
                  `${name}: ${(percent * 100).toFixed(0)}%`
                }
                labelLine={{ stroke: '#8b8fa3' }}
              >
                {fillDistribution.map((entry, i) => (
                  <Cell key={i} fill={entry.color} />
                ))}
              </Pie>
              <Tooltip
                contentStyle={{ background: '#222535', border: '1px solid #2e3248', borderRadius: 8 }}
                formatter={(value: number) => [value.toLocaleString(), 'Contenedores']}
              />
            </PieChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* By shift */}
      <div className="chart-section" style={{ margin: '24px 24px 0' }}>
        <h3>Metricas por turno</h3>
        <ResponsiveContainer width="100%" height={250}>
          <BarChart data={byShift} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
            <XAxis dataKey="shift" tick={{ fill: '#8b8fa3', fontSize: 12 }} />
            <YAxis tick={{ fill: '#8b8fa3', fontSize: 12 }} />
            <Tooltip
              contentStyle={{ background: '#222535', border: '1px solid #2e3248', borderRadius: 8 }}
              labelStyle={{ color: '#e4e6ed' }}
            />
            <Bar dataKey="containers" name="Contenedores" fill="#3b82f6" radius={[4, 4, 0, 0]} />
            <Bar dataKey="needsCollection" name="Pendientes" fill="#f97316" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* ── Comparacion: Rutas originales vs optimizadas ── */}
      {comparison && comparison.circuits_with_routes > 0 && (
        <>
          <h2 style={{ padding: '32px 24px 0', fontSize: 18, fontWeight: 700 }}>
            Impacto de la Optimizacion
          </h2>

          {/* Summary cards */}
          <div className="comparison-cards">
            <div className="card">
              <div className="card-label">Km ahorrados</div>
              <div className="card-value" style={{ color: '#22c55e' }}>
                {comparison.totals.distance_saved_km.toLocaleString()} km
              </div>
              <div className="card-sub">
                {comparison.totals.avg_distance_improvement_pct}% menos distancia
              </div>
            </div>
            <div className="card">
              <div className="card-label">Tiempo ahorrado</div>
              <div className="card-value" style={{ color: '#22c55e' }}>
                {Math.round(comparison.totals.duration_saved_min)} min
              </div>
              <div className="card-sub">
                {comparison.totals.avg_duration_improvement_pct}% menos tiempo
              </div>
            </div>
            <div className="card">
              <div className="card-label">Paradas eliminadas</div>
              <div className="card-value" style={{ color: '#3b82f6' }}>
                {comparison.totals.stops_skipped}
              </div>
              <div className="card-sub">
                {comparison.totals.baseline_stops} &rarr; {comparison.totals.optimized_stops}
              </div>
            </div>
            <div className="card">
              <div className="card-label">Circuitos optimizados</div>
              <div className="card-value">{comparison.circuits_with_routes}</div>
            </div>
          </div>

          {/* Bar chart: top 10 circuits by distance improvement */}
          <div className="chart-section" style={{ margin: '0 24px' }}>
            <h3>Top 10 circuitos — Ahorro de distancia</h3>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart
                data={comparison.by_circuit.slice(0, 10).map((c) => ({
                  name: c.circuit_id.length > 16 ? c.circuit_id.slice(-12) : c.circuit_id,
                  fullName: c.circuit_id,
                  baseline: c.baseline_distance_km,
                  optimizado: c.optimized_distance_km,
                  dist_imp: c.distance_improvement_pct,
                }))}
                margin={{ top: 5, right: 20, bottom: 60, left: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
                <XAxis
                  dataKey="name"
                  tick={{ fill: '#8b8fa3', fontSize: 11 }}
                  angle={-45}
                  textAnchor="end"
                />
                <YAxis
                  tick={{ fill: '#8b8fa3', fontSize: 12 }}
                  label={{ value: 'km', position: 'insideTopLeft', fill: '#8b8fa3', fontSize: 12 }}
                />
                <Tooltip
                  contentStyle={{ background: '#222535', border: '1px solid #2e3248', borderRadius: 8 }}
                  labelStyle={{ color: '#e4e6ed' }}
                  labelFormatter={(label: string) => {
                    const item = comparison.by_circuit.find(
                      (c) => (c.circuit_id.length > 16 ? c.circuit_id.slice(-12) : c.circuit_id) === label,
                    )
                    return item ? item.circuit_id : label
                  }}
                  formatter={(value: number, name: string) => [
                    `${value} km`,
                    name === 'baseline' ? 'Ruta original' : 'Ruta optimizada',
                  ]}
                />
                <Legend
                  formatter={(value) =>
                    value === 'baseline' ? 'Ruta original' : 'Ruta optimizada'
                  }
                />
                <Bar dataKey="baseline" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                <Bar dataKey="optimizado" fill="#22c55e" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Detail table — same top 10 as the chart */}
          <div className="chart-section" style={{ margin: '24px 24px 0' }}>
            <h3>Detalle — Top 10 circuitos con mayor ahorro</h3>
            <div className="table-wrapper" style={{ maxHeight: 400 }}>
              <table>
                <thead>
                  <tr>
                    <th>Circuito</th>
                    <th>Dist. original (km)</th>
                    <th>Dist. optimizada (km)</th>
                    <th>Ahorro dist (%)</th>
                    <th>Tiempo orig (min)</th>
                    <th>Tiempo opt (min)</th>
                    <th>Ahorro tiempo (%)</th>
                    <th>Paradas</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.by_circuit.slice(0, 10).map((c) => (
                    <tr key={c.circuit_id}>
                      <td style={{ fontWeight: 600 }}>{c.circuit_id}</td>
                      <td>{c.baseline_distance_km}</td>
                      <td>{c.optimized_distance_km}</td>
                      <td style={{ color: c.distance_improvement_pct > 0 ? '#22c55e' : '#ef4444' }}>
                        {c.distance_improvement_pct > 0 ? '+' : ''}{c.distance_improvement_pct}%
                      </td>
                      <td>{c.baseline_duration_min}</td>
                      <td>{c.optimized_duration_min}</td>
                      <td style={{ color: c.duration_improvement_pct > 0 ? '#22c55e' : '#ef4444' }}>
                        {c.duration_improvement_pct > 0 ? '+' : ''}{c.duration_improvement_pct}%
                      </td>
                      <td>{c.baseline_stops} &rarr; {c.optimized_stops}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
