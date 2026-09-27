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
    <div className="space-y-2 px-3 pb-3 pt-2">
      <div className="relative">
        <Search
          size={15}
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
        />
        <input
          type="search"
          value={filters.search || ''}
          onChange={(e) => set({ search: e.target.value })}
          placeholder="Search name, number, notes…"
          aria-label="Search conversations"
          className="w-full rounded-xl border border-transparent bg-panel-2 py-2 pl-9 pr-3 text-sm text-ink placeholder:text-faint focus:border-accent/50 focus:bg-panel focus:outline-none"
        />
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        <Toggle
          on={!(filters.stage || filters.unread_only || filters.taken_over)}
          onClick={() =>
            onChange({ search: filters.search || '', stage: '', unread_only: false, taken_over: false })
          }
          label="All"
        />
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
        <select
          value={filters.stage || ''}
          onChange={(e) => set({ stage: e.target.value })}
          aria-label="Filter by stage"
          className={`min-w-0 max-w-[9.5rem] shrink-0 cursor-pointer rounded-full border px-2.5 py-1 text-xs font-medium focus:outline-none ${
            filters.stage
              ? 'border-accent/40 bg-accent/10 text-accent'
              : 'border-edge bg-panel text-dim hover:text-ink'
          }`}
        >
          <option value="">Any stage</option>
          {stages.map((stage) => (
            <option key={stage.key} value={stage.key}>
              {stage.label}
            </option>
          ))}
        </select>

        {active && (
          <button
            type="button"
            onClick={() =>
              onChange({ search: '', stage: '', unread_only: false, taken_over: false })
            }
            className="ml-auto flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-xs font-medium text-faint transition-colors hover:text-ink"
            title="Clear filters"
          >
            <X size={13} /> Clear
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
      aria-pressed={on}
      className={`shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${
        on
          ? 'border-accent/40 bg-accent/10 text-accent'
          : 'border-edge bg-panel text-dim hover:text-ink'
      }`}
    >
      {label}
    </button>
  )
}
