import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Inbox, LogOut, Sparkles } from 'lucide-react'
import useMonitorSocket from './useMonitorSocket.js'
import { api, auth } from './api.js'
import SignIn from './components/SignIn.jsx'
import ConversationList from './components/ConversationList.jsx'
import ConversationThread from './components/ConversationThread.jsx'
import PipelineBoard from './components/PipelineBoard.jsx'
import MetricStrip from './components/MetricStrip.jsx'
import OrgSelector from './components/OrgSelector.jsx'
import PulseLine from './components/PulseLine.jsx'
import BrandMark from './components/BrandMark.jsx'
import WhatsNew, { hasUnseenUpgrades } from './components/WhatsNew.jsx'
import Prospects from './components/Prospects.jsx'

/**
 * Pane switcher, phones only.
 *
 * Hidden from `lg` up, where all three panes are on screen at once and a
 * switcher would be a control that does nothing. "Conversation" only appears
 * once there is one open, so it is never a tab leading to an empty panel.
 */
function PaneTabs({ pane, onPick, waiting }) {
  const tabs = [
    { id: 'list', label: 'Chats', count: waiting },
    ...(pane === 'thread' ? [{ id: 'thread', label: 'Conversation' }] : []),
    { id: 'pipeline', label: 'Pipeline' },
  ]

  return (
    <div className="flex gap-1 rounded-xl border border-edge bg-panel p-1 lg:hidden">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          onClick={() => onPick(tab.id)}
          aria-current={pane === tab.id ? 'page' : undefined}
          className={`flex flex-1 items-center justify-center gap-1.5 rounded-lg px-3 py-2 text-[12px] font-semibold transition-colors ${
            pane === tab.id
              ? 'bg-accent/12 text-accent ring-1 ring-inset ring-accent/25'
              : 'text-dim hover:text-ink'
          }`}
        >
          {tab.label}
          {tab.count ? (
            <span className="font-mono text-2xs text-faint">{tab.count}</span>
          ) : null}
        </button>
      ))}
    </div>
  )
}

export default function App() {
  const [signedIn, setSignedIn] = useState(Boolean(auth.token))

  if (!signedIn) return <SignIn onSignedIn={() => setSignedIn(true)} />
  return <Dashboard onSignedOut={() => setSignedIn(false)} />
}

