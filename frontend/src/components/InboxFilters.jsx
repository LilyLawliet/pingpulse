import { Search, X } from 'lucide-react'
import { DEFAULT_STAGES } from '../format.js'

/**
 * Narrowing the inbox to the conversations that need something.
 *
 * A list sorted by arrival is fine at twenty conversations and useless at two
 * hundred, where the ones that matter — unanswered, or waiting on a person —
 * are scattered through everything already handled.
 *
 * Filtering happens on the server rather than over a list already fetched.
 * Doing it in the browser only narrows the most recent hundred, which looks
 * identical until the conversation somebody is hunting for is the hundred and
 * first and simply is not there.
 *
 * The clear button appears only when something is filtered, because the
 * failure this is most likely to cause is somebody leaving a filter on and
 * concluding their conversations have vanished.
 */
export default function InboxFilters({ filters, onChange, stages = DEFAULT_STAGES }) {
  const set = (patch) => onChange({ ...filters, ...patch })
  const active = Boolean(
    filters.search || filters.stage || filters.unread_only || filters.taken_over,
  )

  return (
    <div className="space-y-2 border-b border-edge px-3 py-2.5">
      <div className="relative">
        <Search
          size={12}
          className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint"
        />
        <input
          value={filters.search || ''}
          onChange={(e) => set({ search: e.target.value })}
          placeholder="Name, number, company, notes"
          className="w-full rounded-lg border border-edge bg-bg py-1.5 pl-7 pr-2 text-2xs text-ink placeholder:text-faint focus:border-accent/60"
        />
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <select
          value={filters.stage || ''}
          onChange={(e) => set({ stage: e.target.value })}
          className="min-w-0 flex-1 rounded-lg border border-edge bg-bg px-2 py-1 text-2xs text-ink focus:border-accent/60"
        >
          <option value="">Every stage</option>
          {stages.map((stage) => (
            <option key={stage.key} value={stage.key}>
              {stage.label}
            </option>
          ))}
        </select>

        <Toggle
          on={Boolean(filters.unread_only)}
          onClick={() => set({ unread_only: !filters.unread_only })}
          label="Unread"
        />
        <Toggle
          on={Boolean(filters.taken_over)}
          onClick={() => set({ taken_over: !filters.taken_over })}
          label="Mine"
          title="Conversations a person has taken over"
        />

        {active && (
          <button
            type="button"
            onClick={() =>
              onChange({ search: '', stage: '', unread_only: false, taken_over: false })
            }
            className="flex items-center gap-1 rounded-lg px-1.5 py-1 text-2xs text-faint transition-colors hover:text-ink"
            title="Clear filters"
          >
            <X size={11} />
          </button>
        )}
      </div>
    </div>
  )
}

function Toggle({ on, onClick, label, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`rounded-lg px-2 py-1 text-2xs transition-colors ${
        on
          ? 'bg-accent/15 text-accent ring-1 ring-inset ring-accent/30'
          : 'border border-edge text-dim hover:text-ink'
      }`}
    >
      {label}
    </button>
  )
}
