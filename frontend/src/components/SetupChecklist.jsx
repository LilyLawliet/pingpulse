import { useCallback, useEffect, useState } from 'react'
import { Check, Circle, Loader2, Rocket } from 'lucide-react'
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
export default function SetupChecklist({ onOpenSettings }) {
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
      <section className="panel flex flex-1 items-center justify-center">
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

  return (
    <section className="panel flex flex-1 flex-col">
      <header className="panel-head">
        <Rocket size={14} className="text-accent" />
        <h2 className="text-xs font-semibold text-ink">Getting set up</h2>
        <span className="ml-auto font-mono text-2xs text-faint">
          {steps.length - remaining} of {steps.length}
        </span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
        <p className="mb-4 text-2xs leading-relaxed text-dim">
          {remaining === 0
            ? 'Everything is ready. Your first conversation will appear here as soon as somebody messages you.'
            : 'Four things, and then the first customer who messages you gets an answer.'}
        </p>

        <ol className="space-y-3">
          {steps.map((step) => (
            <li key={step.title} className="flex gap-2.5">
              <span
                className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full ${
                  step.done ? 'bg-accent/15 text-accent' : 'bg-panel-2 text-faint'
                }`}
              >
                {step.done ? <Check size={12} /> : <Circle size={8} />}
              </span>
              <div className="min-w-0">
                <p
                  className={`text-xs font-semibold ${
                    step.done ? 'text-dim line-through' : 'text-ink'
                  }`}
                >
                  {step.title}
                </p>
                <p className="mt-0.5 text-2xs leading-relaxed text-dim">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>

        {remaining > 0 && (
          <button
            type="button"
            onClick={onOpenSettings}
            className="mt-5 rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-bg transition-opacity hover:opacity-90"
          >
            Open settings
          </button>
        )}
      </div>
    </section>
  )
}
