import { useCallback, useEffect, useRef, useState } from 'react'
import { BookOpen, Check, FileText, Loader2, Trash2, TriangleAlert, Upload } from 'lucide-react'
import { api } from '../api.js'

const ACCEPT = '.pdf,.docx,.txt,.md'

/**
 * What the shop wants the agent to know, uploaded as the files they already have.
 *
 * The agent is told never to invent a price, a size or a stock level, which is
 * correct and means it can only be as useful as what is loaded here. With
 * nothing uploaded it politely declines every product question — so this panel
 * is the difference between a demo and a working agent, and it says so rather
 * than presenting an empty list with no explanation.
 *
 * Files are grouped by the file they came from. One price list becomes a dozen
 * passages, which is right for retrieval and wrong for someone looking at what
 * they sent: they uploaded one document, not twelve.
 */
export default function KnowledgeSettings() {
  const [sources, setSources] = useState([])
  const [readiness, setReadiness] = useState(null)
  const [catalogue, setCatalogue] = useState(null)
  const [scale, setScale] = useState('cents')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  const [dragging, setDragging] = useState(false)
  const picker = useRef(null)

  const load = useCallback(async () => {
    try {
      setSources(await api.listKnowledgeSources())
    } catch {
      // An empty list and a failed read look the same here, and neither is
      // worth an error banner over the upload control that still works.
      setSources([])
    }
    try {
      setReadiness(await api.knowledgeReadiness())
    } catch {
      setReadiness(null)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const send = async (files) => {
    const chosen = Array.from(files || [])
    if (chosen.length === 0) return

    setBusy(true)
    setError(null)
    setNote(null)
    const done = []

    for (const file of chosen) {
      try {
        const result = await api.uploadKnowledge(file)
        done.push(`${result.filename}: ${result.passages_indexed} passage(s)`)
      } catch (err) {
        // The server writes these for a shop owner, not an engineer — a scan
        // with no text layer explains itself. Show it as it came.
        setError(err.message)
        break
      }
    }

    if (done.length) setNote(`Added ${done.join(', ')}.`)
    setBusy(false)
    await load()
  }

  const remove = async (source) => {
    setError(null)
    try {
      await api.deleteKnowledgeSource(source)
      setNote(`Removed ${source}.`)
    } catch (err) {
      setError(err.message)
    }
    await load()
  }

  return (
    <div className="space-y-3">
      <div>
        <span className="eyebrow mb-1.5 block">What your agent should know</span>
        <p className="text-2xs leading-relaxed text-dim">
          Send your price list, product details, and your delivery, payment and returns
          policies. PDF, Word or plain text. The agent quotes only what it finds here —
          it will never invent a price — so the more you add, the more it can answer.
        </p>
      </div>

      {readiness?.catalogue?.products > 0 && (
        <div className="rounded-xl border border-accent/30 bg-accent/5 px-4 py-3">
          <p className="flex items-center gap-2 text-xs font-semibold text-accent">
            <BookOpen size={13} />
            {readiness.catalogue.products} product
            {readiness.catalogue.products === 1 ? '' : 's'} in your WhatsApp catalogue
          </p>
          <p className="mt-1 text-2xs leading-relaxed text-dim">
            We can read these straight from WhatsApp — nothing to upload. Check the
            prices below look right before importing: WhatsApp stores them as whole
            numbers and does not say where the decimal point goes.
          </p>

          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <select
              id="catalogue-scale"
              value={scale}
              onChange={(e) => {
                setScale(e.target.value)
                setCatalogue(null)
              }}
              className="rounded-lg border border-edge bg-bg px-2 py-1.5 text-2xs text-ink"
            >
              <option value="cents">8900 means 89.00</option>
              <option value="whole">8900 means 8,900</option>
              <option value="thousandths">89000 means 89.00</option>
            </select>
            <button
              type="button"
              disabled={busy}
              onClick={async () => {
                setBusy(true)
                setError(null)
                try {
                  setCatalogue(await api.previewCatalogue(scale))
                } catch (err) {
                  setError(err.message)
                }
                setBusy(false)
              }}
              className="rounded-lg border border-edge px-2.5 py-1.5 text-2xs text-dim transition-colors hover:border-edge-hi hover:text-ink"
            >
              Preview
            </button>
            {catalogue && (
              <button
                type="button"
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  setError(null)
                  try {
                    const done = await api.importCatalogue(scale)
                    setNote(`Imported ${done.imported} product(s) from WhatsApp.`)
                    setCatalogue(null)
                  } catch (err) {
                    setError(err.message)
                  }
                  setBusy(false)
                  await load()
                }}
                className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-2xs font-semibold text-bg transition-opacity hover:opacity-90"
              >
                <Check size={12} /> Looks right — import
              </button>
            )}
          </div>

          {catalogue && (
            <ul className="mt-2.5 space-y-1 rounded-lg bg-bg/60 px-3 py-2">
              {catalogue.products.slice(0, 5).map((product, i) => (
                <li key={i} className="flex items-baseline gap-2 text-2xs">
                  <span className="truncate text-ink">{product.name}</span>
                  <span className="ml-auto shrink-0 font-mono text-accent">
                    {product.price}
                  </span>
                </li>
              ))}
              {catalogue.products.length > 5 && (
                <li className="text-2xs text-faint">
                  and {catalogue.products.length - 5} more
                </li>
              )}
            </ul>
          )}
        </div>
      )}

      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          send(e.dataTransfer.files)
        }}
        className={`rounded-xl border border-dashed px-4 py-6 text-center transition-colors ${
          dragging ? 'border-accent bg-accent/5' : 'border-edge-hi bg-panel-2/40'
        }`}
      >
        <input
          id="knowledge-file"
          ref={picker}
          type="file"
          accept={ACCEPT}
          multiple
          className="hidden"
          onChange={(e) => {
            send(e.target.files)
            e.target.value = ''
          }}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => picker.current?.click()}
          className="mx-auto flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
          {busy ? 'Reading…' : 'Choose files'}
        </button>
        <p className="mt-2 text-2xs text-faint">or drop them here · PDF, Word, text</p>
      </div>

      {error && (
        <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
          <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}
      {note && !error && (
        <p className="rounded-lg bg-accent/10 px-3 py-2 text-xs text-accent">{note}</p>
      )}

      {sources.length > 0 && (
        <ul className="divide-y divide-edge overflow-hidden rounded-xl border border-edge">
          {sources.map((row) => (
            <li key={row.source} className="flex items-center gap-3 bg-panel-2/40 px-3 py-2.5">
              <FileText size={14} className="shrink-0 text-platinum-dim" />
              <span className="min-w-0 flex-1 truncate text-xs text-ink">{row.source}</span>
              <span className="shrink-0 font-mono text-2xs text-faint">
                {row.passages} passage{row.passages === 1 ? '' : 's'}
              </span>
              <button
                type="button"
                onClick={() => remove(row.source)}
                aria-label={`Remove ${row.source}`}
                className="shrink-0 rounded-lg p-1 text-faint transition-colors hover:bg-crit/10 hover:text-crit"
              >
                <Trash2 size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}

      {sources.length === 0 && (
        <p className="text-2xs text-faint">
          {readiness?.advice ||
            'Nothing uploaded yet — the agent is answering from the description above only.'}
        </p>
      )}
    </div>
  )
}
