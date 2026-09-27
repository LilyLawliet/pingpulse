import { useCallback, useEffect, useState } from 'react'
import { Check, CircleAlert, Loader2 } from 'lucide-react'
import { api } from '../api.js'

const CATEGORY = {
  whatsapp: 'WhatsApp',
  calendar: 'Calendar',
  llm: 'AI provider',
  delivery: 'Delivery',
  system: 'System',
}

/**
 * What has gone wrong lately, where a shop can see it.
 *
 * These already reach the container logs, which is the wrong place: a client
 * cannot read those, and by the time anybody does the question has stopped
 * being "what failed" and become "why did messages stop on Tuesday".
 *
 * Only unresolved failures are shown, and marking one done removes it. A log
 * that only ever grows is one nobody reads twice — the useful question is what
 * is still wrong, not everything that ever was.
 *
 * The stack trace is kept but folded away. The first line is for the shop; the
 * rest is for whoever they forward it to.
 */
export default function ErrorLog() {
  const [errors, setErrors] = useState(null)
  const [busy, setBusy] = useState(null)
  const [expanded, setExpanded] = useState({})

  const load = useCallback(async () => {
    try {
      setErrors((await api.listErrors(14)).errors || [])
    } catch {
      setErrors([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!errors) return null

  const resolve = async (id) => {
    setBusy(id)
    try {
      await api.resolveError(id)
      setErrors((was) => was.filter((row) => row.id !== id))
    } catch {
      // Left on screen; it is still a real failure either way.
    }
    setBusy(null)
  }

  return (
    <section className="space-y-2.5">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl bg-warn/10 ring-1 ring-inset ring-warn/20">
          <CircleAlert size={14} className="text-warn" />
        </span>
        <div className="min-w-0">
          <h4 className="text-xs font-semibold text-ink">Anything that went wrong</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            The last two weeks. Nothing here means nothing failed.
          </p>
        </div>
      </header>

      {errors.length === 0 ? (
        <p className="rounded-lg bg-panel-2/40 px-3 py-2.5 text-2xs text-dim">
          Nothing to report.
        </p>
      ) : (
        <div className="space-y-1.5">
          {errors.map((row) => (
            <div key={row.id} className="rounded-lg border border-edge bg-panel-2/40 p-2.5">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="rounded bg-warn/12 px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide text-warn">
                  {CATEGORY[row.category] || row.category}
                </span>
                <span className="font-mono text-[11px] text-faint">
                  {new Date(row.created_at).toLocaleString()}
                </span>
                <button
                  type="button"
                  disabled={busy === row.id}
                  onClick={() => resolve(row.id)}
                  className="ml-auto flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs text-faint transition-colors hover:text-ok disabled:opacity-40"
                >
                  {busy === row.id ? (
                    <Loader2 size={11} className="animate-spin" />
                  ) : (
                    <Check size={11} />
                  )}
                  Done
                </button>
              </div>

              <p className="mt-1.5 text-2xs leading-relaxed text-ink">{row.message}</p>

              {row.detail && (
                <>
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded((was) => ({ ...was, [row.id]: !was[row.id] }))
                    }
                    className="mt-1 text-2xs text-faint underline-offset-2 hover:underline"
                  >
                    {expanded[row.id] ? 'Hide details' : 'Details'}
                  </button>
                  {expanded[row.id] && (
                    <pre className="mt-1.5 max-h-40 overflow-auto rounded bg-bg px-2 py-1.5 font-mono text-[11px] leading-relaxed text-dim">
                      {row.detail}
                    </pre>
                  )}
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}
