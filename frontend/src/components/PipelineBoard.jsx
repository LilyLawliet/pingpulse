import { TrendingUp } from 'lucide-react'
import { STAGE_DOT, STAGE_LABEL, STAGE_ORDER, contactLabel, initialsOf, prettyPhone } from '../format.js'

/**
 * Where every lead stands. Ordered by the funnel, so the shape of the
 * business reads before any individual name does.
 */
export default function PipelineBoard({ contacts, selectedId, onSelect, className = '' }) {
  const byStage = STAGE_ORDER.map((stage) => ({
    stage,
    people: contacts.filter((c) => c.pipeline_stage === stage),
  }))
  const total = contacts.length || 1

  return (
    <section className={`panel shrink-0 ${className}`}>
      <header className="panel-head">
        <TrendingUp size={14} className="text-accent" />
        <h2 className="text-xs font-semibold text-ink">Pipeline</h2>
      </header>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-3.5 py-3.5">
        {byStage.map(({ stage, people }) => (
          <div key={stage}>
            <div className="mb-2 flex items-center gap-2">
              <span className={`h-1.5 w-1.5 rounded-full ${STAGE_DOT[stage]}`} />
              <span className="eyebrow">{STAGE_LABEL[stage]}</span>
              <span className="ml-auto font-mono text-xs text-dim">{people.length}</span>
            </div>

            {/* Share of the pipeline sitting at this stage. */}
            <div className="mb-2 h-1 overflow-hidden rounded-full bg-edge">
              <div
                className={`h-full rounded-full ${STAGE_DOT[stage]} transition-[width] duration-500`}
                style={{ width: `${(people.length / total) * 100}%` }}
              />
            </div>

            <div className="space-y-1">
              {people.length === 0 && <p className="py-1 text-2xs text-faint">Nobody here yet</p>}
              {people.map((person) => (
                <button
                  key={person.id}
                  onClick={() => onSelect(person.id)}
                  className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors ${
                    person.id === selectedId ? 'bg-panel-2' : 'hover:bg-panel-2/60'
                  }`}
                >
                  <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-edge text-[9px] font-semibold text-dim">
                    {initialsOf(person.name, person.phone_number)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs text-ink">
                      {contactLabel(person)}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}
