import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, Loader2, Lock, PartyPopper, Save, Settings, TriangleAlert } from 'lucide-react'
import { PageHeader } from './ui.jsx'
import { api } from '../api.js'
import {
  STEPS,
  TIER_HINT,
  TIER_LABEL,
  mergeSetup,
  readSetup,
  requiredLeft,
  setupKnown,
} from '../setup.js'
import AgentSettings from './AgentSettings.jsx'
import CalendarSettings from './CalendarSettings.jsx'
import ErrorLog from './ErrorLog.jsx'
import KnowledgeSettings from './KnowledgeSettings.jsx'
import LearningSettings from './LearningSettings.jsx'
import NotificationSettings from './NotificationSettings.jsx'
import PipelineEditor from './PipelineEditor.jsx'
import WhatsAppSettings from './WhatsAppSettings.jsx'

/**
 * Everything a shop has to set up, in the order it has to be set up.
 *
 * This was one modal with seven sections stacked inside a scrolling box half
 * the width of the screen. Nothing said which of them mattered, which were
 * optional, or which had already been done — so the way to find out what was
 * left was to scroll the whole thing and guess. A client set up a business and
 * never entered their opening hours, which silently switched booking off, and
 * never gave an alert address, so every alert the system raised for them went
 * nowhere. Neither was visible anywhere.
 *
 * So: a page, not a sheet. Steps in order, each one saying what it is for and
 * what happens if it is skipped. Every step's state is read from the real
 * backend rather than ticked off locally, which means a step cannot be marked
 * done by visiting it, and a business set up on another machine shows as done
 * here.
 *
 * Required and optional are marked, because they are genuinely different. A
 * shop with no board customisation works fine. A shop with no WhatsApp
 * connection is not a shop that is running.
 */

// The timezone list the browser already has, so nobody types "America/New_York"
// from memory.
//
// It was a free-text box asking for an IANA name, which is a thing engineers
// know and shop owners do not - so the field existed and was still not a way
// to set a timezone. Falling back to a short list on a browser too old for
// supportedValuesOf, because an empty dropdown would be worse than the box it
// replaced.
const FALLBACK_ZONES = [
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Sao_Paulo', 'Europe/London', 'Europe/Dublin', 'Europe/Paris',
  'Europe/Berlin', 'Europe/Madrid', 'Europe/Istanbul', 'Africa/Lagos',
  'Africa/Johannesburg', 'Africa/Cairo', 'Asia/Dubai', 'Asia/Karachi',
  'Asia/Kolkata', 'Asia/Dhaka', 'Asia/Singapore', 'Asia/Tokyo',
  'Australia/Sydney', 'UTC',
]

const ZONES = (() => {
  try {
    const all = Intl.supportedValuesOf('timeZone')
    return all?.length ? all : FALLBACK_ZONES
  } catch {
    return FALLBACK_ZONES
  }
})()

// What this machine believes, offered as the answer rather than guessed at:
// it is right nearly always, and the one click to accept it is the point.
const detectedZone = (() => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch {
    return ''
  }
})()

/** What the clock says in a zone, so a wrong guess is visible before it books
 *  somebody at four in the morning. */
