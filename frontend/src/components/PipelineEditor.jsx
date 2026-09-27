import { useCallback, useEffect, useState } from 'react'
import { ArrowDown, ArrowUp, Check, Columns3, Loader2, Plus, TriangleAlert, X } from 'lucide-react'
import { api } from '../api.js'

const OUTCOMES = [
  ['', 'In progress'],
  ['booked', 'Booked'],
  ['won', 'Won'],
  ['lost', 'Lost'],
  ['unqualified', 'Unqualified'],
]

/**
 * A shop's own board: its columns, in its own order and its own words.
 *
 * The nine stages that ship are a starting point rather than a rule. A
 * contractor's "Estimate sent" is their whole business and means nothing to a
 * salon, so the columns are editable and everything downstream reads the
 * labels from here.
 *
 * "What it counts as" is the part worth explaining rather than hiding. A board
 * can be renamed and translated freely, so the metrics cannot look for a
 * column called "Won" — the column says what it means, and the number on the
 * dashboard follows whatever that shop calls it.
 *
 * Deleting a column with people in it is refused by the server, and the error
 * says how many are there. That is a better answer than a confirm dialog: the
 * question is not "are you sure" but "where would those people go".
 */
export default function PipelineEditor() {
  const [stages, setStages] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)

  const load = useCallback(async () => {
    try {
      const board = await api.getPipeline()
      setStages(board.stages || [])
    } catch {
      setStages([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (!stages) return null

  const update = (index, patch) =>
    setStages(stages.map((stage, i) => (i === index ? { ...stage, ...patch } : stage)))

  const move = (index, by) => {
    const next = [...stages]
    const target = index + by
    if (target < 0 || target >= next.length) return
    ;[next[index], next[target]] = [next[target], next[index]]
    setStages(next)
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    setNote(null)
    try {
      const board = await api.savePipeline(
        stages.map((stage, index) => ({
          key: stage.key,
          label: stage.label,
          colour: stage.colour || 'slate',
          outcome: stage.outcome || null,
          is_entry: index === 0,
        })),
      )
      setStages(board.stages)
      setNote('Saved. Reload to see the board change.')
    } catch (err) {
      setError(err.message)
    }
    setSaving(false)
  }

  return (
    <section className="space-y-3">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
          <Columns3 size={14} className="text-accent" />
        </span>
        <div className="min-w-0">
          <h4 className="text-xs font-semibold text-ink">Your pipeline</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            The columns on your board. New leads land in the first one.
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

      <div className="space-y-1.5">
        {stages.map((stage, index) => (
          <div key={stage.key} className="flex items-center gap-1.5">
            <span className="w-4 shrink-0 text-center font-mono text-2xs text-faint">
              {index + 1}
            </span>
            <input
              value={stage.label}
              onChange={(e) => update(index, { label: e.target.value })}
              className="min-w-0 flex-1 rounded-lg border border-edge bg-bg px-2 py-1 text-2xs text-ink focus:border-accent/60"
            />
            <select
              value={stage.outcome || ''}
              onChange={(e) => update(index, { outcome: e.target.value || null })}
              title="What this column counts as"
              className="w-24 shrink-0 rounded-lg border border-edge bg-bg px-1.5 py-1 text-2xs text-dim focus:border-accent/60"
            >
              {OUTCOMES.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={() => move(index, -1)}
              disabled={index === 0}
              className="shrink-0 rounded p-1 text-faint transition-colors hover:text-ink disabled:opacity-25"
            >
              <ArrowUp size={11} />
            </button>
            <button
              type="button"
              onClick={() => move(index, 1)}
              disabled={index === stages.length - 1}
              className="shrink-0 rounded p-1 text-faint transition-colors hover:text-ink disabled:opacity-25"
            >
              <ArrowDown size={11} />
            </button>
            <button
              type="button"
              onClick={() => setStages(stages.filter((_, i) => i !== index))}
              title="Remove"
              className="shrink-0 rounded p-1 text-faint transition-colors hover:text-crit"
            >
              <X size={11} />
            </button>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() =>
            setStages([
              ...stages,
              { key: `STAGE_${Date.now().toString(36).toUpperCase()}`, label: 'New stage', colour: 'slate' },
            ])
          }
          className="flex items-center gap-1.5 rounded-lg border border-edge px-2.5 py-1.5 text-2xs text-ink transition-colors hover:bg-panel-2"
        >
          <Plus size={12} /> Add a stage
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={save}
          className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {saving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
          Save board
        </button>
      </div>

      <p className="text-2xs leading-relaxed text-faint">
        A stage with people still in it cannot be removed — move them first, or they
        would vanish from the board entirely.
      </p>
    </section>
  )
}
