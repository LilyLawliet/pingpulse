/**
 * Prove the pairing flow works without a phone in hand.
 *
 * Opens the same websocket the desktop app opens and waits for a QR to be
 * emitted. Getting QR_READY with a real payload means everything up to the
 * scan is working — the session starts, Baileys reaches WhatsApp, and the code
 * is streamed back — so a client who then sees nothing has a phone or network
 * problem, not a server one.
 *
 *   docker compose exec -T wa-qr-service node probe.js <sessionId>
 */

import WebSocket from 'ws'

const sessionId = process.argv[2] || `probe-${Date.now()}`
const secret = process.env.WA_QR_SHARED_SECRET || ''
const url = `ws://127.0.0.1:3100/ws/wa-qr?sessionId=${sessionId}&secret=${secret}`

const seen = []
const socket = new WebSocket(url)

const stop = (code, message) => {
  console.log(message)
  try {
    socket.close()
  } catch {
    /* already closed */
  }
  process.exit(code)
}

socket.on('open', () => console.log(`connected, session ${sessionId}`))

socket.on('message', (data) => {
  const event = JSON.parse(data)
  seen.push(event.status)
  console.log(`  event: ${event.status}${event.qr ? `  (qr, ${event.qr.length} chars)` : ''}`)

  if (event.status === 'QR_READY' && event.qr) {
    console.log(`  looks like an image: ${event.qr.startsWith('data:image/png;base64,')}`)
    stop(0, '\nPASS — a scannable QR was generated and streamed.')
  }
})

socket.on('error', (error) => stop(1, `FAIL — ${error.message}`))

setTimeout(() => stop(1, `FAIL — no QR after 60s. Saw: ${seen.join(', ') || 'nothing'}`), 60000)
