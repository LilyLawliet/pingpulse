const BASE = import.meta.env.VITE_API_BASE || '/api/v1'
const TOKEN_KEY = 'pingpulse.token'

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
  signUp: (body) => request('/auth/signup', { method: 'POST', body: JSON.stringify(body) }),
  logIn: (body) => request('/auth/login', { method: 'POST', body: JSON.stringify(body) }),
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
