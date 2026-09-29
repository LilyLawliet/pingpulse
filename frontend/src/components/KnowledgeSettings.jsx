import { useCallback, useEffect, useRef, useState } from 'react'
import {
  BookOpen,
  CalendarCheck,
  Check,
  FileText,
  Loader2,
  Trash2,
  TriangleAlert,
  Upload,
} from 'lucide-react'
import { api } from '../api.js'
import CatalogueReview from './CatalogueReview.jsx'

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
export default function KnowledgeSettings({ onChanged }) {
  const [sources, setSources] = useState([])
  const [readiness, setReadiness] = useState(null)
  const [catalogue, setCatalogue] = useState(null)
  const [scale, setScale] = useState('cents')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  // What the last upload did to the opening hours, reported by the server.
  const [hours, setHours] = useState(null)
  const [dragging, setDragging] = useState(false)
  // Bumped after an upload or a removal, so the reading below reloads.
  const [readAt, setReadAt] = useState(0)
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
    setReadAt((n) => n + 1)
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
    setHours(null)
    const done = []
    let indexed = 0

    for (const file of chosen) {
      try {
        const result = await api.uploadKnowledge(file)
        // Products with prices are what the agent can quote from, so the
        // count is what says whether a price list was actually understood.
        const products =
          result.products_found > 0
            ? `${result.products_found} product${result.products_found === 1 ? '' : 's'} with prices`
            : 'no priced products found'
        // Pictures in the price table are the photos the agent can send.
        const photos =
          result.photos_found > 0
            ? `, ${result.photos_found} with photo${result.photos_found === 1 ? '' : 's'}`
            : ''
        done.push(`${result.filename}: ${products}${photos}, ${result.passages_indexed} passage(s)`)
        indexed += result.passages_indexed || 0
        // What the file filled in - hours, services, areas - decides whether
        // the agent can book at all and what it says it does. Far too big to
        // leave the owner to discover. The server says it in plain words.
        if (result.from_document) setHours(result.from_document)
      } catch (err) {
        // The server writes these for a shop owner, not an engineer — a scan
        // with no text layer explains itself. Show it as it came.
        setError(err.message)
        break
      }
    }

    if (done.length) setNote(`Added ${done.join(', ')}.`)
    setBusy(false)
    // Told first, and told what the server just confirmed: passages indexed is
    // what makes the knowledge answer "ready". Waiting for this panel's own
    // re-read first held the tick up by a round trip for no reason.
    onChanged?.(indexed > 0 ? { knowledge: true } : undefined)
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
    // No hint: whether what is left is still enough to quote from is the
    // server's call.
    onChanged?.()
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
          To let it send product photos, put a picture in each row of the price table in
          a Word file.
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
                  let imported = 0
                  try {
                    const done = await api.importCatalogue(scale)
                    imported = done.imported || 0
                    setNote(`Imported ${done.imported} product(s) from WhatsApp.`)
                    setCatalogue(null)
                  } catch (err) {
                    setError(err.message)
                  }
                  setBusy(false)
                  onChanged?.(imported > 0 ? { knowledge: true } : undefined)
                  await load()
                }}
                className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90"
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
          className="mx-auto flex items-center gap-2 rounded-lg bg-accent px-3.5 py-2 text-xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-50"
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

      {/*
        What the file did to the shop's opening hours, which decides whether
        the agent can offer an appointment or has to hand over to a person.
        Green when hours were read and are waiting at the hours step; amber
        when none were found, because that is the case where booking stays off
        and somebody has to type them in.
      */}
      {hours && !error && (
        <p
          className={`flex items-start gap-2 rounded-lg px-3 py-2 text-xs ${
            hours.proposed ? 'bg-accent/10 text-accent' : 'bg-warn/10 text-warn'
          }`}
        >
          {hours.proposed ? (
            <CalendarCheck size={13} className="mt-0.5 shrink-0" />
          ) : (
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
          )}
          {hours.detail}
        </p>
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

      <CatalogueReview refreshKey={readAt} onChanged={() => onChanged?.({ knowledge: true })} />

      {sources.length === 0 && (
        <p className="text-2xs text-faint">
          {readiness?.advice ||
            'Nothing uploaded yet — the agent is answering from the description above only.'}
        </p>
      )}
    </div>
  )
}
