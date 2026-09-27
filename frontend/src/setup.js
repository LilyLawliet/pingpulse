import {
  Bell,
  CalendarDays,
  Clock,
  Columns3,
  FileText,
  Globe,
  GraduationCap,
  MessageSquare,
  Store,
  TriangleAlert,
} from 'lucide-react'
import { api } from './api.js'

/**
 * What a business has to set up, in the order it should be set up.
 *
 * One list, read by every place that talks about setup: the Setup page, the
 * count in the sidebar, the welcome on entry and the lock on the other pages.
 * It used to be three lists - the Setup page counted seven required steps, the
 * empty inbox said "four things", and the two were in different orders - so
 * what a new client was told to do depended on which screen they read first.
 *
 * Three tiers, because they are genuinely different:
 *
 *   required     The agent cannot do its job without it. Until every one of
 *                these is done, the rest of the dashboard stays locked and
 *                Setup is where you land.
 *   recommended  It runs without it, but something real goes missing - booking,
 *                alerts, the diary. Counted and shown, never blocking.
 *   optional     Fine to leave as it is.
 *
 * Required steps are ordered so that WhatsApp comes last. Connecting it is the
 * moment customers start getting answers, so everything the agent needs to
 * answer them properly is in place before it.
 */
export const STEPS = [
  {
    key: 'business',
    tier: 'required',
    icon: Store,
    title: 'Your business',
    short: 'What you sell, and how the agent should sell it',
    why: 'Everything the agent says starts here. Without it, it has nothing to work from.',
    skipped: 'The agent answers with no idea what you sell.',
  },
  {
    key: 'knowledge',
    tier: 'required',
    icon: FileText,
    title: 'Prices and knowledge',
    short: 'A price list, catalogue or document it can quote from',
    why: 'The agent will never invent a price, so it can only quote what you give it here.',
    skipped: 'It has to refuse every question about cost.',
  },
  {
    key: 'timezone',
    tier: 'required',
    icon: Globe,
    title: 'Where you are',
    short: 'Your timezone, usually one click',
    why: 'The timezone your day is in. Every opening hour, appointment and follow-up is read against it.',
    skipped:
      'Times are read as UTC instead. The agent offers hours you are shut and books people at the wrong time.',
  },
  {
    key: 'whatsapp',
    tier: 'required',
    icon: MessageSquare,
    title: 'Connect WhatsApp',
    short: 'The number your customers message',
    why: 'Scan the QR with the phone that owns your business number, or use your own Twilio account. This is last on purpose: the moment it connects, customers start getting answers.',
    skipped: 'Nothing reaches you and nothing goes out. The agent is not running.',
  },
  {
    key: 'alerts',
    tier: 'recommended',
    icon: Bell,
    title: 'Alerts',
    short: 'Where you hear about hand-offs and problems',
    why: 'Where you are told when somebody asks for a person, or the agent gets stuck.',
    skipped: 'Alerts are still raised, and delivered to nobody.',
  },
  {
    key: 'hours',
    tier: 'recommended',
    icon: Clock,
    title: 'Hours and booking',
    short: 'When you are open, so it can book appointments',
    why: 'The days and times you are open. Appointments are only ever offered inside them.',
    skipped: 'Booking stays switched off, and the agent hands booking requests to you instead.',
  },
  {
    key: 'calendar',
    tier: 'recommended',
    icon: CalendarDays,
    title: 'Calendar',
    short: 'Bookings in the calendar you already use',
    why: 'A link that puts every booking in the calendar you already use. No account, nothing to sign into.',
    skipped:
      'Appointments are still taken and still shown here, but nothing reaches the calendar you actually check.',
  },
  {
    key: 'pipeline',
    tier: 'optional',
    icon: Columns3,
    title: 'Your board',
    short: 'Rename the columns leads move through',
    why: 'Rename the columns to match how you actually track work.',
    skipped: 'You keep the default columns, which is fine.',
  },
  {
    key: 'learning',
    tier: 'optional',
    icon: GraduationCap,
    title: 'Learning',
    short: 'Let it pick up the way you write',
    why: 'Let the agent pick up your way of writing from replies you send yourself.',
    skipped: 'It keeps the tone you described in Your business.',
  },
  {
    key: 'problems',
    tier: 'optional',
    icon: TriangleAlert,
    title: 'Problems',
    short: 'Anything that has gone wrong',
    why: 'Anything that has gone wrong, and what it means.',
    skipped: null,
  },
]

export const TIER_LABEL = {
  required: 'Required',
  recommended: 'Recommended',
  optional: 'Optional',
}

export const TIER_HINT = {
  required: 'Your agent cannot start without these',
  recommended: 'It runs without these, but something is missing',
  optional: 'Fine to leave as they are',
}

export const REQUIRED_STEPS = STEPS.filter((s) => s.tier === 'required').map((s) => s.key)
export const RECOMMENDED_STEPS = STEPS.filter((s) => s.tier === 'recommended').map((s) => s.key)

const RECOMMENDED_KEYS = ['alerts', 'hours', 'calendar']

/**
 * Whether WhatsApp is connected, from the channel list.
 *
 * The same rule /whatsapp/status applies - the active channel, a live paired
 * session first, then the oldest; Twilio counts as connected once configured,
 * a paired phone only while it is authenticated - but read from
 * /organizations/active/channels, which answers from the database alone.
 * /whatsapp/status also asks the WhatsApp bridge whether it is alive, which
 * can take seconds, and nothing that decides whether the inbox is locked
 * should wait on that.
 */
