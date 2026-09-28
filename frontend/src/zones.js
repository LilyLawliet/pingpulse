/**
 * Turning what somebody types into a timezone the server will accept.
 *
 * A shop owner knows they are in Karachi, or Miami, or "UAE". Very few know
 * that is Asia/Karachi, America/New_York and Asia/Dubai, and a dropdown of four
 * hundred names in that spelling is not a way to find out. So the box takes
 * what they would say, and this works out which zone they mean - checked
 * against the browser's own timezone database, so a name is only accepted if
 * the clock can actually be read in it. The server checks again on save.
 */

// The timezone list the browser already has, with a short fallback for one too
// old to say.
const FALLBACK_ZONES = [
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Sao_Paulo', 'Europe/London', 'Europe/Dublin', 'Europe/Paris',
  'Europe/Berlin', 'Europe/Madrid', 'Europe/Istanbul', 'Africa/Lagos',
  'Africa/Johannesburg', 'Africa/Cairo', 'Asia/Dubai', 'Asia/Karachi',
  'Asia/Kolkata', 'Asia/Dhaka', 'Asia/Singapore', 'Asia/Tokyo',
  'Australia/Sydney', 'UTC',
]

export const ZONES = (() => {
  try {
    const all = Intl.supportedValuesOf('timeZone')
    return all?.length ? all : FALLBACK_ZONES
  } catch {
    return FALLBACK_ZONES
  }
})()

/** What this machine is set to - nearly always the right answer. */
export const detectedZone = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
})()

/**
 * Places people name that are not the city a zone is named after: countries,
 * states, other cities in the same zone, and the abbreviations people use.
 * Only unambiguous ones - "USA" or "CST" could be several zones, and a guess
 * would book somebody at the wrong hour without anybody noticing.
 */
const ALIASES = {
  // Pakistan
  pakistan: 'Asia/Karachi', pk: 'Asia/Karachi', pkt: 'Asia/Karachi', lahore: 'Asia/Karachi',
  islamabad: 'Asia/Karachi', rawalpindi: 'Asia/Karachi', faisalabad: 'Asia/Karachi',
  multan: 'Asia/Karachi', peshawar: 'Asia/Karachi', sialkot: 'Asia/Karachi', quetta: 'Asia/Karachi',
  // Gulf
  uae: 'Asia/Dubai', 'united arab emirates': 'Asia/Dubai', 'abu dhabi': 'Asia/Dubai',
  sharjah: 'Asia/Dubai', ajman: 'Asia/Dubai', gst: 'Asia/Dubai',
  'saudi arabia': 'Asia/Riyadh', saudi: 'Asia/Riyadh', ksa: 'Asia/Riyadh',
  jeddah: 'Asia/Riyadh', mecca: 'Asia/Riyadh', makkah: 'Asia/Riyadh', medina: 'Asia/Riyadh',
  dammam: 'Asia/Riyadh', doha: 'Asia/Qatar', qatar: 'Asia/Qatar', oman: 'Asia/Muscat',
  bahrain: 'Asia/Bahrain', manama: 'Asia/Bahrain', kuwait: 'Asia/Kuwait',
  // South Asia
  india: 'Asia/Kolkata', ist: 'Asia/Kolkata', delhi: 'Asia/Kolkata', 'new delhi': 'Asia/Kolkata',
  mumbai: 'Asia/Kolkata', bombay: 'Asia/Kolkata', bangalore: 'Asia/Kolkata',
  bengaluru: 'Asia/Kolkata', chennai: 'Asia/Kolkata', hyderabad: 'Asia/Kolkata',
  calcutta: 'Asia/Kolkata', bangladesh: 'Asia/Dhaka', 'sri lanka': 'Asia/Colombo',
  nepal: 'Asia/Kathmandu',
  // United States, by the place rather than the letters
  miami: 'America/New_York', florida: 'America/New_York', orlando: 'America/New_York',
  tampa: 'America/New_York', atlanta: 'America/New_York', boston: 'America/New_York',
  washington: 'America/New_York', 'washington dc': 'America/New_York',
  philadelphia: 'America/New_York', 'new jersey': 'America/New_York',
  eastern: 'America/New_York', est: 'America/New_York', edt: 'America/New_York',
  texas: 'America/Chicago', houston: 'America/Chicago', dallas: 'America/Chicago',
  austin: 'America/Chicago', central: 'America/Chicago',
  california: 'America/Los_Angeles', 'san francisco': 'America/Los_Angeles',
  'san diego': 'America/Los_Angeles', seattle: 'America/Los_Angeles',
  pacific: 'America/Los_Angeles', pst: 'America/Los_Angeles', pdt: 'America/Los_Angeles',
  'las vegas': 'America/Los_Angeles', arizona: 'America/Phoenix',
  // Europe, Africa
  uk: 'Europe/London', 'united kingdom': 'Europe/London', england: 'Europe/London',
  britain: 'Europe/London', manchester: 'Europe/London', birmingham: 'Europe/London',
  bst: 'Europe/London', ireland: 'Europe/Dublin', france: 'Europe/Paris',
  germany: 'Europe/Berlin', spain: 'Europe/Madrid', italy: 'Europe/Rome',
  turkey: 'Europe/Istanbul', turkiye: 'Europe/Istanbul', ankara: 'Europe/Istanbul',
  egypt: 'Africa/Cairo', nigeria: 'Africa/Lagos', abuja: 'Africa/Lagos',
  'south africa': 'Africa/Johannesburg', 'cape town': 'Africa/Johannesburg',
  kenya: 'Africa/Nairobi',
}

