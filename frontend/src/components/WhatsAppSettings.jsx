import { useCallback, useEffect, useState } from 'react'
import { Check, Copy, Link2, Loader2, Plug, Trash2, TriangleAlert } from 'lucide-react'
import { api } from '../api.js'
import { backendOrigin } from '../backend.js'

/**
 * Connect a WhatsApp number, so inbound messages know which business they
 * belong to.
 *
 * Two things have to line up for a message to arrive, and until now both were
 * done by hand on the server. Getting either wrong fails silently — the
 * customer messages, and nothing happens at all — so this screen does both and
 * says plainly when one is missing.
 */
export default function WhatsAppSettings({ onChanged }) {
  const [channels, setChannels] = useState(null)
  const [form, setForm] = useState({ phone_number: '', account_sid: '', auth_token: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [copied, setCopied] = useState(false)

  // Whatever this build talks to, so the URL shown is the one to paste.
  const webhookUrl = `${backendOrigin || window.location.origin}/api/v1/whatsapp/webhook`

  const load = useCallback(async () => {
    try {
      setChannels(await api.listChannels())
    } catch {
      setChannels([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const field = (key) => ({
    value: form[key],
    onChange: (event) => setForm((f) => ({ ...f, [key]: event.target.value })),
  })

  const connect = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // Half a credential pair silently falls back to the platform account,
      // which would send this client's replies from the wrong number.
      const sid = form.account_sid.trim()
      const token = form.auth_token.trim()
      if (Boolean(sid) !== Boolean(token)) {
        throw new Error('Enter both the Account SID and the Auth Token, or neither.')
      }

      await api.addChannel({
        channel: 'whatsapp',
        provider: 'twilio',
        phone_number: form.phone_number.trim(),
        account_sid: sid || null,
        auth_token: token || null,
      })
      setForm({ phone_number: '', account_sid: '', auth_token: '' })
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message || 'Could not connect that number.')
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async (id) => {
    setBusy(true)
    try {
      await api.removeChannel(id)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message || 'Could not disconnect that number.')
    } finally {
      setBusy(false)
    }
  }

  const copyWebhook = async () => {
    try {
      await navigator.clipboard.writeText(webhookUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch {
      /* clipboard blocked — the URL is on screen to copy by hand */
    }
  }

  const inputClass =
    'w-full rounded-lg border border-edge bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-faint focus:border-accent/60'

  return (
    <div className="space-y-4">
      <div>
        <h4 className="flex items-center gap-2 text-sm font-semibold text-ink">
          <Plug size={14} className="text-accent" /> WhatsApp connection
        </h4>
        <p className="mt-0.5 text-2xs text-dim">
          The number customers message, and the Twilio account it sends from.
        </p>
      </div>

      {error && <p className="rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">{error}</p>}

      {/* ---------------------------- connected ---------------------------- */}
      {channels === null ? (
        <p className="flex items-center gap-2 text-xs text-faint">
          <Loader2 size={13} className="animate-spin" /> Checking…
        </p>
      ) : channels.length === 0 ? (
        <p className="flex items-start gap-2 rounded-lg bg-warn/10 px-3 py-2.5 text-xs text-warn">
          <TriangleAlert size={14} className="mt-0.5 shrink-0" />
          <span>
            No number connected yet. Until one is, messages sent to this business have
            nowhere to go and are dropped.
          </span>
        </p>
      ) : (
        <ul className="space-y-2">
          {channels.map((channel) => (
            <li
              key={channel.id}
              className="flex items-center gap-3 rounded-lg border border-edge bg-bg px-3 py-2.5"
            >
              <span className="grid h-7 w-7 place-items-center rounded-md bg-accent/12">
                <Check size={13} className="text-accent" />
              </span>
              <div className="min-w-0">
                <p className="font-mono text-[13px] text-ink">{channel.phone_number}</p>
                <p className="text-2xs text-faint">
                  {channel.account_sid
                    ? `Your own Twilio account · ${channel.account_sid.slice(0, 10)}…`
                    : 'Sending on the shared PingPulse Twilio account'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => disconnect(channel.id)}
                disabled={busy}
                className="ml-auto rounded-lg p-1.5 text-faint transition-colors hover:bg-panel-2 hover:text-crit disabled:opacity-40"
                aria-label={`Disconnect ${channel.phone_number}`}
              >
                <Trash2 size={14} />
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* ------------------------------ add -------------------------------- */}
      <div className="space-y-3 rounded-lg border border-edge bg-bg/50 p-3.5">
        <label className="block">
          <span className="eyebrow mb-1.5 block">WhatsApp number</span>
          <input
            {...field('phone_number')}
            className={inputClass}
            placeholder="+14155238886"
            spellCheck={false}
          />
        </label>

        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="eyebrow mb-1.5 block">Twilio Account SID</span>
            <input
              {...field('account_sid')}
              className={`${inputClass} font-mono text-[11px]`}
              placeholder="AC…"
              spellCheck={false}
              autoComplete="off"
            />
          </label>
          <label className="block">
            <span className="eyebrow mb-1.5 block">Twilio Auth Token</span>
            <input
              {...field('auth_token')}
              type="password"
              className={`${inputClass} font-mono text-[11px]`}
              placeholder="••••••••"
              autoComplete="off"
            />
          </label>
        </div>

        <p className="text-2xs text-faint">
          Leave both blank to send on the shared PingPulse account. Once saved, the token
          is stored on the server and never shown again.
        </p>

        <button
          type="button"
          onClick={connect}
          disabled={busy || !form.phone_number.trim()}
          className="flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          <Plug size={13} /> {busy ? 'Connecting…' : 'Connect number'}
        </button>
      </div>

      {/* ---------------------------- webhook ------------------------------ */}
      <div className="rounded-lg border border-edge bg-bg/50 p-3.5">
        <p className="eyebrow mb-1.5 flex items-center gap-1.5">
          <Link2 size={12} /> Paste this into Twilio
        </p>
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-md bg-panel-2 px-2.5 py-2 font-mono text-[11px] text-ink">
            {webhookUrl}
          </code>
          <button
            type="button"
            onClick={copyWebhook}
            className="flex shrink-0 items-center gap-1.5 rounded-lg border border-edge px-2.5 py-2 text-2xs text-dim transition-colors hover:text-ink"
          >
            {copied ? <Check size={12} className="text-accent" /> : <Copy size={12} />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
        <p className="mt-2 text-2xs leading-relaxed text-faint">
          In Twilio: <strong className="text-dim">Messaging → your sender → When a message
          comes in</strong>. It must match exactly — a different scheme, host or trailing
          slash and every message is rejected.
        </p>
      </div>
    </div>
  )
}
