import { useEffect, useState } from 'react'
import { CalendarCheck, Check, Loader2, Mail, RefreshCw, TriangleAlert, Video } from 'lucide-react'
import { api } from '../api.js'

/**
 * The owner's real calendar, both ways, with nothing to sign into.
 *
 * In: the private iCal address Google, Outlook and Apple give every calendar.
 * Every event on it is a time the agent won't offer - only when, never what.
 *
 * Out: each booking, move and cancellation is emailed to the owner as a
 * calendar invitation, which their calendar adds by itself.
 *
 * And meetings: a prospect asking for a demo or a call is booked as a phone or
 * video call of its own length, with the owner's own meeting room link.
 */
export default function OwnCalendar() {
  const [state, setState] = useState(null)
  const [url, setUrl] = useState('')
  const [link, setLink] = useState('')
  const [kind, setKind] = useState('video')
  const [minutes, setMinutes] = useState(30)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [said, setSaid] = useState(null)

  const take = (next) => {
    setState(next)
    setLink(next.meeting_link || '')
    setKind(next.meeting_kind || 'video')
    setMinutes(next.meeting_minutes || 30)
  }

  useEffect(() => {
    api.getCalendarConnection().then(take).catch((err) => setError(err.message))
  }, [])

  const save = async (body) => {
    setBusy(true)
    setError(null)
    setSaid(null)
    try {
      const next = await api.saveCalendarConnection(body)
      take(next)
      setUrl('')
      setSaid(next.check?.says || 'Saved.')
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const check = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.checkCalendarConnection()
      if (result.ok) setSaid(result.says)
      else setError(result.says)
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  if (!state && !error) return <Loader2 size={16} className="animate-spin text-faint" />

  const field =
    'w-full rounded-xl border border-edge bg-panel px-3.5 py-2.5 text-xs text-ink focus:border-accent/60 focus:outline-none'

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <p className="flex items-start gap-2.5 text-sm leading-relaxed text-dim">
          <Mail size={15} className="mt-0.5 shrink-0 text-accent" />
          <span>
            <span className="font-semibold text-ink">Bookings straight into your calendar.</span> Each
            booking is emailed to you as a calendar invitation, which Gmail, Outlook and Apple add by
            themselves. Moving one updates it; cancelling takes it off.
          </span>
        </p>
        {state?.email_available ? (
          <label className="flex items-center gap-2 text-xs text-dim">
            <input
              type="checkbox"
              checked={state.invite_owner}
              disabled={busy}
              onChange={(e) => save({ invite_owner: e.target.checked })}
            />
            Send them to {state.invites_go_to || 'your alert email address'}
          </label>
        ) : (
          <p className="flex items-start gap-2 text-2xs text-warn">
            <TriangleAlert size={12} className="mt-0.5 shrink-0" />
            Email isn't set up on this server, so invitations can't be sent. The calendar link above
            still works.
          </p>
        )}
      </div>

      <p className="rounded-xl border border-edge bg-panel-2/50 px-3.5 py-2.5 text-2xs leading-relaxed text-dim">
        <span className="font-semibold text-ink">Busy some afternoon?</span> Use{' '}
        <span className="font-medium text-ink">Block out time</span> on the Calendar page. The agent
        won't offer any time you've blocked, and there's nothing to set up.
      </p>

      <details className="group rounded-xl border border-edge px-3.5 py-2.5">
        <summary className="cursor-pointer text-xs font-medium text-dim">
          Optional: see what's in another calendar you keep{' '}
          <span className="font-normal text-faint">(Google, Outlook, iCloud · takes a few minutes)</span>
        </summary>
        <div className="mt-3 space-y-3">
        <p className="flex items-start gap-2.5 text-sm leading-relaxed text-dim">
          <CalendarCheck size={15} className="mt-0.5 shrink-0 text-accent" />
          <span>
            Paste the calendar's private address and the agent won't offer a time you're busy there,
            without you blocking it here too. Only when you're busy is read, never what the event is.
          </span>
        </p>
        <details className="text-2xs leading-relaxed text-faint">
          <summary className="cursor-pointer text-dim">Where to find the address</summary>
          <ul className="mt-1.5 list-disc space-y-1.5 pl-4">
            <li>
              <b>Google</b> - on a computer, not the phone app (it isn't there): calendar.google.com →
              hover your calendar in the left list → ⋮ → Settings and sharing → scroll right down to
              Integrate calendar → <i>Secret address in iCal format</i>. If that section is missing on a
              work (Workspace) account, your admin has turned off outside sharing and it can't be
              found; a personal @gmail account always has it.
            </li>
            <li>
              <b>Outlook / Microsoft 365</b> - Settings ⚙ → Calendar → Shared calendars → Publish a
              calendar → choose the calendar → Can view all details → Publish → copy the{' '}
              <i>ICS</i> link, not the HTML one.
            </li>
            <li>
              <b>iCloud</b> - icloud.com → Calendar → the radio-wave icon beside the calendar → tick
              Public Calendar → copy. It starts webcal://, which is fine.
            </li>
          </ul>
        </details>

        {state?.busy_calendar_set ? (
          <div className="flex flex-wrap items-center gap-2 rounded-xl border border-edge bg-panel-2/50 px-3.5 py-2.5 text-xs text-dim">
            <Check size={13} className="text-accent" />
            <span className="flex-1">
              Reading your calendar at <span className="font-medium text-ink">{state.busy_calendar_host}</span>
            </span>
            <button type="button" disabled={busy} onClick={check} className="btn-ghost px-2 py-1 text-xs">
              <RefreshCw size={12} /> Check now
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => save({ busy_calendar_url: '' })}
              className="btn-ghost px-2 py-1 text-xs hover:text-crit"
            >
              Disconnect
            </button>
          </div>
        ) : null}

        <div className="flex gap-2">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={
              state?.busy_calendar_set ? 'Paste a new address to replace it' : 'https://calendar.google.com/calendar/ical/…/basic.ics'
            }
            aria-label="Your calendar's private address"
            className={`${field} font-mono`}
          />
          <button
            type="button"
            disabled={busy || !url.trim()}
            onClick={() => save({ busy_calendar_url: url.trim() })}
            className="btn-primary shrink-0 px-3 py-2 text-xs"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : null}
            Connect
          </button>
        </div>
        <p className="text-2xs leading-relaxed text-faint">
          Treat this address like a password: it shows your whole calendar. It is checked before it is
          saved, and never shown again. If it ever can't be read, the agent offers no times and you get
          an alert.
        </p>
        </div>
      </details>

      <div className="space-y-3 border-t border-edge pt-4">
        <p className="flex items-start gap-2.5 text-sm leading-relaxed text-dim">
          <Video size={15} className="mt-0.5 shrink-0 text-accent" />
          <span>
            <span className="font-semibold text-ink">Meetings.</span> When someone asks for a demo, a
            call or a meeting, it's booked as this - not as the visit your customers get - and they get
            a link to add it to their own calendar.
          </span>
        </p>
        <div className="grid grid-cols-2 gap-2">
          <label className="block text-xs font-medium text-ink">
            Held as
            <select value={kind} onChange={(e) => setKind(e.target.value)} className={`${field} mt-1`}>
              <option value="video">Video call</option>
              <option value="phone">Phone call</option>
            </select>
          </label>
          <label className="block text-xs font-medium text-ink">
            Length (minutes)
            <input
              type="number"
              min={5}
              max={480}
              step={5}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
              className={`${field} mt-1`}
            />
          </label>
        </div>
        <label className="block text-xs font-medium text-ink">
          Your meeting room link <span className="font-normal text-faint">(Zoom, Google Meet, Teams)</span>
          <input
            value={link}
            onChange={(e) => setLink(e.target.value)}
            placeholder="https://meet.google.com/abc-defg-hij"
            className={`${field} mt-1 font-mono`}
          />
        </label>
        <button
          type="button"
          disabled={busy}
          onClick={() => save({ meeting_kind: kind, meeting_minutes: minutes, meeting_link: link.trim() })}
          className="btn-secondary px-3 py-2 text-xs"
        >
          Save meeting settings
        </button>
      </div>

      {said && (
        <p className="flex items-start gap-2 rounded-lg bg-accent/10 px-3 py-2 text-2xs text-accent">
          <Check size={12} className="mt-0.5 shrink-0" />
          {said}
        </p>
      )}
      {error && (
        <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}
    </div>
  )
}
