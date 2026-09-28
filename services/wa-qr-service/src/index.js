/**
 * PingPulse — WhatsApp Web session bridge.
 *
 * Holds one paired WhatsApp Web session per tenant channel and translates
 * between it and the PingPulse API:
 *
 *   inbound   WhatsApp message  ->  POST /api/v1/whatsapp/qr-inbound
 *   outbound  POST /send        ->  WhatsApp message
 *   pairing   WebSocket /ws/wa-qr streams the QR and status changes
 *
 * The payloads are deliberately shaped like Twilio's, because the API feeds
 * both providers into the same pipeline. Nothing downstream knows or cares
 * which transport a message arrived on.
 *
 * Auth credentials are written to disk under SESSIONS_DIR, which is a mounted
 * volume, so a paired phone survives a container restart without re-scanning.
 *
 * This uses an unofficial WhatsApp Web client, so it is a stand-in until
 * official API access is in place rather than a permanent transport.
 */

import { existsSync, mkdirSync, readdirSync, rmSync } from 'node:fs'
import { createServer } from 'node:http'
import path from 'node:path'

import makeWASocket, {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import express from 'express'
import pino from 'pino'
import QRCode from 'qrcode'
import { WebSocketServer } from 'ws'

const PORT = Number(process.env.PORT || 3100)
const API_URL = process.env.PINGPULSE_API_URL || 'http://backend:8000'
const SHARED_SECRET = process.env.WA_QR_SHARED_SECRET || ''
const SESSIONS_DIR = process.env.SESSIONS_DIR || '/data/wa_sessions'
// How long /send waits for a session that is still coming back before giving
// up and letting the API queue the message. A restore after a deploy takes a
// few seconds, so waiting briefly here turns most restarts into a short pause
// rather than a queued retry.
const SEND_WAIT_MS = Number(process.env.SEND_WAIT_MS || 12000)

const log = pino({ level: process.env.LOG_LEVEL || 'info' })

mkdirSync(SESSIONS_DIR, { recursive: true })

/**
 * The WhatsApp Web version to announce, fetched rather than assumed.
 *
 * Baileys ships a hard-coded version number, and WhatsApp closes the
 * handshake on one it considers too old - before the exchange ever reaches
 * the point where a QR is offered. The symptom is a socket that times out
 * with a 408 a second after opening, retried forever, with no QR and nothing
 * in the log naming a cause. Pairing simply never worked, and the only
 * sessions that still connected were ones already holding credentials.
 *
 * So the current version is asked for. Cached for an hour because it changes
 * about weekly and a pairing should not wait on a network call it could
 * reuse, and falling back to whatever the library believes rather than
 * refusing to start: a stale guess sometimes works, and no attempt never
 * does.
 */
let waVersion = null
let waVersionAt = 0
const WA_VERSION_TTL_MS = 60 * 60 * 1000

async function whatsappVersion() {
  if (waVersion && Date.now() - waVersionAt < WA_VERSION_TTL_MS) return waVersion
  try {
    const { version, isLatest } = await fetchLatestBaileysVersion()
    waVersion = version
    waVersionAt = Date.now()
    log.info({ version: version.join('.'), isLatest }, 'announcing this WhatsApp Web version')
  } catch (error) {
    log.warn(
      { err: error?.message },
      'could not look up the current WhatsApp Web version — using the built-in one',
    )
  }
  return waVersion
}

/**
 * Sessions that have reached an open connection at least once since boot.
 *
 * The difference between "this phone is paired and the network wobbled" and
 * "this pairing has never worked". The first deserves retrying for as long as
 * it takes; the second deserves an answer, because retrying in silence is how
 * a broken pairing looked like a slow one for days.
 */
const opened = new Set()
/** Consecutive failed starts for a session that has never opened. */
const attempts = new Map()
const MAX_PAIRING_ATTEMPTS = 5

// Ceiling on the wait between reconnects of an already-paired phone. Five
// minutes is slow enough to be invisible to WhatsApp during a long outage and
// fast enough that a client is not offline for an afternoon after a blip.
const RECONNECT_MAX_WAIT_MS = 5 * 60 * 1000

// Pairings that have exhausted their attempts and must not start themselves
// again. Only a person asking - POST /pair - clears one.
//
// Without this a dead pairing was immortal. Giving up deleted its attempt
// count, so the next thing to touch the session got a fresh five tries, and
// the websocket watcher called startSession on every connect - so a browser
// tab left open on the pairing screen restarted a doomed pairing every few
// minutes, for days. Two of them ran from the 25th to the 27th of September.
//
// That is not merely wasted work. WhatsApp answered 408 and closed every
// attempt before offering a code, for every session on this host, including
// new ones - and the moment the abandoned pairings were stopped, the very
// next attempt produced a QR in twenty seconds. Backing off is the only lever
// there is here, so a pairing that has given up has to actually stop.
const gaveUp = new Set()

/** Live sessions, keyed by channel id. */
const sessions = new Map()
/**
 * Which sessions have an open connection right now.
 *
 * A socket exists in `sessions` from the moment it is created, but it cannot
 * carry a message until WhatsApp reports the connection open. Sending in
 * between throws, which the API would read as a failure worth queueing when
 * waiting a moment would have done.
 */
const ready = new Set()
/** Sockets watching a pairing, keyed by channel id. */
const watchers = new Map()
/**
 * LID -> phone number, for everyone WhatsApp has told us about.
 *
 * WhatsApp addresses more and more chats by LID (a privacy identifier like
 * 153231615328393@lid) and only sometimes attaches the real number to the
 * message. When it does not, the shop owner would otherwise see a fifteen-digit
 * identifier rendered as a phone number that cannot be dialled, and the same
 * person appears twice the moment WhatsApp changes how it addresses them.
 *
 * Baileys hands the pairing over on its contact events, where a contact
 * carries both `lid` and `jid`, so this is a cache of what WhatsApp already
 * knows rather than a guess. It is deliberately global — a LID identifies one
 * account everywhere, not one per session — and in memory only, because the
 * API stores the mapping durably once it has seen it.
 */
const lidPhones = new Map()
/**
 * Conversation history WhatsApp pushed to us, keyed by session.
 *
 * WhatsApp sends a chunk of past conversations when a handset pairs, which is
 * how a shop's existing customers can be found at all — the people who asked
 * about a product and never got an answer are already in there.
 *
 * In memory and capped, deliberately. This is the shop's customers' messages,
 * not the shop's own, and the bridge should hold as little of it as possible
 * for as short a time as possible: the API reads it once, decides what matters,
 * and keeps only that. Restarting the service forgets all of it.
 */
const history = new Map()
const HISTORY_CAP = 4000

function rememberHistory(sessionId, messages) {
  const kept = history.get(sessionId) || []
  for (const message of messages || []) {
    const remote = message.key?.remoteJid || ''
    if (remote.endsWith('@g.us') || remote === 'status@broadcast') continue

    const text =
      message.message?.conversation ||
      message.message?.extendedTextMessage?.text ||
      message.message?.imageMessage?.caption ||
      ''
    if (!text.trim()) continue

    kept.push({
      jid: remote,
      fromMe: Boolean(message.key?.fromMe),
      text: text.slice(0, 2000),
      at: Number(message.messageTimestamp) || 0,
      pushName: message.pushName || '',
    })
  }
  // Oldest first out, so a long sync does not push out what just arrived.
  history.set(sessionId, kept.slice(-HISTORY_CAP))
}

/** Digits of a JID: "15323@lid" / "923097209908:3@s.whatsapp.net" -> digits. */
function jidDigits(jid) {
  return String(jid || '').split('@')[0].split(':')[0].replace(/[^0-9]/g, '')
}

/** Learn a LID -> phone pairing from anything that carries both. */
function rememberLid(lid, phone) {
  const key = jidDigits(lid)
  const value = jidDigits(phone)
  if (!key || !value || key === value) return
  if (lidPhones.get(key) !== value) {
    lidPhones.set(key, value)
    log.info({ lid: key, phone: value }, 'learned a LID to phone mapping')
  }
}

/** Record both directions from a Baileys contact, whichever fields it has. */
function rememberContact(contact) {
  if (!contact) return
  const lid = contact.lid || (String(contact.id || '').endsWith('@lid') ? contact.id : '')
  const phone =
    contact.jid || (String(contact.id || '').endsWith('@s.whatsapp.net') ? contact.id : '')
  rememberLid(lid, phone)
}
/**
 * Latest state per session, so a client that arrives mid-pairing can ask
 * for the current QR instead of waiting for the next one to be pushed.
 * A QR rotates every ~20s, so this is short-lived by nature.
 */
const latest = new Map()

// ---------------------------------------------------------------- helpers
function notifyWatchers(sessionId, event) {
  latest.set(sessionId, { ...event, at: Date.now() })
  const listeners = watchers.get(sessionId)
  if (!listeners) return
  const message = JSON.stringify(event)
  for (const socket of listeners) {
    // A dead tab must never break the pairing it was watching.
    try {
      if (socket.readyState === socket.OPEN) socket.send(message)
    } catch (error) {
      log.warn({ error: error.message }, 'could not reach a watcher')
    }
  }
}

async function callApi(pathname, body) {
  try {
    const response = await fetch(`${API_URL}${pathname}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-PingPulse-Bridge': SHARED_SECRET,
      },
      body: JSON.stringify(body),
    })
    if (!response.ok) {
      log.error({ pathname, status: response.status }, 'API rejected the call')
    }
    return response.ok
  } catch (error) {
    log.error({ pathname, error: error.message }, 'API unreachable')
    return false
  }
}

function reportStatus(sessionId, status, extra = {}) {
  notifyWatchers(sessionId, { type: 'status', status, ...extra })
  callApi('/api/v1/whatsapp/qr-status', { sessionId, status, ...extra })
}

// ---------------------------------------------------------------- session
/**
 * Start (or resume) a session for one channel.
 *
 * Called both when a client asks to pair and on boot for every session already
 * on disk, which is what makes a restart invisible to a paired client.
 */
async function startSession(sessionId) {
  if (sessions.has(sessionId)) return sessions.get(sessionId)

  // A pairing that has given up does not restart itself. Everything that
  // reaches this function other than POST /pair is automatic - a websocket
  // reconnecting, a send arriving - and none of those represent somebody
  // waiting at the screen with a phone in their hand.
  if (gaveUp.has(sessionId)) {
    log.info({ sessionId }, 'not restarting a pairing that gave up — ask again to retry')
    return null
  }

  const folder = path.join(SESSIONS_DIR, sessionId)
  const { state, saveCreds } = await useMultiFileAuthState(folder)

  // Whether this phone has ever been scanned. Read once, here, because it is
  // what separates "reconnect this for as long as it takes" from "this
  // pairing is not working, stop asking".
  //
  // `registered` is not the flag to read. A live client's session, scanned
  // minutes earlier and holding a full identity, had registered: false on
  // disk - so a check on it decided a working phone had never been paired,
  // gave up after five reconnects and marked the number unreachable. `me` is
  // the account WhatsApp handed back at pairing; nothing but a real scan puts
  // it there.
  const wasPaired = Boolean(state.creds?.me?.id || state.creds?.registered)

  const version = await whatsappVersion()
  const socket = makeWASocket({
    // Omitted when the lookup failed, so Baileys falls back to its own.
    ...(version ? { version } : {}),
    auth: state,
    logger: pino({ level: 'silent' }),
    // Shown on the phone's linked-devices screen.
    browser: ['PingPulse', 'Chrome', '1.0.0'],
    markOnlineOnConnect: false,
  })

  sessions.set(sessionId, socket)
  // Only a genuinely new pairing needs a QR. Resuming a stored session would
  // otherwise flash "waiting for a scan" at a client who scanned weeks ago.
  if (!wasPaired) {
    reportStatus(sessionId, 'GENERATING_QR')
  }

  socket.ev.on('creds.update', saveCreds)

  // Where the LID to phone-number pairing comes from. WhatsApp syncs contacts
  // on connect and updates them as it learns more, and each one can carry both
  // identifiers.
  // WhatsApp pushes past conversations after a pairing. This is the only way
  // to find a shop's existing customers; nothing is requested, and nothing here
  // sends anything to anyone.
  socket.ev.on('messaging-history.set', ({ messages, contacts, progress, isLatest }) => {
    contacts?.forEach(rememberContact)
    rememberHistory(sessionId, messages)
    log.info(
      { sessionId, received: messages?.length || 0, held: (history.get(sessionId) || []).length, progress, isLatest },
      'history sync',
    )
  })

  socket.ev.on('contacts.upsert', (contacts) => contacts.forEach(rememberContact))
  socket.ev.on('contacts.update', (contacts) => contacts.forEach(rememberContact))

  socket.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update

    if (qr) {
      // Sent as a data URL so the desktop app can render it directly in an
      // <img>, with no QR library of its own.
      const dataUrl = await QRCode.toDataURL(qr, { margin: 1, width: 320 })

      // Notified once, deliberately. Calling reportStatus as well would run
      // notifyWatchers a second time without the QR attached and overwrite the
      // stored copy, so a client polling for it would see QR_READY and an
      // empty payload forever.
      notifyWatchers(sessionId, { type: 'status', status: 'QR_READY', qr: dataUrl })
      // The API only needs the state; sending it a 7 KB image on every
      // twenty-second rotation would be waste.
      callApi('/api/v1/whatsapp/qr-status', { sessionId, status: 'QR_READY' })
    }

    if (connection === 'open') {
      const phoneNumber = socket.user?.id?.split(':')[0]?.split('@')[0] || null
      log.info({ sessionId, phoneNumber }, 'session authenticated')
      ready.add(sessionId)
      // This pairing works, so later drops are worth retrying indefinitely.
      opened.add(sessionId)
      attempts.delete(sessionId)
      latest.delete(sessionId)
      reportStatus(sessionId, 'AUTHENTICATED', { phoneNumber })
    }

    if (connection === 'close') {
      const status = lastDisconnect?.error?.output?.statusCode
      sessions.delete(sessionId)
      ready.delete(sessionId)

      // Logged out from the phone: the credentials are dead and a re-scan is
      // the only way back. Anything else is a dropped connection worth retrying.
      if (status === DisconnectReason.loggedOut) {
        log.warn({ sessionId }, 'logged out on the phone — re-pairing required')

        // Throw the dead credentials away, which is the part that was missing.
        //
        // Reporting the status was not enough: the folder stayed on disk, and
        // it holds more than the `registered` flag. The identity and noise
        // keys in it have been revoked by WhatsApp, and WhatsApp closes a
        // connection that presents revoked keys immediately - before the
        // handshake ever reaches the point where a QR is offered.
        //
        // So every later attempt to pair loaded those same keys, was refused
        // at once, and emitted no QR. The client sat on "asking WhatsApp for a
        // code" indefinitely while this warning repeated in the log, and no
        // amount of clicking could break out of it, because each click
        // rebuilt the session from the very files that guaranteed the
        // refusal. A number in this state could never be re-paired.
        try {
          rmSync(folder, { recursive: true, force: true })
          log.info({ sessionId }, 'cleared the dead credentials — the next attempt pairs fresh')
        } catch (error) {
          // Worth shouting about: the session is now permanently unpairable
          // until somebody removes the folder by hand.
          log.error(
            { sessionId, err: error?.message },
            'could not clear the dead credentials — this session cannot re-pair',
          )
        }

        opened.delete(sessionId)
        attempts.delete(sessionId)
        reportStatus(sessionId, 'DISCONNECTED', { reason: 'logged_out' })
        return
      }

      // A session that has connected before is worth waiting on for as long
      // as it takes - the phone is paired and the network will come back.
      //
      // `opened` only remembers this process, so after a restart a perfectly
      // good paired session looks brand new. Credentials on disk say
      // otherwise: this phone has been scanned, so a drop is a reconnect and
      // never a pairing that should be given up on. Without this the bridge
      // restarted, failed five reconnects, marked a live client's WhatsApp as
      // unreachable and - once giving up became permanent - stayed that way
      // until somebody scanned a QR that was never needed.
      if (opened.has(sessionId) || wasPaired) {
        // Backed off rather than every three seconds forever. This path never
        // gives up, which is right for a paired phone - but retrying at three
        // seconds for hours is exactly the hammering that got this host
        // refused by WhatsApp in the first place, and it would do it again
        // during any outage long enough to matter.
        const tries = (attempts.get(sessionId) || 0) + 1
        attempts.set(sessionId, tries)
        const wait = Math.min(3000 * 2 ** (tries - 1), RECONNECT_MAX_WAIT_MS)
        log.info({ sessionId, status, tries, wait }, 'connection dropped, reconnecting')
        reportStatus(sessionId, 'DISCONNECTED', { reason: 'reconnecting' })
        setTimeout(() => startSession(sessionId).catch(() => {}), wait)
        return
      }

      // A pairing that has never connected is a different thing, and retrying
      // it in silence is what made a WhatsApp version this bridge could not
      // negotiate look like a QR that was merely slow. It retried every three
      // seconds for as long as anybody left the page open, said nothing, and
      // showed the operator a spinner the whole time.
      const tries = (attempts.get(sessionId) || 0) + 1
      attempts.set(sessionId, tries)

      if (tries >= MAX_PAIRING_ATTEMPTS) {
        // Deliberately not cleared here. Resetting the count on the way out
        // is what let the next caller start the whole doomed cycle again.
        gaveUp.add(sessionId)
        log.error(
          { sessionId, status, tries },
          'giving up on this pairing — WhatsApp closed every attempt before offering a code',
        )
        reportStatus(sessionId, 'DISCONNECTED', { reason: 'unreachable' })
        return
      }

      log.info({ sessionId, status, tries }, 'pairing attempt failed, trying again')
      reportStatus(sessionId, 'DISCONNECTED', { reason: 'reconnecting' })
      setTimeout(() => startSession(sessionId).catch(() => {}), 3000)
    }
  })

  socket.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify') return

    for (const message of messages) {
      // Skip our own outgoing messages and system chatter.
      if (message.key.fromMe || !message.message) continue
      const remote = message.key.remoteJid || ''
      if (remote.endsWith('@g.us') || remote === 'status@broadcast') continue

      const text =
        message.message.conversation ||
        message.message.extendedTextMessage?.text ||
        message.message.imageMessage?.caption ||
        ''
      if (!text.trim()) continue

      // WhatsApp increasingly addresses chats by LID — a privacy identifier
      // like 153231615328393@lid — instead of the sender's phone number. Two
      // things follow, and both used to be wrong here.
      //
      // Replying: the JID has to go back exactly as it came. Stripping @lid
      // and rebuilding <digits>@s.whatsapp.net produces an address for a phone
      // number that does not exist. Baileys does not refuse it, so the send
      // reported success and the reply went nowhere — visible in the dashboard
      // as delivered, never on anyone's phone.
      //
      // Identity: a LID is useless to whoever is running the shop, so where
      // WhatsApp does hand over a real number it is preferred for the contact
      // record. The JID travels alongside so the reply still lands.
      const alternative =
        message.key.senderPn || message.key.remoteJidAlt || message.key.participantPn || ''

      // The LID, when this chat is addressed by one. Sent to the API whether or
      // not we can resolve it: it is the only stable identifier for this person
      // across a WhatsApp account switch, so the API uses it to recognise
      // someone it has already met rather than creating a second contact.
      const lidSource = [remote, message.key.participant, message.key.remoteJidAlt]
        .find((candidate) => String(candidate || '').endsWith('@lid'))
      const lid = lidSource ? jidDigits(lidSource) : ''

      // WhatsApp gave us both, so remember it for the messages where it does not.
      if (lid && alternative) rememberLid(lid, alternative)

      const resolved = alternative || (lid ? lidPhones.get(lid) || '' : '')
      const identity = jidDigits(resolved || remote)

      await callApi('/api/v1/whatsapp/qr-inbound', {
        id: message.key.id,
        sessionId,
        from: identity,
        fromLid: lid,
        fromJid: remote,
        to: socket.user?.id?.split(':')[0]?.split('@')[0] || '',
        body: text,
        pushName: message.pushName || '',
        mediaUrls: [],
      })
    }
  })

  return socket
}

// ------------------------------------------------------------------- http
const app = express()
app.use(express.json({ limit: '2mb' }))

// Only the API may drive this service.
app.use((request, response, next) => {
  if (request.path === '/health') return next()
  if (!SHARED_SECRET || request.get('X-PingPulse-Bridge') !== SHARED_SECRET) {
    return response.status(403).json({ ok: false, error: 'forbidden' })
  }
  next()
})

app.get('/health', (_request, response) => {
  response.json({ status: 'ok', sessions: sessions.size })
})

app.get('/session/:id', (request, response) => {
  const sessionId = request.params.id
  const state = latest.get(sessionId)
  response.json({
    ok: true,
    sessionId,
    connected: sessions.has(sessionId),
    status: state?.status || (sessions.has(sessionId) ? 'AUTHENTICATED' : 'UNKNOWN'),
    qr: state?.qr || null,
  })
})

app.post('/pair', async (request, response) => {
  const { sessionId } = request.body || {}
  if (!sessionId) return response.status(400).json({ ok: false, error: 'sessionId required' })
  try {
    // Somebody asked again, so the count of failures before this starts over.
    // Otherwise a pairing that gave up earlier would refuse on the first
    // attempt of every later try, and clicking again would do nothing.
    attempts.delete(sessionId)
    // A person is at the screen asking, which is the only thing that earns a
    // dead pairing another go.
    gaveUp.delete(sessionId)
    await startSession(sessionId)
    response.json({ ok: true })
  } catch (error) {
    log.error({ error: error.message }, 'could not start session')
    response.status(500).json({ ok: false, error: error.message })
  }
})

/**
 * Wait for a session to be usable, starting it if it is not already running.
 *
 * Returns the socket, or null if it did not come up in time. The caller then
 * reports "session not connected", which the API treats as retryable and
 * parks the message rather than losing it.
 */
async function awaitReady(sessionId) {
  if (ready.has(sessionId)) return sessions.get(sessionId)

  // A paired session that is not running yet — the usual case just after a
  // restart — is brought up here rather than waiting for someone to open the
  // pairing screen.
  if (!sessions.has(sessionId)) {
    try {
      await startSession(sessionId)
    } catch (error) {
      log.error({ sessionId, error: error.message }, 'could not start session for a send')
      return null
    }
  }

  const deadline = Date.now() + SEND_WAIT_MS
  while (Date.now() < deadline) {
    if (ready.has(sessionId)) return sessions.get(sessionId)
    await new Promise((resolve) => setTimeout(resolve, 250))
  }
  return null
}

// "typing…" while the agent works on a reply, kept up until the reply goes.
// WhatsApp drops a composing state after roughly 25 seconds, so it is renewed;
// it is also given up after a minute, so a reply that never comes does not
// leave the customer watching "typing…" forever.
const typing = new Map()

function stopTyping(jid) {
  const timer = typing.get(jid)
  if (timer) clearInterval(timer)
  typing.delete(jid)
}

/**
 * Show the customer the message was read and a reply is being written.
 *
 * Called by the API only once it has decided the agent WILL answer - not for
 * an opted-out customer or a chat a person has taken over, where "typing…"
 * would promise a reply that is not coming. Never fails the caller.
 */
app.post('/presence', async (request, response) => {
  const { sessionId, to, toJid, messageId, state } = request.body || {}
  // Only an open connection: typing is not worth waiting for one.
  const socket = ready.has(sessionId) ? sessions.get(sessionId) : null
  if (!socket) return response.json({ ok: false, error: 'session not connected' })
  const jid = toJid || `${String(to).replace(/[^0-9]/g, '')}@s.whatsapp.net`
  try {
    if (messageId) {
      // Blue ticks: the shop has seen it.
      await socket.readMessages([{ remoteJid: jid, id: messageId, fromMe: false }])
    }
    stopTyping(jid)
    if (state === 'composing') {
      await socket.presenceSubscribe(jid).catch(() => {})
      await socket.sendPresenceUpdate('composing', jid)
      const started = Date.now()
      typing.set(
        jid,
        setInterval(() => {
          if (Date.now() - started > 60000) {
            stopTyping(jid)
            socket.sendPresenceUpdate('paused', jid).catch(() => {})
            return
          }
          socket.sendPresenceUpdate('composing', jid).catch(() => {})
        }, 10000),
      )
    } else {
      await socket.sendPresenceUpdate('paused', jid)
    }
    response.json({ ok: true })
  } catch (error) {
    log.warn({ error: error.message }, 'presence update failed')
    response.json({ ok: false, error: error.message })
  }
})

app.post('/send', async (request, response) => {
  const { sessionId, to, toJid, body, mediaUrls } = request.body || {}
  const socket = await awaitReady(sessionId)

  if (!socket) {
    log.warn({ sessionId }, 'send arrived while the session was not connected')
    return response.json({ ok: false, error: 'session not connected' })
  }

  try {
    // Prefer the exact JID the conversation is on. Rebuilding one from digits
    // only works for plain phone-number chats, and silently addresses nobody
    // for a @lid chat.
    const jid = toJid || `${String(to).replace(/[^0-9]/g, '')}@s.whatsapp.net`
    // The reply is here: "typing…" ends with it.
    stopTyping(jid)
    let sent

    if (mediaUrls?.length) {
      // First image carries the text, the rest follow — same shape the Twilio
      // path produces, so a conversation reads identically either way.
      sent = await socket.sendMessage(jid, { image: { url: mediaUrls[0] }, caption: body })
      for (const url of mediaUrls.slice(1)) {
        await socket.sendMessage(jid, { image: { url } })
      }
    } else {
      sent = await socket.sendMessage(jid, { text: body })
    }

    response.json({ ok: true, id: sent?.key?.id || 'sent' })
  } catch (error) {
    log.error({ error: error.message }, 'send failed')
    response.json({ ok: false, error: error.message })
  }
})

/**
 * What this paired account already knows about itself. Read-only.
 *
 * A WhatsApp Business account can carry a product catalogue, and a shop that
 * has one has already done the work we would otherwise ask them to repeat by
 * uploading a price list. Reading it is the difference between "send us your
 * catalogue" and "we already have it".
 *
 * Three outcomes, and the caller needs to tell them apart:
 *
 *   business: false   a personal WhatsApp account. There is no catalogue to
 *                     read and never will be, so stop asking for one.
 *   catalogue: []     a business account with an empty catalogue. Worth
 *                     pointing them at, because they can fill it in WhatsApp.
 *   catalogue: [...]  products we can index.
 *
 * Nothing here writes. `productCreate` and `productUpdate` exist on the same
 * socket and are deliberately not wired up: this reads a shop's own data, and
 * the first version of that should not be able to alter it.
 */
app.get('/catalog/:sessionId', async (request, response) => {
  const sessionId = request.params.sessionId
  const socket = await awaitReady(sessionId)

  if (!socket) {
    return response.json({ ok: false, error: 'session not connected' })
  }

  // The paired account's own number, which is whose catalogue we want.
  const own = socket.user?.id
  const jid = `${String(own || '').split(':')[0].split('@')[0]}@s.whatsapp.net`

  // WhatsApp does not answer "no" to these questions — it does not answer at
  // all. Asking a personal account for its catalogue hangs until Baileys' own
  // query timeout fires around two minutes later, which is far too long for a
  // panel someone is watching, and long enough that they assume it is broken.
  // Silence for this many seconds is taken as the answer it is.
  const ASK_MS = 8000
  const ask = (promise) =>
    Promise.race([
      promise,
      new Promise((resolve) => setTimeout(() => resolve(undefined), ASK_MS)),
    ]).catch(() => undefined)

  const profile = (await ask(socket.getBusinessProfile(jid))) || null
  const catalogue = await ask(socket.getCatalog({ jid, limit: 100 }))

  const products = catalogue?.products || []
  const truncated = Boolean(catalogue?.nextPageCursor)
  if (!catalogue) {
    log.info({ sessionId }, 'no catalogue answer within the wait — treating as none')
  }

  response.json({
    ok: true,
    business: Boolean(profile),
    profile: profile
      ? {
          description: profile.description || '',
          category: profile.category || '',
          email: profile.email || '',
          website: (profile.website || [])[0] || '',
          address: profile.address || '',
        }
      : null,
    truncated,
    // Passed through close to as WhatsApp gave them. Interpreting the price is
    // the API's job, not the bridge's — the bridge should not be the place a
    // currency assumption is buried.
    products: products.map((p) => ({
      id: p.id,
      retailerId: p.retailerId || '',
      name: p.name || '',
      description: p.description || '',
      price: p.price,
      currency: p.currency || '',
      availability: p.availability || '',
      url: p.url || '',
      images: Object.values(p.imageUrls || {}).filter(Boolean).slice(0, 3),
    })),
  })
})

/**
 * The conversations WhatsApp pushed us for this session. Read-only.
 *
 * Returned as flat messages with their chat, and nothing more: who said what,
 * when, and which way round. Deciding which of them matter is the API's job,
 * because that decision depends on the shop's own catalogue, and the bridge
 * has no business knowing about that.
 */
app.get('/history/:sessionId', (request, response) => {
  const sessionId = request.params.sessionId
  const messages = history.get(sessionId) || []

  const chats = new Map()
  for (const message of messages) {
    const chat = chats.get(message.jid) || { jid: message.jid, pushName: '', messages: [] }
    if (!chat.pushName && message.pushName) chat.pushName = message.pushName
    chat.messages.push({
      fromMe: message.fromMe,
      text: message.text,
      at: message.at,
    })
    chats.set(message.jid, chat)
  }

  for (const chat of chats.values()) chat.messages.sort((a, b) => a.at - b.at)

  response.json({
    ok: true,
    synced: messages.length > 0,
    chats: [...chats.values()],
  })
})

app.post('/logout', async (request, response) => {
  const { sessionId } = request.body || {}
  const socket = sessions.get(sessionId)
  if (socket) {
    try {
      await socket.logout()
    } catch {
      /* already gone */
    }
    sessions.delete(sessionId)
    ready.delete(sessionId)
  }
  reportStatus(sessionId, 'DISCONNECTED', { reason: 'logged_out' })
  response.json({ ok: true })
})

// -------------------------------------------------------------- websocket
const server = createServer(app)
const wss = new WebSocketServer({ server, path: '/ws/wa-qr' })

wss.on('connection', (socket, request) => {
  const url = new URL(request.url, 'http://localhost')
  const sessionId = url.searchParams.get('sessionId')
  const secret = url.searchParams.get('secret')

  // Browsers cannot set headers on a websocket handshake, so the secret comes
  // in the query string — same reason the dashboard's own socket does it.
  if (!sessionId || !SHARED_SECRET || secret !== SHARED_SECRET) {
    socket.close(1008, 'forbidden')
    return
  }

  if (!watchers.has(sessionId)) watchers.set(sessionId, new Set())
  watchers.get(sessionId).add(socket)

  socket.send(
    JSON.stringify({
      type: 'status',
      status: sessions.has(sessionId) ? 'AUTHENTICATED' : 'GENERATING_QR',
    }),
  )

  // Watching is not asking. A tab left open on the pairing screen reconnects
  // its websocket every few minutes, and starting a pairing on each of those
  // is what kept two abandoned sessions hammering WhatsApp for two days. The
  // client asks for a pairing through POST /pair; this only watches one.
  if (!sessions.has(sessionId) && !gaveUp.has(sessionId)) {
    startSession(sessionId).catch((error) =>
      log.error({ error: error.message }, 'pairing failed'),
    )
  }

  socket.on('close', () => {
    watchers.get(sessionId)?.delete(socket)
  })
})

/**
 * Bring every already-paired session back up.
 *
 * Credentials are on a mounted volume precisely so a restart is invisible to a
 * client, but that only holds if something reconnects them. Without this the
 * sessions map is empty after a deploy, /send finds nothing, and replies are
 * silently not delivered.
 */
async function restoreSessions() {
  let folders = []
  try {
    folders = readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
  } catch (error) {
    log.warn({ error: error.message }, 'could not read the sessions directory')
    return
  }

  // A folder without creds.json was started but never scanned; reconnecting it
  // would only produce a QR nobody is watching.
  const paired = folders.filter((name) =>
    existsSync(path.join(SESSIONS_DIR, name, 'creds.json')),
  )

  log.info({ found: folders.length, paired: paired.length }, 'restoring sessions')
  for (const sessionId of paired) {
    try {
      await startSession(sessionId)
      log.info({ sessionId }, 'session restored')
    } catch (error) {
      log.error({ sessionId, error: error.message }, 'could not restore session')
    }
  }
}

server.listen(PORT, '0.0.0.0', async () => {
  log.info({ port: PORT, api: API_URL }, 'wa-qr-service listening')
  await restoreSessions()
})
