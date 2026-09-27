import { ArrowRight, Check, Lock, Rocket } from 'lucide-react'
import { STEPS } from '../setup.js'

/**
 * The required steps as a short list, ticked from the server's answer.
 */
function RequiredList({ setup, only }) {
  const steps = STEPS.filter((s) => s.tier === 'required' && (!only || only.includes(s.key)))
  return (
    <ol className="space-y-2">
      {steps.map((step) => {
        const done = setup?.hasOrg && setup.gate[step.key]
        return (
          <li
            key={step.key}
            className={`flex items-center gap-3 rounded-xl border px-3.5 py-2.5 ${
              done ? 'border-edge bg-panel-2/50' : 'border-edge bg-panel'
            }`}
          >
            <span
              className={`grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold ${
                done ? 'bg-accent text-on-accent' : 'bg-warn/15 text-warn'
              }`}
            >
              {done ? <Check size={14} /> : STEPS.indexOf(step) + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span
                className={`block text-sm font-semibold ${done ? 'text-dim line-through' : 'text-ink'}`}
              >
                {step.title}
              </span>
              <span className="block text-xs text-dim">{step.short}</span>
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/**
 * What somebody is told the moment they come in, while the agent cannot run.
 *
 * Shown on every visit until the required steps are done - not once and
 * dismissed forever - because a business that closed it and walked away is
 * exactly the one that comes back wondering why nobody got an answer.
 */
export function SetupWelcome({ setup, left, onStart, onClose }) {
  const fresh = !setup?.hasOrg
  return (
    <div
      className="scrim fixed inset-0 z-50 grid place-items-center p-4"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="setup-welcome-title"
        className="animate-pop w-full max-w-md rounded-3xl border border-edge bg-panel p-6 shadow-lift sm:p-7"
      >
        <span className="mb-4 grid h-12 w-12 place-items-center rounded-2xl bg-accent/10 text-accent">
          <Rocket size={22} />
        </span>
        <h2 id="setup-welcome-title" className="text-xl font-semibold tracking-tight text-ink">
          {fresh ? 'Welcome to PingPulse' : 'Your agent is not running yet'}
        </h2>
        <p className="mt-1.5 text-sm leading-relaxed text-dim">
          {fresh
            ? 'Four steps and your agent starts answering customers on WhatsApp. The rest of the dashboard opens as soon as they are done.'
            : `${left.length} required step${left.length === 1 ? ' is' : 's are'} still open. Until ${
                left.length === 1 ? 'it is' : 'they are'
              } done, customers who message you get no answer, and the inbox stays locked.`}
        </p>

        <div className="mt-5">
          <RequiredList setup={setup} />
        </div>

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <button type="button" onClick={onStart} className="btn-primary">
            {fresh ? 'Start setup' : `Continue with ${left[0]?.title || 'setup'}`}
            <ArrowRight size={15} />
          </button>
          <button type="button" onClick={onClose} className="btn-ghost">
            Later
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * What a locked page shows instead of itself: what is missing and the way to it.
 */
export function LockedPage({ title, missing, setup, onStart }) {
  return (
    <div className="grid h-full place-items-center overflow-y-auto p-4 sm:p-6">
      <div className="w-full max-w-md py-8">
        <span className="mb-4 grid h-12 w-12 place-items-center rounded-2xl bg-warn/10 text-warn">
          <Lock size={20} />
        </span>
        <h2 className="text-xl font-semibold tracking-tight text-ink">
          {title} opens once setup is done
        </h2>
        <p className="mt-1.5 text-sm leading-relaxed text-dim">
          Your agent cannot work without{' '}
          {missing.length === 1 ? 'this step' : `these ${missing.length} steps`}. Finish{' '}
          {missing.length === 1 ? 'it' : 'them'} and this page unlocks by itself.
        </p>
        <div className="mt-5">
          <RequiredList setup={setup} only={missing.map((s) => s.key)} />
        </div>
        <button type="button" onClick={onStart} className="btn-primary mt-6">
          Go to {missing[0]?.title || 'Setup'} <ArrowRight size={15} />
        </button>
      </div>
    </div>
  )
}
