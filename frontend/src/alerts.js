import { api } from './api.js'

/**
 * Getting a browser subscribed, with as little ceremony as the browser allows.
 *
 * The ceremony is not ours. Every browser requires a real click before it will
 * even ask about notifications: Firefox and Safari reject `requestPermission`
 * outright when there was no user gesture, and Chrome answers an unprompted
 * request by quietly blocking it. A block is sticky — once it is recorded, the
 * click that would have worked no longer does, and the person has to go into
 * site settings to undo it. So asking without a click is not a shortcut past
 * the click; it is a way of destroying it.
 *
 * What can be done without one, and is done here: if this browser has already
 * granted permission, resubscribe it silently on every load. That covers the
 * cases that actually go wrong in practice — a new tab, a cleared service
 * worker, a subscription the push service rotated, a second device where the
 * person already said yes once. They agreed once and it keeps working.
 *
 * The channel that needs no permission and no click at all is email. That is
 * the one to lean on for somebody who will never read a prompt.
 */

// Set only when a person deliberately turns this device off. Without it the
// silent resubscribe below would undo their choice on the very next reload,
// which is worse than not having the feature.
const DECLINED = 'pingpulse.alerts.off'

export function alertsDeclined() {
  try {
    return localStorage.getItem(DECLINED) === '1'
  } catch {
    return false
  }
}

export function rememberDeclined(declined) {
  try {
    if (declined) localStorage.setItem(DECLINED, '1')
    else localStorage.removeItem(DECLINED)
  } catch {
    // A browser with storage blocked still works; it just forgets the choice.
  }
}

function supported() {
  return typeof Notification !== 'undefined' && 'serviceWorker' in navigator
}

/**
 * The VAPID key crosses the wire base64url-encoded and `subscribe` wants raw
 * bytes. Browsers will not convert it for you, and a key one padding character
 * out fails as an opaque InvalidCharacterError.
 */
function keyBytes(base64) {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalised = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(normalised)
  return Uint8Array.from([...raw].map((char) => char.charCodeAt(0)))
}

/** Just enough to tell two of your own devices apart in a list. */
export function shortBrowserName() {
  const agent = navigator.userAgent || ''
  const browser = /Edg\//.test(agent)
    ? 'Edge'
    : /Firefox\//.test(agent)
      ? 'Firefox'
      : /Chrome\//.test(agent)
        ? 'Chrome'
        : /Safari\//.test(agent)
          ? 'Safari'
          : 'Browser'
  const platform = /Android/.test(agent)
    ? 'Android'
    : /iPhone|iPad/.test(agent)
      ? 'iOS'
      : /Mac/.test(agent)
        ? 'Mac'
        : /Windows/.test(agent)
          ? 'Windows'
          : ''
  return [browser, platform].filter(Boolean).join(' on ')
}

async function register(vapidKey) {
  const registration = await navigator.serviceWorker.register(
    new URL('sw.js', window.location.href),
    { scope: './' },
  )
  await navigator.serviceWorker.ready

  // Reuse whatever this browser already holds. Subscribing again with a
  // different key silently fails in some browsers and leaves a second
  // endpoint in others.
  const existing = await registration.pushManager.getSubscription()
  const subscription =
    existing ||
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: keyBytes(vapidKey),
    }))

  await api.subscribePush({ ...subscription.toJSON(), label: shortBrowserName() })
  return subscription
}

/**
 * Subscribe without asking anybody anything.
 *
 * Returns false and does nothing at all unless permission has already been
 * granted. Safe to call on every load — it is idempotent, and the server
 * replaces a repeat subscription rather than accumulating one.
 */
export async function subscribeQuietly(settings) {
  try {
    if (!supported() || Notification.permission !== 'granted') return false
    if (alertsDeclined()) return false

    const found = settings || (await api.notificationSettings())
    if (!found?.push_available || !found?.vapid_public_key) return false

    await register(found.vapid_public_key)
    return true
  } catch {
    // Never surfaced. This runs unbidden on page load, and a browser that
    // cannot do push is not an error the operator needs to read about.
    return false
  }
}

/**
 * Is *this* browser actually subscribed right now?
 *
 * Permission is not the answer. A browser can hold permission and have no
 * subscription at all - the service worker was cleared, the push service
 * dropped it, or the silent resubscribe failed quietly on load, which it is
 * designed to do. The panel read permission alone and so offered to "stop
 * alerting this device" while reporting that no device was set up: the two
 * halves of one sentence disagreeing, with the truth in the unhelpful half.
 */
export async function deviceSubscribed() {
  try {
    if (!supported() || Notification.permission !== 'granted') return false
    const registration = await navigator.serviceWorker.getRegistration()
    const subscription = await registration?.pushManager?.getSubscription()
    return Boolean(subscription)
  } catch {
    return false
  }
}

/** Ask, then subscribe. Must be called from a real click. */
export async function subscribeWithPrompt(settings) {
  if (!supported()) {
    throw new Error('This browser cannot show alerts. Try Chrome, Edge or Firefox.')
  }
  if (!settings?.vapid_public_key) {
    throw new Error('Alerts are not set up on the server yet.')
  }

  const granted = await Notification.requestPermission()
  if (granted !== 'granted') {
    throw new Error(
      'Your browser blocked alerts. Allow notifications for this site in your browser settings, then try again.',
    )
  }

  await register(settings.vapid_public_key)
  rememberDeclined(false)
  return granted
}

/** Stop alerting this device, and remember that it was deliberate. */
export async function unsubscribe() {
  rememberDeclined(true)
  const registration = await navigator.serviceWorker?.getRegistration()
  const subscription = await registration?.pushManager?.getSubscription()
  if (subscription) {
    await api.unsubscribePush(subscription.endpoint)
    await subscription.unsubscribe()
  }
}
