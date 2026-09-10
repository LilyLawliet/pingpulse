import { MessageSquare } from 'lucide-react'
import { STAGE_LABEL, STAGE_STYLE, initialsOf, prettyPhone } from '../format.js'

export default function ConversationList({ contacts, selectedId, onSelect, previews, composing }) {
  return (
    <section className="panel w-[280px] shrink-0">
      <header className="panel-head">
        <MessageSquare size={14} className="text-accent" />
        <h2 className="text-xs font-semibold text-ink">Conversations</h2>
        <span className="ml-auto font-mono text-2xs text-faint">{contacts.length}</span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {contacts.length === 0 && (
          <p className="px-4 py-10 text-center text-xs text-faint">
            No conversations yet.
            <br />
            They appear the moment someone messages you.
          </p>
        )}

        {contacts.map((contact) => {
          const active = contact.id === selectedId
          const preview = previews[contact.id]
          return (
            <button
              key={contact.id}
              onClick={() => onSelect(contact.id)}
              className={`flex w-full items-start gap-3 border-l-2 px-3 py-3 text-left transition-colors ${
                active
                  ? 'border-l-accent bg-panel-2'
                  : 'border-l-transparent hover:bg-panel-2/60'
              }`}
            >
              <span
                className={`grid h-9 w-9 shrink-0 place-items-center rounded-full text-xs font-semibold ${
                  active ? 'bg-accent text-bg' : 'bg-edge text-dim'
                }`}
              >
                {initialsOf(contact.name, contact.phone_number)}
              </span>

              <span className="min-w-0 flex-1">
                <span className="flex items-baseline gap-2">
                  <span className="truncate text-sm font-medium text-ink">
                    {contact.name || prettyPhone(contact.phone_number)}
                  </span>
                </span>
                <span className="mt-0.5 block truncate text-2xs text-dim">
                  {composing.has(contact.id) ? (
                    <span className="text-accent">typing…</span>
                  ) : (
                    preview || prettyPhone(contact.phone_number)
                  )}
                </span>
                <span
                  className={`mt-1.5 inline-block rounded-full px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wider ${
                    STAGE_STYLE[contact.pipeline_stage] || STAGE_STYLE.LEAD
                  }`}
                >
                  {STAGE_LABEL[contact.pipeline_stage] || contact.pipeline_stage}
                </span>
              </span>
            </button>
          )
        })}
      </div>
    </section>
  )
}
