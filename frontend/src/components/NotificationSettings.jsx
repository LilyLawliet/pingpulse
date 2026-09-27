import { useCallback, useEffect, useState } from 'react'
import {
  Bell,
  BellOff,
  Check,
  CircleAlert,
  Clock,
  Loader2,
  Send,
  TriangleAlert,
} from 'lucide-react'
import { api } from '../api.js'
import { clockOf } from '../format.js'
import {
  alertsDeclined,
  deviceSubscribed,
  subscribeWithPrompt,
  unsubscribe as unsubscribeDevice,
} from '../alerts.js'

/**
 * What went wrong, said to the person rather than about the request.
 *
 * `err.message` is whatever the server put in `detail`, and on a bad day it is
 * `statusText` - so the panel that exists to tell somebody their alerts are
 * fine could answer "Internal Server Error". A status code is an instruction
 * to the reader about what to do next, and that is what is shown.
 */
function saidPlainly(err) {
  if (err?.status === 401) return 'Your session has ended. Sign in again to change this.'
  if (err?.status === 403) return 'This account is not allowed to change alert settings.'
  if (err?.status === 404) return 'This business is no longer available on your account.'
  if (err?.status === 429) return 'Too many tries in a row. Wait a moment and try again.'
  if (err?.status >= 500) {
    return 'The server could not do that just now. Nothing was changed - try again in a moment.'
  }
  if (!err?.status) {
    return 'Could not reach the server. Check your connection and try again.'
  }
  return err?.message || 'That did not work, and no reason was given.'
}

/** How one past alert ended up, as a row the eye can scan. */
const OUTCOME = {
  delivered: { icon: Check, tone: 'text-ok', ring: 'ring-ok/25', bg: 'bg-ok/10' },
  trying: { icon: Clock, tone: 'text-dim', ring: 'ring-edge', bg: 'bg-panel-2/40' },
  undelivered: { icon: CircleAlert, tone: 'text-crit', ring: 'ring-crit/25', bg: 'bg-crit/10' },
  nowhere: { icon: BellOff, tone: 'text-warn', ring: 'ring-warn/25', bg: 'bg-warn/10' },
}

/**
 * Getting told when the dashboard is closed.
 *
 * The agent runs whether or not anybody is watching it — that is what it is
 * for. The cost is that the moments it cannot handle are exactly the moments
 * nobody hears about: a customer demanding a manager at nine at night, a reply
 * that never went out, somebody asking to be left alone. Until now the only
 * way to find out was to have this page open at the time.
 *
 * Two things are kept honest here. A switch is only offered when the server
 * can actually act on it, because a settings page that promises email from a
 * machine with no mail account is worse than one that admits it. And "send a
 * test" sends a real one through the real chain, rather than reporting that
 * the settings saved — the question a person has is not "did it save" but
 * "will I hear it".
 */
