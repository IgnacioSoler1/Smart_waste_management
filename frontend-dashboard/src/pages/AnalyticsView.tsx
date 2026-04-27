import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts'
import { MapContainer, TileLayer, useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet.heat'
import { fetchAnalyticsTrends, fetchRouteEfficiencyTrends } from '../api'
import type { AnalyticsResponse, RouteEfficiencyCircuit, RouteEfficiencyTrend, TrendPoint } from '../types'

// ─────────────────────────────────────────────────────────
// Leaflet heat plugin — dynamic import via script tag
// ─────────────────────────────────────────────────────────

function HeatLayer({ data }: { data: [number, number, number][] }) {
  const map = useMap()

  useEffect(() => {
    if (!data.length) return
    const heat = L.heatLayer(data, {
      radius: 10,
      blur: 18,
      maxZoom: 17,
      max: 1.0,
      minOpacity: 0.08,
      gradient: { 0.0: 'transparent', 0.2: '#1d4ed8', 0.45: '#22c55e', 0.65: '#eab308', 0.82: '#f97316', 1.0: '#ef4444' },
    }).addTo(map)
    return () => { map.removeLayer(heat) }
  }, [map, data])

  return null
}

// ─────────────────────────────────────────────────────────
// Props
// ─────────────────────────────────────────────────────────

interface Props {
  analytics: AnalyticsResponse | null
  loading: boolean
}

// ─────────────────────────────────────────────────────────
// Tooltip styles (shared)
// ─────────────────────────────────────────────────────────

const tooltipStyle = {
  background: '#222535',
  border: '1px solid #2e3248',
  borderRadius: 8,
}

// ─────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────

export function AnalyticsView({ analytics, loading }: Props) {
  const [showHeatmap, setShowHeatmap] = useState(true)
  const [selectedCircuit, setSelectedCircuit] = useState<string>('')
  const [trends, setTrends] = useState<TrendPoint[]>([])
  const [trendsLoading, setTrendsLoading] = useState(false)
  const [alertTab, setAlertTab] = useState<'battery' | 'temperature'>('battery')
  const [selectedEffCircuit, setSelectedEffCircuit] = useState<string>('')
  const [effTrends, setEffTrends] = useState<RouteEfficiencyTrend[]>([])
  const [effTrendsLoading, setEffTrendsLoading] = useState(false)
  const [effMetric, setEffMetric] = useState<'pct' | 'km' | 'min'>('pct')

  // Load trends when circuit changes
  useEffect(() => {
    if (!selectedCircuit) {
      setTrends([])
      return
    }
    let cancelled = false
    setTrendsLoading(true)
    fetchAnalyticsTrends(selectedCircuit, 30)
      .then((data) => { if (!cancelled) setTrends(data) })
      .catch(() => { if (!cancelled) setTrends([]) })
      .finally(() => { if (!cancelled) setTrendsLoading(false) })
    return () => { cancelled = true }
  }, [selectedCircuit])

  // Set default selected circuit
  useEffect(() => {
    if (!selectedCircuit && analytics?.by_circuit?.length) {
      setSelectedCircuit(analytics.by_circuit[0].circuit_id)
    }
  }, [analytics, selectedCircuit])

  // Load efficiency trends when circuit changes.
  // '__all__' → fetch all without filter and aggregate by date.
  useEffect(() => {
    if (!selectedEffCircuit) return
    let cancelled = false
    setEffTrendsLoading(true)
    const circuitArg = selectedEffCircuit === '__all__' ? undefined : selectedEffCircuit
    fetchRouteEfficiencyTrends(circuitArg, 30)
      .then((data) => {
        if (cancelled) return
        if (selectedEffCircuit === '__all__') {
          // Aggregate across all circuits: avg % improvements, sum km/min/stops
          const byDate = new Map<string, RouteEfficiencyTrend[]>()
          for (const t of data) {
            const arr = byDate.get(t.date) ?? []
            arr.push(t)
            byDate.set(t.date, arr)
          }
          const aggregated: RouteEfficiencyTrend[] = Array.from(byDate.entries())
            .map(([date, items]) => ({
              circuit_id: '__all__',
              date,
              distance_improvement_pct:
                Math.round((items.reduce((s, i) => s + i.distance_improvement_pct, 0) / items.length) * 10) / 10,
              duration_improvement_pct:
                Math.round((items.reduce((s, i) => s + i.duration_improvement_pct, 0) / items.length) * 10) / 10,
              distance_saved_km:
                Math.round(items.reduce((s, i) => s + i.distance_saved_km, 0) * 10) / 10,
              duration_saved_min:
                Math.round(items.reduce((s, i) => s + i.duration_saved_min, 0) * 10) / 10,
              baseline_distance_km:
                Math.round(items.reduce((s, i) => s + i.baseline_distance_km, 0) * 10) / 10,
              optimized_distance_km:
                Math.round(items.reduce((s, i) => s + i.optimized_distance_km, 0) * 10) / 10,
              stops_skipped: items.reduce((s, i) => s + i.stops_skipped, 0),
              routes: items.reduce((s, i) => s + i.routes, 0),
            }))
            .sort((a, b) => a.date.localeCompare(b.date))
          setEffTrends(aggregated)
        } else {
          setEffTrends(data)
        }
      })
      .catch(() => { if (!cancelled) setEffTrends([]) })
      .finally(() => { if (!cancelled) setEffTrendsLoading(false) })
    return () => { cancelled = true }
  }, [selectedEffCircuit])

  // Default to "all circuits" view when route_efficiency data arrives
  useEffect(() => {
    if (!selectedEffCircuit && analytics?.route_efficiency?.by_circuit?.length) {
      setSelectedEffCircuit('__all__')
    }
  }, [analytics, selectedEffCircuit])

  // Hotspots: top 15
  const hotspots = useMemo(() => {
    if (!analytics) return []
    return analytics.hotspots.slice(0, 15).map((h) => ({
      name: h.circuit_id.length > 16 ? h.circuit_id.slice(-12) : h.circuit_id,
      fullName: h.circuit_id,
      avg_fill: h.avg_fill_level,
      overflow: h.overflow_count,
    }))
  }, [analytics])

  const getBarColor = useCallback((fill: number) => {
    if (fill >= 80) return '#ef4444'
    if (fill >= 60) return '#f97316'
    if (fill >= 40) return '#eab308'
    return '#22c55e'
  }, [])

  const getImprovColor = useCallback((pct: number) => {
    if (pct >= 25) return '#22c55e'
    if (pct >= 10) return '#3b82f6'
    if (pct >= 0)  return '#eab308'
    return '#ef4444'
  }, [])

  const topImprovingChart = useMemo(() => {
    if (!analytics?.route_efficiency) return []
    return analytics.route_efficiency.top_improving.map((c: RouteEfficiencyCircuit) => ({
      name: c.circuit_id.length > 16 ? c.circuit_id.slice(-14) : c.circuit_id,
      fullName: c.circuit_id,
      pct: c.distance_improvement_pct,
      km_saved: Math.round((c.baseline_distance_km - c.optimized_distance_km) * 10) / 10,
      min_saved: Math.round((c.baseline_duration_min - c.optimized_duration_min) * 10) / 10,
      baseline_km: c.baseline_distance_km,
      optimized_km: c.optimized_distance_km,
      zone: c.zone,
      shift: c.shift,
    }))
  }, [analytics])

  const needsAttentionChart = useMemo(() => {
    if (!analytics?.route_efficiency) return []
    return analytics.route_efficiency.needs_attention.map((c: RouteEfficiencyCircuit) => ({
      name: c.circuit_id.length > 16 ? c.circuit_id.slice(-14) : c.circuit_id,
      fullName: c.circuit_id,
      pct: c.distance_improvement_pct,
    }))
  }, [analytics])

  if (loading && !analytics) {
    return <div className="loading">Cargando Analytics...</div>
  }

  if (!analytics) {
    return (
      <div className="loading" style={{ flexDirection: 'column', gap: 12 }}>
        <p>No hay datos de analytics disponibles.</p>
        <p style={{ color: '#8b8fa3', fontSize: 13 }}>
          Ejecuta el Glue ETL job primero:
          <code style={{ display: 'block', marginTop: 8, color: '#3b82f6' }}>
            aws glue start-job-run --job-name smartwaste-dev-daily-analytics
          </code>
        </p>
      </div>
    )
  }

  const { summary, hourly_pattern, heatmap_data, battery_alerts, temperature_alerts } = analytics

  return (
    <div className="kpis-view">
      {/* Header */}
      <div style={{ padding: '0 24px 8px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ color: '#8b8fa3', fontSize: 13 }}>
          Datos del {analytics.date} — generado {analytics.generated_at}
        </span>
      </div>

      {/* ── 1. Summary Cards ── */}
      <div className="cards" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
        <div className="card">
          <div className="card-label">Lecturas del dia</div>
          <div className="card-value">{summary.total_readings.toLocaleString()}</div>
        </div>
        <div className="card">
          <div className="card-label">Contenedores activos</div>
          <div className="card-value">{summary.containers_reporting.toLocaleString()}</div>
        </div>
        <div className="card">
          <div className="card-label">Fill promedio</div>
          <div className="card-value">{summary.avg_fill_level}%</div>
        </div>
        <div className="card">
          <div className="card-label">Overflow (&gt;90%)</div>
          <div className="card-value" style={{ color: '#ef4444' }}>
            {summary.containers_overflowing}
          </div>
        </div>
        <div className="card">
          <div className="card-label">Alertas bateria</div>
          <div className="card-value" style={{ color: summary.battery_alerts > 0 ? '#f97316' : '#22c55e' }}>
            {summary.battery_alerts}
          </div>
        </div>
        <div className="card">
          <div className="card-label">Alertas temperatura</div>
          <div className="card-value" style={{ color: summary.temperature_alerts > 0 ? '#ef4444' : '#22c55e' }}>
            {summary.temperature_alerts}
          </div>
        </div>
      </div>

      {/* ── 2. Heatmap + 3. Hourly Pattern ── */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, padding: '0 24px' }}>
        {/* Heatmap */}
        <div className="chart-section">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h3>Mapa de calor — Nivel de llenado</h3>
            <label style={{ fontSize: 13, color: '#8b8fa3', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={showHeatmap}
                onChange={(e) => setShowHeatmap(e.target.checked)}
                style={{ marginRight: 6 }}
              />
              Mostrar heatmap
            </label>
          </div>
          <div style={{ height: 340, borderRadius: 8, overflow: 'hidden' }}>
            <MapContainer
              center={[-34.88, -56.18]}
              zoom={12}
              style={{ height: '100%', width: '100%' }}
              scrollWheelZoom={true}
            >
              <TileLayer
                attribution='&copy; <a href="https://carto.com">CARTO</a>'
                url="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
              />
              {showHeatmap && heatmap_data.length > 0 && (
                <HeatLayer data={heatmap_data} />
              )}
            </MapContainer>
          </div>
        </div>

        {/* Hourly pattern */}
        <div className="chart-section">
          <h3>Patron horario — Fill level promedio</h3>
          <ResponsiveContainer width="100%" height={340}>
            <AreaChart data={hourly_pattern} margin={{ top: 10, right: 20, bottom: 5, left: 0 }}>
              <defs>
                <linearGradient id="fillGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.4} />
                  <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.05} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
              <XAxis
                dataKey="hour"
                tick={{ fill: '#8b8fa3', fontSize: 12 }}
                tickFormatter={(h: number) => `${h}h`}
              />
              <YAxis
                tick={{ fill: '#8b8fa3', fontSize: 12 }}
                domain={[0, 100]}
                tickFormatter={(v: number) => `${v}%`}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                labelStyle={{ color: '#e4e6ed' }}
                formatter={(value: number) => [`${value.toFixed(1)}%`, 'Fill promedio']}
                labelFormatter={(label: number) => `${label}:00 hs`}
              />
              <Area
                type="monotone"
                dataKey="avg_fill_level"
                stroke="#3b82f6"
                fill="url(#fillGradient)"
                strokeWidth={2}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* ── 4. Hotspots — Top 15 Circuits ── */}
      <div className="chart-section" style={{ margin: '24px 24px 0' }}>
        <h3>Hotspots — Top 15 circuitos por fill promedio del día</h3>
        <ResponsiveContainer width="100%" height={350}>
          <BarChart
            data={hotspots}
            layout="vertical"
            margin={{ top: 5, right: 20, bottom: 5, left: 80 }}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" horizontal={false} />
            <XAxis
              type="number"
              domain={[0, 100]}
              tick={{ fill: '#8b8fa3', fontSize: 12 }}
              tickFormatter={(v: number) => `${v}%`}
            />
            <YAxis
              type="category"
              dataKey="name"
              tick={{ fill: '#8b8fa3', fontSize: 11 }}
              width={75}
            />
            <Tooltip
              contentStyle={tooltipStyle}
              labelStyle={{ color: '#e4e6ed' }}
              formatter={(value: number, name: string) => [
                name === 'avg_fill' ? `${value.toFixed(1)}%` : value,
                name === 'avg_fill' ? 'Fill promedio' : 'Overflow',
              ]}
              labelFormatter={(label: string) => {
                const item = hotspots.find((h) => h.name === label)
                return item ? item.fullName : label
              }}
            />
            <Bar dataKey="avg_fill" radius={[0, 4, 4, 0]}>
              {hotspots.map((entry, i) => (
                <Cell key={i} fill={getBarColor(entry.avg_fill)} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* ── 5. Trends by Circuit ── */}
      <div className="chart-section" style={{ margin: '24px 24px 0' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <h3>Tendencia por circuito (30 dias)</h3>
          <select
            value={selectedCircuit}
            onChange={(e) => setSelectedCircuit(e.target.value)}
            style={{
              background: '#1a1d27',
              color: '#e4e6ed',
              border: '1px solid #2e3248',
              borderRadius: 6,
              padding: '6px 12px',
              fontSize: 13,
            }}
          >
            {analytics.by_circuit.map((c) => (
              <option key={c.circuit_id} value={c.circuit_id}>
                {c.circuit_id}
              </option>
            ))}
          </select>
        </div>
        {trendsLoading ? (
          <div style={{ height: 250, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b8fa3' }}>
            Cargando tendencias...
          </div>
        ) : trends.length === 0 ? (
          <div style={{ height: 250, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b8fa3' }}>
            Sin datos de tendencia para este circuito
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={250}>
            <LineChart data={trends} margin={{ top: 10, right: 20, bottom: 5, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
              <XAxis
                dataKey="date"
                tick={{ fill: '#8b8fa3', fontSize: 11 }}
                tickFormatter={(d: string) => d.slice(5)} // MM-DD
              />
              <YAxis
                tick={{ fill: '#8b8fa3', fontSize: 12 }}
                domain={[0, 100]}
                tickFormatter={(v: number) => `${v}%`}
              />
              <Tooltip
                contentStyle={tooltipStyle}
                labelStyle={{ color: '#e4e6ed' }}
                formatter={(value: number) => [`${value.toFixed(1)}%`, 'Fill promedio']}
              />
              <Line
                type="monotone"
                dataKey="avg_fill_level"
                stroke="#3b82f6"
                strokeWidth={2}
                dot={{ fill: '#3b82f6', r: 3 }}
              />
            </LineChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* ── 6. Route Efficiency ── */}
      {analytics.route_efficiency && (() => {
        const re = analytics.route_efficiency!
        return (
          <>
            {/* Header + summary cards */}
            <div style={{ padding: '24px 24px 0' }}>
              <h3 style={{ marginBottom: 16, color: '#e4e6ed', fontSize: 16, fontWeight: 600 }}>
                Eficiencia de Rutas Optimizadas
              </h3>
              <div className="cards" style={{ gridTemplateColumns: 'repeat(6, 1fr)' }}>
                <div className="card">
                  <div className="card-label">Circuitos con rutas</div>
                  <div className="card-value">{re.summary.circuits_with_routes}</div>
                </div>
                <div className="card">
                  <div className="card-label">Mejora distancia (avg)</div>
                  <div className="card-value" style={{ color: '#22c55e' }}>
                    {re.summary.avg_distance_improvement_pct}%
                  </div>
                </div>
                <div className="card">
                  <div className="card-label">KM ahorrados</div>
                  <div className="card-value" style={{ color: '#22c55e' }}>
                    {re.summary.total_distance_saved_km.toLocaleString()} km
                  </div>
                </div>
                <div className="card">
                  <div className="card-label">Mejora duracion (avg)</div>
                  <div className="card-value" style={{ color: '#22c55e' }}>
                    {re.summary.avg_duration_improvement_pct}%
                  </div>
                </div>
                <div className="card">
                  <div className="card-label">Min ahorrados</div>
                  <div className="card-value" style={{ color: '#22c55e' }}>
                    {re.summary.total_duration_saved_min.toLocaleString()}
                  </div>
                </div>
                <div className="card">
                  <div className="card-label">Paradas saltadas</div>
                  <div className="card-value">{re.summary.total_stops_skipped.toLocaleString()}</div>
                </div>
              </div>
            </div>

            {/* Top improving + needs attention charts */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, padding: '24px 24px 0' }}>
              {/* Top improving circuits */}
              <div className="chart-section">
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <h3>Top 15 — mayor mejora en distancia</h3>
                  <span style={{ fontSize: 12, color: '#8b8fa3' }}>km / min en tabla inferior</span>
                </div>
                <ResponsiveContainer width="100%" height={380}>
                  <BarChart
                    data={topImprovingChart}
                    layout="vertical"
                    margin={{ top: 5, right: 60, bottom: 5, left: 90 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" horizontal={false} />
                    <XAxis
                      type="number"
                      tick={{ fill: '#8b8fa3', fontSize: 12 }}
                      tickFormatter={(v: number) => `${v}%`}
                    />
                    <YAxis
                      type="category"
                      dataKey="name"
                      tick={{ fill: '#8b8fa3', fontSize: 11 }}
                      width={85}
                    />
                    <Tooltip
                      contentStyle={tooltipStyle}
                      labelStyle={{ color: '#e4e6ed' }}
                      formatter={(value: number, name: string) => {
                        if (name === 'pct') return [`${value.toFixed(1)}%`, 'Mejora distancia']
                        if (name === 'km_saved') return [`${value.toFixed(1)} km`, 'KM ahorrados']
                        return [value, name]
                      }}
                      labelFormatter={(label: string) => {
                        const item = topImprovingChart.find((h) => h.name === label)
                        if (!item) return label
                        return `${item.fullName} (${item.zone} / ${item.shift}) — ${item.baseline_km} km → ${item.optimized_km} km`
                      }}
                    />
                    <Bar dataKey="pct" name="pct" radius={[0, 4, 4, 0]}>
                      {topImprovingChart.map((entry, i) => (
                        <Cell key={i} fill={getImprovColor(entry.pct)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
                {/* Numerical table below the chart */}
                <div style={{ marginTop: 8, maxHeight: 160, overflowY: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
                    <thead>
                      <tr style={{ color: '#8b8fa3', borderBottom: '1px solid #2e3248' }}>
                        <th style={{ textAlign: 'left', padding: '4px 8px' }}>Circuito</th>
                        <th style={{ textAlign: 'right', padding: '4px 8px' }}>Mejora</th>
                        <th style={{ textAlign: 'right', padding: '4px 8px' }}>KM base</th>
                        <th style={{ textAlign: 'right', padding: '4px 8px' }}>KM opt.</th>
                        <th style={{ textAlign: 'right', padding: '4px 8px' }}>KM ahorr.</th>
                        <th style={{ textAlign: 'right', padding: '4px 8px' }}>Min ahorr.</th>
                      </tr>
                    </thead>
                    <tbody>
                      {topImprovingChart.map((c) => (
                        <tr key={c.fullName} style={{ borderBottom: '1px solid #1e2132', color: '#c8cad4' }}>
                          <td style={{ padding: '3px 8px', fontFamily: 'monospace', fontSize: 11 }}>{c.fullName}</td>
                          <td style={{ padding: '3px 8px', textAlign: 'right', color: getImprovColor(c.pct) }}>{c.pct.toFixed(1)}%</td>
                          <td style={{ padding: '3px 8px', textAlign: 'right' }}>{c.baseline_km}</td>
                          <td style={{ padding: '3px 8px', textAlign: 'right' }}>{c.optimized_km}</td>
                          <td style={{ padding: '3px 8px', textAlign: 'right', color: '#22c55e' }}>{c.km_saved}</td>
                          <td style={{ padding: '3px 8px', textAlign: 'right', color: '#22c55e' }}>{c.min_saved}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Needs attention (lowest improvement) */}
              <div className="chart-section">
                <h3>Top 15 — menor mejora (requieren atencion)</h3>
                <ResponsiveContainer width="100%" height={380}>
                  <BarChart
                    data={needsAttentionChart}
                    layout="vertical"
                    margin={{ top: 5, right: 40, bottom: 5, left: 90 }}
                  >
                    <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" horizontal={false} />
                    <XAxis
                      type="number"
                      tick={{ fill: '#8b8fa3', fontSize: 12 }}
                      tickFormatter={(v: number) => `${v}%`}
                    />
                    <YAxis
                      type="category"
                      dataKey="name"
                      tick={{ fill: '#8b8fa3', fontSize: 11 }}
                      width={85}
                    />
                    <Tooltip
                      contentStyle={tooltipStyle}
                      labelStyle={{ color: '#e4e6ed' }}
                      formatter={(value: number) => [`${value.toFixed(1)}%`, 'Mejora distancia']}
                      labelFormatter={(label: string) => {
                        const item = needsAttentionChart.find((h) => h.name === label)
                        return item ? item.fullName : label
                      }}
                    />
                    <Bar dataKey="pct" radius={[0, 4, 4, 0]}>
                      {needsAttentionChart.map((entry, i) => (
                        <Cell key={i} fill={getImprovColor(entry.pct)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* Historical route efficiency trends */}
            <div className="chart-section" style={{ margin: '24px 24px 0' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                <h3>
                  Tendencia historica de rutas (30 dias)
                  {selectedEffCircuit && selectedEffCircuit !== '__all__' && (
                    <span style={{ color: '#8b8fa3', fontWeight: 400, fontSize: 13, marginLeft: 8 }}>
                      — {selectedEffCircuit}
                    </span>
                  )}
                </h3>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  {/* Metric toggle */}
                  <div style={{ display: 'flex', gap: 0 }}>
                    {([['pct', '% mejora'], ['km', 'KM ahorr.'], ['min', 'Min ahorr.']] as const).map(([key, label]) => (
                      <button
                        key={key}
                        onClick={() => setEffMetric(key)}
                        style={{
                          padding: '5px 12px',
                          background: effMetric === key ? '#3b82f6' : '#1a1d27',
                          color: effMetric === key ? '#fff' : '#8b8fa3',
                          border: '1px solid #2e3248',
                          borderLeft: key === 'pct' ? '1px solid #2e3248' : 'none',
                          borderRadius: key === 'pct' ? '6px 0 0 6px' : key === 'min' ? '0 6px 6px 0' : '0',
                          cursor: 'pointer',
                          fontSize: 12,
                        }}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  {/* Circuit selector */}
                  <select
                    value={selectedEffCircuit}
                    onChange={(e) => setSelectedEffCircuit(e.target.value)}
                    style={{
                      background: '#1a1d27',
                      color: '#e4e6ed',
                      border: '1px solid #2e3248',
                      borderRadius: 6,
                      padding: '5px 12px',
                      fontSize: 13,
                    }}
                  >
                    <option value="__all__">Todas las rutas (promedio)</option>
                    {re.by_circuit.map((c) => (
                      <option key={c.circuit_id} value={c.circuit_id}>
                        {c.circuit_id}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              {effTrendsLoading ? (
                <div style={{ height: 220, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b8fa3' }}>
                  Cargando tendencias...
                </div>
              ) : effTrends.length === 0 ? (
                <div style={{ height: 220, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#8b8fa3' }}>
                  Sin datos historicos
                </div>
              ) : (
                <>
                  {selectedEffCircuit === '__all__' && (
                    <p style={{ fontSize: 12, color: '#8b8fa3', margin: '4px 0 8px', lineHeight: 1.4 }}>
                      {effMetric === 'pct'
                        ? 'Mejora promedio (%) calculada sobre todos los circuitos con rutas ese día.'
                        : effMetric === 'km'
                        ? 'KM ahorrados totales sumados sobre todos los circuitos ese día.'
                        : 'Minutos ahorrados totales sumados sobre todos los circuitos ese día.'}
                    </p>
                  )}
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={effTrends} margin={{ top: 10, right: 20, bottom: 5, left: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
                      <XAxis
                        dataKey="date"
                        tick={{ fill: '#8b8fa3', fontSize: 11 }}
                        tickFormatter={(d: string) => d.slice(5)}
                      />
                      <YAxis
                        tick={{ fill: '#8b8fa3', fontSize: 12 }}
                        tickFormatter={(v: number) =>
                          effMetric === 'pct' ? `${v}%`
                          : effMetric === 'km' ? `${v} km`
                          : `${v} min`
                        }
                      />
                      <Tooltip
                        contentStyle={tooltipStyle}
                        labelStyle={{ color: '#e4e6ed' }}
                        formatter={(value: number) => {
                          const isAll = selectedEffCircuit === '__all__'
                          if (effMetric === 'pct')
                            return [`${value.toFixed(1)}%`, isAll ? 'Mejora promedio' : 'Mejora distancia']
                          if (effMetric === 'km')
                            return [`${value.toFixed(1)} km`, isAll ? 'KM ahorrados (total)' : 'KM ahorrados']
                          return [`${value.toFixed(1)} min`, isAll ? 'Min ahorrados (total)' : 'Min ahorrados']
                        }}
                      />
                      <Line
                        type="monotone"
                        dataKey={
                          effMetric === 'pct' ? 'distance_improvement_pct'
                          : effMetric === 'km' ? 'distance_saved_km'
                          : 'duration_saved_min'
                        }
                        stroke="#22c55e"
                        strokeWidth={2}
                        dot={{ fill: '#22c55e', r: 3 }}
                        connectNulls
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </>
              )}
            </div>

            {/* By zone + by shift */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, padding: '24px 24px 0' }}>
              {/* By zone */}
              <div className="chart-section">
                <h3>Mejora por zona</h3>
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={re.by_zone} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
                    <XAxis dataKey="zone" tick={{ fill: '#8b8fa3', fontSize: 13 }} />
                    <YAxis
                      tick={{ fill: '#8b8fa3', fontSize: 12 }}
                      tickFormatter={(v: number) => `${v}%`}
                    />
                    <Tooltip
                      contentStyle={tooltipStyle}
                      labelStyle={{ color: '#e4e6ed' }}
                      formatter={(value: number, name: string) => [
                        name === 'avg_distance_improvement_pct' ? `${value.toFixed(1)}%` : `${value.toFixed(1)} km`,
                        name === 'avg_distance_improvement_pct' ? 'Mejora promedio' : 'KM ahorrados',
                      ]}
                    />
                    <Bar dataKey="avg_distance_improvement_pct" name="avg_distance_improvement_pct" radius={[4, 4, 0, 0]}>
                      {re.by_zone.map((entry, i) => (
                        <Cell key={i} fill={getImprovColor(entry.avg_distance_improvement_pct)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
                <div style={{ marginTop: 8 }}>
                  {re.by_zone.map((z) => (
                    <div key={z.zone} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #2e3248', fontSize: 13, color: '#8b8fa3' }}>
                      <span style={{ color: '#e4e6ed', textTransform: 'capitalize' }}>{z.zone}</span>
                      <span>{z.circuits} circuitos · {z.total_saved_km} km ahorrados</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* By shift */}
              <div className="chart-section">
                <h3>Mejora por turno</h3>
                <ResponsiveContainer width="100%" height={180}>
                  <BarChart data={re.by_shift} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#2e3248" />
                    <XAxis dataKey="shift" tick={{ fill: '#8b8fa3', fontSize: 13 }} />
                    <YAxis
                      tick={{ fill: '#8b8fa3', fontSize: 12 }}
                      tickFormatter={(v: number) => `${v}%`}
                    />
                    <Tooltip
                      contentStyle={tooltipStyle}
                      labelStyle={{ color: '#e4e6ed' }}
                      formatter={(value: number, name: string) => [
                        name === 'avg_distance_improvement_pct' ? `${value.toFixed(1)}%` : `${value.toFixed(1)} km`,
                        name === 'avg_distance_improvement_pct' ? 'Mejora promedio' : 'KM ahorrados',
                      ]}
                    />
                    <Bar dataKey="avg_distance_improvement_pct" name="avg_distance_improvement_pct" radius={[4, 4, 0, 0]}>
                      {re.by_shift.map((entry, i) => (
                        <Cell key={i} fill={getImprovColor(entry.avg_distance_improvement_pct)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
                <div style={{ marginTop: 8 }}>
                  {re.by_shift.map((s) => (
                    <div key={s.shift} style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0', borderBottom: '1px solid #2e3248', fontSize: 13, color: '#8b8fa3' }}>
                      <span style={{ color: '#e4e6ed' }}>Turno {s.shift}</span>
                      <span>{s.circuits} circuitos · {s.total_saved_km} km ahorrados</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </>
        )
      })()}

      {/* ── 7. Alerts Table ── */}
      <div className="chart-section" style={{ margin: '24px 24px 0' }}>
        <div style={{ display: 'flex', gap: 0, marginBottom: 12 }}>
          <button
            onClick={() => setAlertTab('battery')}
            style={{
              padding: '8px 20px',
              background: alertTab === 'battery' ? '#2a2e3f' : 'transparent',
              color: alertTab === 'battery' ? '#e4e6ed' : '#8b8fa3',
              border: '1px solid #2e3248',
              borderRadius: '6px 0 0 6px',
              cursor: 'pointer',
              fontSize: 13,
            }}
          >
            Bateria ({battery_alerts.length})
          </button>
          <button
            onClick={() => setAlertTab('temperature')}
            style={{
              padding: '8px 20px',
              background: alertTab === 'temperature' ? '#2a2e3f' : 'transparent',
              color: alertTab === 'temperature' ? '#e4e6ed' : '#8b8fa3',
              border: '1px solid #2e3248',
              borderLeft: 'none',
              borderRadius: '0 6px 6px 0',
              cursor: 'pointer',
              fontSize: 13,
            }}
          >
            Temperatura ({temperature_alerts.length})
          </button>
        </div>

        <div className="table-wrapper" style={{ maxHeight: 300 }}>
          {alertTab === 'battery' ? (
            <table>
              <thead>
                <tr>
                  <th>Contenedor</th>
                  <th>Circuito</th>
                  <th>Bateria minima</th>
                </tr>
              </thead>
              <tbody>
                {battery_alerts.length === 0 ? (
                  <tr><td colSpan={3} style={{ textAlign: 'center', color: '#8b8fa3' }}>Sin alertas</td></tr>
                ) : (
                  battery_alerts.map((a) => (
                    <tr key={a.container_id}>
                      <td style={{ fontWeight: 600 }}>{a.container_id}</td>
                      <td>{a.circuit_id}</td>
                      <td style={{ color: a.min_battery < 10 ? '#ef4444' : '#f97316' }}>
                        {a.min_battery.toFixed(1)}%
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Contenedor</th>
                  <th>Circuito</th>
                  <th>Temperatura</th>
                  <th>Ubicacion</th>
                </tr>
              </thead>
              <tbody>
                {temperature_alerts.length === 0 ? (
                  <tr><td colSpan={4} style={{ textAlign: 'center', color: '#8b8fa3' }}>Sin alertas</td></tr>
                ) : (
                  temperature_alerts.map((a) => (
                    <tr key={a.container_id}>
                      <td style={{ fontWeight: 600 }}>{a.container_id}</td>
                      <td>{a.circuit_id}</td>
                      <td style={{ color: '#ef4444' }}>{a.temperature.toFixed(1)}°C</td>
                      <td style={{ color: '#8b8fa3', fontSize: 12 }}>
                        {a.lat.toFixed(4)}, {a.lon.toFixed(4)}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
