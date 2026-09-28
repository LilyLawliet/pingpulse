import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, Loader2, MapPin, TriangleAlert } from 'lucide-react'
import { ZONES, cityOf, detectedZone, localTime, resolveZone } from '../zones.js'

const HOW = {
  city: 'city',
  place: 'place',
  name: 'name',
  offset: 'offset',
}

/**
 * Where the business is, typed the way its owner would say it.
 *
 * Checked on every keystroke against the browser's timezone database, and
 * saved by itself once what was typed is unambiguous and has been left alone
 * for a moment - there is no Save button to forget. The server checks the
 * name again, and if it refuses, its reason is shown in place of the tick.
 *
 * `saved` is the zone the server holds; `onSave(zone)` resolves once it has
 * accepted one, or throws with the server's words.
 */
export default function TimezoneField({ saved, onSave, disabled = false }) {
  const current = saved && saved !== 'UTC' ? saved : ''
  const [text, setText] = useState(current ? cityOf(current) : '')
  const [state, setState] = useState(current ? 'saved' : 'idle')
  const [error, setError] = useState(null)
  const timer = useRef(null)
  const latest = useRef(current)

  const result = useMemo(() => resolveZone(text), [text])

  // Somebody else saving it - another tab, the business form - is reflected
  // here unless this box is mid-edit.
  useEffect(() => {
    if (saved && saved !== 'UTC' && saved !== latest.current && state !== 'saving') {
      latest.current = saved
      setText(cityOf(saved))
      setState('saved')
    }
  }, [saved, state])

  useEffect(() => () => clearTimeout(timer.current), [])

  const save = async (zone) => {
    clearTimeout(timer.current)
    if (!zone || disabled) return
    if (zone === latest.current) {
      setState('saved')
      return
    }
    setState('saving')
    setError(null)
    try {
      await onSave(zone)
      latest.current = zone
      setState('saved')
    } catch (err) {
      setState('refused')
      setError(err?.message || 'That timezone was not accepted.')
    }
  }

  const change = (value) => {
    setText(value)
    setError(null)
    const found = resolveZone(value)
    clearTimeout(timer.current)
    if (!found.zone) {
      if (!value.trim()) {
        setState('idle')
        return
      }
      // "Dub" is on its way to Dubai. Only once they stop is it worth saying
      // nothing matches.
      setState('typing')
      timer.current = setTimeout(() => setState('unknown'), 900)
      return
    }
    // Saved once they stop typing, so "Karachi" is not saved as "Kara" on the
    // way - that is not a zone and would never match, but a half-typed city
    // that happens to be one would.
    setState(found.zone === latest.current ? 'saved' : 'found')
    timer.current = setTimeout(() => save(found.zone), 700)
  }

  const zone = result.zone
  const described =
    zone && result.how !== HOW.name && result.how !== HOW.offset
      ? `${text.trim()} is in ${zone}`
      : zone

  return (
    <div className="space-y-3">
      <label className="block">
        <span className="mb-1.5 block text-sm font-medium text-ink">
          Where is your business?
        </span>
        <div className="relative">
          <MapPin
            size={15}
            className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint"
          />
          <input
            value={text}
            disabled={disabled}
            onChange={(event) => change(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault()
                save(resolveZone(text).zone)
              }
            }}
            list="pp-zones"
            spellCheck={false}
            autoComplete="off"
            placeholder="A city or country, like Karachi, Dubai or Miami"
            aria-describedby="pp-zone-status"
            className={`w-full rounded-xl border bg-panel py-2.5 pl-10 pr-10 text-sm text-ink placeholder:text-faint focus:outline-none focus:ring-4 disabled:opacity-60 ${
              state === 'unknown' || state === 'refused'
                ? 'border-crit/50 focus:border-crit/60 focus:ring-crit/10'
                : 'border-edge focus:border-accent/60 focus:ring-accent/10'
            }`}
          />
          <span className="absolute right-3.5 top-1/2 -translate-y-1/2">
            {state === 'saving' && <Loader2 size={15} className="animate-spin text-faint" />}
            {state === 'saved' && <Check size={15} className="text-ok" />}
          </span>
        </div>
        <datalist id="pp-zones">
          {ZONES.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </label>

      {/* One line that always says where things stand, in words. */}
      <div id="pp-zone-status" aria-live="polite" className="text-2xs leading-relaxed">
        {(state === 'idle' || state === 'typing') && (
          <p className="text-faint">
            Type your city or country. It is checked as you type and saved by itself.
          </p>
        )}
        {(state === 'found' || state === 'saving') && zone && (
          <p className="text-dim">
            <span className="font-semibold text-ink">{described}</span> — it is{' '}
            {localTime(zone)} there now. {state === 'saving' ? 'Saving…' : 'Saving in a moment…'}
          </p>
        )}
        {state === 'saved' && (latest.current || zone) && (
          <p className="flex items-center gap-1.5 text-ok">
            <Check size={12} />
            <span>
              Saved as <span className="font-semibold">{latest.current || zone}</span>. It is{' '}
              {localTime(latest.current || zone)} there now — if that is not your clock, change
              it.
            </span>
          </p>
        )}
        {state === 'unknown' && (
          <div className="space-y-2">
            <p className="flex items-start gap-1.5 text-crit">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" />
              <span>
                &ldquo;{text.trim()}&rdquo; is not a timezone we can find.{' '}
                {result.suggestions?.length ? 'Did you mean one of these?' : 'Try the nearest big city instead.'}
              </span>
            </p>
            {latest.current && (
              <p className="text-faint">
                Still saved as <span className="font-semibold text-dim">{latest.current}</span>{' '}
                - nothing changes until what you type is found.
              </p>
            )}
            {result.suggestions?.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {result.suggestions.map((name) => (
                  <button
                    key={name}
                    type="button"
                    onClick={() => {
                      setText(cityOf(name))
                      save(name)
                    }}
                    className="rounded-full border border-edge px-2.5 py-1 text-2xs font-medium text-dim transition-colors hover:border-accent/50 hover:text-accent"
                  >
                    {name}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        {state === 'refused' && (
          <p className="flex items-start gap-1.5 text-crit">
            <TriangleAlert size={12} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </p>
        )}
      </div>

      {/* The one click that is usually the whole job: this machine is nearly
          always right. */}
      {detectedZone && detectedZone !== 'UTC' && detectedZone !== latest.current && (
        <button
          type="button"
          disabled={disabled}
          onClick={() => {
            setText(cityOf(detectedZone))
            save(detectedZone)
          }}
          className="flex w-full items-center justify-between rounded-xl border border-edge px-4 py-3 text-left text-sm text-dim transition-colors hover:border-accent/40 hover:bg-accent/5 hover:text-ink disabled:opacity-60"
        >
          <span>
            This computer is set to <span className="font-semibold text-ink">{detectedZone}</span>
          </span>
          <span className="text-xs font-semibold text-accent">Use it</span>
        </button>
      )}
    </div>
  )
}
