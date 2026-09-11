import { useEffect, useRef, useState } from 'react'
import {
  AlarmClock,
  Bot,
  CheckCheck,
  ChevronLeft,
  Clock,
  Sparkles,
  TriangleAlert,
  User,
  X,
} from 'lucide-react'
import { STAGE_LABEL, STAGE_STYLE, clockOf, contactLabel, initialsOf, isPlaceholderNumber, prettyPhone } from '../format.js'
import { mediaUrl } from '../backend.js'
import { api } from '../api.js'

/** Our own stored media is loaded from wherever the backend is.
 *
 * The absolute URL stored against a message is the one WhatsApp fetches. A
 * browser sent to that host can hit an interstitial and fail silently, so
 * anything under /media/ is reduced to a path first. `mediaUrl` then puts the
 * backend's origin back on when we are not same-origin with it — which is the
 * case in the desktop app, where a bare "/media/..." would resolve against the
 * shell instead of the server.
 */
function displayable(url) {
  const marker = '/media/'
  const at = url.indexOf(marker)
  const path = at !== -1 && !url.includes('cdn.') ? url.slice(at) : url
  return mediaUrl(path)
}

/** What happened to a reply we sent.
 *
 * Three outcomes, three marks. A single grey tick is WhatsApp's "sent, not
 * read yet", so it is exactly the wrong thing to show for a reply that never
 * left the building — and a reply waiting on a retry is not the same as one
 * that was lost, which is why "queued" says so rather than borrowing either
 * of the other two.
 */
function DeliveryMark({ message }) {
  // Older rows predate the status column; a stored sid still means delivered.
  const status = message.delivery_status || (message.twilio_sid ? 'SENT' : 'FAILED')

  if (status === 'SENT') return <CheckCheck size={11} className="text-accent" />

  if (status === 'QUEUED') {
    return (
      <span
        className="flex items-center gap-1 text-warn"
        title="WhatsApp was briefly unreachable. This is queued and will be sent automatically."
      >
        <Clock size={11} /> queued
      </span>
    )
  }

  return (
    <span
      className="flex items-center gap-1 text-crit"
      title="This reply was generated but WhatsApp did not accept it. The customer has not seen it."
    >
      <TriangleAlert size={11} /> not delivered
    </span>
  )
}

function Bubble({ message }) {
  const fromCustomer = message.sender === 'user'
  // A person took over and typed this. Shown differently from the agent's own
  // replies because the operator needs to see at a glance which words were
  // theirs — and because the two are no longer the same thing anywhere else.
  const fromOperator = message.sender === 'operator'

  return (
    <div className={`flex animate-land ${fromCustomer ? 'justify-start' : 'justify-end'}`}>
      <div
        className={`max-w-[46ch] rounded-2xl px-3.5 py-2.5 ${
          fromCustomer
            ? 'rounded-tl-sm bg-panel-2 text-ink'
            : fromOperator
              ? 'rounded-tr-sm bg-platinum/10 text-ink ring-1 ring-inset ring-platinum/25'
              : 'rounded-tr-sm bg-accent/12 text-ink ring-1 ring-inset ring-accent/25'
        }`}
      >
        {fromOperator && (
          <span className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-platinum">
            <User size={10} /> You
          </span>
        )}
        {!fromCustomer && !fromOperator && (
          <span className="mb-1 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-accent">
            <Sparkles size={10} /> AI agent
          </span>
        )}
        <p className="whitespace-pre-wrap text-[13px] leading-relaxed">{message.content}</p>

        {(message.media_urls || []).length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {message.media_urls.map((url) => (
              <a key={url} href={displayable(url)} target="_blank" rel="noreferrer">
                <img
                  src={displayable(url)}
                  alt=""
                  loading="lazy"
                  // A dead catalogue link should leave no broken-image icon.
                  onError={(e) => {
                    e.currentTarget.parentElement.style.display = 'none'
                  }}
                  className="h-24 w-20 rounded-lg border border-edge object-cover"
                />
              </a>
            ))}
          </div>
        )}
        <span
          className={`mt-1.5 flex items-center gap-1 font-mono text-[10px] ${
            fromCustomer ? 'text-faint' : 'justify-end text-faint'
          }`}
        >
          {clockOf(message.created_at)}
          {!fromCustomer && <DeliveryMark message={message} />}
        </span>
      </div>
    </div>
  )
}

