import { apiBase } from './backend'

// Resolved in backend.js: relative in the browser, absolute in the
// desktop shell, overridable at build time with VITE_API_BASE_URL.
const BASE = apiBase
const TOKEN_KEY = 'pingpulse.token'
const DEVICE_KEY = 'pingpulse.device'

/**
 * A stable id for this installation.
 *
 * Generated once and kept locally, so the same machine keeps its licence seat
 * across restarts while a token pasted on a different machine asks for a new
 * one. A licence covers one team, not unlimited copies.
 */
function deviceId() {
  try {
    let id = localStorage.getItem(DEVICE_KEY)
    if (!id) {
      id = (crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`)
        .replace(/-/g, '')
        .slice(0, 32)
      localStorage.setItem(DEVICE_KEY, id)
    }
    return id
  } catch {
    // Private mode or blocked storage: no id, so no seat is claimed. The
    // request still works — seats stop sharing, they do not gate access.
    return ''
  }
}

/** The bearer token lives in localStorage so a refresh keeps you signed in. */
export const auth = {
  get token() {
    try {
      return localStorage.getItem(TOKEN_KEY)
    } catch {
      return null
    }
  },
  set(token) {
    try {
      if (token) localStorage.setItem(TOKEN_KEY, token)
      else localStorage.removeItem(TOKEN_KEY)
    } catch {
      // Private browsing — the session simply won't survive a refresh.
    }
  },
  clear() {
    this.set(null)
  },
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function request(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`
  const device = deviceId()
  if (device) headers['X-PingPulse-Device'] = device

  const response = await fetch(`${BASE}${path}`, { ...options, headers })

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      detail = body.detail || detail
    } catch {
      // non-JSON error body
    }
    // An expired token should land you on the sign-in screen, not in a loop.
    if (response.status === 401) auth.clear()
    throw new ApiError(response.status, detail)
  }
  return response.status === 204 ? null : response.json()
}

export const api = {
  // ------------------------------ identity ------------------------------
  // There is no sign-up and no password. A client is issued an access
  // token out of band and exchanges it for a session here.
  logIn: (token) => request('/auth/login', { method: 'POST', body: JSON.stringify({ token }) }),
  verifyToken: (token) =>
    request('/auth/verify-token', { method: 'POST', body: JSON.stringify({ token }) }),
  session: () => request('/auth/session'),
  me: () => request('/auth/me'),

  // --------------------------- organizations ----------------------------
  listOrganizations: () => request('/organizations'),
  activeOrganization: () => request('/organizations/active'),
  createOrganization: (body) => request('/organizations', { method: 'POST', body: JSON.stringify(body) }),
  updateActiveOrganization: (body) =>
    request('/organizations/active', { method: 'PATCH', body: JSON.stringify(body) }),
  switchOrganization: (organizationId) =>
    request('/organizations/switch', {
      method: 'POST',
      body: JSON.stringify({ organization_id: organizationId }),
    }),

  // ------------------------- WhatsApp connection ------------------------
  listChannels: () => request('/organizations/active/channels'),
  addChannel: (body) =>
    request('/organizations/active/channels', { method: 'POST', body: JSON.stringify(body) }),
  removeChannel: (id) =>
    request(`/organizations/active/channels/${id}`, { method: 'DELETE' }),
  startPairing: (id) =>
    request(`/organizations/active/channels/${id}/pair`, { method: 'POST' }),
  pairingState: (id) => request(`/organizations/active/channels/${id}/qr`),

  // -------------------------------- CRM ---------------------------------
  listContacts: () => request('/crm/contacts'),
  contactMessages: (id) => request(`/crm/contacts/${id}/messages`),
  updateContact: (id, body) =>
    request(`/crm/contacts/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  addTags: (id, tags) =>
    request(`/crm/contacts/${id}/tags`, { method: 'POST', body: JSON.stringify({ tags }) }),
  removeTag: (id, tag) =>
    request(`/crm/contacts/${id}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' }),

  // ------------------------------- misc ---------------------------------
  stats: () => request('/stats'),
}
