import { useCallback, useEffect, useState } from 'react'
import {
  Bell,
  Check,
  Columns3,
  FileText,
  GraduationCap,
  Loader2,
  MessageSquare,
  Save,
  Store,
  Settings,
  TriangleAlert,
} from 'lucide-react'
import { PageHeader } from './ui.jsx'
import { api } from '../api.js'
import AgentSettings from './AgentSettings.jsx'
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

/** The required steps, for anything that only needs to count them. */
export const REQUIRED_STEPS = ['business', 'whatsapp', 'knowledge', 'hours', 'alerts']

/**
 * Which setup steps are done, read from the real backend.
 *
 * Exported so the sidebar can say how many are left without opening this page.
 */
export async function readSetup() {
  const next = {
    business: false,
    whatsapp: false,
    knowledge: false,
    hours: false,
    alerts: false,
  }
  let org = null

  try {
    org = await api.activeOrganization()
    // The timezone counts. Without one every hour this business states is
    // read as UTC, so a step calling itself done without it would be
    // reporting a setup that cannot book anybody correctly.
    const zone = (org?.timezone || '').trim()
    next.business =
      Boolean((org?.sales_prompt || '').trim().length > 40) &&
      Boolean(zone) &&
      zone !== 'UTC'
    next.knowledge = Boolean((org?.product_rules || '').trim())
  } catch {
    /* not loaded */
  }
  try {
    const status = await api.whatsappStatus()
    next.whatsapp = Boolean(status?.connected)
  } catch {
    /* a failed check is not a finished step */
  }
  try {
    const readiness = await api.knowledgeReadiness()
    next.knowledge = next.knowledge || Boolean(readiness?.ready || readiness?.documents > 0)
  } catch {
    /* not ready */
  }
  try {
    const config = await api.getAgentConfig()
    next.hours = Object.keys(config?.agent_config?.business_hours || {}).length > 0
  } catch {
    /* not ready */
  }
  try {
    const alerts = await api.notificationSettings()
    next.alerts = Boolean(alerts?.email || alerts?.devices > 0 || alerts?.subscribed)
  } catch {
    /* not ready */
  }
  return { next, org }
}