function localTime(zone) {
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

const CURRENCIES = ['USD', 'EUR', 'GBP', 'PKR', 'AED', 'SAR', 'INR', 'TRY', 'NGN', 'ZAR']
const LANGUAGES = [
  ['en', 'English'],
  ['ur', 'Urdu'],
  ['ar', 'Arabic'],
  ['fr', 'French'],
  ['es', 'Spanish'],
  ['de', 'German'],
  ['tr', 'Turkish'],
  ['hi', 'Hindi'],
]

// Example prices in the currency actually chosen. A fixed example in rupees
// above a dropdown reading USD is the first thing a new business used to read.
const EXAMPLE_PRICES = {
  USD: ['120', '450', '150'],
  EUR: ['110', '420', '140'],
  GBP: ['95', '360', '120'],
  PKR: ['12,000', '45,000', '15,000'],
  AED: ['450', '1,650', '550'],
  SAR: ['450', '1,700', '560'],
  INR: ['10,000', '38,000', '12,500'],
  TRY: ['4,000', '15,000', '5,000'],
  NGN: ['180,000', '700,000', '230,000'],
  ZAR: ['2,200', '8,300', '2,800'],
}

function sellingExample(currency) {
  const code = EXAMPLE_PRICES[currency] ? currency : 'USD'
  const [low, high, delivery] = EXAMPLE_PRICES[code]
  return `Designer sneakers and heels, ${code} ${low}–${high}. Free delivery over ${code} ${delivery}.`
}

const inputClass =
  'w-full rounded-xl border border-edge bg-panel px-3.5 py-2.5 text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none focus:ring-4 focus:ring-accent/10'

/** The business form's fields, from the organization the server returned. */
function formFrom(org) {
  return {
    name: org.name || '',
    target_tone: org.target_tone || '',
    product_rules: org.product_rules || '',
    sales_prompt: org.sales_prompt || '',
    default_currency: org.default_currency || 'USD',
    default_language: org.default_language || 'en',
    timezone: org.timezone || '',
  }
}

export default function SettingsPage({
  open = true,
  onSaved,
  onProgress,
  initialStep = 'business',
  // What the dashboard already knows. Rendered from straight away, so a step
  // is on screen the moment Setup opens instead of after a second round of
  // the same checks; the page still re-reads in the background.
  initialState = null,
}) {
  const [active, setActive] = useState(initialStep)
  const [setup, setSetup] = useState(initialState)
  // Shown once, the moment the last required step is done.
  const [justFinished, setJustFinished] = useState(false)
  const [form, setForm] = useState(() =>
    initialState?.org ? formFrom(initialState.org) : initialState ? { ...EMPTY_FORM } : null,
  )
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  const [preparing, setPreparing] = useState(false)

  // Every step's state comes from the backend. A step cannot be completed by
  // looking at it, and one done elsewhere already shows as done.
  //
  // The form is filled from the server only when there is nothing in it yet,
  // or straight after a save. A background re-read that rewrote it would throw
  // away whatever somebody was halfway through typing.
  const check = useCallback(
    async ({ refill = false } = {}) => {
      const apply = (state) => {
        if (!setupKnown(state)) return
        setForm((was) => {
          if (was && !refill) return was
          return state.org ? formFrom(state.org) : was || { ...EMPTY_FORM }
        })
        setSetup((was) => mergeSetup(was, state))
        onProgress?.(state)
      }
      // The steps and the form appear as soon as the business itself has
      // answered, not after every check has.
      const state = await readSetup(apply)
      apply(state)
      // A check that failed is not an answer; ask again rather than leave the
      // page saying "checking" for good.
      if (state.error && mounted.current) {
        retry.current = setTimeout(() => check(), 5000)
      }
      return state
    },
    [onProgress],
  )

  const mounted = useRef(true)
  const retry = useRef(null)
  useEffect(
    () => () => {
      mounted.current = false
      clearTimeout(retry.current)
    },
    [],
  )

  useEffect(() => {
    if (open) check()
  }, [open, check])

  const field = (key) => ({
    value: form?.[key] ?? '',
    onChange: (event) => setForm((f) => ({ ...f, [key]: event.target.value })),
  })

  // Re-read after a save, and say so if that save was the last thing standing
  // between this business and a running agent.
  const recheck = async () => {
    const before = requiredLeft(setup).length
    const after = requiredLeft(await check({ refill: true })).length
    if (before > 0 && after === 0) setJustFinished(true)
  }

  const saveBusiness = async (event) => {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setNote(null)
    try {
      // An empty box means "not answered", not "the empty string" - which the
      // server would reject as an invalid timezone.
      const zone = (form.timezone || '').trim()
      const body = { ...form, timezone: zone || undefined }
      // An access token can arrive with no business attached. Then there is
      // nothing to update yet, so this creates it - and makes it the active one.
      const saved = setup?.hasOrg
        ? await api.updateActiveOrganization(body)
        : await api.createOrganization(body)
      setNote('Saved.')
      await onSaved?.(saved)
      await recheck()
    } catch (err) {
      // The server writes these for a shop owner - "'Miami' is not a timezone.
      // Use an IANA name like America/New_York" is the whole answer, and
      // replacing it with "that did not save" throws the answer away.
      setError(err?.message || 'That did not save. Check the details and try again.')
    }
    setSaving(false)
  }

  const saveTimezone = async (event) => {
    event.preventDefault()
    const zone = (form.timezone || '').trim()
    setNote(null)
    if (!zone) {
      setError('Choose your timezone first.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      // Only the zone. Sending the whole form from here would write back a
      // business description this step never showed anybody.
      const saved = await api.updateActiveOrganization({ timezone: zone })
      setNote('Saved.')
      onSaved?.(saved)
      await recheck()
    } catch (err) {
      setError(err?.message || 'That did not save. Check the details and try again.')
    }
    setSaving(false)
  }

  /**
   * Make the business record exist, named after whoever the token was issued to.
   *
   * Every step saves against a business, and an access token can arrive
   * without one. Asking people to fill in the business form first just to
   * unlock the rest made the order a rule when it is not one: a price list
   * can go up before the description is written. So opening any other step
   * creates the record with the token's name, which the business step can
   * rename. It stays unticked until that step is actually filled in.
   */
  const creating = useRef(null)
  const ensureBusiness = useCallback(() => {
    if (creating.current) return creating.current
    creating.current = (async () => {
      setPreparing(true)
      setError(null)
      try {
        let name = ''
        try {
          name = ((await api.session())?.client_name || '').trim()
        } catch {
          /* fall back to a neutral name the business step can change */
        }
        await api.createOrganization({ name: name || 'My business' })
        await onSaved?.()
        await check({ refill: true })
      } catch (err) {
        setError(err?.message || 'Could not set up your business. Try again.')
        creating.current = null
      }
      setPreparing(false)
    })()
    return creating.current
  }, [check, onSaved])

  const needsBusiness =
    setup !== null && !setup.hasOrg && (STEPS.find((s) => s.key === active) || STEPS[0]).key !== 'business'
  useEffect(() => {
    if (needsBusiness) ensureBusiness()
  }, [needsBusiness, ensureBusiness])

  /** Move to a step, without carrying the last one's message along. */
  const pick = (key) => {
    setActive(key)
    setError(null)
    setNote(null)
  }

  const ready = setup?.done || null
  const noBusiness = setup !== null && !setup.hasOrg
  // Every step is open, in any order. They are independent of each other; the
  // only thing they share is the business record they are saved against.
  const current = STEPS.find((s) => s.key === active) || STEPS[0]
  // Opening another step before that record exists creates it (see below), so
  // until it does there is nothing for that step's panel to load or save.
  const waitingForBusiness = noBusiness && current.key !== 'business'
  const outstanding = requiredLeft(setup)
  const requiredCount = STEPS.filter((s) => s.tier === 'required').length
  const requiredDone = setup ? requiredCount - outstanding.length : 0
  const firstOpen = outstanding[0]
  // A phone that was connected and has dropped: set up, but not running now.
  const whatsappDropped = setup?.hasOrg && setup.gate.whatsapp && !setup.done.whatsapp

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        icon={Settings}
        title="Setup"
        subtitle={
          setup === null
            ? 'Checking what is done…'
            : noBusiness
              ? 'Start by telling the agent about your business.'
              : outstanding.length === 0
                ? 'Your agent is running. Anything below is there to make it better.'
                : `${outstanding.length} required step${outstanding.length === 1 ? '' : 's'} before your agent can start.`
        }
      >
        {setup && (
          <div className="flex items-center gap-3" title="Required steps done">
            <div className="h-2 w-32 overflow-hidden rounded-full bg-edge">
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-500"
                style={{ width: `${(requiredDone / requiredCount) * 100}%` }}
              />
            </div>
            <span className="text-xs font-semibold tabular-nums text-dim">
              {requiredDone}/{requiredCount} required
            </span>
          </div>
        )}
      </PageHeader>

      {/* Said at the top of the page, in words, rather than left to be
          inferred from a count - so nobody wonders why the inbox is locked. */}
      {setup && (outstanding.length > 0 || justFinished || whatsappDropped) && (
        <div className="shrink-0 px-4 pb-4 sm:px-6 lg:px-8">
          {justFinished ? (
            <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-accent/30 bg-accent/10 px-4 py-3">
              <PartyPopper size={18} className="shrink-0 text-accent" />
              <p className="min-w-0 flex-1 text-sm text-ink">
                <span className="font-semibold">Your agent is running.</span>{' '}
                <span className="text-dim">
                  The inbox, board and analytics are open. The recommended steps are still
                  worth doing.
                </span>
              </p>
            </div>
          ) : outstanding.length > 0 ? (
            <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-warn/30 bg-warn/10 px-4 py-3">
              <Lock size={16} className="shrink-0 text-warn" />
              <p className="min-w-0 flex-1 text-sm text-ink">
                <span className="font-semibold">Your agent is not running yet.</span>{' '}
                <span className="text-dim">
                  Still needed: {outstanding.map((step) => step.title).join(', ')}. Do them in
                  any order - the rest of PingPulse opens once they are done.
                </span>
              </p>
              {firstOpen && firstOpen.key !== current.key && (
                <button
                  type="button"
                  onClick={() => pick(firstOpen.key)}
                  className="btn-secondary shrink-0 px-3 py-1.5 text-xs"
                >
                  Go to {firstOpen.title} <ArrowRight size={13} />
                </button>
              )}
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-crit/30 bg-crit/10 px-4 py-3">
              <TriangleAlert size={16} className="shrink-0 text-crit" />
              <p className="min-w-0 flex-1 text-sm text-ink">
                <span className="font-semibold">WhatsApp is not connected right now.</span>{' '}
                <span className="text-dim">
                  Nothing is being answered until it reconnects.
                </span>
              </p>
              {current.key !== 'whatsapp' && (
                <button
                  type="button"
                  onClick={() => pick('whatsapp')}
                  className="btn-secondary shrink-0 px-3 py-1.5 text-xs"
                >
                  Reconnect <ArrowRight size={13} />
                </button>
              )}
            </div>
          )}
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row lg:gap-6 lg:px-8 lg:pb-6">
        {/* The steps, grouped by how much they matter. A row on a phone, a
            column on a desktop - the order is the same either way, because
            the order is the instruction. */}
        <nav className="flex shrink-0 gap-1.5 overflow-x-auto border-b border-edge px-4 pb-3 sm:px-6 lg:w-[290px] lg:flex-col lg:self-start lg:overflow-visible lg:rounded-2xl lg:border lg:bg-panel lg:p-2 lg:shadow-card">
          {['required', 'recommended', 'optional'].map((tier) => (
            <div key={tier} className="flex shrink-0 gap-1.5 lg:block lg:space-y-0.5">
              <div className="hidden px-3 pb-1 pt-3 first:pt-1 lg:block">
                <p
                  className={`text-[11px] font-semibold uppercase tracking-[0.08em] ${
                    tier === 'required' ? 'text-warn' : 'text-faint'
                  }`}
                >
                  {TIER_LABEL[tier]}
                </p>
                <p className="text-2xs text-faint">{TIER_HINT[tier]}</p>
              </div>
              {STEPS.filter((step) => step.tier === tier).map((step) => {
                const index = STEPS.indexOf(step)
                const done = ready?.[step.key]
                const dropped = step.key === 'whatsapp' && whatsappDropped
                const selected = step.key === current.key
                return (
                  <button
                    key={step.key}
                    type="button"
                    onClick={() => pick(step.key)}
                    className={`flex shrink-0 items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors lg:w-full ${
                      selected
                        ? 'bg-accent/10 text-ink ring-1 ring-inset ring-accent/25'
                        : 'text-dim hover:bg-panel-2 hover:text-ink'
                    }`}
                  >
                    <span
                      className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-[11px] font-semibold ${
                        dropped
                          ? 'bg-crit/15 text-crit'
                          : done
                            ? 'bg-ok/15 text-ok'
                            : step.tier === 'required'
                              ? 'bg-warn/15 text-warn'
                              : 'bg-panel-2 text-faint'
                      }`}
                    >
                      {dropped ? '!' : done ? <Check size={11} /> : index + 1}
                    </span>
                    <span className="min-w-0 flex-1 whitespace-nowrap text-sm font-medium lg:whitespace-normal">
                      {step.title}
                      {step.tier === 'required' && !done && !dropped && (
                        <span className="ml-1 text-warn lg:hidden" aria-label="required">
                          *
                        </span>
                      )}
                    </span>
                  </button>
                )
              })}
            </div>
          ))}
        </nav>

        <div className="min-w-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 lg:rounded-2xl lg:border lg:border-edge lg:bg-panel lg:px-8 lg:py-7 lg:shadow-card">
          <div className="mx-auto max-w-2xl space-y-6">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <current.icon size={18} className="shrink-0 text-accent" />
                <h3 className="text-base font-semibold text-ink">
                  {noBusiness && current.key === 'business' ? 'Create your business' : current.title}
                </h3>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                    current.tier === 'required'
                      ? 'bg-warn/10 text-warn'
                      : 'bg-panel-2 text-faint'
                  }`}
                >
                  {TIER_LABEL[current.tier]}
                </span>
                {ready?.[current.key] && (
                  <span className="flex items-center gap-1 rounded-full bg-ok/10 px-2 py-0.5 text-[11px] font-semibold text-ok">
                    <Check size={10} /> done
                  </span>
                )}
              </div>
              <p className="mt-1.5 text-sm leading-relaxed text-dim">{current.why}</p>
              {current.skipped && !ready?.[current.key] && (
                <p className="mt-3 flex items-start gap-2 rounded-xl bg-warn/10 px-3.5 py-2.5 text-xs leading-relaxed text-warn">
                  <TriangleAlert size={12} className="mt-0.5 shrink-0" />
                  <span>
                    <strong className="font-semibold">If you skip this:</strong>{' '}
                    {current.skipped}
                  </span>
                </p>
              )}
            </div>

            {current.key === 'business' &&
              (form === null ? (
                <Loader2 size={16} className="animate-spin text-faint" />
              ) : (
                <form onSubmit={saveBusiness} className="space-y-4">
                  {error && (
                    <p className="rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">{error}</p>
                  )}
                  {note && (
                    <p className="rounded-lg bg-ok/10 px-3 py-2 text-2xs text-ok">{note}</p>
                  )}

                  <label className="block">
                    <span className="mb-1.5 block text-sm font-medium text-ink">Business name</span>
                    <input required {...field('name')} className={inputClass} placeholder="Luxe Footwear" />
                  </label>

                  <label className="block">
                    <span className="mb-1.5 block text-sm font-medium text-ink">How should it sound?</span>
                    <input
                      {...field('target_tone')}
                      className={inputClass}
                      placeholder="Warm, confident, a little playful"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-1.5 block text-sm font-medium text-ink">What you sell</span>
                    <textarea
                      rows={3}
                      {...field('product_rules')}
                      className={inputClass}
                      placeholder={sellingExample(form.default_currency)}
                    />
                    <span className="mt-1 block text-2xs text-faint">
                      Prices here are ones the agent may quote. It will not invent any others.
                    </span>
                  </label>

                  <div className="grid grid-cols-2 gap-3">
                    <label className="block">
                      <span className="mb-1.5 block text-sm font-medium text-ink">Currency</span>
                      <select {...field('default_currency')} className={inputClass}>
                        {CURRENCIES.map((code) => (
                          <option key={code} value={code}>
                            {code}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="block">
                      <span className="mb-1.5 block text-sm font-medium text-ink">Replies in</span>
                      <select {...field('default_language')} className={inputClass}>
                        {LANGUAGES.map(([code, label]) => (
                          <option key={code} value={code}>
                            {label}
                          </option>
                        ))}
                      </select>
                    </label>
                  </div>

                  <label className="block">
                    <span className="mb-1.5 block text-sm font-medium text-ink">How it should sell</span>
                    <textarea
                      required
                      rows={5}
                      {...field('sales_prompt')}
                      className={inputClass}
                      placeholder="Greet by name, answer the question, always quote a price, and offer to reserve a pair."
                    />
                  </label>

                  <button
                    type="submit"
                    disabled={saving}
                    className="btn-primary"
                  >
                    <Save size={14} />{' '}
                    {saving ? 'Saving…' : noBusiness ? 'Create my business' : 'Save'}
                  </button>
                </form>
              ))}

            {current.key === 'timezone' &&
              (form === null ? (
                <Loader2 size={16} className="animate-spin text-faint" />
              ) : (
                <form onSubmit={saveTimezone} className="space-y-4">
                  {error && (
                    <p className="rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">{error}</p>
                  )}
                  {note && (
                    <p className="rounded-lg bg-ok/10 px-3 py-2 text-2xs text-ok">{note}</p>
                  )}

                  <label className="block">
                    <span className="mb-1.5 block text-sm font-medium text-ink">Your timezone</span>
                    <select {...field('timezone')} className={inputClass}>
                      <option value="">Choose your timezone…</option>
                      {ZONES.map((zone) => (
                        <option key={zone} value={zone}>
                          {zone === detectedZone ? `${zone} — this computer` : zone}
                        </option>
                      ))}
                    </select>
                    <span className="mt-1.5 block text-2xs leading-relaxed text-faint">
                      Picked from the list your browser already has, so nobody
                      has to remember how an IANA name is spelt.
                    </span>
                  </label>

                  {/* The one click that is the point: this machine is nearly
                      always right, and typing is what people got wrong. */}
                  {detectedZone && form.timezone !== detectedZone && (
                    <button
                      type="button"
                      onClick={() => setForm((f) => ({ ...f, timezone: detectedZone }))}
                      className="flex w-full items-center justify-between rounded-xl border border-edge px-4 py-3 text-left text-sm text-dim transition-colors hover:border-accent/40 hover:bg-accent/5 hover:text-ink"
                    >
                      <span>
                        This computer is set to{' '}
                        <span className="font-semibold text-ink">{detectedZone}</span>
                      </span>
                      <span className="text-xs font-semibold text-accent">Use it</span>
                    </button>
                  )}

                  {form.timezone && (
                    <p className="rounded-xl bg-panel-2/60 px-3.5 py-2.5 text-2xs leading-relaxed text-dim">
                      It is{' '}
                      <span className="font-semibold text-ink">{localTime(form.timezone)}</span>{' '}
                      there now. If that is not your clock, the zone is wrong.
                    </p>
                  )}

                  <button type="submit" disabled={saving} className="btn-primary">
                    <Save size={14} /> {saving ? 'Saving…' : 'Save'}
                  </button>
                </form>
              ))}

            {waitingForBusiness ? (
              error && !preparing ? (
                <div className="space-y-3">
                  <p className="rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">{error}</p>
                  <button type="button" onClick={ensureBusiness} className="btn-secondary">
                    Try again
                  </button>
                </div>
              ) : (
                <p className="flex items-center gap-2 text-sm text-dim">
                  <Loader2 size={15} className="animate-spin" /> Setting up your business…
                </p>
              )
            ) : (
              <>
              {current.key === 'whatsapp' && <WhatsAppSettings onChanged={recheck} />}
              {current.key === 'knowledge' && <KnowledgeSettings onChanged={recheck} />}
              {current.key === 'hours' && (
                <AgentSettings
                    onCalendar={() => pick('calendar')}
                    onTimezone={() => pick('timezone')}
                    onChanged={recheck}
                  />
              )}
              {current.key === 'calendar' && <CalendarSettings onChanged={recheck} />}
              {current.key === 'alerts' && <NotificationSettings onChanged={recheck} />}
              {current.key === 'pipeline' && <PipelineEditor />}
              {current.key === 'learning' && <LearningSettings />}
              {current.key === 'problems' && <ErrorLog />}
              </>
            )}

            <Next steps={STEPS} active={current.key} ready={ready} onPick={pick} />
          </div>
        </div>
      </div>
    </div>
  )
}

const EMPTY_FORM = {
  name: '',
  target_tone: '',
  product_rules: '',
  sales_prompt: '',
  default_currency: 'USD',
  timezone: '',
  default_language: 'en',
}

/**
 * Somewhere to go next, so the order is something you can follow rather than
 * something you have to remember.
 *
 * While required steps are open, "next" is the next one of those, not the
 * next row down - otherwise it walks somebody into the recommended steps with
 * the agent still unable to start.
 */
function Next({ steps, active, ready, onPick }) {
  const index = steps.findIndex((s) => s.key === active)
  const openRequired = steps.filter((s) => s.tier === 'required' && !ready?.[s.key] && s.key !== active)
  const next =
    openRequired.find((s) => steps.indexOf(s) > index) || openRequired[0] || steps[index + 1]
  if (!next) return null
  return (
    <button
      type="button"
      onClick={() => onPick(next.key)}
      className="flex w-full items-center justify-between rounded-xl border border-edge px-4 py-3 text-left text-sm text-dim transition-colors hover:border-accent/40 hover:bg-accent/5 hover:text-ink"
    >
      <span>
        Next: <span className="font-semibold">{next.title}</span>
        <span className={next.tier === 'required' ? 'text-warn' : 'text-faint'}>
          {' '}
          ({TIER_LABEL[next.tier].toLowerCase()})
        </span>
      </span>
      <span aria-hidden>→</span>
    </button>
  )
}
