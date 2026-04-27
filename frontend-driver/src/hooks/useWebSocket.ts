import { useEffect, useRef, useState, useCallback } from 'react'
import type { WsStatus } from '../types'

const WS_URL = import.meta.env.VITE_WS_URL ?? ''
const MAX_RETRIES    = 8
const BASE_DELAY_MS  = 1_500
const MAX_DELAY_MS   = 30_000

interface UseWebSocketResult {
  status: WsStatus
  send: (data: unknown) => void
}

/**
 * Conexión WebSocket al backend con reconexión automática.
 *
 * Backoff exponencial: 1.5 s → 3 s → 6 s → ... → 30 s (máximo).
 * Después de MAX_RETRIES intentos sin éxito pasa a status "error".
 *
 * @param truckId   ID del camión (query param)
 * @param circuitId ID del circuito (query param)
 * @param onMessage Callback llamado con el objeto JSON parseado
 */
export function useWebSocket(
  truckId: string,
  circuitId: string,
  onMessage: (data: unknown) => void,
): UseWebSocketResult {
  const [status, setStatus]   = useState<WsStatus>('connecting')
  const wsRef                 = useRef<WebSocket | null>(null)
  const retryCount            = useRef(0)
  const retryTimer            = useRef<ReturnType<typeof setTimeout>>()
  const onMessageRef          = useRef(onMessage)
  // Evita que onclose de un WebSocket viejo dispare reconexión tras desmontar
  const mountedRef            = useRef(true)

  // Mantener onMessage actualizado sin re-suscribir el efecto
  useEffect(() => { onMessageRef.current = onMessage }, [onMessage])

  const connect = useCallback(() => {
    if (!WS_URL || !truckId || !circuitId) return

    const url = `${WS_URL}?truck_id=${encodeURIComponent(truckId)}&circuit_id=${encodeURIComponent(circuitId)}`
    const ws  = new WebSocket(url)
    wsRef.current = ws
    setStatus('connecting')

    ws.onopen = () => {
      retryCount.current = 0
      setStatus('open')
    }

    ws.onmessage = (event) => {
      try {
        onMessageRef.current(JSON.parse(event.data as string))
      } catch {
        // mensaje no-JSON: ignorar
      }
    }

    ws.onclose = () => {
      if (!mountedRef.current) return
      setStatus('closed')
      if (retryCount.current >= MAX_RETRIES) {
        setStatus('error')
        return
      }
      const delay = Math.min(BASE_DELAY_MS * 2 ** retryCount.current, MAX_DELAY_MS)
      retryCount.current += 1
      retryTimer.current = setTimeout(connect, delay)
    }

    ws.onerror = () => {
      // onerror siempre va seguido de onclose; el retry se maneja ahí
    }
  }, [truckId, circuitId])

  useEffect(() => {
    mountedRef.current = true
    connect()
    return () => {
      mountedRef.current = false
      clearTimeout(retryTimer.current)
      wsRef.current?.close()
    }
  }, [connect])

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  return { status, send }
}
