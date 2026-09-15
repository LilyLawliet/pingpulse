import { useCallback, useEffect, useState } from 'react'
import { Bell, BellOff, Check, Loader2, Send, TriangleAlert } from 'lucide-react'
import { api } from '../api.js'
import {
  alertsDeclined,
  subscribeWithPrompt,
  unsubscribe as unsubscribeDevice,
} from '../alerts.js'

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
export default function NotificationSettings() {
  const [state, setState] = useState(null)
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
      setError(err.message)
      setState(null)
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
    } catch (err) {
      setError(err.message)
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
    } catch (err) {
      setError(err.message)
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
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const test = async () => {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const result = await api.testNotification()
      const push = result.delivery?.push
      const email = result.delivery?.email
      setNote(`Sent. Push: ${push}. Email: ${email}.`)
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const canPush = state.push_available && permission !== 'unsupported'
  // Granted-and-not-deliberately-turned-off is what "this device is set
  // up" actually means. Permission alone stays granted after somebody
  // switches it off, so the button would offer to stop something that
  // had already stopped.
  const deviceOn = permission === 'granted' && !alertsDeclined()

  return (
    <section className="space-y-3">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-accent/12 ring-1 ring-inset ring-accent/25">
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
          {canPush
            ? 'Alerts appear even when this page is closed, as long as the browser is running.'
            : 'This server has no alert keys set up, so alerts cannot be sent to a browser yet.'}
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
        className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
        Save alerts
      </button>
    </section>
  )
}

