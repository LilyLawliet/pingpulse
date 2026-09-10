import { useCallback, useEffect, useRef, useState } from 'react'

import { socketUrl } from './backend'
import { auth } from './api.js'

// socketUrl lives in backend.js because the desktop shell and the browser need
// different answers: the browser can use a relative host, the shell cannot.

const MAX_EVENTS = 300
const MAX_BACKOFF_MS = 15000
const PING_EVERY_MS = 25000
// If nothing at all comes back for this long — not even a pong — the socket is
// open in name only. A laptop waking from sleep, or a NAT that dropped the
// mapping while nobody was typing, both leave a connection that looks alive
// and delivers nothing.
const SILENCE_LIMIT_MS = 70000

/**
 * Auto-reconnecting client for /ws/monitor.
 *
 * The dashboard must survive the backend being redeployed underneath it. That
 * is not just "retry the socket": it means the window keeps whatever it is
 * showing, reconnects on its own with a backoff that does not hammer a server
 * still starting up, and tells the caller when it came back so the data on
 * screen can be refreshed rather than left stale.
 *
 * `generation` is that signal — it increments on every successful open, so a
 * caller can refetch with `useEffect(..., [generation])` and get the state it
 * missed while disconnected.
 */
export default function useMonitorSocket() {
  const [connected, setConnected] = useState(false)
  const [generation, setGeneration] = useState(0)
  const [events, setEvents] = useState([])

  const socketRef = useRef(null)
  const attemptRef = useRef(0)
  const timerRef = useRef(null)
  const lastSeenRef = useRef(Date.now())
  const closedByUs = useRef(false)

  const clearTimer = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }

  const connect = useCallback(() => {
    if (closedByUs.current) return
    clearTimer()

    // A socket that is already open or opening must not be duplicated — the
    // network and focus handlers below can both fire at once.
    const existing = socketRef.current
    if (existing && (existing.readyState === WebSocket.OPEN || existing.readyState === WebSocket.CONNECTING)) {
      return
    }

    let socket
    try {
      socket = new WebSocket(socketUrl(auth.token))
    } catch {
      timerRef.current = setTimeout(connect, 3000)
      return
    }
    socketRef.current = socket
    lastSeenRef.current = Date.now()

    socket.onopen = () => {
      attemptRef.current = 0
      lastSeenRef.current = Date.now()
      setConnected(true)
      // Whatever happened while we were away is not in `events`; the caller
      // reloads from the API on this.
      setGeneration((n) => n + 1)
    }

    socket.onmessage = (raw) => {
      lastSeenRef.current = Date.now()
      try {
        const event = JSON.parse(raw.data)
        if (event.type === 'pong') return
        setEvents((prev) =>
          [...prev, { ...event, id: `${Date.now()}-${Math.random()}` }].slice(-MAX_EVENTS),
        )
      } catch {
        // ignore malformed frames
      }
    }

    socket.onclose = () => {
      setConnected(false)
      if (closedByUs.current) return
      attemptRef.current += 1
      // Exponential, capped, and jittered. Without the jitter every open copy
      // of the app reconnects on the same tick after a deploy and arrives as
      // one burst against a backend that has only just started.
      const base = Math.min(1000 * 2 ** (attemptRef.current - 1), MAX_BACKOFF_MS)
      const delay = base / 2 + Math.random() * (base / 2)
      clearTimer()
      timerRef.current = setTimeout(connect, delay)
    }

    socket.onerror = () => socket.close()
  }, [])

  /** Try again right now, rather than waiting out the current backoff. */
  const reconnectNow = useCallback(() => {
    if (closedByUs.current) return
    attemptRef.current = 0
    const socket = socketRef.current
    if (socket && socket.readyState === WebSocket.OPEN) return
    if (socket && socket.readyState === WebSocket.CONNECTING) return
    clearTimer()
    connect()
  }, [connect])

  useEffect(() => {
    closedByUs.current = false
    connect()

    const heartbeat = setInterval(() => {
      const socket = socketRef.current
      if (socket?.readyState === WebSocket.OPEN) {
        // A socket that has gone quiet for longer than the ping interval
        // allows is dropped deliberately, which puts it back through the
        // reconnect path instead of leaving a dead line looking connected.
        if (Date.now() - lastSeenRef.current > SILENCE_LIMIT_MS) {
          socket.close()
          return
        }
        try {
          socket.send('ping')
        } catch {
          socket.close()
        }
      }
    }, PING_EVERY_MS)

    // The machine coming back from sleep, the network returning, or the
    // window being looked at again are all better signals than a timer.
    const wake = () => reconnectNow()
    const onVisible = () => {
      if (document.visibilityState === 'visible') reconnectNow()
    }
    window.addEventListener('online', wake)
    window.addEventListener('focus', wake)
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      closedByUs.current = true
      clearInterval(heartbeat)
      clearTimer()
      window.removeEventListener('online', wake)
      window.removeEventListener('focus', wake)
      document.removeEventListener('visibilitychange', onVisible)
      socketRef.current?.close()
    }
  }, [connect, reconnectNow])

  const clear = useCallback(() => setEvents([]), [])

  return { connected, events, clear, generation, reconnectNow }
}