function whatsappFrom(channels) {
  const active = (channels || []).filter((c) => c.is_active)
  const paired = (c) => (c.whatsapp_provider || 'TWILIO').toUpperCase() === 'QR_SESSION'
  const live = (c) => !paired(c) || c.session_status === 'AUTHENTICATED'
  const connected = active.some(live)
  // Connected at some point: set up and working before, whatever it is now.
  const ever = connected || active.some((c) => paired(c) && c.session_connected_at)
  return { connected, ever }
}

/**
 * Which steps are done, read from the real backend.
 *
 * Nothing is ticked locally, so a step cannot be completed by visiting it, and
 * one done on another machine shows as done here.
 *
 * Every request goes out at once. As soon as the three that decide the lock
 * have answered - the business, its channels, its knowledge - `onRequired` is
 * called with a state marked `pending`, so the lock and the welcome never wait
 * on the recommended checks. The returned promise resolves with everything.
 *
 * Two views of WhatsApp, because they answer different questions:
 *
 *   done.whatsapp   Connected right now. What Setup ticks.
 *   gate.whatsapp   Has this business ever been connected. What the lock reads,
 *                   so a phone offline for an hour does not lock a working shop
 *                   out of its own inbox.
 *
 * `error` is set when a required check failed for any reason other than "no
 * business yet". That is not an answer, and nothing locks on it.
 */
export async function readSetup(onRequired) {
  let org = null
  let hasOrg = true
  let error = false
  let rules = false
  let documents = false
  let whatsapp = { connected: false, ever: false }
  const rest = { alerts: false, hours: false, calendar: false }

  const failed = (err) => {
    // 409 is the backend saying this account has no business yet - a token
    // issued without one. That is the first thing to set up, not a failure.
    if (err?.status === 409) hasOrg = false
    else error = true
  }

  const required = [
    api.activeOrganization().then((found) => {
      org = found
      rules = Boolean((found?.product_rules || '').trim())
    }, failed),
    api.listChannels().then((channels) => {
      whatsapp = whatsappFrom(channels)
    }, failed),
    api.knowledgeReadiness().then((readiness) => {
      documents = Boolean(readiness?.ready || readiness?.documents > 0)
    }, failed),
  ]
  // One that fails is a step not yet done, never a reason to hold the rest up.
  const recommended = [
    api.getAgentConfig().then((config) => {
      rest.hours = Object.keys(config?.agent_config?.business_hours || {}).length > 0
    }),
    api.getCalendarSubscription().then((calendar) => {
      rest.calendar = Boolean(calendar?.active)
    }),
    api.notificationSettings().then((alerts) => {
      rest.alerts = Boolean(alerts?.email || alerts?.devices > 0 || alerts?.subscribed)
    }),
  ].map((check) => check.catch(() => {}))

  const snapshot = (pending) => {
    const zone = (org?.timezone || '').trim()
    const done = {
      // The server fills in "You are a helpful sales agent." for a business
      // that never wrote its own, so a line that short is not an answer.
      business: Boolean((org?.sales_prompt || '').trim().length > 40),
      knowledge: rules || documents,
      // Created businesses start on UTC. Nobody chose that, so it does not count.
      timezone: Boolean(zone) && zone !== 'UTC',
      whatsapp: whatsapp.connected,
      ...rest,
    }
    return {
      done,
      gate: { ...done, whatsapp: whatsapp.ever },
      org,
      hasOrg: hasOrg && Boolean(org),
      error,
      pending,
    }
  }

  await Promise.all(required)
  // With no business every other call is a 409 too; there is nothing to wait for.
  if (!hasOrg) {
    const state = snapshot(false)
    onRequired?.(state)
    return state
  }
  onRequired?.(snapshot(true))
  await Promise.all(recommended)
  return snapshot(false)
}

/**
 * Fold a fresh answer into the one on screen.
 *
 * A `pending` answer has not heard back about the recommended steps yet, so it
 * keeps what was already known about them for the same business rather than
 * flickering every badge to "not done" for the length of a request.
 */
export function mergeSetup(previous, next) {
  if (!next) return previous
  if (!next.pending || !previous || previous.org?.id !== next.org?.id) return next
  const kept = Object.fromEntries(RECOMMENDED_KEYS.map((key) => [key, previous.done[key]]))
  return {
    ...next,
    done: { ...next.done, ...kept },
    gate: { ...next.gate, ...kept },
    pending: previous.pending,
  }
}

/** Whether this answer can be acted on: it came back, and it is not a failure. */
export function setupKnown(state) {
  return Boolean(state) && !state.error
}

/** The required steps still open, in order. */
export function requiredLeft(state) {
  if (!setupKnown(state)) return []
  if (!state.hasOrg) return STEPS.filter((s) => s.tier === 'required')
  return STEPS.filter((s) => s.tier === 'required' && !state.gate[s.key])
}

export function recommendedLeft(state) {
  if (!setupKnown(state) || !state.hasOrg || state.pending) return []
  return STEPS.filter((s) => s.tier === 'recommended' && !state.done[s.key])
}

/**
 * Which pages need which steps.
 *
 * Testing the agent needs to know what it sells, but not a phone: trying it
 * before a customer does is exactly what the sandbox is for, and nothing is
 * sent from it. Everything else waits for the whole required set.
 */
const NEEDS = {
  inbox: REQUIRED_STEPS,
  board: REQUIRED_STEPS,
  analytics: REQUIRED_STEPS,
  test: ['business', 'knowledge'],
  setup: [],
}

/** The steps still standing between this business and a page. */
export function missingFor(view, state) {
  const needs = NEEDS[view] || []
  if (needs.length === 0) return []
  if (!setupKnown(state)) return []
  if (!state.hasOrg) return STEPS.filter((s) => needs.includes(s.key))
  return STEPS.filter((s) => needs.includes(s.key) && !state.gate[s.key])
}
