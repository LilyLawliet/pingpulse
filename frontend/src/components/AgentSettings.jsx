import { useCallback, useEffect, useState } from 'react'
import { Check, Clock, FileText, Loader2, TriangleAlert } from 'lucide-react'
import { api } from '../api.js'

const DAYS = [
  ['monday', 'Mon'],
  ['tuesday', 'Tue'],
  ['wednesday', 'Wed'],
  ['thursday', 'Thu'],
  ['friday', 'Fri'],
  ['saturday', 'Sat'],
  ['sunday', 'Sun'],
]

/**
 * The rules a shop sets for how its agent behaves.
 *
 * Everything here is optional, and a shop that fills in none of it keeps
 * exactly the agent it has. That is not a default to be tidied away later: it
 * is what makes this safe to put in front of a business that is mid-conversation
 * with a customer right now.
 *
 * Hours are per day rather than one range, because "9 to 5 except Saturdays,
 * when we shut at 1" is what shops actually do and a single range cannot say
 * it. Being closed does not stop the agent answering — it stops it promising
 * somebody will call in ten minutes.
 *
 * Lists are typed one per line rather than as tag chips. A chip editor is
 * nicer to look at and worse to use for a person pasting in the twelve
 * services they already have written down somewhere.
 */
export default function AgentSettings() {
  const [config, setConfig] = useState(null)
  const [zone, setZone] = useState('UTC')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  // Hours a document offered, waiting to be confirmed. Null once they are.
  const [fromDocument, setFromDocument] = useState(null)

  const load = useCallback(async () => {
    try {
      const found = await api.getAgentConfig()
      const stored = found.agent_config || {}

      // Hours read out of an uploaded document are offered here, in the form,
      // rather than written straight to the config. Booking reads
      // business_hours, so writing it from parsed prose would have the agent
      // offering real times to real customers on the strength of a regular
      // expression. Prefilled and left for a person to look at and save: the
      // document does the typing, somebody still says yes.
      const suggested = stored.hours_from_document
      if (suggested?.hours && !stored.business_hours) {
        setConfig({ ...stored, business_hours: suggested.hours })
        setFromDocument(suggested)
      } else {
        setConfig(stored)
        setFromDocument(null)
      }
      setZone(found.timezone || 'UTC')
    } catch {
      setConfig({})
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!config) return null

  const lines = (key) => (config[key] || []).join('\n')
  const setLines = (key, value) =>
    setConfig({
      ...config,
      [key]: value
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean),
    })

  const hours = config.business_hours || {}
  const setHours = (day, patch) =>
    setConfig({
      ...config,
      business_hours: { ...hours, [day]: { ...(hours[day] || {}), ...patch } },
    })
  const clearDay = (day) => {
    const next = { ...hours }
    delete next[day]
    setConfig({ ...config, business_hours: next })
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    setNote(null)
    try {
      // Deliberately without a timezone. This screen no longer owns it, and
      // sending the one it happens to have loaded would quietly satisfy the
      // server's "set your timezone before setting hours" check with the
      // default nobody chose.
      await api.saveAgentConfig(config)
      setNote(
        fromDocument
          ? 'Saved. Booking is on, and these are the hours appointments are offered in.'
          : 'Saved. New conversations follow these from now on.',
      )
      // Confirmed now, so it is no longer a suggestion. The server drops the
      // stored copy for the same reason.
      setFromDocument(null)
    } catch (err) {
      setError(err.message)
    }
    setSaving(false)
  }

  return (
    <section className="space-y-3">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-accent/12 ring-1 ring-inset ring-accent/25">
          <Clock size={14} className="text-accent" />
        </span>
        <div className="min-w-0">
          <h4 className="text-xs font-semibold text-ink">How your agent should behave</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            All optional. Leave it empty and nothing about your agent changes.
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

      {/*
        Shown, not edited. The timezone is asked for in Your business, which
        comes first - it used to be asked for here, after the document upload
        that needs it, so it was reliably unset at the one moment it mattered.
        Keeping an editor in both places is how one of them goes stale.
      */}
      {zone && zone !== 'UTC' ? (
        <p className="text-2xs text-faint">
          Times below are read in <span className="text-dim">{zone}</span>. Change it
          in <span className="text-dim">Your business</span>.
        </p>
      ) : (
        <p className="flex items-start gap-2 rounded-lg bg-warn/10 px-3 py-2 text-2xs text-warn">
          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
          <span>
            Your timezone is not set, so these hours cannot be saved yet — without one
            they would be read as UTC, which is how somebody gets offered an
            appointment in the middle of the night. Set it in{' '}
            <span className="font-semibold">Your business</span>.
          </span>
        </p>
      )}

      {fromDocument && (
        <p className="flex items-start gap-2 rounded-lg bg-accent/10 px-3 py-2 text-2xs text-accent">
          <FileText size={12} className="mt-0.5 shrink-0" />
          <span>
            These hours were read from{' '}
            <span className="font-semibold">{fromDocument.source}</span>. Nothing is
            booked against them until you save — check them first, since a document
            can word its hours in a way this reads differently.
          </span>
        </p>
      )}

      <div>
        <span className="eyebrow mb-1.5 block">Opening hours</span>
        <div className="space-y-1">
          {DAYS.map(([key, label]) => {
            const day = hours[key]
            return (
              <div key={key} className="flex items-center gap-2">
                <span className="w-9 shrink-0 text-2xs text-dim">{label}</span>
                <input
                  type="time"
                  value={day?.open || ''}
                  onChange={(e) => setHours(key, { open: e.target.value })}
                  className="min-w-0 flex-1 rounded-lg border border-edge bg-bg px-2 py-1 text-2xs text-ink focus:border-accent/60"
                />
                <span className="text-2xs text-faint">to</span>
                <input
                  type="time"
                  value={day?.close || ''}
                  onChange={(e) => setHours(key, { close: e.target.value })}
                  className="min-w-0 flex-1 rounded-lg border border-edge bg-bg px-2 py-1 text-2xs text-ink focus:border-accent/60"
                />
                <button
                  type="button"
                  onClick={() => clearDay(key)}
                  title="Closed this day"
                  className="shrink-0 rounded px-1.5 py-1 text-2xs text-faint transition-colors hover:text-crit"
                >
                  closed
                </button>
              </div>
            )
          })}
        </div>
        <p className="mt-1.5 text-2xs leading-relaxed text-faint">
          Outside these hours it still answers — it just will not promise that somebody
          will call straight back.
        </p>
      </div>

      <TextList
        label="Services you offer"
        hint="One per line. It will say plainly that anything not listed is not something you do."
        value={lines('services')}
        onChange={(value) => setLines('services', value)}
      />
      <TextList
        label="Areas you serve"
        hint="One per line. Anywhere else, it says so rather than promising to check."
        value={lines('service_areas')}
        onChange={(value) => setLines('service_areas', value)}
      />
      <TextList
        label="Words that should fetch a person"
        hint="One per line. These are added to refunds, complaints and requests for a manager, which always do."
        value={lines('escalate_on')}
        onChange={(value) => setLines('escalate_on', value)}
      />

      <label className="block">
        <span className="eyebrow mb-1 block">Never promise</span>
        <textarea
          rows={2}
          value={config.never_promise || ''}
          onChange={(e) => setConfig({ ...config, never_promise: e.target.value })}
          placeholder="Same-day work. Discounts over 10%."
          className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink placeholder:text-faint focus:border-accent/60"
        />
      </label>

      <label className="block">
        <span className="eyebrow mb-1 block">Pricing rules</span>
        <textarea
          rows={2}
          value={config.pricing_rules || ''}
          onChange={(e) => setConfig({ ...config, pricing_rules: e.target.value })}
          placeholder="No quotes under $200. Always mention the callout fee."
          className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink placeholder:text-faint focus:border-accent/60"
        />
      </label>

      <button
        type="button"
        disabled={saving}
        onClick={save}
        className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
      >
        {saving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
        Save rules
      </button>
    </section>
  )
}

function TextList({ label, hint, value, onChange }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1 block">{label}</span>
      <textarea
        rows={3}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink focus:border-accent/60"
      />
      <span className="mt-1 block text-2xs leading-relaxed text-faint">{hint}</span>
    </label>
  )
}
