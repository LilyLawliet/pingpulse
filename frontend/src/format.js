export const STAGE_ORDER = ['LEAD', 'QUALIFIED', 'DEMO_BOOKED', 'CLOSED']

// Customer-facing wording — the database keeps the enum, the screen shows English.
export const STAGE_LABEL = {
  LEAD: 'New lead',
  QUALIFIED: 'Interested',
  DEMO_BOOKED: 'Booked',
  CLOSED: 'Won',
}

export const STAGE_STYLE = {
  LEAD: 'bg-edge text-dim',
  QUALIFIED: 'bg-customer/15 text-customer',
  DEMO_BOOKED: 'bg-warn/15 text-warn',
  CLOSED: 'bg-accent/15 text-accent',
}

export const STAGE_DOT = {
  LEAD: 'bg-faint',
  QUALIFIED: 'bg-customer',
  DEMO_BOOKED: 'bg-warn',
  CLOSED: 'bg-accent',
}

export function initialsOf(name, phone) {
  const source = (name || '').trim()
  if (source) {
    const parts = source.split(/\s+/)
    return (parts[0][0] + (parts[1]?.[0] || '')).toUpperCase()
  }
  return (phone || '?').replace(/\D/g, '').slice(-2)
}

/**
 * +923052544605 -> +92 305 254 4605
 * Grouped from the right (4-3-3) so whatever is left is the country code —
 * country codes vary in length, so anchoring on the left mis-splits them.
 */
export function prettyPhone(phone) {
  if (!phone) return ''
  const digits = phone.replace(/\D/g, '')
  if (digits.length < 8) return phone
  const last4 = digits.slice(-4)
  const mid = digits.slice(-7, -4)
  const area = digits.slice(-10, -7)
  const country = digits.slice(0, -10)
  return [country && `+${country}`, area, mid, last4].filter(Boolean).join(' ')
}

/**
 * How to label a contact whose number WhatsApp has not given us.
 *
 * WhatsApp addresses some chats by LID — a privacy identifier — and until it
 * hands over the real number that identifier is all we have. Running it through
 * prettyPhone produced "+15323 161 532 8393": a number that cannot be dialled,
 * presented as though it could. Say what is actually true instead, and keep the
 * last digits so two unnamed people are still tellable apart.
 */
export function isPlaceholderNumber(contact) {
  if (!contact?.wa_lid) return false
  return (contact.phone_number || '').replace(/\D/g, '') === String(contact.wa_lid)
}

export function contactLabel(contact) {
  if (!contact) return ''
  if (contact.name) return contact.name
  if (isPlaceholderNumber(contact)) {
    return `WhatsApp user ···${String(contact.wa_lid).slice(-4)}`
  }
  return prettyPhone(contact.phone_number)
}

export function clockOf(iso) {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/** 1240 -> "1.2s" ; 840 -> "0.8s" */
export function seconds(ms) {
  if (!ms && ms !== 0) return '—'
  return `${(ms / 1000).toFixed(1)}s`
}
