// ─────────────────────────────────────────────────────────
// Helpers compartidos
// ─────────────────────────────────────────────────────────

export function fillColor(level: number): string {
  if (level >= 80) return '#ef4444'   // red
  if (level >= 60) return '#f97316'   // orange
  if (level >= 30) return '#eab308'   // yellow
  return '#22c55e'                     // green
}

export function fillClass(level: number): string {
  if (level >= 80) return 'full'
  if (level >= 60) return 'high'
  if (level >= 30) return 'medium'
  return 'low'
}

export function formatDistance(meters: number): string {
  return meters >= 1000
    ? `${(meters / 1000).toFixed(1)} km`
    : `${Math.round(meters)} m`
}

export function formatDuration(seconds: number): string {
  const mins = Math.round(seconds / 60)
  if (mins >= 60) {
    const h = Math.floor(mins / 60)
    const m = mins % 60
    return `${h}h ${m}m`
  }
  return `${mins} min`
}
