import type { Route, Stop } from '../types'

// ── Helpers ────────────────────────────────────────────────────────────────

function fillBadge(fillLevel: number, emptied: boolean): { label: string; style: React.CSSProperties } {
  if (emptied) return {
    label: '✓ Vaciado',
    style: { background: '#f3f4f6', color: '#6b7280', border: '1px solid #e5e7eb' },
  }
  if (fillLevel > 70) return {
    label: `${fillLevel}%`,
    style: { background: '#fee2e2', color: '#b91c1c', border: '1px solid #fca5a5' },
  }
  if (fillLevel > 40) return {
    label: `${fillLevel}%`,
    style: { background: '#fef3c7', color: '#92400e', border: '1px solid #fcd34d' },
  }
  return {
    label: `${fillLevel}%`,
    style: { background: '#dcfce7', color: '#166534', border: '1px solid #86efac' },
  }
}

function fmt(seconds: number): string {
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return h > 0 ? `${h}h ${m}min` : `${m}min`
}

// ── Estilos inline (mobile-first, sin framework) ──────────────────────────

const S = {
  sidebar: {
    display: 'flex',
    flexDirection: 'column' as const,
    height: '100%',
    background: '#f9fafb',
    borderLeft: '1px solid #e5e7eb',
    overflow: 'hidden',
  },
  header: {
    padding: '14px 16px 10px',
    background: '#1e6b3c',
    color: '#fff',
    flexShrink: 0,
  },
  headerTitle: {
    fontSize: '15px',
    fontWeight: 700,
    margin: 0,
    letterSpacing: '0.02em',
  },
  headerMeta: {
    fontSize: '12px',
    opacity: 0.85,
    marginTop: '4px',
    display: 'flex',
    gap: '12px',
  },
  emptyState: {
    padding: '32px 16px',
    textAlign: 'center' as const,
    color: '#6b7280',
    fontSize: '14px',
  },
  list: {
    flex: 1,
    overflowY: 'auto' as const,
    padding: '8px 0',
  },
  stopItem: (highlighted: boolean, emptied: boolean): React.CSSProperties => ({
    display: 'flex',
    alignItems: 'flex-start',
    gap: '10px',
    padding: '10px 14px',
    margin: '0 8px 4px',
    borderRadius: '8px',
    background: highlighted ? '#f0fdf4' : '#fff',
    border: highlighted ? '1px solid #bbf7d0' : '1px solid #f3f4f6',
    opacity: emptied ? 0.55 : 1,
    transition: 'all 0.15s ease',
    cursor: 'default',
  }),
  seqBadge: (emptied: boolean): React.CSSProperties => ({
    minWidth: '28px',
    height: '28px',
    borderRadius: '50%',
    background: emptied ? '#e5e7eb' : '#1e6b3c',
    color: emptied ? '#9ca3af' : '#fff',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: '12px',
    fontWeight: 700,
    flexShrink: 0,
  }),
  stopInfo: {
    flex: 1,
    minWidth: 0,
  },
  stopId: {
    fontSize: '13px',
    fontWeight: 600,
    color: '#111827',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap' as const,
  },
  stopDemand: {
    fontSize: '11px',
    color: '#6b7280',
    marginTop: '2px',
  },
  badge: (style: React.CSSProperties): React.CSSProperties => ({
    display: 'inline-block',
    padding: '1px 7px',
    borderRadius: '999px',
    fontSize: '11px',
    fontWeight: 600,
    marginTop: '4px',
    ...style,
  }),
  emptyBtn: (emptied: boolean): React.CSSProperties => ({
    flexShrink: 0,
    padding: '6px 10px',
    borderRadius: '6px',
    border: 'none',
    background: emptied ? '#f3f4f6' : '#1e6b3c',
    color: emptied ? '#9ca3af' : '#fff',
    fontSize: '12px',
    fontWeight: 600,
    cursor: emptied ? 'default' : 'pointer',
    whiteSpace: 'nowrap' as const,
    transition: 'background 0.15s ease',
  }),
  footer: {
    padding: '10px 16px',
    borderTop: '1px solid #e5e7eb',
    fontSize: '11px',
    color: '#9ca3af',
    textAlign: 'center' as const,
    flexShrink: 0,
  },
}

