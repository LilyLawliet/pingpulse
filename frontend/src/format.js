/**
 * The board is no longer a constant here.
 *
 * Each organization keeps its own columns, in its own order and its own words,
 * so the stages arrive from /api/v1/pipeline and the screen renders whatever
 * comes back. These defaults are the fallback for the moment before that call
 * returns, and they match what the server falls back to, so the board never
 * flickers between two different sets of columns.
 *
 * A contact stores a key and the screen shows a label. Renaming a column must
 * not orphan the people standing in it.
 */
export const DEFAULT_STAGES = [
  { key: 'NEW_LEAD', label: 'New lead', colour: 'slate' },
  { key: 'CONTACTED', label: 'Contacted', colour: 'sky' },
  { key: 'QUALIFIED', label: 'Qualified', colour: 'cyan' },
  { key: 'ESTIMATE_SCHEDULED', label: 'Estimate scheduled', colour: 'violet' },
  { key: 'ESTIMATE_SENT', label: 'Estimate sent', colour: 'amber' },
  { key: 'FOLLOW_UP', label: 'Follow-up', colour: 'orange' },
  { key: 'WON', label: 'Won', colour: 'emerald', outcome: 'won' },
  { key: 'LOST', label: 'Lost', colour: 'rose', outcome: 'lost' },
  { key: 'UNQUALIFIED', label: 'Unqualified', colour: 'zinc', outcome: 'unqualified' },
]

// Colour names rather than classes cross the wire, because a tenant picking a
// colour should not be choosing a Tailwind utility string. Written out in full
// so the class names survive Tailwind's build-time scan, which cannot see a
// string this code assembles at runtime.
const SWATCH = {
  slate: { chip: 'bg-edge text-dim', dot: 'bg-faint' },
  zinc: { chip: 'bg-edge text-dim', dot: 'bg-faint' },
  sky: { chip: 'bg-customer/15 text-customer', dot: 'bg-customer' },
  cyan: { chip: 'bg-customer/15 text-customer', dot: 'bg-customer' },
  violet: { chip: 'bg-warn/15 text-warn', dot: 'bg-warn' },
  amber: { chip: 'bg-warn/15 text-warn', dot: 'bg-warn' },
  orange: { chip: 'bg-warn/15 text-warn', dot: 'bg-warn' },
  emerald: { chip: 'bg-accent/15 text-accent', dot: 'bg-accent' },
  rose: { chip: 'bg-crit/15 text-crit', dot: 'bg-crit' },
}
const FALLBACK_SWATCH = SWATCH.slate

/** Look a stage up by the key a contact actually stores. */
export function stageOf(stages, key) {
  return (stages || DEFAULT_STAGES).find((stage) => stage.key === key) || null
}

/** What to call this stage on screen, falling back to the raw key. */
export function stageLabel(stages, key) {
  return stageOf(stages, key)?.label || key || 'Unknown'
}

export function stageChip(stages, key) {
  return (SWATCH[stageOf(stages, key)?.colour] || FALLBACK_SWATCH).chip
}

export function stageDot(stages, key) {
  return (SWATCH[stageOf(stages, key)?.colour] || FALLBACK_SWATCH).dot
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
