import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, PlugZap } from 'lucide-react'
import { api } from '../api.js'
import { prettyPhone } from '../format.js'

/**
 * Whether this shop is actually connected, in the header where it is seen.
 *
 * The question an operator asks first when replies stop is "is it still on?",
 * and until now the only way to answer it was to message the number and wait.
 * A dot in the header answers it continuously and costs nothing to read.
 *
 * Three states rather than two. Green and red are obvious; amber is for
 * "connected, but something is wrong" — a session that is authenticated while
 * the bridge is unreachable, or a day with failed deliveries in it. Collapsing
 * that into red would cry wolf, and into green would hide the thing the
 * operator most needs to know.
 *
 * Polled rather than pushed, slowly. This is a reassurance light, not an
 * alarm, and a dashboard that opens a socket to render a dot is a dashboard
 * with one more thing that can break.
 */
const REFRESH_MS = 60000

export default function ConnectionStatus({ compact = false, placement = 'down' }) {
  const [status, setStatus] = useState(null)
  const [open, setOpen] = useState(false)

  const load = useCallback(async () => {
    try {
      setStatus(await api.whatsappStatus())
    } catch {
      setStatus(null)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  if (!status) {
    return <Loader2 size={14} className="animate-spin text-faint" />
  }

  const degraded =
    status.connected && (status.bridge_reachable === false || status.failed_last_day > 0)

  // Written out rather than assembled, because Tailwind scans the source at
  // build time and cannot see a class name this code puts together at runtime
  // - `text-${tone}` compiles to nothing and the dot renders colourless.
  const TONE = {
    bad: 'text-crit',
    degraded: 'text-warn',
    good: 'text-ok',
  }
  const tone = !status.connected ? TONE.bad : degraded ? TONE.degraded : TONE.good
  const Icon = status.connected && !degraded ? CheckCircle2 : AlertTriangle

  const label = !status.connected
    ? 'Not connected'
    : degraded
      ? 'Needs a look'
      : 'Connected'

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((was) => !was)}
        className={`flex items-center gap-2 rounded-lg text-xs transition-colors ${
          compact ? 'p-2 hover:bg-panel-2' : 'w-full py-0.5 hover:opacity-80'
        } ${tone}`}
        title="WhatsApp connection"
        aria-expanded={open}
      >
        <Icon size={compact ? 18 : 14} />
        {!compact && (
          <span className="flex-1 text-left">
            <span className="font-semibold">WhatsApp</span>{' '}
            <span className="text-dim">· {label.toLowerCase()}</span>
          </span>
        )}
      </button>

      {open && (
        <div
          className={`absolute z-40 w-72 rounded-2xl border border-edge bg-panel p-4 shadow-lift ${
            placement === 'up' ? 'bottom-full left-0 mb-2' : 'right-0 top-full mt-1.5'
          }`}
        >
          <div className="flex items-center gap-2">
            <PlugZap size={13} className={tone} />
            <span className="text-xs font-semibold text-ink">{label}</span>
          </div>

          {status.phone_number ? (
            <dl className="mt-2.5 space-y-1.5 text-2xs">
              <Row label="Number" value={prettyPhone(status.phone_number)} mono />
              <Row
                label="Connected by"
                value={status.provider === 'QR_SESSION' ? 'WhatsApp Web' : 'Twilio'}
              />
              {status.session_status && (
                <Row label="Session" value={status.session_status.toLowerCase()} />
              )}
              {status.provider === 'QR_SESSION' && (
                <Row
                  label="Bridge"
                  value={
                    status.bridge_reachable === null
                      ? 'unknown'
                      : status.bridge_reachable
                        ? 'reachable'
                        : 'not answering'
                  }
                />
              )}
              <Row label="Last message in" value={ago(status.last_inbound_at)} />
              <Row label="Last message out" value={ago(status.last_outbound_at)} />
              {status.failed_last_day > 0 && (
                <Row
                  label="Failed today"
                  value={`${status.failed_last_day}`}
                  tone="text-crit"
                />
              )}
            </dl>
          ) : (
            <p className="mt-2 text-2xs leading-relaxed text-dim">{status.reason}</p>
          )}
        </div>
      )}
    </div>
  )
}

function Row({ label, value, mono = false, tone = 'text-ink' }) {
  return (
    <div className="flex gap-2">
      <dt className="w-28 shrink-0 text-faint">{label}</dt>
      <dd className={`min-w-0 flex-1 ${mono ? 'font-mono' : ''} ${tone}`}>{value}</dd>
    </div>
  )
}

/** "4m ago" — vague on purpose; the exact second is never the question. */
function ago(iso) {
  if (!iso) return 'never'
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 90) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}
