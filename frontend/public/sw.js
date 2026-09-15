/* The bit of PingPulse that runs when nothing else is running.
 *
 * The agent answers customers whether or not anybody has the dashboard open —
 * that is the whole point of it. The cost is that when a customer demands a
 * manager at nine at night and the agent correctly hands the conversation
 * over, nobody finds out until somebody happens to open a tab. This file is
 * what closes that gap: the browser keeps it alive after every tab is shut,
 * and the push service wakes it.
 *
 * Deliberately tiny and deliberately not a cache. A service worker that
 * caches the dashboard would keep serving yesterday's bundle after a deploy,
 * and the API it talks to would have moved on. Push only.
 */

self.addEventListener('install', (event) => {
  // Take over immediately rather than waiting for every old tab to close.
  // A shop that has just switched alerts on should not have to restart the
  // browser before the first one arrives.
  event.waitUntil(self.skipWaiting())
})

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener('push', (event) => {
  // Anything could arrive here — an empty push is a legitimate way for a
  // server to wake a worker, and a malformed one must not throw and lose a
  // real alert queued behind it.
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = { title: 'PingPulse', body: event.data ? event.data.text() : '' }
  }

  const title = data.title || 'PingPulse'
  const options = {
    body: data.body || '',
    icon: './logo-256.png',
    badge: './logo-128.png',
    // Two alerts about the same conversation replace each other rather than
    // stacking. The server already collapses repeats; this catches the case
    // where an older one is still sitting on the lock screen.
    tag: data.tag || 'pingpulse',
    renotify: true,
    data: { url: data.url || './', event: data.event || null },
    // The events that reach here are the ones somebody chose to be
    // interrupted for, so they stay on screen until they are dealt with.
    requireInteraction: data.event === 'escalation',
  }

  event.waitUntil(self.registration.showNotification(title, options))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = (event.notification.data && event.notification.data.url) || './'

  // Focus a dashboard that is already open rather than opening a second one.
  // Somebody tapping an alert wants the conversation, not another tab.
  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then((windows) => {
        for (const client of windows) {
          if (client.url.includes('/app') && 'focus' in client) return client.focus()
        }
        return self.clients.openWindow ? self.clients.openWindow(target) : undefined
      }),
  )
})