function Dashboard({ onSignedOut }) {
  const { connected, events, generation } = useMonitorSocket()

  const [organizations, setOrganizations] = useState([])
  const [selectedOrg, setSelectedOrg] = useState(null)
  const [contacts, setContacts] = useState([])
  const [selectedContact, setSelectedContact] = useState(null)
  const [threads, setThreads] = useState({})
  const [composing, setComposing] = useState(new Set())
  const [stats, setStats] = useState(null)
  /**
   * Which pane a phone is showing. Three panes side by side is the right
   * layout on a desktop and impossible on a 390px screen, so below `lg` they
   * become one at a time — list, the conversation, or the pipeline. Above it
   * the classes below are overridden and this is ignored entirely, so there is
   * no second layout to keep in step.
   */
  const [mobilePane, setMobilePane] = useState('list')
  const [showUpgrades, setShowUpgrades] = useState(false)
  const [showProspects, setShowProspects] = useState(false)
  // Only shown once there is something to act on — a button offering an empty
  // list is a button that teaches people to ignore it.
  const [waiting, setWaiting] = useState(0)
  // Only until they open it once. A dot that never goes away is noise.
  const [unseen, setUnseen] = useState(hasUnseenUpgrades)

  const seenEvents = useRef(0)

  const loadOrganizations = useCallback(async () => {
    try {
      const memberships = await api.listOrganizations()
      setOrganizations(memberships.map((m) => m.organization))
      const active = memberships.find((m) => m.is_active)
      setSelectedOrg(active ? active.organization.id : memberships[0]?.organization.id ?? null)
    } catch (err) {
      if (err.status === 401) onSignedOut()
      setOrganizations([])
    }
  }, [onSignedOut])

  const loadContacts = useCallback(async () => {
    try {
      // Scoped server-side to the active organization — no id is sent.
      const rows = await api.listContacts()
      setContacts(rows)
      // Keep the open conversation, but only if it still exists — replayed
      // history can point at a contact that has since been deleted, which
      // would otherwise leave the thread pane stuck on "coming in".
      setSelectedContact((current) =>
        rows.some((row) => row.id === current) ? current : rows[0]?.id ?? null
      )
    } catch (err) {
      if (err.status === 401) onSignedOut()
      setContacts([])
    }
  }, [selectedOrg, onSignedOut])

  const loadStats = useCallback(async () => {
    try {
      setStats(await api.stats())
    } catch {
      setStats(null)
    }
  }, [])

  useEffect(() => {
    loadOrganizations()
    loadStats()
  }, [loadOrganizations, loadStats])

  const countWaiting = useCallback(async () => {
    try {
      const found = await api.listProspects(30)
      setWaiting(found.available ? found.prospects.length : 0)
    } catch {
      setWaiting(0)
    }
  }, [])

  useEffect(() => {
    countWaiting()
  }, [countWaiting, selectedOrg])

  useEffect(() => {
    loadContacts()
  }, [loadContacts])


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

  // Pull a thread the first time its conversation is opened.
  useEffect(() => {
    if (!selectedContact || threads[selectedContact]) return
    let cancelled = false
    api
      .contactMessages(selectedContact)
      .then((messages) => {
        if (!cancelled) mergeMessages(selectedContact, messages)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [selectedContact, threads, mergeMessages])

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
  const firstConnection = useRef(true)
  useEffect(() => {
    if (generation === 0) return
    if (firstConnection.current) {
      // The mount effects above have already loaded this.
      firstConnection.current = false
      return
    }
    loadOrganizations()
    loadContacts()
    loadStats()
    refreshOpenThread()
  }, [generation, loadOrganizations, loadContacts, loadStats, refreshOpenThread])

  // Live traffic drives the whole screen: new bubbles, typing state, stages.
  useEffect(() => {
    const fresh = events.slice(seenEvents.current)
    if (fresh.length === 0) return
    seenEvents.current = events.length

    for (const event of fresh) {
      const data = event.data || {}
      const contactId = data.contact_id

      if (event.type === 'inbound_message' && contactId) {
        appendMessage(contactId, {
          id: data.message_id || `in-${event.id}`,
          sender: 'user',
          content: data.content,
          media_urls: data.media_urls || [],
          created_at: event.timestamp,
        })
        setSelectedContact(contactId)
        if (data.new_contact) loadContacts()
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
        setContacts((prev) =>
          prev.map((c) => (c.id === contactId ? { ...c, pipeline_stage: data.to } : c))
        )
      }

      // Emitted after the write is committed — the only point at which a
      // re-read is guaranteed to include this conversation.
      if (event.type === 'sync') {
        loadContacts()
        loadStats()
        // A drained outbox changes the delivery mark on messages already on
        // screen, so the open thread is re-read rather than appended to.
        if (data.outbox) refreshOpenThread()
      }
    }
  }, [events, appendMessage, loadContacts, loadStats, refreshOpenThread])

  const previews = useMemo(() => {
    const map = {}
    for (const [contactId, messages] of Object.entries(threads)) {
      const last = messages[messages.length - 1]
      if (last) map[contactId] = last.content
    }
    return map
  }, [threads])

  const activeContact = contacts.find((c) => c.id === selectedContact) || null

  return (
    <div className="relative flex h-full flex-col gap-2 p-2 sm:gap-3 sm:p-3">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-edge bg-panel px-3 py-2.5 sm:gap-x-4 sm:px-4 sm:py-3">
        <BrandMark size={34} />

        <div className="hidden items-center gap-3 border-l border-edge pl-4 md:flex">
          <PulseLine beat={events.length} />
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-3">
          <OrgSelector
            organizations={organizations}
            selectedId={selectedOrg}
            onSelect={async (id) => {
              if (!id || id === selectedOrg) return
              await api.switchOrganization(id)
              setSelectedOrg(id)
              setSelectedContact(null)
              setThreads({})
              await loadOrganizations()
              await loadContacts()
              await loadStats()
            }}
            onSaved={async (saved) => {
              await loadOrganizations()
              if (saved?.id) {
                setSelectedContact(null)
                setThreads({})
                await loadContacts()
              }
            }}
          />

          <span
            className={`flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-semibold ${
              connected
                ? 'border-accent/25 bg-accent/10 text-accent'
                : 'border-warn/25 bg-warn/10 text-warn'
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                connected ? 'animate-breathe bg-accent' : 'bg-warn'
              }`}
            />
            {connected ? 'Live' : 'Reconnecting'}
          </span>

          {waiting > 0 && (
            <button
              onClick={() => setShowProspects(true)}
              title="People who never got a reply"
              className="flex items-center gap-1.5 rounded-full border border-warn/25 bg-warn/10 px-3 py-1.5 text-[11px] font-semibold text-warn transition-colors hover:bg-warn/15"
            >
              <Inbox size={12} />
              {waiting} waiting
            </button>
          )}

          <button
            onClick={() => {
              setShowUpgrades(true)
              setUnseen(false)
            }}
            title="What's new"
            className="relative rounded-lg border border-edge p-1.5 text-dim transition-colors hover:border-edge-hi hover:text-ink"
          >
            <Sparkles size={13} />
            {unseen && (
              <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-accent ring-2 ring-panel" />
            )}
          </button>

          <button
            onClick={() => {
              auth.clear()
              onSignedOut()
            }}
            title="Sign out"
            className="rounded-lg border border-edge p-1.5 text-faint transition-colors hover:border-edge-hi hover:text-ink"
          >
            <LogOut size={13} />
          </button>
        </div>
      </header>

      {/* A phone reading a conversation should spend its height on the
          conversation. The numbers stay one tap away under Chats, and on a
          desktop nothing moves. */}
      <div className={mobilePane === 'thread' ? 'hidden lg:block' : ''}>
        <MetricStrip stats={stats} contacts={contacts} />
      </div>

      <PaneTabs pane={mobilePane} onPick={setMobilePane} waiting={contacts.length} />

      <main className="flex min-h-0 flex-1 gap-2 sm:gap-3">
        <ConversationList
          className={`${mobilePane === 'list' ? 'flex' : 'hidden'} w-full lg:flex lg:w-[280px]`}
          contacts={contacts}
          selectedId={selectedContact}
          onSelect={(id) => {
            setSelectedContact(id)
            setMobilePane('thread')
          }}
          previews={previews}
          composing={composing}
        />
        <ConversationThread
          className={`${mobilePane === 'thread' ? 'flex' : 'hidden'} lg:flex`}
          onBack={() => setMobilePane('list')}
          contact={activeContact}
          messages={threads[selectedContact] || []}
          composing={composing.has(selectedContact)}
          // A conversation can be selected by a live event a moment before the
          // contact list catches up — that is arriving, not idle.
          arriving={Boolean(selectedContact) && !activeContact}
          // Scheduling or cancelling a follow-up is stored on the contact, so
          // the list has to be re-read for the panel to show what it now says.
          onChanged={loadContacts}
        />
        <PipelineBoard
          className={`${mobilePane === 'pipeline' ? 'flex' : 'hidden'} w-full lg:flex lg:w-[290px]`}
          contacts={contacts}
          selectedId={selectedContact}
          onSelect={(id) => {
            setSelectedContact(id)
            setMobilePane('thread')
          }}
        />
      </main>

      {showUpgrades && <WhatsNew onClose={() => setShowUpgrades(false)} />}
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
