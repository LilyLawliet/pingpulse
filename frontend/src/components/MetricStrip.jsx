import { CalendarCheck, MessagesSquare, UserPlus, Zap } from 'lucide-react'
import { seconds } from '../format.js'

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

export default function MetricStrip({ stats, contacts }) {
  const pipeline = stats?.pipeline || {}
  const replied = stats?.messages || 0
  const avgMs = stats?.providers?.length
    ? stats.providers.reduce((sum, p) => sum + p.avg_latency_ms * p.calls, 0) /
      stats.providers.reduce((sum, p) => sum + p.calls, 0)
    : null

  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-edge bg-edge lg:grid-cols-4">
      <Tile icon={MessagesSquare} label="Messages handled" value={replied} />
      <Tile icon={UserPlus} label="People talking to you" value={contacts.length} />
      <Tile
        icon={CalendarCheck}
        label="Booked"
        value={(pipeline.DEMO_BOOKED || 0) + (pipeline.CLOSED || 0)}
      />
      <Tile
        icon={Zap}
        label="Average reply time"
        value={avgMs ? seconds(avgMs) : '—'}
        hint="always on"
      />
    </div>
  )
}
