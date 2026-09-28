import { useState } from 'react'
import { FlaskConical, Loader2, RotateCcw, Send, ShieldAlert, TriangleAlert } from 'lucide-react'
import { PageHeader } from './ui.jsx'
import { api } from '../api.js'

/**
 * Try a message against the real agent without a real customer receiving it.
 *
 * Every shop wants to know what it will say before it says it to somebody who
 * matters, and the only way to find out used to be messaging the number from
 * a second phone. That tests it, and it also puts a fake lead in the pipeline
 * and a real message in somebody's WhatsApp.
 *
 * This runs the same prompt assembly, the same knowledge base and the same
 * operating rules, and sends nothing. A sandbox that tests a different prompt
 * from the live one tests nothing, which is why it is the real path with the
 * sending removed rather than a separate imitation of it.
 *
 * Escalations are shown rather than answered. Somebody typing "I want a
 * refund" should see that a real conversation stops there and waits for a
 * person — that is the behaviour, and hiding it behind a smooth reply that
 * would never have been sent would be a lie about what the agent does.
 */
const SUGGESTIONS = [
  'How much is it?',
  'Do you deliver today?',
  'Can I book for tomorrow?',
  'I want a refund',
]

export default function AgentSandbox() {
  const [turns, setTurns] = useState([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const send = async () => {
    const text = message.trim()
    if (!text || busy) return

    setBusy(true)
    setError(null)
    const history = turns
      .filter((turn) => turn.reply)
      .flatMap((turn) => [
        { sender: 'user', content: turn.message },
        { sender: 'agent', content: turn.reply },
      ])

    try {
      const result = await api.simulate(text, history)
      setTurns((was) => [...was, { message: text, ...result }])
      setMessage('')
      try {
        localStorage.setItem('pingpulse.tested', 'yes')
      } catch {
        // Storage blocked; the setup checklist simply keeps this step open.
      }
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader
        icon={FlaskConical}
        title="Test your agent"
        subtitle="The real agent, with your prices and rules. Nothing is sent to anyone."
      >
        {turns.length > 0 && (
          <button type="button" onClick={() => setTurns([])} className="btn-secondary">
            <RotateCcw size={14} /> Start over
          </button>
        )}
      </PageHeader>

      <div className="mx-4 mb-4 flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-edge bg-panel shadow-card sm:mx-6 lg:mx-8 lg:mb-6">
        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-5 sm:px-6">
          {turns.length === 0 && (
            <div className="mx-auto max-w-md py-10 text-center">
              <span className="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-2xl bg-accent/10 text-accent">
                <FlaskConical size={22} />
              </span>
              <p className="text-base font-semibold text-ink">Ask it what a customer would</p>
              <p className="mt-1.5 text-sm leading-relaxed text-dim">
                Try a price, something you do not sell, and a complaint. Those three show
                whether it is set up properly.
              </p>
              <div className="mt-5 flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((text) => (
                  <button
                    key={text}
                    type="button"
                    onClick={() => setMessage(text)}
                    className="rounded-full border border-edge px-3 py-1.5 text-xs font-medium text-dim transition hover:border-accent/50 hover:text-accent"
                  >
                    {text}
                  </button>
                ))}
              </div>
            </div>
          )}

          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          {turns.map((turn, index) => (
            <div key={index} className="space-y-2">
              <p className="ml-auto w-fit max-w-[80%] rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm leading-relaxed text-on-accent">
                {turn.message}
              </p>

              {turn.escalated ? (
                <div className="w-fit max-w-[85%] rounded-2xl rounded-bl-md bg-warn/10 px-4 py-3">
                  <p className="flex items-center gap-1.5 text-2xs font-semibold text-warn">
                    <ShieldAlert size={12} />
                    Handed to a person
                  </p>
                  <p className="mt-1 text-2xs leading-relaxed text-dim">{turn.note}</p>
                </div>
              ) : (
                <div className="w-fit max-w-[85%] rounded-2xl rounded-bl-md bg-panel-2 px-4 py-2.5">
                  <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{turn.reply}</p>
                  <QuoteRead quote={turn.quote} />
                  <p className="mt-1.5 flex flex-wrap items-center gap-x-2 font-mono text-[11px] text-faint">
                    <span>{turn.provider}</span>
                    <span>{turn.latency_ms}ms</span>
                    {turn.fallback_used && (
                      <span
                        className="text-warn"
                        title={
                          turn.provider === 'none'
                            ? 'Neither AI provider answered, so this reply was put together directly from your setup and documents.'
                            : 'The first AI provider failed; the second one answered.'
                        }
                      >
                        {turn.provider === 'none' ? 'answered without AI' : 'fallback'}
                      </span>
                    )}
                    {turn.knowledge_used?.length > 0 && (
                      <span>read: {turn.knowledge_used.join(', ')}</span>
                    )}
                  </p>
                </div>
              )}
            </div>
          ))}
        </div>

        <footer className="flex items-end gap-2 border-t border-edge bg-panel px-3 py-3 sm:px-4">
          <textarea
            rows={1}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                send()
              }
            }}
            placeholder="How much are the black boots?"
            className="min-h-[44px] flex-1 resize-none rounded-xl border border-edge bg-panel-2 px-4 py-2.5 text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:bg-panel focus:outline-none"
          />
          <button
            type="button"
            disabled={busy || !message.trim()}
            onClick={send}
            className="btn-primary h-11"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
            Ask
          </button>
        </footer>
      </div>
    </div>
  )
}

/**
 * How the price list was read for this message: the product it matched, the
 * unit it is sold in, and the sums the reply was allowed to use. Shown so a
 * shop can see why the agent said what it said - and spot a row read wrongly
 * before a customer does.
 */
function QuoteRead({ quote }) {
  if (!quote || (!quote.lines?.length && !quote.options?.length)) return null
  return (
    <div className="mt-2 space-y-1 rounded-xl border border-edge bg-panel px-3 py-2 text-[11px] leading-relaxed text-dim">
      <p className="font-semibold text-faint">From your price list</p>
      {quote.lines.map((line) => (
        <p key={line.item}>
          <span className="text-ink">{line.item}</span> — sold as {line.sold_as} at {line.price}
          {line.total && line.units ? `; ${line.units} = ${line.total}` : ''}
        </p>
      ))}
      {quote.options.map((choices) => (
        <p key={choices.join('|')}>
          Could be any of: <span className="text-ink">{choices.join(', ')}</span>
        </p>
      ))}
      {quote.subtotal && (
        <p>
          Subtotal <span className="text-ink">{quote.subtotal}</span> before tax and delivery
        </p>
      )}
    </div>
  )
}
