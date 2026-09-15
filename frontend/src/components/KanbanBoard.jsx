import { useCallback, useEffect, useMemo, useState } from 'react'
import { Columns3, Loader2, TriangleAlert, X } from 'lucide-react'
import { api } from '../api.js'
import { DEFAULT_STAGES, contactLabel, initialsOf, stageDot } from '../format.js'

/**
 * The board, as a board: columns side by side, scrolling sideways.
 *
 * The rail in the main layout answers "where does everything stand" at a
 * glance and is the right thing for a 290px column. It is not a Kanban — you
 * cannot see two stages next to each other, and you cannot move anybody. This
 * is the other half.
 *
 * Cards are draggable, and they also carry a stage picker. That is not
 * redundancy for its own sake: HTML5 drag-and-drop does not fire on touch at
 * all, so on the phone that is the primary device for most of these operators
 * the drag handle is decoration. One of the two always works.
 *
 * Moves are optimistic. A lead dragged across the board snaps to its new
 * column immediately and only rolls back if the server refuses, because the
 * alternative is a card that hangs in mid-air for the length of a round trip
 * and makes the whole board feel broken.
 */
export default function KanbanBoard({ contacts, stages = DEFAULT_STAGES, onClose, onChanged, onOpen }) {
  const [board, setBoard] = useState(contacts)
  const [dragging, setDragging] = useState(null)
  const [over, setOver] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    setBoard(contacts)
  }, [contacts])

  const columns = useMemo(() => {
    const list = stages.length ? stages : DEFAULT_STAGES
    return list.map((stage) => ({
      stage,
      people: board.filter((person) => person.pipeline_stage === stage.key),
    }))
  }, [board, stages])

  const move = useCallback(
    async (contactId, toStage) => {
      const person = board.find((row) => row.id === contactId)
      if (!person || person.pipeline_stage === toStage) return

      const was = person.pipeline_stage
      setError(null)
      setBusy(contactId)
      setBoard((rows) =>
        rows.map((row) => (row.id === contactId ? { ...row, pipeline_stage: toStage } : row)),
      )
      try {
        await api.updateContact(contactId, { pipeline_stage: toStage })
        onChanged?.()
      } catch (err) {
        // Put it back where it came from. A card that stays in the new column
        // after the server refused is a lie the operator will act on.
        setBoard((rows) =>
          rows.map((row) => (row.id === contactId ? { ...row, pipeline_stage: was } : row)),
        )
        setError(err.message)
      }
      setBusy(null)
    },
    [board, onChanged],
  )

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-bg/95 backdrop-blur-sm">
      <header className="flex shrink-0 flex-wrap items-center gap-2.5 border-b border-edge bg-panel px-4 py-3">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-accent/12 ring-1 ring-inset ring-accent/25">
          <Columns3 size={15} className="text-accent" />
        </span>
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink">Your board</h3>
          <p className="mt-0.5 text-2xs text-dim">
            Drag a lead to move it, or use the menu on the card
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

      {error && (
        <p className="flex shrink-0 items-start gap-2 bg-crit/10 px-4 py-2 text-2xs text-crit">
          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}

      {/* The horizontal scroll the whole component exists for. Columns keep a
          fixed width and refuse to shrink, so nine stages stay legible rather
          than being squeezed into whatever is on screen. */}
      <div className="min-h-0 flex-1 overflow-x-auto overflow-y-hidden p-3">
        <div className="flex h-full gap-3">
          {columns.map(({ stage, people }) => (
            <section
              key={stage.key}
              onDragOver={(event) => {
                event.preventDefault()
                setOver(stage.key)
              }}
              onDragLeave={() => setOver((current) => (current === stage.key ? null : current))}
              onDrop={(event) => {
                event.preventDefault()
                setOver(null)
                const id = event.dataTransfer.getData('text/plain') || dragging
                if (id) move(id, stage.key)
              }}
              className={`flex h-full w-64 shrink-0 flex-col rounded-xl border bg-panel transition-colors ${
                over === stage.key ? 'border-accent/50 bg-panel-2' : 'border-edge'
              }`}
            >
              <header className="flex shrink-0 items-center gap-2 border-b border-edge px-3 py-2.5">
                <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${stageDot(stages, stage.key)}`} />
                <span className="min-w-0 flex-1 truncate text-2xs font-semibold text-ink">
                  {stage.label}
                </span>
                <span className="font-mono text-2xs tabular-nums text-dim">{people.length}</span>
              </header>

              <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2">
                {people.length === 0 && (
                  <p className="px-1 py-2 text-2xs text-faint">Nobody here</p>
                )}
                {people.map((person) => (
                  <article
                    key={person.id}
                    draggable
                    onDragStart={(event) => {
                      event.dataTransfer.setData('text/plain', person.id)
                      event.dataTransfer.effectAllowed = 'move'
                      setDragging(person.id)
                    }}
                    onDragEnd={() => {
                      setDragging(null)
                      setOver(null)
                    }}
                    className={`rounded-lg border border-edge bg-panel-2/60 p-2 transition-opacity ${
                      dragging === person.id ? 'opacity-40' : ''
                    } ${busy === person.id ? 'opacity-60' : ''}`}
                  >
                    <button
                      type="button"
                      onClick={() => onOpen?.(person.id)}
                      className="flex w-full items-center gap-2 text-left"
                    >
                      <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-edge text-[9px] font-semibold text-dim">
                        {initialsOf(person.name, person.phone_number)}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-2xs text-ink">
                          {contactLabel(person)}
                        </span>
                        {person.deal_value != null && (
                          <span className="block font-mono text-[10px] tabular-nums text-accent">
                            {Number(person.deal_value).toLocaleString()}
                          </span>
                        )}
                      </span>
                      {busy === person.id && (
                        <Loader2 size={11} className="shrink-0 animate-spin text-faint" />
                      )}
                    </button>

                    {/* The half that works on a phone. */}
                    <select
                      value={person.pipeline_stage}
                      onChange={(event) => move(person.id, event.target.value)}
                      aria-label={`Move ${contactLabel(person)} to another stage`}
                      // Present but receding. Drawn as a full input it was the
                      // loudest thing on the card, which made a board of leads
                      // read as a board of dropdowns. It comes forward on
                      // hover and focus, and is always there for a finger.
                      className="mt-1 w-full cursor-pointer rounded border border-transparent bg-transparent px-1 py-0.5 text-[10px] text-faint transition-colors hover:border-edge hover:bg-bg hover:text-dim focus:border-accent/60 focus:bg-bg focus:text-ink"
                    >
                      {(stages.length ? stages : DEFAULT_STAGES).map((option) => (
                        <option key={option.key} value={option.key}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </article>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
