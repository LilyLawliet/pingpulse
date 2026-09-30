import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BellOff,
  CalendarDays,
  ShoppingBag,
  ChartNoAxesColumn,
  ChevronRight,
  CircleAlert,
  Columns3,
  FlaskConical,
  Inbox,
  Loader2,
  Lock,
  LogOut,
  Menu,
  MessagesSquare,
  Monitor,
  Moon,
  Settings,
  Sparkles,
  Sun,
  X,
} from 'lucide-react'
import useMonitorSocket from './useMonitorSocket.js'
import { api, auth, setCurrentBusiness } from './api.js'
import { subscribeQuietly } from './alerts.js'
import { DEFAULT_STAGES } from './format.js'
import { useTheme } from './theme.js'
import SignIn from './components/SignIn.jsx'
import ConversationList from './components/ConversationList.jsx'
import ConversationThread from './components/ConversationThread.jsx'
import MetricStrip, { MetricWindow } from './components/MetricStrip.jsx'
import OrgSelector from './components/OrgSelector.jsx'
import SettingsPage from './components/SettingsPage.jsx'
import { LockedPage, SetupWelcome } from './components/SetupGate.jsx'
import {
  mergeSetup,
  missingFor,
  readSetup,
  recommendedLeft,
  requiredLeft,
  setupKnown,
} from './setup.js'
import PulseLine from './components/PulseLine.jsx'
import BrandMark from './components/BrandMark.jsx'
import WhatsNew, { hasUnseenUpgrades } from './components/WhatsNew.jsx'
import Prospects from './components/Prospects.jsx'
import LeadProfileDrawer from './components/LeadProfileDrawer.jsx'
import SetupChecklist from './components/SetupChecklist.jsx'
import ConnectionStatus from './components/ConnectionStatus.jsx'
import AgentSandbox from './components/AgentSandbox.jsx'
import Analytics from './components/Analytics.jsx'
import KanbanBoard from './components/KanbanBoard.jsx'
import Calendar from './components/Calendar.jsx'
import Orders from './components/Orders.jsx'
import { PageBoundary } from './components/ui.jsx'
import Profile, { ProfileButton } from './components/Profile.jsx'

/**
 * The places you can go. One list drives the sidebar on a desktop and the tab
 * bar on a phone, so the two can never disagree about what exists.
 *
 * These used to be five full-screen overlays opened from a row of a dozen
 * buttons in the header, which gave no sense of where you were or how to get
 * back. They are pages now, and the one you are on is always highlighted.
 */
const VIEWS = [
  { id: 'inbox', label: 'Inbox', short: 'Inbox', icon: MessagesSquare },
  { id: 'board', label: 'Board', short: 'Board', icon: Columns3 },
  { id: 'orders', label: 'Orders', short: 'Orders', icon: ShoppingBag },
  { id: 'calendar', label: 'Calendar', short: 'Calendar', icon: CalendarDays },
  { id: 'analytics', label: 'Analytics', short: 'Stats', icon: ChartNoAxesColumn },
  { id: 'test', label: 'Test agent', short: 'Test', icon: FlaskConical },
  { id: 'setup', label: 'Setup', short: 'Setup', icon: Settings },
]

const EMPTY_FILTERS = { search: '', stage: '', unread_only: false, taken_over: false }

/** Is anything narrowing the list right now? */
function filtering(filters) {
  return Boolean(
    filters.search || filters.stage || filters.unread_only || filters.taken_over,
  )
}

/** Light, dark, or whatever the device says. */
function ThemeSwitch() {
  const { choice, pick } = useTheme()
  const options = [
    ['light', Sun, 'Light'],
    ['dark', Moon, 'Dark'],
    ['system', Monitor, 'Match device'],
  ]
  return (
    <div className="seg w-full" role="group" aria-label="Theme">
      {options.map(([key, Icon, label]) => (
        <button
          key={key}
          type="button"
          title={label}
          aria-label={label}
          aria-pressed={choice === key}
          onClick={() => pick(key)}
          className="seg-item flex flex-1 justify-center py-1.5"
        >
          <Icon size={14} />
        </button>
      ))}
    </div>
  )
}

/**
 * Everything that needs a person, in one place.
 *
 * These were four coloured pills in the header - "2 waiting", "Alerts off",
 * "2 alerts missed" and a connection state - each the same size and shape as
 * every navigation button beside them, so none of them read as urgent. They
 * are rows now, each one saying what is wrong and taking you to the fix, and
 * the block is absent entirely when nothing is.
 */
