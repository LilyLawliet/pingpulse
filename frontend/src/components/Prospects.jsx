import { useCallback, useEffect, useState } from 'react'
import { Clock, Inbox, Loader2, Send, TriangleAlert, X } from 'lucide-react'
import { api } from '../api.js'
import { prettyPhone } from '../format.js'

/**
 * Customers already in the shop's WhatsApp who asked something and never got
 * a reply.
 *
 * The most valuable thing in a shop's history and the easiest to turn into
 * spam, so the screen is built around the distinction. Every row shows the
 * customer's own words and what they asked about, and every reply is written
 * and sent one at a time. There is no select-all, and there will not be: the
 * transport is an unofficial WhatsApp client, and sending in bulk over one of
 * those gets the shop's number banned.
 *
 * The counter is shown rather than hidden for the same reason. Someone who can
 * see "6 of 40 today" is being told the pace matters; a silent limit only
 * appears as a failure at number forty-one.
 */
export default function Prospects({ onClose, onReplied }) {
  const [state, setState] = useState(null)
  const [loading, setLoading] = useState(true)
  const [drafts, setDrafts] = useState({})
  const [sending, setSending] = useState(null)
  const [error, setError] = useState(null)
  const [done, setDone] = useState({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setState(await api.listProspects(30))
    } catch (err) {
      setError(err.message)
    }
    setLoading(false)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const send = async (person) => {
    const message = (drafts[person.jid] || '').trim()
    if (!message) return

    setSending(person.jid)
    setError(null)
    try {
      const result = await api.replyToProspect(person.jid, message)
      setDone((prev) => ({ ...prev, [person.jid]: true }))
      setState((prev) => (prev ? { ...prev, sent_today: result.sent_today } : prev))
      onReplied?.()
    } catch (err) {
      setError(err.message)
    }
    setSending(null)
  }

  const people = (state?.prospects || []).filter((p) => !done[p.jid])

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm">
      <div className="max-h-[88vh] w-full max-w-2xl overflow-auto rounded-2xl border border-edge bg-panel shadow-lift">
        <header className="sticky top-0 flex items-center gap-2.5 border-b border-edge bg-panel px-5 py-4">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-warn/12 ring-1 ring-inset ring-warn/25">
            <Inbox size={15} className="text-warn" />
          </span>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-ink">Never answered</h3>
            <p className="mt-0.5 text-2xs text-dim">
              People who asked about something and did not hear back
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-auto rounded-lg p-1 text-faint transition-colors hover:bg-panel-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>

        <div className="space-y-3 px-5 py-5">
          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          {loading && (
            <p className="flex items-center gap-2 py-6 text-xs text-dim">
              <Loader2 size={13} className="animate-spin" /> Reading your conversations…
            </p>
          )}

          {!loading && state && !state.available && (
            <p className="py-6 text-xs text-dim">{state.reason}</p>
          )}

          {!loading && state?.available && people.length === 0 && (
            <div className="py-6">
              <p className="text-xs text-ink">Nobody is waiting on you.</p>
              <p className="mt-1.5 text-2xs leading-relaxed text-dim">
                This reads the conversations WhatsApp sends across when a phone is first
                connected. If you connected a while ago there may be none to show — they
                arrive with a fresh pairing.
              </p>
            </div>
          )}

          {people.length > 0 && (
            <p className="text-2xs leading-relaxed text-dim">
              Each of these asked a question your shop never answered. Reply to them one at
              a time and in your own words — WhatsApp treats a burst of identical messages
              as spam, and it is your number at risk.{' '}
              <span className="font-mono text-faint">
                {state.sent_today} of {state.daily_limit} sent today
              </span>
            </p>
          )}

          {people.map((person) => (
            <div key={person.jid} className="rounded-xl border border-edge bg-panel-2/40 p-4">
              <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
                <span className="text-xs font-semibold text-ink">
                  {person.name || prettyPhone(`+${person.number}`)}
                </span>
                {person.name && (
                  <span className="font-mono text-2xs text-faint">
                    {prettyPhone(`+${person.number}`)}
                  </span>
                )}
                <span className="ml-auto flex items-center gap-1 font-mono text-2xs text-warn">
                  <Clock size={11} />
                  {person.days_ago === 0 ? 'today' : `${person.days_ago}d ago`}
                </span>
              </div>

              <p className="mt-2 rounded-lg bg-panel-2 px-3 py-2 text-[13px] leading-relaxed text-ink">
                {person.last_message}
              </p>

              {person.matched.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {person.matched.map((term) => (
                    <span
                      key={term}
                      className="rounded-md bg-accent/10 px-2 py-0.5 text-[10px] text-accent"
                    >
                      {term}
                    </span>
                  ))}
                </div>
              )}

              <div className="mt-2.5 flex flex-wrap items-end gap-2">
                <textarea
                  id={`reply-${person.jid}`}
                  rows={2}
                  value={drafts[person.jid] || ''}
                  onChange={(e) =>
                    setDrafts((prev) => ({ ...prev, [person.jid]: e.target.value }))
                  }
                  placeholder="Answer their question…"
                  className="min-w-[220px] flex-1 rounded-lg border border-edge bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-faint focus:border-accent/60"
                />
                <button
                  type="button"
                  disabled={sending === person.jid || !(drafts[person.jid] || '').trim()}
                  onClick={() => send(person)}
                  className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
                >
                  {sending === person.jid ? (
                    <Loader2 size={13} className="animate-spin" />
                  ) : (
                    <Send size={13} />
                  )}
                  Reply
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
