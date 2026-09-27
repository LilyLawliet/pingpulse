import { Bot, MessageSquare, UserRound } from 'lucide-react'
import InboxFilters from './InboxFilters.jsx'
import { Avatar } from './ui.jsx'
import { contactLabel, initialsOf, prettyPhone, stageChip, stageLabel } from '../format.js'

export default function ConversationList({
  contacts,
  selectedId,
  onSelect,
  previews,
  composing,
  className = '',
  stages,
  filters,
  onFilters,
}) {
  return (
    <section className={`panel shrink-0 ${className}`}>
      <header className="flex shrink-0 items-center gap-2 px-4 pb-1 pt-3.5">
        <h2 className="text-sm font-semibold text-ink">Conversations</h2>
        <span className="rounded-full bg-panel-2 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-dim">
          {contacts.length}
        </span>
      </header>

      {onFilters && (
        <InboxFilters filters={filters || {}} onChange={onFilters} stages={stages} />
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {contacts.length === 0 && (
          <div className="px-6 py-12 text-center">
            <span className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-2xl bg-panel-2 text-faint">
              <MessageSquare size={18} />
            </span>
            {narrowed(filters) ? (
              <>
                <p className="text-sm font-medium text-ink">Nothing matches those filters</p>
                <p className="mt-1 text-xs text-dim">Clear them to see every conversation.</p>
              </>
            ) : (
              <>
                <p className="text-sm font-medium text-ink">No conversations yet</p>
                <p className="mt-1 text-xs text-dim">
                  They appear the moment someone messages you.
                </p>
              </>
            )}
          </div>
        )}

        {contacts.map((contact) => {
          const active = contact.id === selectedId
          const preview = previews[contact.id] || contact.summary || prettyPhone(contact.phone_number)
          const typing = composing.has(contact.id)
          const yours = contact.ai_enabled === false
          return (
            <button
              key={contact.id}
              onClick={() => onSelect(contact.id)}
              aria-current={active ? 'true' : undefined}
              className={`flex w-full items-center gap-3 rounded-xl px-2.5 py-2.5 text-left transition-colors ${
                active ? 'bg-accent/10' : 'hover:bg-panel-2'
              }`}
            >
              <span className="relative">
                <Avatar
                  seed={contact.id}
                  active={active}
                  text={initialsOf(contact.name, contact.phone_number)}
                />
                <span
                  className={`absolute -bottom-0.5 -right-0.5 grid h-4 w-4 place-items-center rounded-full ring-2 ring-panel ${yours ? 'bg-warn text-on-accent' : 'bg-panel-2 text-accent'}`}
                  title={yours ? 'You are handling this one' : 'The agent is replying'}
                >
                  {yours ? <UserRound size={9} /> : <Bot size={9} />}
                </span>
              </span>

              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">
                    {contactLabel(contact)}
                  </span>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${stageChip(
                      stages,
                      contact.pipeline_stage,
                    )}`}
                  >
                    {stageLabel(stages, contact.pipeline_stage)}
                  </span>
                </span>
                <span className="mt-0.5 block truncate text-xs text-dim">
                  {typing ? (
                    <span className="font-medium text-accent">Agent is typing…</span>
                  ) : (
                    preview
                  )}
                </span>
              </span>
            </button>
          )
        })}
      </div>
    </section>
  )
}

/**
 * Whether the empty list is empty because of a filter.
 *
 * Worth the few lines: the likeliest failure of a filter bar is somebody
 * leaving one on and concluding their conversations have disappeared.
 */
function narrowed(filters) {
  const f = filters || {}
  return Boolean(f.search || f.stage || f.unread_only || f.taken_over)
}