function Attention({
  waiting,
  alertsReach,
  alertsLost,
  whatsappDropped,
  onProspects,
  onAlerts,
  onWhatsApp,
}) {
  const items = []
  // First, because it is the worst: set up and working before, and now
  // nothing is being answered at all.
  if (whatsappDropped) {
    items.push({
      key: 'whatsapp',
      icon: CircleAlert,
      tone: 'text-crit bg-crit/10',
      title: 'WhatsApp is offline',
      body: 'Nobody is being answered. Reconnect it.',
      onClick: onWhatsApp,
    })
  }
  if (alertsLost > 0) {
    items.push({
      key: 'lost',
      icon: CircleAlert,
      tone: 'text-crit bg-crit/10',
      title: `${alertsLost} alert${alertsLost === 1 ? '' : 's'} missed`,
      body: 'They could not be delivered. See why.',
      onClick: onAlerts,
    })
  }
  if (waiting > 0) {
    items.push({
      key: 'waiting',
      icon: Inbox,
      tone: 'text-warn bg-warn/10',
      title: `${waiting} never answered`,
      body: 'Messaged you and got no reply',
      onClick: onProspects,
    })
  }
  if (alertsReach === false) {
    items.push({
      key: 'off',
      icon: BellOff,
      tone: 'text-warn bg-warn/10',
      title: 'Alerts are off',
      body: 'Nothing reaches you while this is closed',
      onClick: onAlerts,
    })
  }
  if (items.length === 0) return null

  return (
    <div className="space-y-1">
      <p className="eyebrow px-3 pb-1">Needs attention</p>
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          onClick={item.onClick}
          className="group flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left transition-colors hover:bg-panel-2"
        >
          <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${item.tone}`}>
            <item.icon size={15} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-medium text-ink">{item.title}</span>
            <span className="block text-2xs leading-snug text-faint">{item.body}</span>
          </span>
          <ChevronRight size={14} className="shrink-0 text-faint group-hover:text-dim" />
        </button>
      ))}
    </div>
  )
}

/** The sidebar: who you are, where you can go, and what needs you. */
function Sidebar({
  view,
  onView,
  setupLeft,
  suggestedLeft,
  locked,
  connected,
  beat,
  unseen,
  onWhatsNew,
  onSignOut,
  org,
  attention,
  onClose,
  statusKey,
  profile,
}) {
  return (
    <div className="flex h-full min-h-0 flex-col gap-5 overflow-y-auto p-3">
      <div className="flex items-center justify-between gap-2 px-2 pt-2">
        <BrandMark size={34} />
        {onClose && (
          <button type="button" onClick={onClose} aria-label="Close menu" className="btn-ghost p-2">
            <X size={18} />
          </button>
        )}
      </div>

      {org}

      <nav className="space-y-0.5" aria-label="Main">
        {VIEWS.map((item) => {
          const current = view === item.id
          const closed = locked(item.id)
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onView(item.id)}
              aria-current={current ? 'page' : undefined}
              title={closed ? 'Opens once the required setup steps are done' : undefined}
              className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors ${
                current
                  ? 'bg-accent/10 text-ink'
                  : closed
                    ? 'text-faint hover:bg-panel-2'
                    : 'text-dim hover:bg-panel-2 hover:text-ink'
              }`}
            >
              <item.icon size={18} className={current ? 'text-accent' : 'text-faint'} />
              <span className="flex-1 text-left">{item.label}</span>
              {closed && <Lock size={13} className="shrink-0 text-faint" aria-label="locked" />}
              {item.id === 'setup' && setupLeft > 0 && (
                <span
                  className="rounded-full bg-warn/15 px-2 py-0.5 text-[11px] font-semibold text-warn"
                  title={`${setupLeft} required step${setupLeft === 1 ? '' : 's'} left`}
                >
                  {setupLeft} required
                </span>
              )}
              {item.id === 'setup' && setupLeft === 0 && suggestedLeft > 0 && (
                <span
                  className="rounded-full bg-panel-2 px-2 py-0.5 text-[11px] font-semibold text-dim"
                  title={`${suggestedLeft} recommended step${suggestedLeft === 1 ? '' : 's'} left`}
                >
                  {suggestedLeft} suggested
                </span>
              )}
            </button>
          )
        })}
      </nav>

      {attention}

      <div className="mt-auto space-y-3">
        <div className="space-y-2 rounded-2xl border border-edge bg-panel-2/50 p-3">
          <div className="flex items-center gap-2">
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${
                connected ? 'animate-breathe bg-accent' : 'bg-warn'
              }`}
            />
            <span className={`text-xs font-semibold ${connected ? 'text-ink' : 'text-warn'}`}>
              {connected ? 'Live' : 'Reconnecting…'}
            </span>
            <span className="ml-auto">
              <PulseLine beat={beat} width={84} height={22} />
            </span>
          </div>
          <ConnectionStatus placement="up" key={statusKey} />
        </div>

        {profile}

        <ThemeSwitch />

        <div className="flex items-center gap-1">
          <button type="button" onClick={onWhatsNew} className="btn-ghost relative flex-1 justify-start">
            <Sparkles size={15} />
            What&rsquo;s new
            {unseen && <span className="ml-auto h-2 w-2 rounded-full bg-accent" />}
          </button>
          <button
            type="button"
            onClick={onSignOut}
            className="btn-ghost"
            title="Sign out"
            aria-label="Sign out"
          >
            <LogOut size={15} />
          </button>
        </div>
      </div>
    </div>
  )
}

/** Shown in place of a gated page while the server is asked about setup. */
function CheckingSetup({ failed, onRetry }) {
  return (
    <div className="grid h-full place-items-center p-6">
      <div className="flex max-w-sm flex-col items-center text-center">
        {failed ? (
          <>
            <p className="text-sm font-semibold text-ink">Could not reach PingPulse</p>
            <p className="mt-1 text-sm text-dim">
              Your setup could not be checked. Trying again in a few seconds.
            </p>
            <button type="button" onClick={onRetry} className="btn-secondary mt-4">
              Try now
            </button>
          </>
        ) : (
          <>
            <Loader2 size={20} className="animate-spin text-faint" />
            <p className="mt-3 text-sm text-dim">Checking your setup…</p>
          </>
        )}
      </div>
    </div>
  )
}

export default function App() {
  const [signedIn, setSignedIn] = useState(Boolean(auth.token))

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />
  return <Dashboard onSignedOut={() => setSignedIn(false)} />
}

function Dashboard({ onSignedOut }) {
  const [organizations, setOrganizations] = useState([])
  const [selectedOrg, setSelectedOrg] = useState(null)
  // The live feed is this business's alone: the server sends a socket only
  // the events of the business it asked for, and a switch opens a new one.
  const { connected, events, generation } = useMonitorSocket(selectedOrg)
  const selectedOrgRef = useRef(null)
  selectedOrgRef.current = selectedOrg
  // Every request from this tab names this tab's business - set during
  // render, so it is in place before any page's effects ask for anything.
  setCurrentBusiness(selectedOrg)
  const [contacts, setContacts] = useState([])
  const [allContacts, setAllContacts] = useState([])
  const contactsRef = useRef([])
  contactsRef.current = contacts
  // Bumped when the socket says the WhatsApp connection changed, so the
  // connection light re-reads at once instead of on its next minute.
  const [waStatusTick, setWaStatusTick] = useState(0)
  const [selectedContact, setSelectedContact] = useState(null)
  const [threads, setThreads] = useState({})
  const [composing, setComposing] = useState(new Set())
  const [stats, setStats] = useState(null)
  // This organization's own board. Seeded from the shared defaults so the
  // columns never flicker between two different sets while the call is in
  // flight, and replaced by whatever the server says belongs to this tenant.
  const [stages, setStages] = useState(DEFAULT_STAGES)
  const [filters, setFilters] = useState(EMPTY_FILTERS)
  const [showDrawer, setShowDrawer] = useState(false)
  const [window_, setWindow_] = useState('all')
  // Which page is showing. Pages rather than overlays, so there is always a
  // "you are here" and nothing to close to get back.
  const [view, setView] = useState('inbox')
  // Setup remounts on this, so "open alert settings" lands on that step even
  // when Setup is already the page on screen.
  const [setupStep, setSetupStep] = useState({ key: 'business', n: 0 })
  // What the server says about setup. Null until the first answer, so
  // nothing locks or welcomes on a guess while it is still loading.
  const [setup, setSetup] = useState(null)
  const [orgsLoaded, setOrgsLoaded] = useState(false)
  // The welcome is shown on every visit until the agent can run. Once a
  // visit has shown it, it stays closed for the rest of that visit.
  const [welcomed, setWelcomed] = useState(false)
  const [showWelcome, setShowWelcome] = useState(false)
  // Who the token was issued to. The dashboard never asked before, so the
  // only place a client could see their own name was the email with the token.
  const [session, setSession] = useState(null)
  const [showProfile, setShowProfile] = useState(false)
  /**
   * Which pane a phone is showing in the inbox: the list, or one
   * conversation. From `lg` up both are on screen and this is ignored.
   */
  const [mobilePane, setMobilePane] = useState('list')
  const [menuOpen, setMenuOpen] = useState(false)
  const [showUpgrades, setShowUpgrades] = useState(false)
  const [showProspects, setShowProspects] = useState(false)
  // Null until we know. Shown only once we are sure nothing can reach
  // them, so the warning never flashes up during a normal load.
  const [alertsReach, setAlertsReach] = useState(null)
  // Alerts that ran out of chances without reaching anybody. Until now this
  // was a row in a table with nothing on any screen, so "I was never told"
  // and "it was never delivered" were the same thing seen from here.
  const [alertsLost, setAlertsLost] = useState(0)
  // Only shown once there is something to act on — a button offering an empty
  // list is a button that teaches people to ignore it.
  const [waiting, setWaiting] = useState(0)
  // Only until they open it once. A dot that never goes away is noise.
  const [unseen, setUnseen] = useState(hasUnseenUpgrades)

  const seenEvents = useRef(0)

  const loadOrganizations = useCallback(async (prefer = null) => {
    try {
      const memberships = await api.listOrganizations()
      setOrganizations(memberships.map((m) => m.organization))
      const ids = memberships.map((m) => m.organization.id)
      const active = memberships.find((m) => m.is_active)
      // This tab stays on the business it is showing while it is still a
      // member: another tab switching must not move this one.
      setSelectedOrg((current) => {
        if (prefer && ids.includes(prefer)) return prefer
        if (current && ids.includes(current)) return current
        return active ? active.organization.id : ids[0] ?? null
      })
    } catch (err) {
      if (err.status === 401) onSignedOut()
      setOrganizations([])
    }
    setOrgsLoaded(true)
  }, [onSignedOut])

  // Only the newest answer to each of these may land. A search typed quickly,
  // or a switch of business, leaves older requests still on their way, and
  // the one that arrives last used to win - showing the wrong results, or
  // the last business's conversations under this one's name.
  const contactsSeq = useRef(0)
  const statsSeq = useRef(0)
  const loadContacts = useCallback(async () => {
    contactsSeq.current += 1
    const mine = contactsSeq.current
    try {
      // Scoped server-side to the active organization — no id is sent. The
      // filters go to the server rather than narrowing a list already fetched,
      // which would only ever search the most recent hundred.
      const narrowed = filtering(filters)
      // The board, the calendar and the counts are about everybody, not about
      // the inbox's search: with a filter on, the whole list is read as well.
      const [rows, everyone] = await Promise.all([
        api.listContacts(filters),
        narrowed ? api.listContacts({}) : null,
      ])
      if (mine !== contactsSeq.current) return
      setContacts(rows)
      setAllContacts(everyone || rows)
      // Keep the open conversation, but only if it still exists — replayed
      // history can point at a contact that has since been deleted, which
      // would otherwise leave the thread pane stuck on "coming in".
      setSelectedContact((current) =>
        rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null
      )
    } catch (err) {
      if (mine !== contactsSeq.current) return
      if (err.status === 401) onSignedOut()
      setContacts([])
      setAllContacts([])
    }
  }, [selectedOrg, onSignedOut, filters])

  const loadStats = useCallback(async () => {
    statsSeq.current += 1
    const mine = statsSeq.current
    try {
      const found = await api.stats(window_)
      if (mine === statsSeq.current) setStats(found)
    } catch {
      if (mine === statsSeq.current) setStats(null)
    }
  }, [window_, selectedOrg])

  const pipelineSeq = useRef(0)
  const loadPipeline = useCallback(async () => {
    pipelineSeq.current += 1
    const mine = pipelineSeq.current
    try {
      const board = await api.getPipeline()
      if (mine === pipelineSeq.current && board?.stages?.length) setStages(board.stages)
    } catch {
      // The defaults are already on screen and are what the server falls back
      // to as well, so a failed read changes nothing a person would notice.
    }
  }, [])

  useEffect(() => {
    loadOrganizations()
  }, [loadOrganizations])

  useEffect(() => {
    loadStats()
  }, [loadStats])

  // Each business has its own board. It was read once, when the dashboard
  // opened, so every other business showed the first one's columns.
  useEffect(() => {
    setStages(DEFAULT_STAGES)
    loadPipeline()
  }, [selectedOrg, loadPipeline])

  /**
   * Keep this browser subscribed, without ever asking.
   *
   * No permission prompt happens here and none can: every browser refuses to
   * ask without a click, and an unprompted request is answered with a block
   * that then sticks. What this does is resubscribe a browser that has
   * already said yes — after a new tab, a cleared worker, or a subscription
   * the push service rotated — so one click years ago keeps working.
   *
   * It also settles whether anything can currently reach this shop at all,
   * which is what the warning in the header is for.
   */
  const alertsSeq = useRef(0)
  const checkAlerts = useCallback(async () => {
    alertsSeq.current += 1
    const mine = alertsSeq.current
    try {
      const settings = await api.notificationSettings()
      await subscribeQuietly(settings)
      const after = await api.notificationSettings()
      if (mine !== alertsSeq.current) return
      setAlertsReach(after.devices > 0 || Boolean(after.email))
      setAlertsLost(after.undelivered || 0)
    } catch {
      if (mine !== alertsSeq.current) return
      // Never a visible failure. An older deployment has no such endpoint,
      // and a dashboard that will not load because alerts could not be
      // checked would be a poor trade.
      setAlertsReach(null)
      setAlertsLost(0)
    }
  }, [])

  useEffect(() => {
    checkAlerts()
  }, [checkAlerts, selectedOrg])

  const waitingSeq = useRef(0)
  const countWaiting = useCallback(async () => {
    waitingSeq.current += 1
    const mine = waitingSeq.current
    try {
      const found = await api.listProspects(30)
      if (mine === waitingSeq.current) setWaiting(found.available ? found.prospects.length : 0)
    } catch {
      if (mine === waitingSeq.current) setWaiting(0)
    }
  }, [])

  useEffect(() => {
    countWaiting()
  }, [countWaiting, selectedOrg])

  const onSetupProgress = useCallback((state) => {
    setSetup((previous) => mergeSetup(previous, state))
  }, [])

  // Only the newest check may change what is on screen. A slow answer about
  // the business somebody just switched away from must not land on top of the
  // one they switched to.
  const checkSeq = useRef(0)

  // What Setup reports - a save it has just applied, or a read it has just
  // made - is newer than anything this dashboard asked for before it, so any
  // such answer still on its way is dropped rather than allowed to undo it.
  const onSetupReport = useCallback(
    (state) => {
      // A report about a business this dashboard has since left is not news
      // about the one on screen.
      const about = state?.org?.id
      if (about && selectedOrgRef.current && about !== selectedOrgRef.current) return
      checkSeq.current += 1
      onSetupProgress(state)
    },
    [onSetupProgress],
  )

  const checkSetup = useCallback(() => {
    checkSeq.current += 1
    const mine = checkSeq.current
    const report = (state) => {
      if (mine === checkSeq.current) onSetupProgress(state)
    }
    return readSetup(report)
      .then(report)
      .catch(() => {})
  }, [onSetupProgress])

  // Asked the moment the dashboard opens, alongside the business list rather
  // than after it: the lock is decided by this answer, and every request
  // queued in front of it is time the wrong page could be on screen.
  useEffect(() => {
    checkSetup()
  }, [checkSetup])

  useEffect(() => {
    api
      .session()
      .then(setSession)
      .catch(() => {})
  }, [])

  // Asked again when the business changes. The first time selectedOrg is set
  // is the list arriving for the business already being checked, so that one
  // is skipped rather than asked twice.
  const checkedOrg = useRef(undefined)
  useEffect(() => {
    if (!orgsLoaded) return
    if (checkedOrg.current === undefined) {
      checkedOrg.current = selectedOrg
      return
    }
    if (checkedOrg.current === selectedOrg) return
    checkedOrg.current = selectedOrg
    // Forget the last business's answer first, so its unlocked pages are not
    // shown for the new one while the new one is being asked about.
    setSetup(null)
    checkSetup()
  }, [orgsLoaded, selectedOrg, checkSetup])

  // A failed check is not an answer. Ask again rather than lock, or unlock,
  // on it.
  useEffect(() => {
    if (!setup?.error) return
    const timer = setTimeout(checkSetup, 5000)
    return () => clearTimeout(timer)
  }, [setup, checkSetup])


  const setupLeft = requiredLeft(setup)
  const suggestedLeft = recommendedLeft(setup)
  const locked = useCallback((id) => missingFor(id, setup).length > 0, [setup])

  // Coming in to a business that cannot run yet: say so, and start them on
  // Setup at the first thing missing rather than on a locked inbox.
  useEffect(() => {
    if (!setupKnown(setup) || welcomed) return
    setWelcomed(true)
    const left = requiredLeft(setup)
    if (left.length === 0) return
    setShowWelcome(true)
    setSetupStep((was) => ({ key: left[0].key, n: was.n + 1 }))
    setView('setup')
  }, [setup, welcomed])

  const go = useCallback((next) => {
    setView(next)
    setMenuOpen(false)
    if (next === 'inbox') setMobilePane('list')
  }, [])

  const openSetup = useCallback((step = 'business') => {
    setSetupStep((was) => ({ key: step, n: was.n + 1 }))
    setView('setup')
    setMenuOpen(false)
  }, [])

  // Escape closes whatever is on top, one layer at a time.
  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== 'Escape') return
      if (showProfile) setShowProfile(false)
      else if (showWelcome) setShowWelcome(false)
      else if (showProspects) setShowProspects(false)
      else if (showUpgrades) setShowUpgrades(false)
      else if (showDrawer) setShowDrawer(false)
      else if (menuOpen) setMenuOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [showProfile, showWelcome, showProspects, showUpgrades, showDrawer, menuOpen])

  const openConversation = useCallback((id) => {
    setSelectedContact(id)
    setView('inbox')
    setMobilePane('thread')
  }, [])

  // Typing a search waits for a pause, rather than asking once per letter.
  useEffect(() => {
    const timer = setTimeout(loadContacts, filters.search ? 250 : 0)
    return () => clearTimeout(timer)
  }, [loadContacts, filters.search])


  /**
   * Merge rows into a thread by message id, keeping chronological order.
   * Live events and the REST fetch both carry the row's real primary key, so
   * the same message arriving twice collapses into one bubble.
   */
  const mergeMessages = useCallback((contactId, incoming) => {
    setThreads((prev) => {
      const byId = new Map()
      for (const message of [...(prev[contactId] || []), ...incoming]) {
        byId.set(message.id, { ...(byId.get(message.id) || {}), ...message })
      }
      const merged = [...byId.values()].sort(
        (a, b) => new Date(a.created_at) - new Date(b.created_at)
      )
      return { ...prev, [contactId]: merged }
    })
  }, [])

  // Pull a thread's history the first time its conversation is opened.
  //
  // "Opened before" is its own record, not "a thread exists": a live message
  // for a conversation nobody had opened created a one-message thread, and
  // opening it then skipped the history and showed that message alone.
  const fetchedThreads = useRef(new Set())
  useEffect(() => {
    if (!selectedContact || fetchedThreads.current.has(selectedContact)) return
    fetchedThreads.current.add(selectedContact)
    let cancelled = false
    const contactId = selectedContact
    api
      .contactMessages(contactId)
      .then((messages) => {
        if (!cancelled) mergeMessages(contactId, messages)
      })
      .catch(() => {
        fetchedThreads.current.delete(contactId)
      })
    return () => {
      cancelled = true
    }
  }, [selectedContact, mergeMessages])

  // Reading a conversation marks it read, so "Unread" means unread.
  useEffect(() => {
    if (!selectedContact || view !== 'inbox') return
    api.markRead(selectedContact).catch(() => {})
  }, [selectedContact, view])

  const appendMessage = useCallback(
    (contactId, message) => mergeMessages(contactId, [message]),
    [mergeMessages]
  )

  const selectedContactRef = useRef(null)
  selectedContactRef.current = selectedContact

  /** Re-read the open conversation from the API, replacing what we hold. */
  const refreshOpenThread = useCallback(async () => {
    const contactId = selectedContactRef.current
    if (!contactId) return
    try {
      const messages = await api.contactMessages(contactId)
      mergeMessages(contactId, messages)
    } catch {
      // A refresh that fails changes nothing on screen; the socket will
      // reconnect and ask again.
    }
  }, [mergeMessages])

  // The socket came back, so we were disconnected — for a deploy, a sleeping
  // laptop, a dropped network. Everything on screen is now as old as the gap,
  // including the delivery mark on a reply that has since gone out, so it is
  // re-read rather than left to look current.
  //
  // Only on a new connection. The loaders change identity with the search,
  // the business and the stats window, and listing them used to re-run all of
  // this on every keystroke in the search box.
  const handledGeneration = useRef(0)
  useEffect(() => {
    if (generation === 0 || generation === handledGeneration.current) return
    const first = handledGeneration.current === 0
    handledGeneration.current = generation
    // The mount effects above have already loaded this.
    if (first) return
    // Threads not open now may have missed messages in the gap too.
    fetchedThreads.current = new Set(selectedContactRef.current ? [selectedContactRef.current] : [])
    loadContacts()
    loadStats()
    refreshOpenThread()
    checkSetup()
  }, [generation, loadContacts, loadStats, refreshOpenThread, checkSetup])

  // Live traffic drives the whole screen: new bubbles, typing state, stages.
  //
  // Read by sequence number, not by position: the list is capped, and
  // counting by its length stopped every live update after the 300th event.
  useEffect(() => {
    const fresh = events.filter((event) => event.seq > seenEvents.current)
    if (fresh.length === 0) return
    seenEvents.current = fresh[fresh.length - 1].seq

    for (const event of fresh) {
      const data = event.data || {}
      const contactId = data.contact_id
      // Replayed on connecting: already in what the API returns, and not
      // something that has just happened.
      if (event.replay) continue
      // Another business's event has no place on this screen.
      if (data.organization_id && data.organization_id !== selectedOrgRef.current) continue

      if (event.type === 'inbound_message' && contactId) {
        appendMessage(contactId, {
          id: data.message_id || `in-${event.id}`,
          sender: 'user',
          content: data.content,
          media_urls: data.media_urls || [],
          created_at: event.timestamp,
        })
        // A new message does not take over the conversation somebody is
        // reading - it only opens one when nothing is open.
        if (!selectedContactRef.current) setSelectedContact(contactId)
        if (data.new_contact || !contactsRef.current.some((c) => c.id === contactId)) loadContacts()
      }

      if (event.type === 'ai_thinking' && contactId) {
        setComposing((prev) => new Set(prev).add(contactId))
      }

      if (event.type === 'outbound_message' && contactId) {
        setComposing((prev) => {
          const next = new Set(prev)
          next.delete(contactId)
          return next
        })
        appendMessage(contactId, {
          id: data.message_id || `out-${event.id}`,
          sender: 'agent',
          content: data.content,
          twilio_sid: data.twilio_sid,
          delivery_status: data.delivery_status,
          media_urls: data.media_urls || [],
          created_at: event.timestamp,
        })
        loadStats()
      }

      if (event.type === 'stage_change' && contactId) {
        const moved = (prev) =>
          prev.map((c) => (c.id === contactId ? { ...c, pipeline_stage: data.to } : c))
        setContacts(moved)
        setAllContacts(moved)
      }

      // Emitted after the write is committed — the only point at which a
      // re-read is guaranteed to include this conversation.
      if (event.type === 'sync') {
        loadContacts()
        loadStats()
        // A drained outbox changes the delivery mark on messages already on
        // screen, so the open thread is re-read rather than appended to.
        if (data.outbox) refreshOpenThread()
        // The WhatsApp connection changed: the sidebar, the lock and the
        // connection light all read it from Setup's answer.
        if (data.wa_session_status) {
          checkSetup()
          setWaStatusTick((n) => n + 1)
        }
      }
    }
  }, [events, appendMessage, loadContacts, loadStats, refreshOpenThread, checkSetup])

  const previews = useMemo(() => {
    const map = {}
    for (const [contactId, messages] of Object.entries(threads)) {
      const last = messages[messages.length - 1]
      if (last) map[contactId] = last.content
    }
    return map
  }, [threads])

  const activeContact = contacts.find((c) => c.id === selectedContact) || null

  /**
   * Forget everything that belongs to the business being left.
   *
   * The list, the numbers, the board, the open conversation, the drawer, the
   * filters: all of it was the last business's, and leaving any of it up -
   * even for the moment a request takes - showed one shop's customers under
   * another shop's name.
   */
  const leaveBusiness = useCallback(() => {
    checkSeq.current += 1
    contactsSeq.current += 1
    statsSeq.current += 1
    setSetup(null)
    setContacts([])
    setAllContacts([])
    setStats(null)
    setStages(DEFAULT_STAGES)
    setSelectedContact(null)
    setThreads({})
    fetchedThreads.current = new Set()
    setComposing(new Set())
    setShowDrawer(false)
    setMobilePane('list')
    setFilters(EMPTY_FILTERS)
  }, [])

  const [switchError, setSwitchError] = useState(null)
  const selectOrg = async (id) => {
    if (!id || id === selectedOrg) return
    setSwitchError(null)
    leaveBusiness()
    try {
      await api.switchOrganization(id)
      setCurrentBusiness(id)
      setSelectedOrg(id)
    } catch (err) {
      // Still on the business we were on: say so, and put its pages back
      // rather than leaving every page "checking" for good.
      setSwitchError(err?.message || 'Could not switch business. Try again.')
      checkSetup()
    }
    await loadOrganizations()
  }

  const signOut = () => {
    auth.clear()
    onSignedOut()
  }

  // Remounts the connection light when the business or its WhatsApp step
  // changes, so it never lags a minute behind what Setup just said.
  const statusKey = `${selectedOrg || 'none'}-${setup?.done?.whatsapp ? 1 : 0}-${waStatusTick}`
  const whatsappDropped = Boolean(setup?.hasOrg && setup.gate.whatsapp && !setup.done.whatsapp)
  const attentionCount =
    (waiting > 0 ? 1 : 0) +
    (alertsReach === false ? 1 : 0) +
    (alertsLost > 0 ? 1 : 0) +
    (whatsappDropped ? 1 : 0)

  const sidebar = (onClose) => (
    <Sidebar
      view={view}
      onView={go}
      setupLeft={setupLeft.length}
      suggestedLeft={suggestedLeft.length}
      locked={locked}
      statusKey={statusKey}
      profile={
        <ProfileButton
          session={session}
          onOpen={() => {
            setShowProfile(true)
            setMenuOpen(false)
          }}
        />
      }
      connected={connected}
      beat={events.length}
      unseen={unseen}
      onClose={onClose}
      onWhatsNew={() => {
        setShowUpgrades(true)
        setUnseen(false)
        setMenuOpen(false)
      }}
      onSignOut={signOut}
      org={
        <>
        {switchError && (
          <p role="alert" className="mb-2 rounded-lg bg-crit/10 px-2.5 py-1.5 text-2xs text-crit">
            {switchError}
          </p>
        )}
        <OrgSelector
          organizations={organizations}
          selectedId={selectedOrg}
          onSelect={selectOrg}
          emptyLabel={setupKnown(setup) && !setup.hasOrg ? 'No business yet' : 'Loading…'}
          onSaved={async (saved) => {
            // A business made with "+" becomes the active one: the last
            // one's list, numbers and board go with it.
            if (saved?.id && saved.id !== selectedOrgRef.current) leaveBusiness()
            await loadOrganizations(saved?.id || null)
          }}
        />
        </>
      }
      attention={
        <Attention
          waiting={waiting}
          alertsReach={alertsReach}
          alertsLost={alertsLost}
          whatsappDropped={whatsappDropped}
          onWhatsApp={() => openSetup('whatsapp')}
          onProspects={() => {
            setShowProspects(true)
            setMenuOpen(false)
          }}
          onAlerts={() => openSetup('alerts')}
        />
      }
    />
  )

  const inThread = view === 'inbox' && mobilePane === 'thread'
  const emptyInbox = contacts.length === 0 && !filtering(filters)
  const currentView = VIEWS.find((item) => item.id === view) || VIEWS[0]

  return (
    <div className="flex h-full min-h-0">
      {/* Desktop sidebar. */}
      <aside className="hidden w-[264px] shrink-0 border-r border-edge bg-panel lg:block">
        {sidebar(null)}
      </aside>

      {/* The same sidebar as a drawer on a phone or tablet. */}
      {menuOpen && (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <button
            type="button"
            aria-label="Close menu"
            className="scrim absolute inset-0"
            onClick={() => setMenuOpen(false)}
          />
          <aside className="animate-slide-left relative h-full w-[300px] max-w-[85vw] border-r border-edge bg-panel shadow-lift">
            {sidebar(() => setMenuOpen(false))}
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Phone top bar. Hidden inside a conversation, which has its own. */}
        <header
          className={`${inThread ? 'hidden' : 'flex'} shrink-0 items-center gap-2 border-b border-edge bg-panel px-3 py-2.5 lg:hidden`}
        >
          <button
            type="button"
            onClick={() => setMenuOpen(true)}
            aria-label="Open menu"
            className="btn-ghost relative p-2"
          >
            <Menu size={20} />
            {(attentionCount > 0 || unseen) && (
              <span
                className={`absolute right-1.5 top-1.5 h-2 w-2 rounded-full ring-2 ring-panel ${
                  attentionCount > 0 ? 'bg-warn' : 'bg-accent'
                }`}
              />
            )}
          </button>
          <h1 className="min-w-0 flex-1 truncate text-base font-semibold text-ink">
            {currentView.label}
          </h1>
          <span
            className={`h-2 w-2 shrink-0 rounded-full ${connected ? 'animate-breathe bg-accent' : 'bg-warn'}`}
            title={connected ? 'Live' : 'Reconnecting'}
          />
          <ConnectionStatus compact key={statusKey} />
        </header>

        <main className="min-h-0 flex-1">
          {/* Keyed by business as well as page: every page reads the active
              business when it mounts, so switching has to start it afresh.
              Without this, Setup kept the previous business's form on screen
              under the new one's name - and saving it would have written
              that business's details over this one's. */}
          <PageBoundary key={`${view}-${selectedOrg || 'none'}`}>
          {/* Nothing gated is drawn until the server has said whether it is
              allowed. Drawing the inbox first and locking it a moment later
              shows conversations to somebody who is meant to be finishing
              setup - and remembering the last answer to skip the wait would
              do the same on the visit after it stopped being true. */}
          {/* The same holds for the moment between the answer arriving and
              the welcome taking over: a locked page drawn for one render and
              then replaced is still a flash. */}
          {(!setupKnown(setup) || (!welcomed && requiredLeft(setup).length > 0)) &&
          view !== 'setup' ? (
            <CheckingSetup failed={Boolean(setup?.error)} onRetry={checkSetup} />
          ) : missingFor(view, setup).length > 0 ? (
            <LockedPage
              title={currentView.label}
              missing={missingFor(view, setup)}
              setup={setup}
              onStart={() => openSetup(missingFor(view, setup)[0].key)}
            />
          ) : (
          <>
          {view === 'inbox' && (
            <div className="flex h-full min-h-0 flex-col">
              <div
                className={`${inThread ? 'hidden lg:block' : 'block'} shrink-0 space-y-3 px-3 pt-3 sm:px-6 lg:space-y-4 lg:px-8 lg:pt-7`}
              >
                <div className="flex flex-wrap items-center gap-3">
                  <div className="hidden min-w-0 flex-1 lg:block">
                    <h2 className="text-xl font-semibold tracking-tight text-ink">Inbox</h2>
                    <p className="mt-0.5 text-sm text-dim">
                      Every WhatsApp conversation, answered as it arrives.
                    </p>
                  </div>
                  <MetricWindow value={window_} onChange={setWindow_} />
                </div>
                <MetricStrip stats={stats} contacts={allContacts} />
              </div>

              <div className="flex min-h-0 flex-1 gap-4 p-3 sm:px-6 lg:px-8 lg:pb-6 lg:pt-5">
                <ConversationList
                  className={`${mobilePane === 'list' && !emptyInbox ? 'flex' : 'hidden'} w-full lg:flex lg:w-[340px]`}
                  contacts={contacts}
                  filters={filters}
                  onFilters={setFilters}
                  selectedId={selectedContact}
                  onSelect={(id) => {
                    setSelectedContact(id)
                    setMobilePane('thread')
                  }}
                  previews={previews}
                  composing={composing}
                  stages={stages}
                />
                {emptyInbox ? (
                  <SetupChecklist
                    setup={setup}
                    offline={whatsappDropped}
                    onOpenStep={(key) => openSetup(key)}
                    onTest={() => go('test')}
                  />
                ) : (
                  <ConversationThread
                    className={`${mobilePane === 'thread' ? 'flex' : 'hidden'} lg:flex`}
                    onBack={() => setMobilePane('list')}
                    contact={activeContact}
                    messages={threads[selectedContact] || []}
                    composing={composing.has(selectedContact)}
                    // A conversation can be selected by a live event a moment
                    // before the contact list catches up - that is arriving,
                    // not idle.
                    arriving={Boolean(selectedContact) && !activeContact}
                    // A follow-up is stored on the contact, so the list is
                    // re-read for the panel to show what it now says.
                    onChanged={loadContacts}
                    stages={stages}
                    onOpenProfile={() => setShowDrawer(true)}
                  />
                )}
              </div>
            </div>
          )}

          {view === 'board' && (
            <KanbanBoard
              contacts={allContacts}
              stages={stages}
              onChanged={loadContacts}
              onOpen={openConversation}
            />
          )}
          {view === 'orders' && <Orders onOpenConversation={openConversation} />}
          {view === 'calendar' && (
            <Calendar
              contacts={allContacts}
              onOpenSetup={openSetup}
              onOpenConversation={openConversation}
            />
          )}
          {view === 'analytics' && <Analytics />}
          {view === 'test' && <AgentSandbox />}
          {view === 'setup' && (
            <SettingsPage
              key={setupStep.n}
              initialStep={setupStep.key}
              // Only an answer about this business. One about the business
              // just left, or the "no business yet" answer from before one
              // was made, filled this business's form with the wrong details.
              initialState={
                setupKnown(setup) && setup.hasOrg && setup.org?.id === selectedOrg ? setup : null
              }
              onPipelineChanged={loadPipeline}
              onAlertsChanged={checkAlerts}
              onSaved={async (saved) => {
                // A business created from Setup becomes the active one, and
                // everything shown for the old "no business" has to be re-read.
                await loadOrganizations(saved?.id || null)
                await loadContacts()
                await loadStats()
              }}
              onProgress={onSetupReport}
            />
          )}
          </>
          )}
          </PageBoundary>
        </main>

        {/* Phone tab bar. A conversation gets the whole screen. */}
        <nav
          aria-label="Main"
          className={`${inThread ? 'hidden' : 'grid'} shrink-0 grid-cols-7 border-t border-edge bg-panel pb-[env(safe-area-inset-bottom)] lg:hidden`}
        >
          {VIEWS.map((item) => {
            const current = view === item.id
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => go(item.id)}
                aria-current={current ? 'page' : undefined}
                className={`relative flex flex-col items-center gap-1 py-2 text-[11px] font-medium transition-colors ${
                  current ? 'text-accent' : 'text-faint hover:text-ink'
                }`}
              >
                <item.icon size={20} />
                <span className="flex items-center gap-0.5">
                  {locked(item.id) && <Lock size={9} aria-label="locked" />}
                  {item.short}
                </span>
                {item.id === 'setup' && setupLeft.length > 0 && (
                  <span className="absolute right-[calc(50%-16px)] top-1.5 h-2 w-2 rounded-full bg-warn ring-2 ring-panel" />
                )}
              </button>
            )
          })}
        </nav>
      </div>

      {showWelcome && (
        <SetupWelcome
          setup={setup}
          left={setupLeft}
          onClose={() => setShowWelcome(false)}
          onStart={() => {
            setShowWelcome(false)
            if (setupLeft[0]) openSetup(setupLeft[0].key)
          }}
        />
      )}
      {showProfile && (
        <Profile
          session={session}
          // The one the server accepted at sign-in, which is what is stored.
          token={session?.token || auth.token}
          onClose={() => setShowProfile(false)}
          onSignOut={signOut}
        />
      )}
      {showUpgrades && <WhatsNew onClose={() => setShowUpgrades(false)} />}
      {showDrawer && activeContact && (
        <LeadProfileDrawer
          contact={activeContact}
          stages={stages}
          onClose={() => setShowDrawer(false)}
          onSaved={loadContacts}
          onDeleted={(id) => {
            // Gone from the screen at once, then re-read: the server has
            // already removed them, so the list only confirms it.
            setShowDrawer(false)
            setMobilePane('list')
            setContacts((rows) => rows.filter((row) => row.id !== id))
            setThreads((all) => {
              const { [id]: _gone, ...rest } = all
              return rest
            })
            setSelectedContact((current) => (current === id ? null : current))
            loadContacts()
            loadStats()
          }}
        />
      )}
      {showProspects && (
        <Prospects
          onClose={() => {
            setShowProspects(false)
            countWaiting()
          }}
          onReplied={() => {
            loadContacts()
            countWaiting()
          }}
        />
      )}
    </div>
  )
}
