import { useState } from 'react'
import { FlaskConical, Loader2, Send, ShieldAlert, TriangleAlert, X } from 'lucide-react'
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
export default function AgentSandbox({ onClose }) {
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
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm">
      <div className="flex max-h-[88vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-edge bg-panel shadow-lift">
        <header className="flex items-center gap-2.5 border-b border-edge px-5 py-4">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-accent/12 ring-1 ring-inset ring-accent/25">
            <FlaskConical size={15} className="text-accent" />
          </span>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-ink">Try it out</h3>
            <p className="mt-0.5 text-2xs text-dim">
              The real agent, with your prices and rules. Nothing is sent.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="ml-auto rounded-lg p-1 text-faint transition-colors hover:bg-panel-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {turns.length === 0 && (
            <p className="py-6 text-center text-2xs leading-relaxed text-dim">
              Type what a customer might ask. Try a price, something you do not sell,
              and a complaint — the three that show whether it is set up properly.
            </p>
          )}

          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          {turns.map((turn, index) => (
            <div key={index} className="space-y-2">
              <p className="ml-auto max-w-[80%] rounded-xl rounded-br-sm bg-customer/15 px-3 py-2 text-[13px] leading-relaxed text-ink">
                {turn.message}
              </p>

              {turn.escalated ? (
                <div className="max-w-[85%] rounded-xl rounded-bl-sm bg-warn/10 px-3 py-2.5">
                  <p className="flex items-center gap-1.5 text-2xs font-semibold text-warn">
                    <ShieldAlert size={12} />
                    Handed to a person
                  </p>
                  <p className="mt-1 text-2xs leading-relaxed text-dim">{turn.note}</p>
                </div>
              ) : (
                <div className="max-w-[85%] rounded-xl rounded-bl-sm bg-panel-2 px-3 py-2">
                  <p className="text-[13px] leading-relaxed text-ink">{turn.reply}</p>
                  <p className="mt-1.5 flex flex-wrap items-center gap-x-2 font-mono text-[10px] text-faint">
                    <span>{turn.provider}</span>
                    <span>{turn.latency_ms}ms</span>
                    {turn.fallback_used && <span className="text-warn">fallback</span>}
                    {turn.knowledge_used?.length > 0 && (
                      <span>read: {turn.knowledge_used.join(', ')}</span>
                    )}
                  </p>
                </div>
              )}
            </div>
          ))}
        </div>

        <footer className="flex items-end gap-2 border-t border-edge px-5 py-3.5">
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
            className="min-h-[38px] flex-1 resize-none rounded-lg border border-edge bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-faint focus:border-accent/60"
          />
          <button
            type="button"
            disabled={busy || !message.trim()}
            onClick={send}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {busy ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
            Ask
          </button>
        </footer>
      </div>
    </div>
  )
}
