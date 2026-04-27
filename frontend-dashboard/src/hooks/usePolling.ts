import { useCallback, useEffect, useRef, useState } from 'react'

const INTERVAL_MS = 30_000

export function usePolling<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  options: { paused?: boolean } = {},
): { data: T | null; loading: boolean; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const mountedRef = useRef(true)

  const load = useCallback(async () => {
    try {
      const result = await fetcher()
      if (mountedRef.current) {
        setData(result)
        setError(null)
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      if (mountedRef.current) setLoading(false)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(() => {
    mountedRef.current = true
    setLoading(true)
    load()
    if (options.paused) return () => { mountedRef.current = false }
    const id = setInterval(load, INTERVAL_MS)
    return () => {
      mountedRef.current = false
      clearInterval(id)
    }
  }, [load, options.paused])

  return { data, loading, error, refresh: load }
}