function Composing() {
  return (
    <div className="flex animate-land justify-end">
      <div className="flex items-center gap-2 rounded-2xl rounded-tr-sm bg-accent/8 px-3.5 py-3 ring-1 ring-inset ring-accent/20">
        <Bot size={13} className="text-accent" />
        <span className="flex gap-1">
          <span className="dot-1 h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="dot-2 h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="dot-3 h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        <span className="text-[11px] text-dim">writing a reply</span>
      </div>
    </div>
  )
}

/** What the vision model read out of the last photo they sent. */
function PhotoRead({ contact }) {
  const seen = contact.metadata?.last_received_image_analysis
  if (!seen) return null

  const bits = [seen.colour, seen.pattern, seen.category, seen.fabric].filter(Boolean)
  return (
    <div className="border-b border-edge bg-panel-2/40 px-4 py-2">
      <p className="eyebrow mb-1">Read from their photo</p>
      <div className="flex flex-wrap items-center gap-1.5">
        {bits.map((bit) => (
          <span
            key={bit}
            className="rounded-md bg-accent/12 px-2 py-0.5 text-[10px] capitalize text-accent"
          >
            {bit}
          </span>
        ))}
        {seen.description && (
          <span className="text-[11px] text-dim">{seen.description}</span>
        )}
      </div>
    </div>
  )
}

/** What the agent has picked up about this customer, so the shop can see it too.
 *
 * Reads the structured memory the agent actually uses, falling back to the
 * older per-column fields for contacts created before that existed. */
function KnownFacts({ contact }) {
  const remembered = contact.memory?.facts || {}
  const label = (key, value) => (key === 'size' ? `Size ${value}` : value)

  const fromMemory = Object.entries(remembered)
    .filter(([, entry]) => entry?.value)
    .map(([key, entry]) => label(key, entry.value))

  const legacy = [
    contact.shoe_size && `Size ${contact.shoe_size}`,
    contact.city,
    contact.category_interest,
    contact.colour_preference,
    contact.budget_note,
  ].filter(Boolean)

  // Deduplicate so a fact held in both places is shown once.
  const facts = [...new Set([...fromMemory, ...legacy])]

  if (facts.length === 0) return null

  return (
    <div className="ml-3 hidden flex-wrap items-center gap-1.5 lg:flex">
      {facts.map((fact) => (
        <span
          key={fact}
          className="rounded-md bg-panel-2 px-2 py-0.5 text-[10px] capitalize text-dim"
          title="Remembered from the conversation"
        >
          {fact}
        </span>
      ))}
    </div>
  )
}

/** Schedule a nudge for a conversation that has gone quiet.
 *
 * The agent already follows up on its own, but only for warm conversations and
 * only after four hours — which makes it impossible to see working. This drives
 * the same machinery by hand: same queued task, same transport, same
 * cancellation, just a delay you choose. A customer who replies before it lands
 * cancels it, exactly as they would the automatic one.
 */
