import { useEffect, useRef } from 'react'
import { Bot, CheckCheck, Clock, Sparkles, TriangleAlert } from 'lucide-react'
import { STAGE_LABEL, STAGE_STYLE, clockOf, initialsOf, prettyPhone } from '../format.js'
import { mediaUrl } from '../backend.js'

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
  return (
    <div className={`flex animate-land ${fromCustomer ? 'justify-start' : 'justify-end'}`}>
      <div
        className={`max-w-[46ch] rounded-2xl px-3.5 py-2.5 ${
          fromCustomer
            ? 'rounded-tl-sm bg-panel-2 text-ink'
            : 'rounded-tr-sm bg-accent/12 text-ink ring-1 ring-inset ring-accent/25'
        }`}
      >
        {!fromCustomer && (
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

export default function ConversationThread({ contact, messages, composing, arriving }) {
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages.length, composing])

  if (!contact && arriving) {
    return (
      <section className="panel flex-1 items-center justify-center">
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
      <section className="panel flex-1 items-center justify-center">
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
    <section className="panel flex-1">
      <header className="panel-head">
        <span className="grid h-8 w-8 place-items-center rounded-full bg-edge text-2xs font-semibold text-dim">
          {initialsOf(contact.name, contact.phone_number)}
        </span>
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-ink">
            {contact.name || prettyPhone(contact.phone_number)}
          </h2>
          <p className="font-mono text-2xs text-faint">{prettyPhone(contact.phone_number)}</p>
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

      <footer className="shrink-0 border-t border-edge px-4 py-2.5">
        <p className="flex items-center gap-1.5 text-[11px] text-faint">
          <Bot size={12} className="text-accent" />
          Replies are sent automatically — no one has to be at a desk.
        </p>
      </footer>
    </section>
  )
}