export default function NotificationSettings({ onChanged }) {
  const [state, setState] = useState(null)
  const [recent, setRecent] = useState([])
  // Whether this browser holds a live subscription, which is not the same
  // question as whether it holds permission.
  const [onThisDevice, setOnThisDevice] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  const [permission, setPermission] = useState(
    typeof Notification !== 'undefined' ? Notification.permission : 'unsupported',
  )

  const load = useCallback(async () => {
    try {
      setState(await api.notificationSettings())
    } catch (err) {
      setError(saidPlainly(err))
      setState(null)
    }
    setOnThisDevice(await deviceSubscribed())
    try {
      const found = await api.recentNotifications(7)
      setRecent(found.notifications || [])
    } catch {
      // A history that will not load must not take the settings down with
      // it. The switches above are the part somebody came here to use.
      setRecent(null)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!state) return null

  const toggle = (key) => {
    setState({
      ...state,
      events: state.events.map((event) =>
        event.key === key ? { ...event, enabled: !event.enabled } : event,
      ),
    })
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      // A yes or no for every event on screen, rather than a list of the ones
      // that are on. An event added to the server later then arrives at its
      // own default instead of looking like something this shop refused.
      await api.saveNotificationSettings({
        events: Object.fromEntries(state.events.map((e) => [e.key, e.enabled])),
        email: state.email || '',
      })
      setNote('Saved.')
      // Setup ticks this step from the server, so it has to be told to look.
      onChanged?.()
    } catch (err) {
      setError(saidPlainly(err))
    }
    setBusy(false)
  }

  const enablePush = async () => {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await subscribeWithPrompt(state)
      setNote('This device will now be alerted.')
      await load()
      onChanged?.()
    } catch (err) {
      setError(saidPlainly(err))
    }
    setBusy(false)
  }

  const disablePush = async () => {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      await unsubscribeDevice()
      setNote('This device will no longer be alerted.')
      await load()
      onChanged?.()
    } catch (err) {
      setError(saidPlainly(err))
    }
    setBusy(false)
  }

  const test = async () => {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const result = await api.testNotification()
      // The server already worked out what happened and said it in words.
      // This used to print the delivery map - "Sent. Push: no devices.
      // Email: no address." - which announces success and then lists the two
      // reasons it did not happen.
      if (result.status === 'delivered') setNote(`${result.summary}.`)
      else setError(`${result.summary}${result.detail ? ` - ${result.detail}` : ''}.`)
      await load()
    } catch (err) {
      setError(saidPlainly(err))
    }
    setBusy(false)
  }

  const canPush = state.push_available && permission !== 'unsupported'
  // Granted-and-not-deliberately-turned-off is what "this device is set
  // up" actually means. Permission alone stays granted after somebody
  // switches it off, so the button would offer to stop something that
  // had already stopped.
  const deviceOn = permission === 'granted' && !alertsDeclined() && onThisDevice

  return (
    <section className="space-y-3">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
          <Bell size={14} className="text-accent" />
        </span>
        <div className="min-w-0">
          <h4 className="text-xs font-semibold text-ink">Tell me when something happens</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            Your agent keeps working with this page closed. These are how you find
            out about the things it cannot handle on its own.
          </p>
        </div>
      </header>

      {error && (
        <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}
      {note && (
        <p className="flex items-start gap-2 rounded-lg bg-ok/10 px-3 py-2 text-2xs text-ok">
          <Check size={12} className="mt-0.5 shrink-0" />
          {note}
        </p>
      )}

      {/*
        The thing this panel used to have no way of saying. An alert that ran
        out of chances left a row in the database and nothing on any screen,
        so "I never got told" and "it was never sent" looked the same from
        here - and the person who needed to know was the one who could not
        find out.
      */}
      {state.undelivered > 0 && (
        <div className="rounded-lg border border-crit/25 bg-crit/10 px-3 py-2.5">
          <p className="flex items-start gap-2 text-2xs font-semibold text-crit">
            <CircleAlert size={12} className="mt-0.5 shrink-0" />
            {state.undelivered === 1
              ? 'One alert in the past week never reached you'
              : `${state.undelivered} alerts in the past week never reached you`}
          </p>
          <p className="mt-1 pl-5 text-2xs leading-relaxed text-dim">
            Whatever they were about happened anyway. The list below says which
            ones and why.
          </p>
        </div>
      )}

      <div>
        <span className="eyebrow mb-1.5 block">What is worth interrupting you</span>
        <div className="space-y-1">
          {state.events.map((event) => (
            <label
              key={event.key}
              className="flex cursor-pointer items-start gap-2 rounded-lg px-1 py-1 transition-colors hover:bg-panel-2/40"
            >
              <input
                type="checkbox"
                checked={event.enabled}
                onChange={() => toggle(event.key)}
                className="mt-0.5 h-3 w-3 shrink-0 accent-accent"
              />
              <span className="min-w-0 text-2xs leading-relaxed text-ink">
                {event.description}
              </span>
            </label>
          ))}
        </div>
        <p className="mt-1.5 text-2xs leading-relaxed text-faint">
          The same thing happening twice in half an hour only tells you once — four
          buzzes about one angry customer is how people learn to ignore the buzz.
        </p>
      </div>

      <label className="block">
        <span className="eyebrow mb-1 block">Email them to</span>
        <input
          type="email"
          value={state.email ?? ''}
          onChange={(e) => setState({ ...state, email: e.target.value })}
          placeholder={state.suggested_email || 'you@yourbusiness.com'}
          disabled={!state.email_available}
          className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs text-ink placeholder:text-faint focus:border-accent/60 disabled:opacity-40"
        />
        <span className="mt-1 block text-2xs leading-relaxed text-faint">
          {state.email_available
            ? 'Leave it empty for no email. Email needs nothing installed and no ' +
              'permission — it is the one that reaches you on a machine you have ' +
              'never opened this on.'
            : 'Email is not set up on this server, so this does nothing yet.'}
        </span>
        {state.email_available && !state.email && state.suggested_email && (
          <button
            type="button"
            onClick={() => setState({ ...state, email: state.suggested_email })}
            className="mt-1 text-2xs text-accent underline-offset-2 hover:underline"
          >
            Use {state.suggested_email}
          </button>
        )}
      </label>

      <div className="rounded-lg border border-edge bg-panel-2/40 p-2.5">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="text-2xs font-semibold text-ink">On this device</span>
          <span className="text-2xs text-faint">
            {state.devices} {state.devices === 1 ? 'device' : 'devices'} set up
          </span>
        </div>
        <p className="mt-1 text-2xs leading-relaxed text-dim">
          {!canPush
            ? 'This server has no alert keys set up, so alerts cannot be sent to a browser yet.'
            : permission === 'granted' && !onThisDevice && !alertsDeclined()
              ? 'This browser has permission but is not set up to receive anything - ' +
                'press the button below to finish it.'
              : 'Alerts appear even when this page is closed, as long as the browser is running.'}
        </p>
        <div className="mt-2 flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy || !canPush}
            onClick={deviceOn ? disablePush : enablePush}
            className="flex items-center gap-1.5 rounded-lg border border-edge px-2.5 py-1.5 text-2xs text-ink transition-colors hover:bg-panel-2 disabled:opacity-40"
          >
            {busy ? (
              <Loader2 size={12} className="animate-spin" />
            ) : deviceOn ? (
              <BellOff size={12} />
            ) : (
              <Bell size={12} />
            )}
            {deviceOn ? 'Stop alerting this device' : 'Alert this device'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={test}
            className="flex items-center gap-1.5 rounded-lg border border-edge px-2.5 py-1.5 text-2xs text-dim transition-colors hover:text-ink disabled:opacity-40"
          >
            <Send size={12} /> Send a test
          </button>
        </div>
      </div>

      <button
        type="button"
        disabled={busy}
        onClick={save}
        className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-40"
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
        Save alerts
      </button>

      {/*
        The receipt. "Did I get told?" is answerable from a phone in a car
        park, and so is the follow-up nobody could answer before: if not, why
        not. The words come from the server - the browser is never handed a
        delivery map to interpret.
      */}
      <div>
        <span className="eyebrow mb-1.5 block">What you have been told lately</span>
        {recent === null ? (
          <p className="text-2xs leading-relaxed text-faint">
            This list could not be loaded just now. Your alert settings above are
            unaffected.
          </p>
        ) : recent.length === 0 ? (
          <p className="text-2xs leading-relaxed text-faint">
            Nothing in the past week. This fills in as things happen - it is not a
            sign anything is wrong.
          </p>
        ) : (
          <ul className="space-y-1">
            {recent.slice(0, 12).map((item) => {
              const look = OUTCOME[item.status] || OUTCOME.trying
              const Icon = look.icon
              return (
                <li
                  key={item.id}
                  className={`flex items-start gap-2 rounded-lg px-2 py-1.5 ring-1 ring-inset ${look.bg} ${look.ring}`}
                >
                  <Icon size={12} className={`mt-0.5 shrink-0 ${look.tone}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-x-2">
                      <span className="text-2xs font-semibold text-ink">{item.title}</span>
                      <span className="text-2xs text-faint">{clockOf(item.created_at)}</span>
                    </div>
                    <p className={`mt-0.5 text-2xs leading-relaxed ${look.tone}`}>
                      {item.summary}
                      {item.detail ? ` - ${item.detail}` : ''}
                    </p>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </section>
  )
}