// ── Componente ─────────────────────────────────────────────────────────────

interface StopListProps {
  route: Route | null
  loading: boolean
  error: string | null
  emptiedIds: Set<string>
  highlightedId: string | null
  onStopSelect: (stop: Stop) => void
  onEmpty: (stop: Stop) => void
}

export function StopList({
  route,
  loading,
  error,
  emptiedIds,
  highlightedId,
  onStopSelect,
  onEmpty,
}: StopListProps) {
  const sortedStops = route
    ? [...route.stops].sort((a, b) => a.sequence - b.sequence)
    : []

  const doneCount = sortedStops.filter(s => emptiedIds.has(s.container_id)).length

  return (
    <div style={S.sidebar}>
      {/* Header con resumen de la ruta */}
      <div style={S.header}>
        <p style={S.headerTitle}>
          {route ? `Circuito ${route.circuit_id}` : 'Sin ruta asignada'}
        </p>
        {route && (
          <div style={S.headerMeta}>
            <span>📍 {sortedStops.length} paradas</span>
            <span>📏 {(route.total_distance_m / 1000).toFixed(1)} km</span>
            <span>⏱ {fmt(route.total_duration_s)}</span>
          </div>
        )}
        {route && doneCount > 0 && (
          <div style={{ fontSize: '12px', marginTop: '4px', opacity: 0.9 }}>
            ✓ {doneCount} de {sortedStops.length} vaciados
          </div>
        )}
      </div>

      {/* Lista de paradas */}
      <div style={S.list}>
        {loading && (
          <div style={S.emptyState}>Cargando ruta...</div>
        )}

        {!loading && error && (
          <div style={{ ...S.emptyState, color: '#dc2626' }}>
            Error: {error}
          </div>
        )}

        {!loading && !error && !route && (
          <div style={S.emptyState}>
            <div style={{ fontSize: '32px', marginBottom: '8px' }}>🗺️</div>
            Sin ruta activa para este circuito.
            <br />La ruta se actualizará automáticamente.
          </div>
        )}

        {sortedStops.map((stop) => {
          const emptied    = emptiedIds.has(stop.container_id)
          const highlighted = stop.container_id === highlightedId
          const { label, style: badgeStyle } = fillBadge(stop.fill_level, emptied)

          return (
            <div
              key={stop.container_id}
              style={{ ...S.stopItem(highlighted, emptied), cursor: 'pointer' }}
              onClick={() => onStopSelect(stop)}
            >
              {/* Número de secuencia */}
              <div style={S.seqBadge(emptied)}>{stop.sequence}</div>

              {/* Info del contenedor */}
              <div style={S.stopInfo}>
                <div style={S.stopId}>{stop.container_id}</div>
                <div style={S.stopDemand}>{stop.demand_kg.toFixed(0)} kg estimados</div>
                <span style={S.badge(badgeStyle)}>{label}</span>
              </div>

              {/* Botón vaciar */}
              <button
                style={S.emptyBtn(emptied)}
                disabled={emptied}
                onClick={() => !emptied && onEmpty(stop)}
                title={emptied ? 'Ya vaciado' : 'Marcar como vaciado'}
              >
                {emptied ? '✓' : 'Vaciar'}
              </button>
            </div>
          )
        })}
      </div>

      {/* Footer con última actualización */}
      {route && (
        <div style={S.footer}>
          Calculada {new Date(route.created_at).toLocaleTimeString('es-UY', { hour: '2-digit', minute: '2-digit' })}
          {' · '}{route.solver}
        </div>
      )}
    </div>
  )
}