function FollowUpControl({ contact, onChanged }) {
  const pending = contact.metadata?.followup_due_at || null
  const [open, setOpen] = useState(false)
  const [minutes, setMinutes] = useState(2)
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  // Reset when the conversation changes, or the form carries over.
  useEffect(() => {
    setOpen(false)
    setError(null)
    setMessage('')
  }, [contact.id])

  const schedule = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.scheduleFollowup(contact.id, { minutes: Number(minutes), message })
      setOpen(false)
      setMessage('')
      onChanged?.()
    } catch (err) {
      setError(err.message || 'Could not schedule that.')
    } finally {
      setBusy(false)
    }
  }

  const cancel = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.cancelFollowup(contact.id)
      onChanged?.()
    } catch (err) {
      setError(err.message || 'Could not cancel that.')
    } finally {
      setBusy(false)
    }
  }

  if (pending) {
    const due = new Date(pending)
    const valid = !Number.isNaN(due.getTime())
    return (
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="flex items-center gap-1.5 rounded-md bg-accent/12 px-2 py-1 text-accent">
          <AlarmClock size={12} />
          Follow-up {valid ? `at ${due.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : 'scheduled'}
        </span>
        {contact.metadata?.followup_message && (
          <span className="max-w-[32ch] truncate text-faint" title={contact.metadata.followup_message}>
            “{contact.metadata.followup_message}”
          </span>
        )}
        <button
          type="button"
          onClick={cancel}
          disabled={busy}
          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-faint transition-colors hover:bg-panel-2 hover:text-crit disabled:opacity-40"
        >
          <X size={11} /> cancel
        </button>
        {/* A reply from the customer cancels it too, which is the point. */}
        <span className="text-faint">— cancelled automatically if they write back</span>
        {error && <span className="text-crit">{error}</span>}
      </div>
    )
  }

  if (!open) {
    return (
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="flex items-center gap-1.5 rounded-md border border-edge px-2 py-1 text-[11px] text-dim transition-colors hover:border-accent/40 hover:text-accent"
        >
          <AlarmClock size={12} /> Schedule a follow-up
        </button>
        {error && <span className="text-[11px] text-crit">{error}</span>}
      </div>
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <select
        value={minutes}
        onChange={(e) => setMinutes(e.target.value)}
        className="rounded-md border border-edge bg-bg px-2 py-1 text-[11px] text-ink"
      >
        <option value={2}>in 2 minutes</option>
        <option value={15}>in 15 minutes</option>
        <option value={60}>in 1 hour</option>
        <option value={240}>in 4 hours</option>
        <option value={1440}>tomorrow</option>
      </select>
      <input
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        placeholder="Leave empty to use the agent's own wording"
        className="min-w-0 flex-1 rounded-md border border-edge bg-bg px-2 py-1 text-[11px] text-ink placeholder:text-faint"
      />
      <button
        type="button"
        onClick={schedule}
        disabled={busy}
        className="rounded-md bg-accent px-2.5 py-1 text-[11px] font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
      >
        {busy ? 'Scheduling…' : 'Schedule'}
      </button>
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="rounded-md px-1.5 py-1 text-faint transition-colors hover:text-ink"
      >
        <X size={12} />
      </button>
      {error && <span className="w-full text-crit">{error}</span>}
    </div>
  )
}

export default function ConversationThread({
  contact,
  messages,
  composing,
  arriving,
  onChanged,
  className = '',
  onBack,
}) {
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, composing])

  if (!contact && arriving) {
    return (
      <section className={`panel flex-1 items-center justify-center ${className}`}>
        <div className="flex items-center gap-2.5 text-xs text-dim">
          <span className="flex gap-1">
            <span className="dot-1 h-1.5 w-1.5 rounded-full bg-accent" />
            <span className="dot-2 h-1.5 w-1.5 rounded-full bg-accent" />
            <span className="dot-3 h-1.5 w-1.5 rounded-full bg-accent" />
          </span>
          New conversation coming in
        </div>
      </section>
    )
  }

  if (!contact) {
    return (
      <section className={`panel flex-1 items-center justify-center ${className}`}>
        <div className="max-w-xs px-6 text-center">
          <span className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-full bg-panel-2">
            <Sparkles size={18} className="text-accent" />
          </span>
          <p className="text-sm font-medium text-ink">Your agent is on duty</p>
          <p className="mt-1 text-xs text-dim">
            Every WhatsApp message gets an instant, on-brand reply. Pick a conversation to read
            along.
          </p>
        </div>
      </section>
    )
  }

  return (
    <section className={`panel flex-1 ${className}`}>
      <header className="panel-head">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            aria-label="Back to conversations"
            className="-ml-1 rounded-lg p-1 text-dim transition-colors hover:text-ink lg:hidden"
          >
            <ChevronLeft size={16} />
          </button>
        )}
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-edge text-2xs font-semibold text-dim">
          {initialsOf(contact.name, contact.phone_number)}
        </span>
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-ink">
            {contactLabel(contact)}
          </h2>
          <p className="font-mono text-2xs text-faint">
            {isPlaceholderNumber(contact)
              ? 'number not shared by WhatsApp'
              : prettyPhone(contact.phone_number)}
          </p>
        </div>
        <KnownFacts contact={contact} />

        <span
          className={`ml-auto rounded-full px-2.5 py-1 text-[10px] font-semibold uppercase tracking-wider ${
            STAGE_STYLE[contact.pipeline_stage] || STAGE_STYLE.LEAD
          }`}
        >
          {STAGE_LABEL[contact.pipeline_stage] || contact.pipeline_stage}
        </span>
      </header>

      <PhotoRead contact={contact} />

      <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto px-4 py-4">
        {messages.map((message) => (
          <Bubble key={message.id} message={message} />
        ))}
        {composing && <Composing />}
        <div ref={endRef} />
      </div>

      <footer className="shrink-0 space-y-2 border-t border-edge px-4 py-2.5">
        <FollowUpControl contact={contact} onChanged={onChanged} />
        <p className="flex items-center gap-1.5 text-[11px] text-faint">
          <Bot size={12} className="text-accent" />
          Replies are sent automatically — no one has to be at a desk.
        </p>
      </footer>
    </section>
  )
}
