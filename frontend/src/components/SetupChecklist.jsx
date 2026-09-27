import { ArrowRight, Check, FlaskConical, Radio, TriangleAlert } from 'lucide-react'
import { STEPS } from '../setup.js'

/**
 * What a business with no conversations yet looks at.
 *
 * The inbox only opens once the required steps are done, so by the time this
 * is on screen the agent is already able to answer. What is left to say is
 * that it is waiting for the first customer, and which of the recommended
 * steps would make that first conversation go better.
 *
 * Steps are read from the same list and the same server answer as Setup, so
 * this can never disagree with it about what is done. It disappears the moment
 * a conversation exists.
 */
export default function SetupChecklist({ setup, offline = false, onOpenStep, onTest, className = '' }) {
  const recommended = STEPS.filter((step) => step.tier === 'recommended')
  const open = recommended.filter((step) => !setup?.done?.[step.key])

  return (
    <section className={`panel flex-1 overflow-y-auto ${className}`}>
      <div className="mx-auto w-full max-w-xl px-5 py-8 sm:px-8 sm:py-12">
        {offline ? (
          <>
            <span className="mb-5 grid h-12 w-12 place-items-center rounded-2xl bg-crit/10 text-crit">
              <TriangleAlert size={22} />
            </span>
            <h2 className="text-xl font-semibold tracking-tight text-ink">
              WhatsApp is not connected right now
            </h2>
            <p className="mt-1.5 text-sm leading-relaxed text-dim">
              Nothing reaches the agent until it reconnects, so nobody who messages you now
              gets an answer.
            </p>
            <button type="button" onClick={() => onOpenStep?.('whatsapp')} className="btn-primary mt-4">
              Reconnect WhatsApp <ArrowRight size={15} />
            </button>
          </>
        ) : (
          <>
            <span className="mb-5 grid h-12 w-12 place-items-center rounded-2xl bg-accent/10 text-accent">
              <Radio size={22} />
            </span>
            <h2 className="text-xl font-semibold tracking-tight text-ink">
              Your agent is ready for customers
            </h2>
            <p className="mt-1.5 text-sm leading-relaxed text-dim">
              The first conversation appears here the moment somebody messages your WhatsApp
              number, and the agent answers it straight away.
            </p>
          </>
        )}

        {open.length > 0 && (
          <>
            <p className="mt-7 text-sm font-semibold text-ink">
              Worth doing before they arrive
            </p>
            <p className="mt-0.5 text-xs text-dim">
              Not required. Each one fills a gap the agent otherwise works around.
            </p>
          </>
        )}

        <ol className="mt-3 space-y-2.5">
          {recommended.map((step) => {
            const done = Boolean(setup?.done?.[step.key])
            return (
              <li key={step.key}>
                <button
                  type="button"
                  onClick={() => onOpenStep?.(step.key)}
                  className={`flex w-full items-center gap-3.5 rounded-2xl border px-4 py-3.5 text-left transition-colors ${
                    done
                      ? 'border-edge bg-panel-2/50'
                      : 'border-edge bg-panel hover:border-accent/40 hover:bg-accent/5'
                  }`}
                >
                  <span
                    className={`grid h-7 w-7 shrink-0 place-items-center rounded-full ${
                      done ? 'bg-accent text-on-accent' : 'bg-panel-2 text-dim ring-1 ring-inset ring-edge'
                    }`}
                  >
                    {done ? <Check size={14} /> : <step.icon size={14} />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span
                      className={`block text-sm font-semibold ${done ? 'text-dim line-through' : 'text-ink'}`}
                    >
                      {step.title}
                    </span>
                    <span className="mt-0.5 block text-xs leading-relaxed text-dim">
                      {done ? step.short : step.skipped}
                    </span>
                  </span>
                  {!done && <ArrowRight size={15} className="shrink-0 text-faint" />}
                </button>
              </li>
            )
          })}
        </ol>

        {onTest && (
          <button type="button" onClick={onTest} className="btn-secondary mt-6">
            <FlaskConical size={15} /> Test the agent first
          </button>
        )}
      </div>
    </section>
  )
}
