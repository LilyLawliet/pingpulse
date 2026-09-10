/**
 * Where the backend lives.
 *
 * The dashboard runs in two very different places and they need opposite
 * defaults:
 *
 *   In a browser it is served by the same Nginx that proxies /api and /ws to
 *   the backend, so a *relative* URL is correct and keeps the dev-server proxy
 *   working too.
 *
 *   In the desktop app there is no such proxy. The page is served by the shell
 *   itself from an internal origin (tauri://localhost), so a relative URL
 *   resolves to the shell and every request fails. It needs an absolute URL.
 *
 * `VITE_API_BASE_URL` is baked in at build time and wins in both cases, which
 * is how one installer is pointed at a particular client's deployment.
 */

// Used only when the app is running in a desktop shell with nothing configured.
const DEFAULT_BACKEND_ORIGIN = 'https://pingpulse.duckdns.org'

const trimSlashes = (value) => (value || '').replace(/\/+$/, '')

/** True when the page is running inside the Tauri shell rather than a browser. */
const isDesktopShell = () => {
  if (typeof window === 'undefined') return false
  return (
    '__TAURI_INTERNALS__' in window ||
    '__TAURI__' in window ||
    window.location.protocol === 'tauri:' ||
    window.location.hostname === 'tauri.localhost'
  )
}

/**
 * The backend's origin, e.g. "https://pingpulse.duckdns.org".
 * An empty string means "same origin as this page" — the browser case.
 */
export const backendOrigin = (() => {
  const configured = trimSlashes(import.meta.env.VITE_API_BASE_URL)
  if (configured) return configured
  return isDesktopShell() ? DEFAULT_BACKEND_ORIGIN : ''
})()

/**
 * Base path for REST calls.
 *
 * VITE_API_BASE is the older variable and holds a full base *path* rather than
 * an origin. It still wins if set, so existing builds keep working.
 */
export const apiBase =
  trimSlashes(import.meta.env.VITE_API_BASE) || `${backendOrigin}/api/v1`

/**
 * Absolute URL of the live monitor websocket, including the access token.
 *
 * The token goes in the query string because a browser cannot set an
 * Authorization header on a websocket handshake — there is no API for it. The
 * server validates it before accepting the connection.
 */
export const socketUrl = (token) => {
  const base = (() => {
    const override = import.meta.env.VITE_WS_URL
    if (override) return override

    if (backendOrigin) {
      // http -> ws, https -> wss, without hard-coding either.
      return `${backendOrigin.replace(/^http/, 'ws')}/ws/monitor`
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}/ws/monitor`
  })()

  if (!token) return base
  return `${base}?token=${encodeURIComponent(token)}`
}

/**
 * Rewrite a backend-hosted media URL so it loads from wherever the backend
 * actually is. In the browser this is a no-op; in the desktop shell a
 * same-origin "/media/..." would otherwise resolve against the shell.
 */
export const mediaUrl = (url) => {
  if (!url) return url
  if (backendOrigin && url.startsWith('/')) return `${backendOrigin}${url}`
  return url
}
