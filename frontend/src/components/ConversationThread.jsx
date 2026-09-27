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
  UserSquare2,
  X,
} from 'lucide-react'
import { clockOf, contactLabel, initialsOf, isPlaceholderNumber, prettyPhone, stageChip, stageLabel } from '../format.js'
import { mediaUrl } from '../backend.js'
import { api } from '../api.js'
import { Avatar } from './ui.jsx'

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

// Anything that looks like a link, so a transcript can show it as one.
const LINK = /(https?:\/\/[^\s]+)/g

/**
 * A message, with its links clickable and readable.
 *
 * The calendar links this agent sends are around 150 characters of url-encoded
 * query string. Printed in full they overflowed the bubble - there is no
 * whitespace in a URL to wrap at - and put a horizontal scrollbar across the
 * whole dashboard.
 *
 * Shown by host instead, because what an operator needs from a transcript is
 * that a booking link went out, not what its `details` parameter contained.
 * The full URL stays in the title attribute and in the href, so nothing is
 * hidden from anybody who wants it.
 */
function withLinks(content) {
  return (content || '').split(LINK).map((piece, index) => {
    if (index % 2 === 0) return piece
    let label = piece
    try {
      const url = new URL(piece)
      label = url.hostname.replace(/^www\./, '') + (url.pathname === '/' ? '' : url.pathname)
    } catch {
      /* not a URL after all — show it as typed */
    }
    return (
      <a
        key={`${index}-${piece}`}
        href={piece}
        target="_blank"
        rel="noreferrer"
        title={piece}
        className="underline decoration-dotted underline-offset-2 hover:opacity-80"
      >
        {label.length > 48 ? `${label.slice(0, 48)}…` : label}
      </a>
    )
  })
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
        className={`max-w-[min(52ch,85%)] rounded-2xl px-4 py-2.5 shadow-card ${
          fromCustomer
            ? 'rounded-bl-md border border-edge bg-panel text-ink'
            : fromOperator
              ? 'rounded-br-md bg-customer/10 text-ink ring-1 ring-inset ring-customer/25'
              : 'rounded-br-md bg-accent/10 text-ink ring-1 ring-inset ring-accent/20'
        }`}
      >
        {fromOperator && (
          <span className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold text-customer">
            <User size={11} /> You
          </span>
        )}
        {!fromCustomer && !fromOperator && (
          <span className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold text-accent">
            <Sparkles size={11} /> AI agent
          </span>
        )}
        {/* `break-words` alone does not break a 150-character URL, which is
            one unbreakable token; `anywhere` is what stops it overflowing the
            bubble and scrolling the entire board sideways. */}
        <p className="whitespace-pre-wrap break-words [overflow-wrap:anywhere] text-sm leading-relaxed">
          {withLinks(message.content)}
        </p>

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
          className={`mt-1 flex items-center gap-1 text-[11px] tabular-nums ${
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
      <div className="flex items-center gap-2 rounded-2xl rounded-br-md bg-accent/10 px-4 py-3 ring-1 ring-inset ring-accent/20">
        <Bot size={13} className="text-accent" />
        <span className="flex gap-1">
          <span className="dot-1 h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="dot-2 h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="dot-3 h-1.5 w-1.5 rounded-full bg-accent" />
        </span>
        <span className="text-[12px] text-dim">writing a reply</span>
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
    <div className="border-b border-edge px-4 py-2.5">
      <p className="eyebrow mb-1.5">Read from their photo</p>
      <div className="flex flex-wrap items-center gap-1.5">
        {bits.map((bit) => (
          <span
            key={bit}
            className="rounded-md bg-accent/12 px-2 py-0.5 text-[11px] capitalize text-accent"
          >
            {bit}
          </span>
        ))}
        {seen.description && (
          <span className="text-[12px] text-dim">{seen.description}</span>
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
    <div className="ml-2 hidden min-w-0 flex-wrap items-center gap-1.5 xl:flex">
      {facts.map((fact) => (
        <span
          key={fact}
          className="rounded-full bg-panel-2 px-2.5 py-0.5 text-[11px] font-medium capitalize text-dim"
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
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
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
          className="btn-secondary px-3 py-1.5 text-xs"
        >
          <AlarmClock size={14} /> Schedule follow-up
        </button>
        {error && <span className="text-[12px] text-crit">{error}</span>}
      </div>
    )
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-[12px]">
      <select
        value={minutes}
        onChange={(e) => setMinutes(e.target.value)}
        className="rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-xs text-ink"
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
        className="min-w-0 flex-1 rounded-lg border border-edge bg-panel px-2.5 py-1.5 text-xs text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none"
      />
      <button
        type="button"
        onClick={schedule}
        disabled={busy}
        className="btn-primary px-3 py-1.5 text-xs"
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
  stages,
  onOpenProfile,
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
          <span className="mx-auto mb-4 grid h-14 w-14 place-items-center rounded-2xl bg-accent/10">
            <Sparkles size={24} className="text-accent" />
          </span>
          <p className="text-base font-semibold text-ink">Your agent is on duty</p>
          <p className="mt-1.5 text-sm leading-relaxed text-dim">
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
            className="-ml-1.5 rounded-lg p-1.5 text-dim transition-colors hover:bg-panel-2 hover:text-ink lg:hidden"
          >
            <ChevronLeft size={20} />
          </button>
        )}
        <button
          type="button"
          onClick={onOpenProfile}
          className="flex min-w-0 items-center gap-3 rounded-xl text-left"
          title="Lead details"
        >
          <Avatar seed={contact.id} text={initialsOf(contact.name, contact.phone_number)} />
          <span className="min-w-0">
            <span className="block truncate text-sm font-semibold text-ink hover:underline">
              {contactLabel(contact)}
            </span>
            <span className="block text-2xs tabular-nums text-faint">
              {isPlaceholderNumber(contact)
                ? 'number not shared by WhatsApp'
                : prettyPhone(contact.phone_number)}
            </span>
          </span>
        </button>
        <KnownFacts contact={contact} />

        <span
          className={`ml-auto hidden shrink-0 rounded-full px-2.5 py-1 text-[11px] font-semibold sm:inline ${
            stageChip(stages, contact.pipeline_stage)
          }`}
        >
          {stageLabel(stages, contact.pipeline_stage)}
        </span>

        {/* Shown here rather than only in the drawer: the moment somebody
            most wants to take a conversation over is while they are reading
            one going wrong. */}
        {contact.ai_enabled === false && (
          <span
            title="You have this conversation — the agent is not replying"
            className="shrink-0 rounded-full bg-warn/15 px-2.5 py-1 text-[11px] font-semibold text-warn max-sm:ml-auto"
          >
            You&rsquo;re replying
          </span>
        )}

        {onOpenProfile && (
          <button
            type="button"
            onClick={onOpenProfile}
            title="Lead details"
            className="btn-secondary shrink-0 px-2.5 py-1.5 text-xs max-sm:ml-auto"
          >
            <UserSquare2 size={14} />
            <span className="hidden xl:inline">Details</span>
          </button>
        )}
      </header>

      <PhotoRead contact={contact} />

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-panel-2/40 px-3 py-5 sm:px-6">
        {messages.map((message) => (
          <Bubble key={message.id} message={message} />
        ))}
        {composing && <Composing />}
        <div ref={endRef} />
      </div>

      <footer className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-2 border-t border-edge px-4 py-3">
        <FollowUpControl contact={contact} onChanged={onChanged} />
        <p className="ml-auto flex items-center gap-1.5 text-xs text-faint">
          <Bot size={14} className="text-accent" />
          {contact.ai_enabled === false
            ? 'The agent is paused here. Resume it from Details.'
            : 'The agent replies automatically.'}
        </p>
      </footer>
    </section>
  )
}
