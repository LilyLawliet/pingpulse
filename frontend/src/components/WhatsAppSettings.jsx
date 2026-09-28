import { useCallback, useEffect, useState } from 'react'
import {
  Check,
  Cloud,
  Copy,
  Link2,
  Loader2,
  Plug,
  QrCode,
  Smartphone,
  Trash2,
  TriangleAlert,
} from 'lucide-react'
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
  const [provider, setProvider] = useState('TWILIO')
  const [form, setForm] = useState({ phone_number: '', account_sid: '', auth_token: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [copied, setCopied] = useState(false)
  const [pairing, setPairing] = useState(null) // { channelId, status, qr }
  // Whether the other way of connecting is on screen.
  //
  // Somebody scanning a QR is doing one thing, and the Twilio chooser, the
  // number field and two credential boxes sat underneath it the whole time -
  // none of which they need, all of which they have to read past to find out
  // whether the code has arrived yet. A business runs on one method at a
  // time, so the second one is a door, not a panel.
  const [showOtherWay, setShowOtherWay] = useState(false)

  // A business runs on one WhatsApp method, so there is at most one channel.
  const connected = channels?.[0] || null

  // Offer the method they are not already on, so the panel is about switching
  // rather than re-picking what is already true.
  useEffect(() => {
    if (!connected) return
    setProvider(connected.whatsapp_provider === 'QR_SESSION' ? 'TWILIO' : 'QR_SESSION')
  }, [connected?.id, connected?.whatsapp_provider])

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
      if (provider === 'TWILIO' && Boolean(sid) !== Boolean(token)) {
        throw new Error('Enter both the Account SID and the Auth Token, or neither.')
      }

      await api.addChannel({
        channel: 'whatsapp',
        provider: 'twilio',
        // Null for a QR pairing: the handset reports its own number when the
        // session authenticates, and the server refuses a typed one anyway.
        phone_number: provider === 'TWILIO' ? form.phone_number.trim() : null,
        whatsapp_provider: provider,
        // Only meaningful for Twilio; the QR path pairs a phone instead.
        account_sid: provider === 'TWILIO' ? sid || null : null,
        auth_token: provider === 'TWILIO' ? token || null : null,
      })
      setForm({ phone_number: '', account_sid: '', auth_token: '' })
      // A Twilio number is connected the moment the server has it. A QR
      // channel is not, until the phone is scanned - that tick comes below.
      onChanged?.(provider === 'TWILIO' ? { whatsapp: true } : undefined)
      await load()
    } catch (err) {
      setError(err.message || 'Could not connect that number.')
    } finally {
      setBusy(false)
    }
  }


  // While a pairing panel is open, poll for the current QR. WhatsApp rotates
  // the code every twenty seconds or so, so a single fetch would go stale on
  // screen; polling also catches the moment the phone links.
  useEffect(() => {
    if (!pairing?.channelId) return undefined
    // Keeps polling until the *channel* is authenticated, not until the bridge
    // says so. The bridge reporting a live session is the start of the story:
    // it still has to reach the API, and when that call failed the session ran
    // for days with nothing in PingPulse aware of it.
    if (connected?.session_status === 'AUTHENTICATED') return undefined

    let cancelled = false
    const tick = async () => {
      try {
        const state = await api.pairingState(pairing.channelId)
        if (cancelled) return
        setPairing((current) =>
          current && current.channelId === pairing.channelId
            ? { ...current, status: state.status, qr: state.qr }
            : current,
        )
        if (state.status === 'AUTHENTICATED') {
          onChanged?.({ whatsapp: true })
          await load()
        }
      } catch {
        /* the bridge may still be starting; the next tick retries */
      }
    }

    tick()
    const timer = setInterval(tick, 2500)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [pairing?.channelId, connected?.session_status, load, onChanged])

  const beginPairing = async (channelId) => {
    setError(null)
    setPairing({ channelId, status: 'GENERATING_QR', qr: null })
    try {
      await api.startPairing(channelId)
    } catch (err) {
      setError(err.message || 'Could not start pairing.')
      setPairing(null)
    }
  }

  const disconnect = async (id) => {
    setBusy(true)
    try {
      await api.removeChannel(id)
      onChanged?.()
      await load()
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
              className="flex flex-wrap items-center gap-3 rounded-lg border border-edge bg-bg px-3 py-2.5"
            >
              {/*
                A tick for a connection nobody has scanned yet says the job is
                done. Twilio is live the moment it is saved; a QR pairing is
                not live until a phone has been on the other end of it.
              */}
              {channel.whatsapp_provider !== 'QR_SESSION' ||
              channel.session_status === 'AUTHENTICATED' ? (
                <span className="grid h-7 w-7 place-items-center rounded-md bg-accent/12">
                  <Check size={13} className="text-accent" />
                </span>
              ) : (
                <span className="grid h-7 w-7 place-items-center rounded-md bg-warn/12">
                  <QrCode size={13} className="text-warn" />
                </span>
              )}
              <div className="min-w-0">
                {/* A QR channel has no number until the handset reports one,
                    and an empty line here reads as a missing number rather
                    than as one still on its way. */}
                {channel.phone_number ? (
                  <p className="font-mono text-[13px] text-ink">{channel.phone_number}</p>
                ) : (
                  <p className="text-[13px] text-dim">Number arrives with the scan</p>
                )}
                <p className="flex items-center gap-1.5 text-2xs text-faint">
                  {channel.whatsapp_provider === 'QR_SESSION' ? (
                    <>
                      <Smartphone size={11} />
                      {/*
                        "Paired phone" was said in every state, including
                        before any phone had been near it. A connection that
                        has never been scanned is not a paired phone, and
                        calling it one tells the operator a job is done when
                        it has not been started. Whether a number is known is
                        the honest test: it can only have come from a handset.
                      */}
                      {channel.session_status === 'AUTHENTICATED'
                        ? 'Paired phone · session active'
                        : channel.phone_number
                          ? 'Paired phone · disconnected, re-scan needed'
                          : 'Not linked yet · scan the code to connect'}
                    </>
                  ) : (
                    <>
                      <Cloud size={11} />
                      {channel.account_sid
                        ? `Your own Twilio account · ${channel.account_sid.slice(0, 10)}…`
                        : 'Shared PingPulse Twilio account'}
                    </>
                  )}
                </p>
              </div>
              {/* A duplicate handset. Said separately from the session
                  status, because the session may be perfectly alive: this is
                  about two businesses claiming one phone, and the one that
                  scanned last is the one messages reach. */}
              {channel.number_conflict && (
                <p className="order-last flex w-full basis-full items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
                  <TriangleAlert size={12} className="mt-0.5 shrink-0" />
                  <span>
                    This phone is also connected to {channel.number_conflict}. One
                    handset cannot serve two businesses — whichever scanned it last
                    receives the messages. Disconnect it from the other one.
                  </span>
                </p>
              )}

              <div className="ml-auto flex items-center gap-1.5">
                {channel.whatsapp_provider === 'QR_SESSION' &&
                  channel.session_status !== 'AUTHENTICATED' && (
                    <button
                      type="button"
                      onClick={() => beginPairing(channel.id)}
                      className="flex items-center gap-1.5 rounded-lg border border-accent/40 px-2.5 py-1.5 text-2xs font-semibold text-accent transition-colors hover:bg-accent/10"
                    >
                      <QrCode size={12} /> Show QR
                    </button>
                  )}
                <button
                  type="button"
                  onClick={() => disconnect(channel.id)}
                  disabled={busy}
                  className="rounded-lg p-1.5 text-faint transition-colors hover:bg-panel-2 hover:text-crit disabled:opacity-40"
                  aria-label={`Disconnect ${channel.phone_number || 'this connection'}`}
                >
                  <Trash2 size={14} />
                </button>
              </div>

              {pairing?.channelId === channel.id && (
                <div className="mt-3 w-full basis-full border-t border-edge pt-3">
                  {channel.session_status === 'AUTHENTICATED' ? (
                    <p className="flex items-center gap-2 text-xs text-accent">
                      <Check size={14} /> Linked. This phone now sends and receives.
                    </p>
                  ) : pairing.status === 'AUTHENTICATED' ? (
                    /* The bridge has the session; PingPulse has not recorded
                       it yet. Normally a second or two. If it stays here, the
                       callback is failing and nothing will route - which is
                       the state a shop sat in for days while this said the
                       phone was linked and sending. */
                    <p className="flex items-center gap-2 text-xs text-warn">
                      <Loader2 size={13} className="animate-spin" /> Phone scanned.
                      Confirming with PingPulse…
                    </p>
                  ) : pairing.qr ? (
                    <div className="flex items-start gap-4">
                      {/* A white plate: QR readers struggle against a dark UI. */}
                      <img
                        src={pairing.qr}
                        alt="WhatsApp pairing QR code"
                        className="h-40 w-40 shrink-0 rounded-lg bg-white p-2"
                      />
                      <ol className="space-y-1 text-2xs leading-relaxed text-dim">
                        <li>1. Open WhatsApp on the phone</li>
                        <li>2. Settings → Linked devices</li>
                        <li>3. Link a device</li>
                        <li>4. Scan this code</li>
                        <li className="pt-1 text-faint">
                          The code refreshes on its own. You only do this once.
                        </li>
                      </ol>
                    </div>
                  ) : (
                    <p className="flex items-center gap-2 text-xs text-faint">
                      <Loader2 size={13} className="animate-spin" /> Asking WhatsApp for a
                      code…
                    </p>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {/* -------------------------- strategy ------------------------------- */}
      {/* Nothing set up yet means they have to choose, so it opens itself.
          Once something is connected - or a scan is in progress - this is the
          way to the method they did not pick, and stays shut until asked. */}
      {!showOtherWay && (channels?.length > 0 || pairing) && (
        <button
          type="button"
          onClick={() => setShowOtherWay(true)}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-edge bg-bg/50 px-3 py-2.5 text-2xs text-dim transition-colors hover:border-edge-hi hover:text-ink"
        >
          <Cloud size={12} className="text-faint" />
          Connect a different way
        </button>
      )}

      <div
        className={`space-y-3 rounded-lg border border-edge bg-bg/50 p-3.5 ${
          showOtherWay || !(channels?.length > 0 || pairing) ? '' : 'hidden'
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="eyebrow block">Connection strategy</span>
          {showOtherWay && (
            <button
              type="button"
              onClick={() => setShowOtherWay(false)}
              className="text-2xs text-faint transition-colors hover:text-ink"
            >
              Hide
            </button>
          )}
        </div>
        {/* One method at a time, enforced by the API. Saying so here means the
            existing connection disappearing is expected rather than alarming. */}
        {connected ? (
          <p className="flex items-start gap-1.5 text-2xs leading-relaxed text-dim">
            <TriangleAlert size={12} className="mt-0.5 shrink-0 text-warn" />
            <span>
              You are connected over{' '}
              <strong className="text-ink">
                {connected.whatsapp_provider === 'QR_SESSION'
                  ? 'WhatsApp Web'
                  : 'the Twilio API'}
              </strong>
              . Connecting a number here replaces it — a business runs on one
              WhatsApp method at a time.
            </span>
          </p>
        ) : null}
        <div className="grid grid-cols-2 gap-2">
          {[
            {
              id: 'TWILIO',
              icon: Cloud,
              title: 'Twilio Cloud API',
              blurb: 'Official API. Costs per message.',
            },
            {
              id: 'QR_SESSION',
              icon: QrCode,
              title: 'WhatsApp Web QR',
              blurb: 'Link a phone by scanning a QR code.',
            },
          ]
            // The method already connected is not offered again; switching
            // means picking the other one.
            .filter(({ id }) => !connected || connected.whatsapp_provider !== id)
            .map(({ id, icon: Icon, title, blurb }) => (
            <button
              key={id}
              type="button"
              onClick={() => setProvider(id)}
              className={`rounded-lg border p-3 text-left transition-colors ${
                provider === id
                  ? 'border-accent/60 bg-accent/8'
                  : 'border-edge hover:border-edge-hi'
              }`}
            >
              <Icon size={15} className={provider === id ? 'text-accent' : 'text-faint'} />
              <p className="mt-1.5 text-xs font-semibold text-ink">{title}</p>
              <p className="mt-0.5 text-2xs leading-snug text-faint">{blurb}</p>
            </button>
          ))}
        </div>

        {/*
          Only Twilio asks for the number.

          Scanning a QR already identifies the handset - the bridge reads its
          number off the session the moment it authenticates - so asking for it
          first made the operator type a fact the system was about to be told,
          and made that typed copy the one the uniqueness rule was enforced
          against. Two organizations ended up holding one phone, written
          "+923097209908" and "923097209908", and neither spelling collided.
        */}
        {provider === 'TWILIO' && (
          <label className="block">
            <span className="eyebrow mb-1.5 block">WhatsApp number</span>
            <input
              {...field('phone_number')}
              className={inputClass}
              placeholder="+14155238886"
              spellCheck={false}
            />
          </label>
        )}

        <div className={`grid grid-cols-2 gap-3 ${provider === 'TWILIO' ? '' : 'hidden'}`}>
          <label className="block">
            <span className="eyebrow mb-1.5 block">Twilio Account SID</span>
            <input
              {...field('account_sid')}
              className={`${inputClass} font-mono text-[12px]`}
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
              className={`${inputClass} font-mono text-[12px]`}
              placeholder="••••••••"
              autoComplete="off"
            />
          </label>
        </div>

        {provider === 'TWILIO' ? (
          <p className="text-2xs text-faint">
            Leave both blank to send on the shared PingPulse account. Once saved, the
            token is stored on the server and never shown again.
          </p>
        ) : (
          <p className="text-2xs text-faint">
            Nothing to type. A QR code appears here — scan it from WhatsApp on the
            phone you want to use, under Linked devices. The number is read from
            that phone, and you only scan once.
          </p>
        )}

        <button
          type="button"
          onClick={connect}
          disabled={busy || (provider === 'TWILIO' && !form.phone_number.trim())}
          className="flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          <Plug size={13} />{' '}
          {busy
            ? 'Connecting…'
            : connected
              ? 'Replace connection'
              : provider === 'TWILIO'
                ? 'Connect number'
                : 'Show QR code'}
        </button>
      </div>

      {/* ---------------------------- webhook ------------------------------ */}
      {/* Only Twilio calls a webhook. A paired session pushes messages to the
          bridge, which posts them onward — nothing to configure. */}
      <div
        className={`rounded-lg border border-edge bg-bg/50 p-3.5 ${
          channels?.some((c) => c.whatsapp_provider !== 'QR_SESSION') !== false ? '' : 'hidden'
        }`}
      >
        <p className="eyebrow mb-1.5 flex items-center gap-1.5">
          <Link2 size={12} /> Paste this into Twilio
        </p>
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-md bg-panel-2 px-2.5 py-2 font-mono text-[12px] text-ink">
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
