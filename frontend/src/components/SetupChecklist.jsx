import { useCallback, useEffect, useState } from 'react'
import { ArrowRight, Check, FlaskConical, Loader2, Rocket } from 'lucide-react'
import { api } from '../api.js'

/**
 * What a shop with no conversations yet should be looking at.
 *
 * The middle of the dashboard used to be blank until the first customer
 * messaged, which is the worst moment to say nothing: it is the point where
 * somebody is deciding whether this thing works. The blank panel reads as
 * broken rather than as empty, and the four things that need doing before a
 * first message can arrive are not obvious from anywhere else on the screen.
 *
 * The steps are checked against the real state rather than ticked off locally,
 * so a shop that connected WhatsApp on another machine sees it done here, and
 * so a step cannot be marked complete by clicking it.
 *
 * It disappears the moment a conversation exists. An onboarding panel that
 * outstays its welcome is clutter on the screen somebody uses all day.
 */
export default function SetupChecklist({ onOpenSettings, onTest, className = '' }) {
  const [state, setState] = useState(null)

  const load = useCallback(async () => {
    const next = { whatsapp: false, knowledge: false, prompt: false, tested: false }
    try {
      const status = await api.whatsappStatus()
      next.whatsapp = Boolean(status.connected)
    } catch {
      // A failed check is not a completed step.
    }
    try {
      const readiness = await api.knowledgeReadiness()
      next.knowledge = Boolean(readiness?.ready || readiness?.documents > 0)
    } catch {
      /* not ready */
    }
    try {
      const org = await api.activeOrganization()
      next.prompt = Boolean((org?.sales_prompt || '').trim().length > 40)
    } catch {
      /* not ready */
    }
    try {
      next.tested = localStorage.getItem('pingpulse.tested') === 'yes'
    } catch {
      /* storage blocked */
    }
    setState(next)
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!state) {
    return (
      <section className={`panel flex-1 items-center justify-center ${className}`}>
        <Loader2 size={16} className="animate-spin text-faint" />
      </section>
    )
  }

  const steps = [
    {
      done: state.whatsapp,
      title: 'Connect your WhatsApp',
      body: 'Scan the QR with the phone that owns your business number, or enter your Twilio details.',
    },
    {
      done: state.prompt,
      title: 'Say what you sell and how',
      body: 'A few sentences in your own words. This is what the agent works from before anything else.',
    },
    {
      done: state.knowledge,
      title: 'Give it your prices',
      body: 'Upload a price list or import your WhatsApp catalogue. It will never invent a price, so it can only quote what you give it.',
    },
    {
      done: state.tested,
      title: 'Try it before a customer does',
      body: 'Use the sandbox to see exactly what it would say. Nothing is sent.',
    },
  ]
  const remaining = steps.filter((step) => !step.done).length

  const done = steps.length - remaining

  return (
    <section className={`panel flex-1 overflow-y-auto ${className}`}>
      <div className="mx-auto w-full max-w-xl px-5 py-8 sm:px-8 sm:py-12">
        <span className="mb-5 grid h-12 w-12 place-items-center rounded-2xl bg-accent/10 text-accent">
          <Rocket size={22} />
        </span>
        <h2 className="text-xl font-semibold tracking-tight text-ink">
          {remaining === 0 ? 'You are ready for customers' : 'Let’s get you live'}
        </h2>
        <p className="mt-1.5 text-sm leading-relaxed text-dim">
          {remaining === 0
            ? 'Everything is ready. Your first conversation will appear here as soon as somebody messages you.'
            : 'Four things, and then the first customer who messages you gets an answer.'}
        </p>

        <div className="mt-5 flex items-center gap-3">
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-edge">
            <div
              className="h-full rounded-full bg-accent transition-[width] duration-500"
              style={{ width: `${(done / steps.length) * 100}%` }}
            />
          </div>
          <span className="text-xs font-semibold tabular-nums text-dim">
            {done} of {steps.length}
          </span>
        </div>

        <ol className="mt-6 space-y-2.5">
          {steps.map((step, index) => (
            <li
              key={step.title}
              className={`flex gap-3.5 rounded-2xl border px-4 py-3.5 ${
                step.done ? 'border-edge bg-panel-2/50' : 'border-edge bg-panel'
              }`}
            >
              <span
                className={`mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold ${
                  step.done ? 'bg-accent text-on-accent' : 'bg-panel-2 text-dim ring-1 ring-inset ring-edge'
                }`}
              >
                {step.done ? <Check size={14} /> : index + 1}
              </span>
              <div className="min-w-0">
                <p
                  className={`text-sm font-semibold ${step.done ? 'text-dim line-through' : 'text-ink'}`}
                >
                  {step.title}
                </p>
                <p className="mt-0.5 text-xs leading-relaxed text-dim">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>

        <div className="mt-6 flex flex-wrap gap-2">
          {remaining > 0 && (
            <button type="button" onClick={onOpenSettings} className="btn-primary">
              Continue setup <ArrowRight size={15} />
            </button>
          )}
          {onTest && (
            <button type="button" onClick={onTest} className="btn-secondary">
              <FlaskConical size={15} /> Test the agent
            </button>
          )}
        </div>
      </div>
    </section>
  )
}