const plain = (value) =>
  (value || '')
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase()
    .replace(/[_\-/,.]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()

/** A zone the browser can read the clock in, as its canonical name, or null. */
export function realZone(name) {
  if (!name) return null
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: name }).resolvedOptions().timeZone
  } catch {
    return null
  }
}

/** The city part of a zone name, as somebody would type it: "New York". */
export function cityOf(zone) {
  return (zone.split('/').pop() || zone).replace(/_/g, ' ')
}

/**
 * What they typed, as a timezone.
 *
 * Returns `{ zone, how }` when it is unambiguous - `how` says what it was read
 * as, so the page can say "Lahore is in Asia/Karachi" - or `{ zone: null,
 * suggestions }` when it is not, with the zones that come closest.
 */
export function resolveZone(input) {
  const typed = (input || '').trim()
  const key = plain(typed)
  if (!key) return { zone: null, suggestions: [] }

  // Typed exactly, in any case: "asia/karachi", "America/New_York".
  const exact = ZONES.find((zone) => zone.toLowerCase() === typed.toLowerCase().replace(/ /g, '_'))
  if (exact) return { zone: exact, how: 'name' }

  if (ALIASES[key]) return { zone: ALIASES[key], how: 'place' }

  // A city a zone is named after: "karachi", "new york", "sao paulo".
  const byCity = ZONES.filter((zone) => plain(cityOf(zone)) === key)
  if (byCity.length === 1) return { zone: byCity[0], how: 'city' }

  // A fixed offset: "UTC+5", "GMT-4", "+3". Only whole hours - that is all the
  // Etc zones can say - and they do not change for daylight saving.
  const offset = typed.toLowerCase().replace(/\s/g, '').match(/^(?:utc|gmt)?([+-])(\d{1,2})(?::?00)?$/)
  if (offset) {
    const hours = Number(offset[2])
    if (hours <= 14) {
      // The Etc names are inverted by POSIX convention: UTC+5 is Etc/GMT-5.
      const zone = hours === 0 ? 'UTC' : `Etc/GMT${offset[1] === '+' ? '-' : '+'}${hours}`
      if (realZone(zone)) return { zone, how: 'offset' }
    }
  }

  // Anything else the browser recognises, as long as it is a place/name pair
  // like the old "US/Eastern". A bare abbreviation it happens to know - "CST"
  // is Chicago to it and China to a billion people - is not taken on trust.
  const known = typed.includes('/') ? realZone(typed) : null
  if (known) return { zone: known, how: 'name' }

  if (byCity.length > 1) return { zone: null, suggestions: byCity }

  // Close to a name: "york" is part of New York.
  const containing = ZONES.filter((zone) => plain(zone).includes(key))
    .sort((a, b) => a.length - b.length)
    .slice(0, 5)
  if (containing.length) return { zone: null, suggestions: containing }

  // A typing slip: "Karachy", "Dubia", "Lahor". Within two letters of a city a
  // zone is named after, or of a place above, for anything long enough that
  // two letters is a slip rather than a different word.
  if (key.length < 4) return { zone: null, suggestions: [] }
  const near = new Map()
  const consider = (name, zone) => {
    const distance = editDistance(key, name)
    if (distance <= 2 && (!near.has(zone) || near.get(zone) > distance)) near.set(zone, distance)
  }
  for (const zone of ZONES) consider(plain(cityOf(zone)), zone)
  for (const [place, zone] of Object.entries(ALIASES)) if (place.length >= 4) consider(place, zone)
  const suggestions = [...near.entries()]
    .sort((a, b) => a[1] - b[1])
    .map(([zone]) => zone)
    .slice(0, 5)
  return { zone: null, suggestions }
}

/** How many single-letter changes turn one word into the other. */
function editDistance(a, b) {
  if (Math.abs(a.length - b.length) > 2) return 3
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i)
  for (let i = 1; i <= a.length; i += 1) {
    const current = [i]
    for (let j = 1; j <= b.length; j += 1) {
      current[j] = Math.min(
        previous[j] + 1,
        current[j - 1] + 1,
        previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      )
    }
    previous = current
  }
  return previous[b.length]
}

/** The time in a zone right now, so a wrong one is visible before it books anybody. */
export function localTime(zone) {
  try {
    return new Intl.DateTimeFormat(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      timeZone: zone,
    }).format(new Date())
  } catch {
    return '—'
  }
}
