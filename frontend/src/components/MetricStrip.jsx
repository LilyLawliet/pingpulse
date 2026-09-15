import { CalendarCheck, MessagesSquare, Trophy, UserPlus, Zap } from 'lucide-react'
import { seconds } from '../format.js'

const WINDOWS = [
  ['today', 'Today'],
  ['7d', '7 days'],
  ['30d', '30 days'],
  ['all', 'All time'],
]

function Tile({ icon: Icon, label, value, hint }) {
  return (
    <div className="flex items-center gap-2.5 bg-panel px-3 py-2.5 sm:gap-3 sm:px-4 sm:py-3">
      <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-panel-2">
        <Icon size={14} className="text-accent" />
      </span>
      <div className="min-w-0">
        <p className="font-mono text-base font-semibold leading-none tabular-nums text-ink sm:text-lg">
          {value}
        </p>
        <p className="mt-1 truncate text-2xs text-dim">{label}</p>
      </div>
      {hint && <span className="ml-auto hidden text-2xs text-faint sm:inline">{hint}</span>}
    </div>
  )
}

/**
 * The numbers across the top, over a chosen span of time.
 *
 * Two things changed here from the version that shipped. The counts used to be
 * all-time only, which answers "how has this gone since we installed it" when
 * the question somebody actually has is "how has this week gone". And one tile
 * said "Booked", which could mean an appointment, a job or a sale depending on
 * the business reading it.
 *
 * "Booked" now shows whatever that shop calls the column it marked as booked —
 * "Estimate scheduled" for a contractor, "Appointment booked" for a salon.
 * The board carries the meaning and the label follows the tenant, so the
 * number stops being ambiguous without anybody configuring a metric.
 */
export default function MetricStrip({ stats, contacts, window: span = 'all', onWindow }) {
  const outcomes = stats?.outcomes || {}
  const labels = stats?.outcome_labels || {}
  const avgMs = stats?.providers?.length
    ? stats.providers.reduce((sum, p) => sum + p.avg_latency_ms * p.calls, 0) /
      stats.providers.reduce((sum, p) => sum + p.calls, 0)
    : null

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-1">
        {WINDOWS.map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => onWindow?.(key)}
            className={`rounded-lg px-2 py-1 text-2xs transition-colors ${
              span === key
                ? 'bg-accent/15 text-accent ring-1 ring-inset ring-accent/30'
                : 'text-faint hover:text-ink'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-edge bg-edge lg:grid-cols-5">
        <Tile icon={MessagesSquare} label="Messages handled" value={stats?.messages ?? 0} />
        <Tile icon={UserPlus} label="Active contacts" value={contacts.length} />
        <Tile
          icon={CalendarCheck}
          // The tenant's own word for it, falling back only when no column has
          // been marked as the booked one.
          label={labels.booked || 'Booked'}
          value={outcomes.booked || 0}
        />
        <Tile icon={Trophy} label={labels.won || 'Won'} value={outcomes.won || 0} />
        <Tile
          icon={Zap}
          label="Average reply time"
          value={avgMs ? seconds(avgMs) : '—'}
          hint="always on"
        />
      </div>
    </div>
  )
}