export default function SettingsPage({ open = true, onSaved, onProgress, initialStep = 'business' }) {
  const [active, setActive] = useState(initialStep)
  const [ready, setReady] = useState(null)
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)

  // Every step's state comes from the backend. A step cannot be completed by
  // looking at it, and one done elsewhere already shows as done.
  const check = useCallback(async () => {
    const { next, org } = await readSetup()
    if (org) {
      setForm({
        name: org.name || '',
        target_tone: org.target_tone || '',
        product_rules: org.product_rules || '',
        sales_prompt: org.sales_prompt || '',
        default_currency: org.default_currency || 'USD',
        default_language: org.default_language || 'en',
        timezone: org.timezone || '',
      })
    } else {
      setForm((was) => was || { ...EMPTY_FORM })
    }
    setReady(next)
    onProgress?.(next)
  }, [onProgress])

  useEffect(() => {
    if (open) check()
  }, [open, check])

  if (!open) return null

  const field = (key) => ({
    value: form?.[key] ?? '',
    onChange: (event) => setForm((f) => ({ ...f, [key]: event.target.value })),
  })

  const saveBusiness = async (event) => {
    event.preventDefault()
    setSaving(true)
    setError(null)
    setNote(null)
    try {
      // An empty box means "not answered", not "the empty string" - which the
      // server would reject as an invalid timezone.
      const zone = (form.timezone || '').trim()
      const saved = await api.updateActiveOrganization({
        ...form,
        timezone: zone || undefined,
      })
      setNote('Saved.')
      onSaved?.(saved)
      await check()
    } catch (err) {
      // The server writes these for a shop owner - "'Miami' is not a timezone.
      // Use an IANA name like America/New_York" is the whole answer, and
      // replacing it with "that did not save" throws the answer away.
      setError(err?.message || 'That did not save. Check the details and try again.')
    }
    setSaving(false)
  }

  const STEPS = [
    {
      key: 'business',
      icon: Store,
      title: 'Your business',
      required: true,
      why: 'Everything the agent says starts here. Without it, it has nothing to work from.',
      skipped: 'The agent answers with no idea what you sell.',
    },
    {
      key: 'whatsapp',
      icon: MessageSquare,
      title: 'Connect WhatsApp',
      required: true,
      why: 'The number your customers message. Scan the QR with the phone that owns it, or use your own Twilio account.',
      skipped: 'Nothing reaches you and nothing goes out. The agent is not running.',
    },
    {
      key: 'knowledge',
      icon: FileText,
      title: 'Prices and knowledge',
      required: true,
      why: 'The agent will never invent a price, so it can only quote what you give it here.',
      skipped: 'It has to refuse every question about cost.',
    },
    {
      key: 'hours',
      icon: Check,
      title: 'Hours and booking',
      required: true,
      why: 'Your timezone and opening hours. Appointments are only ever offered inside them.',
      skipped: 'Booking stays switched off, and the agent hands booking requests to you instead.',
    },
    {
      key: 'alerts',
      icon: Bell,
      title: 'Alerts',
      required: true,
      why: 'Where you are told when somebody asks for a person, or the agent gets stuck.',
      skipped: 'Alerts are still raised, and delivered to nobody.',
    },
    {
      key: 'pipeline',
      icon: Columns3,
      title: 'Your board',
      required: false,
      why: 'Rename the columns to match how you actually track work.',
      skipped: 'You keep the default columns, which is fine.',
    },
    {
      key: 'learning',
      icon: GraduationCap,
      title: 'Learning',
      required: false,
      why: 'Let the agent pick up your way of writing from replies you send yourself.',
      skipped: 'It keeps the tone you described above.',
    },
    {
      key: 'problems',
      icon: TriangleAlert,
      title: 'Problems',
      required: false,
      why: 'Anything that has gone wrong, and what it means.',
      skipped: null,
    },
  ]

  const current = STEPS.find((s) => s.key === active) || STEPS[0]
  const outstanding = STEPS.filter((s) => s.required && ready && !ready[s.key])
  const requiredCount = STEPS.filter((s) => s.required).length
  const requiredDone = requiredCount - outstanding.length

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        icon={Settings}
        title="Setup"
        subtitle={
          ready === null
            ? 'Checking what is done…'
            : outstanding.length === 0
              ? 'Everything needed is done. The rest is optional.'
              : `${outstanding.length} thing${outstanding.length === 1 ? '' : 's'} still needed before this runs properly.`
        }
      >
        {ready && (
          <div className="flex items-center gap-3">
            <div className="h-2 w-32 overflow-hidden rounded-full bg-edge">
              <div
                className="h-full rounded-full bg-accent transition-[width] duration-500"
                style={{ width: `${(requiredDone / requiredCount) * 100}%` }}
              />
            </div>
            <span className="text-xs font-semibold tabular-nums text-dim">
              {requiredDone}/{requiredCount}
            </span>
          </div>
        )}
      </PageHeader>

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row lg:gap-6 lg:px-8 lg:pb-6">
        {/* The steps. A row on a phone, a column on a desktop — the order is
            the same either way, because the order is the instruction. */}
        <nav className="flex shrink-0 gap-1.5 overflow-x-auto border-b border-edge px-4 pb-3 sm:px-6 lg:w-[270px] lg:flex-col lg:self-start lg:overflow-visible lg:rounded-2xl lg:border lg:bg-panel lg:p-2 lg:shadow-card">
          {STEPS.map((step, index) => {
            const done = ready?.[step.key]
            const selected = step.key === active
            return (
              <button
                key={step.key}
                onClick={() => setActive(step.key)}
                className={`flex shrink-0 items-center gap-3 rounded-xl px-3 py-2.5 text-left transition-colors lg:w-full ${
                  selected
                    ? 'bg-accent/10 text-ink ring-1 ring-inset ring-accent/25'
                    : 'text-dim hover:bg-panel-2 hover:text-ink'
                }`}
              >
                <span
                  className={`grid h-6 w-6 shrink-0 place-items-center rounded-full text-[11px] font-semibold ${
                    done
                      ? 'bg-ok/15 text-ok'
                      : step.required
                        ? 'bg-warn/15 text-warn'
                        : 'bg-panel-2 text-faint'
                  }`}
                >
                  {done ? <Check size={11} /> : index + 1}
                </span>
                <span className="min-w-0 flex-1 whitespace-nowrap text-sm font-medium lg:whitespace-normal">
                  {step.title}
                </span>
                {!step.required && (
                  <span className="hidden rounded-full bg-panel-2 px-2 py-0.5 text-[11px] text-faint lg:inline">optional</span>
                )}
              </button>
            )
          })}
        </nav>

        <div className="min-w-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 lg:rounded-2xl lg:border lg:border-edge lg:bg-panel lg:px-8 lg:py-7 lg:shadow-card">
          <div className="mx-auto max-w-2xl space-y-6">
            <div>
              <div className="flex items-center gap-2">
                <current.icon size={18} className="shrink-0 text-accent" />
                <h3 className="text-base font-semibold text-ink">{current.title}</h3>
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

            {active === 'business' &&
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

                  {/*
                    Asked for here, first, rather than at the booking step.
                    It used to sit after the document upload that needs it, so
                    it was reliably unset at the one moment it mattered: hours
                    read out of a handbook could not be saved, and the reason
                    appeared in a message that vanished on the next click.
                  */}
                  <label className="block">
                    <span className="eyebrow mb-1.5 block">Where you are</span>
                    <select {...field('timezone')} className={inputClass}>
                      <option value="">Choose your timezone…</option>
                      {ZONES.map((zone) => (
                        <option key={zone} value={zone}>
                          {zone === detectedZone ? `${zone} — this computer` : zone}
                        </option>
                      ))}
                    </select>
                    <span className="mt-1 block text-2xs text-faint">
                      Every opening time and appointment is read against this.
                      {detectedZone && form.timezone !== detectedZone && (
                        <>
                          {' '}This computer is set to{' '}
                          <button
                            type="button"
                            onClick={() =>
                              setForm((f) => ({ ...f, timezone: detectedZone }))
                            }
                            className="font-semibold text-accent underline-offset-2 hover:underline"
                          >
                            {detectedZone}
                          </button>
                          .
                        </>
                      )}
                    </span>
                  </label>

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
                    <Save size={14} /> {saving ? 'Saving…' : 'Save'}
                  </button>
                </form>
              ))}

            {active === 'whatsapp' && <WhatsAppSettings onChanged={check} />}
            {active === 'knowledge' && <KnowledgeSettings />}
            {active === 'hours' && <AgentSettings />}
            {active === 'alerts' && <NotificationSettings />}
            {active === 'pipeline' && <PipelineEditor />}
            {active === 'learning' && <LearningSettings />}
            {active === 'problems' && <ErrorLog />}

            <Next steps={STEPS} active={active} onPick={setActive} />
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

/** Somewhere to go next, so the order is something you can follow rather than
 *  something you have to remember. */
function Next({ steps, active, onPick }) {
  const index = steps.findIndex((s) => s.key === active)
  const next = steps[index + 1]
  if (!next) return null
  return (
    <button
      onClick={() => onPick(next.key)}
      className="flex w-full items-center justify-between rounded-xl border border-edge px-4 py-3 text-left text-sm text-dim transition-colors hover:border-accent/40 hover:bg-accent/5 hover:text-ink"
    >
      <span>
        Next: <span className="font-semibold">{next.title}</span>
        {!next.required && <span className="text-faint"> (optional)</span>}
      </span>
      <span aria-hidden>→</span>
    </button>
  )
}
