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

/**
 * A file upload, which the JSON helper above cannot do.
 *
 * Setting Content-Type by hand on a FormData body omits the multipart
 * boundary, and the server then cannot find the file at all. The browser
 * writes that header itself, correctly, when we leave it alone.
 */
async function upload(path, file, fields = {}) {
  const body = new FormData()
  body.append('file', file)
  for (const [key, value] of Object.entries(fields)) body.append(key, value)

  const headers = {}
  if (auth.token) headers.Authorization = `Bearer ${auth.token}`
  const device = deviceId()
  if (device) headers['X-PingPulse-Device'] = device

  const response = await fetch(`${BASE}${path}`, { method: 'POST', headers, body })
  if (!response.ok) {
    let detail = response.statusText
    try {
      detail = (await response.json()).detail || detail
    } catch {
      // non-JSON error body
    }
    if (response.status === 401) auth.clear()
    throw new ApiError(response.status, detail)
  }
  return response.json()
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

  // Follow-ups. The automatic sequence fires hours later for warm
  // conversations only, so this is the same machinery driven by hand — which
  // is the only way to watch it work.
  scheduleFollowup: (id, { minutes, message }) =>
    request(`/contacts/${id}/followup`, {
      method: 'POST',
      body: JSON.stringify({ minutes, message: message || null }),
    }),
  cancelFollowup: (id) => request(`/contacts/${id}/followup`, { method: 'DELETE' }),
  updateContact: (id, body) =>
    request(`/crm/contacts/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  addTags: (id, tags) =>
    request(`/crm/contacts/${id}/tags`, { method: 'POST', body: JSON.stringify({ tags }) }),
  removeTag: (id, tag) =>
    request(`/crm/contacts/${id}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' }),

  // --------------------------- what it knows ----------------------------
  // A shop's catalogue and policies, read out of the files they already have.
  knowledgeReadiness: () => request('/knowledge/readiness'),
  listKnowledgeSources: () => request('/knowledge/sources'),
  // A catalogue is previewed before it is imported: WhatsApp reports prices as
  // an integer without saying what scale it is on, and a wrong guess has the
  // agent quoting a hundredth of the real price with total confidence.
  previewCatalogue: (scale) => request(`/knowledge/catalogue/preview?scale=${scale}`),
  importCatalogue: (scale) =>
    request(`/knowledge/catalogue/import?scale=${scale}`, { method: 'POST' }),
  uploadKnowledge: (file, docType = 'policy') =>
    upload('/knowledge/upload', file, { doc_type: docType }),
  deleteKnowledgeSource: (source) =>
    request(`/knowledge/sources?source=${encodeURIComponent(source)}`, { method: 'DELETE' }),

  // ------------------------ unanswered customers ------------------------
  // People already in the shop's WhatsApp who asked something and never got a
  // reply. Listing is read-only; replying goes one conversation at a time.
  listProspects: (days = 30) => request(`/prospects?days=${days}`),
  replyToProspect: (jid, message) =>
    request('/prospects/reply', { method: 'POST', body: JSON.stringify({ jid, message }) }),

  // ------------------------------- misc ---------------------------------
  stats: () => request('/stats'),
}
