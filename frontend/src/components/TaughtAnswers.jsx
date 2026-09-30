import { useCallback, useEffect, useState } from 'react'
import { BookOpenCheck, Check, Loader2, Plus, TriangleAlert, X } from 'lucide-react'
import { api } from '../api.js'

/**
 * The agent learning from the team's own answers.
 *
 * When the agent can't answer something, the question lands here. When
 * someone at the shop replies to that customer from the dashboard, the reply
 * is shown beside it. "Teach" makes it something the agent knows from then on.
 * Nothing is learned from the agent's own replies, and nothing is live until a
 * person presses Teach - which is what makes it safe to leave running.
 */
export default function TaughtAnswers() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [adding, setAdding] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api.listLearnedAnswers())
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!data && !error) return <Loader2 size={16} className="animate-spin text-faint" />

  const suggested = data?.suggested || []
  const waiting = data?.waiting || []
  const taught = data?.taught || []

  return (
    <section className="space-y-4">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
          <BookOpenCheck size={14} className="text-accent" />
        </span>
        <div className="min-w-0 flex-1">
          <h4 className="text-xs font-semibold text-ink">Answers from your team</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            Optional. The agent works from your documents on its own. This is only for things your
            documents don't say: questions it couldn't answer come here, with your team's reply
            beside them. Press <b>Teach</b> if you want it to answer that itself next time.
            Nothing here is used until you do.
          </p>
        </div>
      </header>

      {error && (
        <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}

      {suggested.length > 0 && (
        <div className="space-y-2">
          <p className="eyebrow">Ready to teach ({suggested.length})</p>
          {suggested.map((row) => (
            <Suggestion key={row.id} row={row} onDone={load} />
          ))}
        </div>
      )}

      {waiting.length > 0 && (
        <div className="space-y-1.5">
          <p className="eyebrow">Waiting for your team's answer ({waiting.length})</p>
          <ul className="space-y-1">
            {waiting.map((row) => (
              <li
                key={row.id}
                className="flex items-start gap-2 rounded-lg border border-edge px-3 py-2 text-2xs text-dim"
              >
                <span className="min-w-0 flex-1">
                  <span className="text-ink">“{row.question}”</span>
                  {row.customer && <span className="text-faint"> · {row.customer}</span>}
                </span>
                <Dismiss row={row} onDone={load} label="Not needed" />
              </li>
            ))}
          </ul>
          <p className="text-2xs text-faint">
            Answer the customer in the Inbox and it moves up to "Ready to teach".
          </p>
        </div>
      )}

      {suggested.length === 0 && waiting.length === 0 && (
        <p className="rounded-lg border border-edge px-3 py-2.5 text-2xs text-dim">
          Nothing waiting. Questions the agent can't answer will appear here.
        </p>
      )}

      {adding ? (
        <Editor
          onCancel={() => setAdding(false)}
          onSave={async (question, answer) => {
            await api.teachNewAnswer(question, answer)
            setAdding(false)
            await load()
          }}
        />
      ) : (
        <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => setAdding(true)}>
          <Plus size={13} /> Teach an answer yourself
        </button>
      )}

      {taught.length > 0 && (
        <details className="rounded-lg border border-edge px-3 py-2">
          <summary className="cursor-pointer text-xs font-medium text-ink">
            What the agent has been taught ({taught.length})
          </summary>
          <ul className="mt-2 space-y-2">
            {taught.map((row) => (
              <li key={row.id} className="flex items-start gap-2 text-2xs">
                <span className="min-w-0 flex-1">
                  <span className="block font-medium text-ink">{row.question}</span>
                  <span className="block whitespace-pre-wrap text-dim">{row.answer}</span>
                </span>
                <Dismiss row={row} onDone={load} label="Forget" />
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

function Suggestion({ row, onDone }) {
  const [editing, setEditing] = useState(false)
  if (editing) {
    return (
      <Editor
        initialQuestion={row.question}
        initialAnswer={row.answer}
        onCancel={() => setEditing(false)}
        onSave={async (question, answer) => {
          await api.teachAnswer(row.id, question, answer)
          await onDone()
        }}
      />
    )
  }
  return (
    <div className="space-y-2 rounded-xl border border-edge bg-panel-2/40 px-3 py-2.5">
      <p className="text-2xs text-faint">
        {row.customer ? `${row.customer} asked` : 'A customer asked'}
      </p>
      <p className="text-xs font-medium text-ink">“{row.question}”</p>
      <p className="whitespace-pre-wrap rounded-lg bg-panel px-2.5 py-2 text-xs text-dim">{row.answer}</p>
      {row.watch_out?.length > 0 && (
        <div className="rounded-lg bg-warn/10 px-2.5 py-2 text-2xs text-warn">
          <p className="font-semibold">Check before teaching - this is for every customer:</p>
          <ul className="mt-0.5 list-disc pl-4">
            {row.watch_out.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {row.watch_out?.length > 0 ? (
          <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => setEditing(true)}>
            Edit, then teach
          </button>
        ) : (
          <TeachNow row={row} onDone={onDone} />
        )}
        {row.watch_out?.length === 0 && (
          <button type="button" className="btn-ghost px-2.5 py-1 text-xs" onClick={() => setEditing(true)}>
            Edit first
          </button>
        )}
        <Dismiss row={row} onDone={onDone} label="Don't teach" />
      </div>
    </div>
  )
}

function TeachNow({ row, onDone }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  return (
    <>
      <button
        type="button"
        className="btn-primary px-3 py-1.5 text-xs"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setError(null)
          try {
            await api.teachAnswer(row.id, row.question, row.answer)
            await onDone()
          } catch (err) {
            setError(err.message)
            setBusy(false)
          }
        }}
      >
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />} Teach
      </button>
      {error && <span className="text-2xs text-crit">{error}</span>}
    </>
  )
}

function Dismiss({ row, onDone, label }) {
  const [busy, setBusy] = useState(false)
  return (
    <button
      type="button"
      className="btn-ghost shrink-0 px-2 py-1 text-2xs text-faint hover:text-crit"
      disabled={busy}
      onClick={async () => {
        setBusy(true)
        try {
          await api.dismissLearnedAnswer(row.id)
          await onDone()
        } catch {
          setBusy(false)
        }
      }}
    >
      <X size={11} /> {label}
    </button>
  )
}

function Editor({ initialQuestion = '', initialAnswer = '', onSave, onCancel }) {
  const [question, setQuestion] = useState(initialQuestion)
  const [answer, setAnswer] = useState(initialAnswer)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const field =
    'mt-1 block w-full rounded-xl border border-edge bg-panel px-3 py-2 text-xs text-ink focus:border-accent/60 focus:outline-none'
  return (
    <div className="space-y-2 rounded-xl border border-accent/30 px-3 py-2.5">
      <label className="block text-2xs font-medium text-ink">
        When a customer asks
        <input value={question} onChange={(e) => setQuestion(e.target.value)} className={field} />
      </label>
      <label className="block text-2xs font-medium text-ink">
        The answer is (true for every customer)
        <textarea value={answer} onChange={(e) => setAnswer(e.target.value)} rows={3} className={field} />
      </label>
      {error && <p className="text-2xs text-crit">{error}</p>}
      <div className="flex gap-2">
        <button
          type="button"
          className="btn-primary px-3 py-1.5 text-xs"
          disabled={busy || question.trim().length < 3 || !answer.trim()}
          onClick={async () => {
            setBusy(true)
            setError(null)
            try {
              await onSave(question.trim(), answer.trim())
            } catch (err) {
              setError(err.message)
              setBusy(false)
            }
          }}
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />} Teach
        </button>
        <button type="button" className="btn-ghost px-2.5 py-1 text-xs" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  )
}
