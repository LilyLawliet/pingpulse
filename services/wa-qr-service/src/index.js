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

import { existsSync, mkdirSync, readdirSync } from 'node:fs'
import { createServer } from 'node:http'
import path from 'node:path'

import makeWASocket, {
  DisconnectReason,
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

  const folder = path.join(SESSIONS_DIR, sessionId)
  const { state, saveCreds } = await useMultiFileAuthState(folder)

  const socket = makeWASocket({
    auth: state,
    logger: pino({ level: 'silent' }),
    // Shown on the phone's linked-devices screen.
    browser: ['PingPulse', 'Chrome', '1.0.0'],
    markOnlineOnConnect: false,
  })

  sessions.set(sessionId, socket)
  // Only a genuinely new pairing needs a QR. Resuming a stored session would
  // otherwise flash "waiting for a scan" at a client who scanned weeks ago.
  if (!state.creds?.registered) {
    reportStatus(sessionId, 'GENERATING_QR')
  }

  socket.ev.on('creds.update', saveCreds)

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
        reportStatus(sessionId, 'DISCONNECTED', { reason: 'logged_out' })
        return
      }

      log.info({ sessionId, status }, 'connection dropped, reconnecting')
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

      await callApi('/api/v1/whatsapp/qr-inbound', {
        id: message.key.id,
        sessionId,
        from: remote.split('@')[0],
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

app.post('/send', async (request, response) => {
  const { sessionId, to, body, mediaUrls } = request.body || {}
  const socket = await awaitReady(sessionId)

  if (!socket) {
    log.warn({ sessionId }, 'send arrived while the session was not connected')
    return response.json({ ok: false, error: 'session not connected' })
  }

  try {
    const jid = `${String(to).replace(/[^0-9]/g, '')}@s.whatsapp.net`
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

  startSession(sessionId).catch((error) =>
    log.error({ error: error.message }, 'pairing failed'),
  )

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
