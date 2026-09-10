import { useCallback, useEffect, useRef, useState } from 'react'

// In dev Vite proxies /ws to the backend; in the Nginx image the same path is
// proxied to the backend container, so a relative URL works in both.
const socketUrl = () => {
  const override = import.meta.env.VITE_WS_URL
  if (override) return override
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/monitor`
}

const MAX_EVENTS = 300

/**
 * Auto-reconnecting client for /ws/monitor.
 * Backs off 1s -> 15s so a backend restart does not spin the browser.
 */
export default function useMonitorSocket() {
  const [connected, setConnected] = useState(false)
  const [events, setEvents] = useState([])
  const socketRef = useRef(null)
  const attemptRef = useRef(0)
  const timerRef = useRef(null)
  const closedByUs = useRef(false)

  const connect = useCallback(() => {
    if (closedByUs.current) return
    let socket
    try {
      socket = new WebSocket(socketUrl())
    } catch {
      timerRef.current = setTimeout(connect, 3000)
      return
    }
    socketRef.current = socket

    socket.onopen = () => {
      attemptRef.current = 0
      setConnected(true)
    }

    socket.onmessage = (raw) => {
      try {
        const event = JSON.parse(raw.data)
        if (event.type === 'pong') return
        setEvents((prev) => [...prev, { ...event, id: `${Date.now()}-${Math.random()}` }].slice(-MAX_EVENTS))
      } catch {
        // ignore malformed frames
      }
    }

    socket.onclose = () => {
      setConnected(false)
      if (closedByUs.current) return
      attemptRef.current += 1
      const delay = Math.min(1000 * 2 ** (attemptRef.current - 1), 15000)
      timerRef.current = setTimeout(connect, delay)
    }

    socket.onerror = () => socket.close()
  }, [])

  useEffect(() => {
    closedByUs.current = false
    connect()

    const keepalive = setInterval(() => {
      if (socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send('ping')
      }
    }, 25000)

    return () => {
      closedByUs.current = true
      clearInterval(keepalive)
      if (timerRef.current) clearTimeout(timerRef.current)
      socketRef.current?.close()
    }
  }, [connect])

  const clear = useCallback(() => setEvents([]), [])

  return { connected, events, clear }
}
