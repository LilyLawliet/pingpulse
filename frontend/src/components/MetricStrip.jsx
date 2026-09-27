import { CalendarCheck, MessagesSquare, Trophy, UserPlus, Zap } from 'lucide-react'
import { seconds } from '../format.js'
import { Segmented } from './ui.jsx'

const WINDOWS = [
  ['today', 'Today'],
  ['7d', '7d'],
  ['30d', '30d'],
  ['all', 'All'],
]

function Tile({ icon: Icon, label, value, tone = 'text-accent bg-accent/10' }) {
  return (
    <div className="flex min-w-max flex-1 items-center gap-3 lg:min-w-0 rounded-2xl border border-edge bg-panel px-3.5 py-3 shadow-card">
      <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-xl ${tone}`}>
        <Icon size={16} />
      </span>
      <div className="min-w-0">
        <p className="text-lg font-semibold leading-none tracking-tight tabular-nums text-ink">
          {value}
        </p>
        <p className="mt-1 truncate text-2xs text-dim">{label}</p>
      </div>
    </div>
  )
}

/**
 * The numbers across the top of the inbox, over a chosen span of time.
 *
 * "Booked" shows whatever that shop calls the column it marked as booked -
 * "Estimate scheduled" for a contractor, "Appointment booked" for a salon - so
 * the number means the same thing to everybody reading it.
 *
 * On a phone the tiles scroll sideways in one row rather than stacking into a
 * wall of numbers above the conversations.
 */
export default function MetricStrip({ stats, contacts }) {
  const outcomes = stats?.outcomes || {}
  const labels = stats?.outcome_labels || {}
  const avgMs = stats?.providers?.length
    ? stats.providers.reduce((sum, p) => sum + p.avg_latency_ms * p.calls, 0) /
      stats.providers.reduce((sum, p) => sum + p.calls, 0)
    : null

  return (
    <div className="-mx-3 flex gap-2.5 overflow-x-auto px-3 pb-0.5 sm:mx-0 sm:px-0">
      <Tile icon={MessagesSquare} label="Messages handled" value={stats?.messages ?? 0} />
      <Tile
        icon={UserPlus}
        label="Active contacts"
        value={contacts.length}
        tone="text-customer bg-customer/10"
      />
      <Tile
        icon={CalendarCheck}
        label={labels.booked || 'Booked'}
        value={outcomes.booked || 0}
        tone="text-warn bg-warn/10"
      />
      <Tile icon={Trophy} label={labels.won || 'Won'} value={outcomes.won || 0} />
      <Tile
        icon={Zap}
        label="Avg. reply time"
        value={avgMs ? seconds(avgMs) : '—'}
        tone="text-customer bg-customer/10"
      />
    </div>
  )
}

/** The span the tiles count over. */
export function MetricWindow({ value, onChange }) {
  return <Segmented label="Time span" options={WINDOWS} value={value} onChange={onChange} />
}
