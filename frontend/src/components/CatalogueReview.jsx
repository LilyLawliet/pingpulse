import { useCallback, useEffect, useState } from 'react'
import { Check, ChevronDown, Loader2, ScanSearch, Trash2, TriangleAlert } from 'lucide-react'
import { api } from '../api.js'

/**
 * What each uploaded file was read into, for the owner to check.
 *
 * The agent quotes from these rows, not from the file: every price here is one
 * the agent may say. The model reads the file and code checks every price and
 * name against it, but the owner is the authority on their own prices, so
 * anything can be corrected or removed here, and "Looks right" records that
 * somebody looked.
 */
export default function CatalogueReview({ refreshKey, onChanged }) {
  const [readings, setReadings] = useState([])

  const load = useCallback(async () => {
    try {
      setReadings(await api.listCatalogue())
    } catch {
      setReadings([])
    }
  }, [])

  useEffect(() => {
    load()
  }, [load, refreshKey])

  if (readings.length === 0) return null

  return (
    <div className="space-y-3">
      <span className="eyebrow block">What your agent will quote</span>
      {readings.map((reading) => (
        <Reading
          key={reading.id}
          reading={reading}
          onSaved={(saved) => {
            setReadings((was) => was.map((r) => (r.id === saved.id ? saved : r)))
            onChanged?.()
          }}
        />
      ))}
    </div>
  )
}

function Reading({ reading, onSaved }) {
  const [items, setItems] = useState(reading.items)
  const [rules, setRules] = useState(reading.rules)
  const [dirty, setDirty] = useState(false)
  const [open, setOpen] = useState(reading.status !== 'confirmed')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    setItems(reading.items)
    setRules(reading.rules)
    setDirty(false)
  }, [reading])

  const change = (index, key, value) => {
    setItems((was) => was.map((item, i) => (i === index ? { ...item, [key]: value } : item)))
    setDirty(true)
  }

  const save = async (body) => {
    setBusy(true)
    setError(null)
    try {
      onSaved(await api.correctCatalogue(reading.id, body))
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const confirmed = reading.status === 'confirmed'

  return (
    <div className="overflow-hidden rounded-xl border border-edge">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 bg-panel-2/40 px-3 py-2.5 text-left"
      >
        <ScanSearch size={14} className="shrink-0 text-platinum-dim" />
        <span className="min-w-0 flex-1 truncate text-xs text-ink">{reading.source}</span>
        <span className="shrink-0 text-2xs text-faint">
          {items.length} product{items.length === 1 ? '' : 's'} ·{' '}
          {new Set(rules.map((r) => r.sentence)).size} rule
          {new Set(rules.map((r) => r.sentence)).size === 1 ? '' : 's'}
        </span>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
            confirmed ? 'bg-accent/10 text-accent' : 'bg-warn/10 text-warn'
          }`}
        >
          {confirmed ? 'Checked' : 'Please check'}
        </span>
        <ChevronDown
          size={13}
          className={`shrink-0 text-faint transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div className="space-y-3 px-3 py-3">
          <p className="text-2xs leading-relaxed text-dim">
            {reading.read_by === 'model'
              ? 'Read by the AI and checked against your file: every price here is written in it. '
              : 'Read by the built-in reader, because the AI was not available. '}
            These are the only prices the agent will quote. Correct anything that is wrong.
          </p>

          {items.length > 0 && (
            <ul className="divide-y divide-edge rounded-lg border border-edge">
              {items.map((item, index) => (
                <li key={item.id || index} className="flex items-center gap-2 px-2.5 py-1.5">
                  <div className="min-w-0 flex-1">
                    <input
                      value={item.name}
                      onChange={(e) => change(index, 'name', e.target.value)}
                      aria-label="Product name"
                      className="w-full rounded-md bg-transparent px-1 py-0.5 text-xs text-ink focus:bg-bg focus:outline-none"
                    />
                    <p className="truncate px-1 text-2xs text-faint">
                      {[
                        item.details,
                        item.sold_as && `per ${item.sold_as}`,
                        item.pack && `pack of ${Number(item.pack)}`,
                        item.starting && 'starting price',
                      ]
                        .filter(Boolean)
                        .join(' · ')}
                    </p>
                  </div>
                  <span className="shrink-0 text-2xs text-faint">{item.currency}</span>
                  <input
                    value={item.price}
                    onChange={(e) => change(index, 'price', e.target.value)}
                    inputMode="decimal"
                    aria-label={`Price of ${item.name}`}
                    className="w-20 shrink-0 rounded-md border border-edge bg-bg px-1.5 py-0.5 text-right font-mono text-xs text-ink"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setItems((was) => was.filter((_, i) => i !== index))
                      setDirty(true)
                    }}
                    aria-label={`Remove ${item.name}`}
                    className="shrink-0 rounded-md p-1 text-faint transition-colors hover:bg-crit/10 hover:text-crit"
                  >
                    <Trash2 size={12} />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {rules.length > 0 && (
            <ul className="space-y-1">
              {rules.map((rule, index) =>
                // A rule with several bands is one sentence: shown once.
                rules.findIndex((r) => r.sentence === rule.sentence) !== index ? null : (
                <li key={index} className="flex items-start gap-2 text-2xs text-dim">
                  <span className="mt-0.5 shrink-0 rounded bg-panel-2 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-faint">
                    {rule.topic}
                  </span>
                  <span className="flex-1 leading-relaxed">{rule.sentence}</span>
                  <button
                    type="button"
                    onClick={() => {
                      setRules((was) => was.filter((r) => r.sentence !== rule.sentence))
                      setDirty(true)
                    }}
                    aria-label="Remove this rule"
                    className="shrink-0 rounded-md p-1 text-faint transition-colors hover:bg-crit/10 hover:text-crit"
                  >
                    <Trash2 size={11} />
                  </button>
                </li>
                ),
              )}
            </ul>
          )}

          {reading.left_out?.length > 0 && (
            <details className="text-2xs text-faint">
              <summary className="cursor-pointer">
                {reading.left_out.length} thing{reading.left_out.length === 1 ? '' : 's'} left out,
                because your file does not say them
              </summary>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {reading.left_out.map((why, i) => (
                  <li key={i}>{why}</li>
                ))}
              </ul>
            </details>
          )}

          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            {dirty && (
              <button
                type="button"
                disabled={busy}
                onClick={() => save({ items, rules })}
                className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                Save corrections
              </button>
            )}
            {!dirty && !confirmed && (
              <button
                type="button"
                disabled={busy}
                onClick={() => save({ status: 'confirmed' })}
                className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                <Check size={12} /> Looks right
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
