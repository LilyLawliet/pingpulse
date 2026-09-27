import { useCallback, useEffect, useState } from 'react'
import { CalendarDays, Check, Copy, Loader2, TriangleAlert } from 'lucide-react'
import { api } from '../api.js'

/**
 * The diary, on the phone the business actually runs its day from.
 *
 * This was a small panel at the bottom of the hours form, below the services
 * list — three screens down from anything a shop opens that page to do. A
 * business could take a booking and have nowhere to see it except a dashboard
 * they are not looking at, which is how an appointment gets missed by the one
 * person who agreed to it. So it is a setup step of its own now, and it is
 * counted: booking that nobody can see is not finished setup.
 *
 * A subscription URL rather than a connected account: every calendar client
 * already knows how to read one, nobody signs into anything, and no consent
 * screen can change underneath it. It is read-only by construction, which
 * keeps the database the one place allowed to say an appointment exists.
 *
 * The link is a secret, so replacing it is offered in exactly those terms —
 * it is how a link that has been forwarded gets taken back.
 */
export default function CalendarSettings({ onChanged }) {
  const [state, setState] = useState(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState(null)

  const load = useCallback(() => {
    api
      .getCalendarSubscription()
      .then(setState)
      .catch(() => setState({ active: false, urls: null }))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!state) {
    return <Loader2 size={16} className="animate-spin text-faint" />
  }

  const run = async (action) => {
    setBusy(true)
    setError(null)
    try {
      setState(await action())
      setCopied(false)
      // The step is ticked from the server, so tell whoever is counting.
      onChanged?.()
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(state.urls.https)
      setCopied(true)
    } catch {
      setError('Could not copy — select the link and copy it by hand.')
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2 rounded-xl border border-edge bg-panel-2/50 px-4 py-3.5">
        <p className="flex items-start gap-2.5 text-sm leading-relaxed text-dim">
          <CalendarDays size={15} className="mt-0.5 shrink-0 text-accent" />
          <span>
            <span className="font-semibold text-ink">
              Every booking, in the calendar you already use.
            </span>{' '}
            Subscribe once from your phone or laptop and appointments appear
            there by themselves — iPhone, Google Calendar, Outlook. There is no
            account to connect and nothing to sign into.
          </span>
        </p>
        <p className="text-xs leading-relaxed text-faint">
          It is read-only. Nothing can be changed or cancelled from the
          calendar, because the only thing allowed to say an appointment exists
          is this system.
        </p>
      </div>

      {error && (
        <p className="rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">{error}</p>
      )}

      {state.active ? (
        <div className="space-y-3">
          <a
            href={state.urls.webcal}
            className="btn-primary inline-flex w-auto"
          >
            <CalendarDays size={14} />
            Add to this device
          </a>

          <label className="block">
            <span className="eyebrow mb-1.5 block">Your link</span>
            <div className="flex gap-2">
              <input
                readOnly
                value={state.urls.https}
                onFocus={(event) => event.target.select()}
                className="w-full rounded-xl border border-edge bg-panel px-3.5 py-2.5 font-mono text-2xs text-dim focus:border-accent/60 focus:outline-none"
              />
              <button
                type="button"
                onClick={copy}
                className="flex shrink-0 items-center gap-1.5 rounded-xl border border-edge px-3 text-xs font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink"
              >
                {copied ? <Check size={13} /> : <Copy size={13} />}
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
            <span className="mt-1.5 block text-2xs leading-relaxed text-faint">
              Paste it into any other phone that needs the same diary.
            </span>
          </label>

          <p className="flex items-start gap-2 rounded-xl bg-warn/10 px-3.5 py-2.5 text-2xs leading-relaxed text-warn">
            <TriangleAlert size={12} className="mt-0.5 shrink-0" />
            <span>
              Anyone holding this link can see your appointments — it is the
              only thing standing in front of them. Replacing it stops every
              phone already subscribed, which is how you take one back.
            </span>
          </p>

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => run(api.createCalendarSubscription)}
              className="rounded-xl border border-edge px-3 py-2 text-xs font-semibold text-dim transition-colors hover:border-warn/50 hover:text-ink disabled:opacity-40"
            >
              Replace link
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(api.deleteCalendarSubscription)}
              className="rounded-xl border border-edge px-3 py-2 text-xs font-semibold text-faint transition-colors hover:border-crit/50 hover:text-crit disabled:opacity-40"
            >
              Turn off
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          disabled={busy}
          onClick={() => run(api.createCalendarSubscription)}
          className="btn-primary"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <CalendarDays size={14} />}
          Create my calendar link
        </button>
      )}
    </div>
  )
}
