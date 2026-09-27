import { useCallback, useEffect, useState } from 'react'
import {
  CalendarDays,
  Check,
  Clock,
  Copy,
  Eye,
  FileText,
  Lightbulb,
  Loader2,
  Plus,
  TriangleAlert,
  Undo2,
} from 'lucide-react'
import { api } from '../api.js'

const DAYS = [
  ['monday', 'Mon'],
  ['tuesday', 'Tue'],
  ['wednesday', 'Wed'],
  ['thursday', 'Thu'],
  ['friday', 'Fri'],
  ['saturday', 'Sat'],
  ['sunday', 'Sun'],
]

/**
 * The rules a shop sets for how its agent behaves.
 *
 * Everything here is optional, and a shop that fills in none of it keeps
 * exactly the agent it has. That is not a default to be tidied away later: it
 * is what makes this safe to put in front of a business that is mid-conversation
 * with a customer right now.
 *
 * Hours are per day rather than one range, because "9 to 5 except Saturdays,
 * when we shut at 1" is what shops actually do and a single range cannot say
 * it. Being closed does not stop the agent answering — it stops it promising
 * somebody will call in ten minutes.
 *
 * Lists are typed one per line rather than as tag chips. A chip editor is
 * nicer to look at and worse to use for a person pasting in the twelve
 * services they already have written down somewhere.
 *
 * Three fields here cannot be read out of a document, because they describe
 * nothing about the business: what the agent must never promise, how it may
 * talk about price, and which words fetch a person. Those are decisions. They
 * used to be three empty boxes, which asks somebody to author policy from
 * nothing — so a trade draft fills them with text that is visibly a draft,
 * past conversations offer the words that really did precede a handover, and
 * both are only ever a prefill. Saving is still what turns any of it on.
 */
const FIELD_NAMES = {
  business_hours: 'Opening hours',
  services: 'Services',
  service_areas: 'Areas you serve',
  never_promise: 'Never promise',
  pricing_rules: 'Pricing rules',
  escalate_on: 'Words that fetch a person',
}

/** The three a trade draft fills. Mirrors `trade_defaults.DRAFT_FIELDS`. */
const DRAFT_FIELDS = ['never_promise', 'pricing_rules', 'escalate_on']

/** "Opening hours and Services", for a sentence rather than a key list. */
function describeFields(keys) {
  const names = keys.map((key) => FIELD_NAMES[key] || key)
  if (names.length <= 1) return names[0] || ''
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** The files those fields came from, named once each. */
function namedSources(keys, sources) {
  const files = [...new Set(keys.map((key) => sources?.[key]).filter(Boolean))]
  if (files.length === 0) return 'your document'
  if (files.length === 1) return files[0]
  return `${files.slice(0, -1).join(', ')} and ${files[files.length - 1]}`
}

const isBlank = (value) => (Array.isArray(value) ? value.length === 0 : !value)

export default function AgentSettings() {
  const [config, setConfig] = useState(null)
  const [zone, setZone] = useState('UTC')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)
  // Hours a document offered, waiting to be confirmed. Null once they are.
  const [fromDocument, setFromDocument] = useState(null)

  const [trades, setTrades] = useState([])
  const [tradeKey, setTradeKey] = useState('')
  // What the chosen draft filled, and what it left alone because the shop had
  // already answered. Cleared when the trade changes.
  const [drafted, setDrafted] = useState(null)

  const [suggestions, setSuggestions] = useState(null)
  const [preview, setPreview] = useState(null)
  const [previewing, setPreviewing] = useState(false)
  const [lastChange, setLastChange] = useState(null)
  const [undoing, setUndoing] = useState(false)
  // Whether this business can actually take an appointment, and what is
  // stopping it. Read from the server rather than worked out here, because
  // the server is what refuses the save.
  const [readiness, setReadiness] = useState(null)

  const load = useCallback(async () => {
    try {
      const found = await api.getAgentConfig()
      const stored = found.agent_config || {}

      // What the uploaded documents said, offered here in the form rather
      // than written straight to the config. Booking reads business_hours, so
      // writing it from parsed prose would have the agent offering real times
      // to real customers on the strength of a regular expression. Prefilled
      // and left for a person to look at and save: the document does the
      // typing, somebody still says yes.
      const offered = stored.from_document?.fields || {}
      const sources = stored.from_document?.sources || {}

      // A field the shop has already answered is left alone. A document
      // arriving later must not quietly move an answer somebody typed - if
      // it disagrees, that is said out loud below and applied only on asking.
      const filling = Object.keys(offered).filter((key) => isBlank(stored[key]))
      const differing = Object.keys(offered).filter(
        (key) =>
          !isBlank(stored[key]) &&
          JSON.stringify(stored[key]) !== JSON.stringify(offered[key]),
      )

      const prefilled = { ...stored }
      filling.forEach((key) => {
        prefilled[key] = offered[key]
      })
      setConfig(prefilled)
      setFromDocument(
        filling.length || differing.length
          ? { offered, sources, filling, differing }
          : null,
      )
      setZone(found.timezone || 'UTC')
      setLastChange(found.last_change || null)
      setReadiness(found.booking || null)
    } catch {
      setConfig({})
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // Both are advice rather than settings, so neither is allowed to stop the
  // page rendering. A tenant with no history gets an empty report and no
  // panel, which is the correct amount of nothing.
  useEffect(() => {
    api
      .getTradeDrafts()
      .then((found) => setTrades(found.trades || []))
      .catch(() => setTrades([]))
    api
      .getConfigSuggestions()
      .then(setSuggestions)
      .catch(() => setSuggestions(null))
  }, [])

  if (!config) return null

  const lines = (key) => (config[key] || []).join('\n')
  const setLines = (key, value) =>
    setConfig({
      ...config,
      [key]: value
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean),
    })

  const hours = config.business_hours || {}
  const setHours = (day, patch) =>
    setConfig({
      ...config,
      business_hours: { ...hours, [day]: { ...(hours[day] || {}), ...patch } },
    })
  const clearDay = (day) => {
    const next = { ...hours }
    delete next[day]
    setConfig({ ...config, business_hours: next })
  }

  /**
   * Fill the three policy fields from a trade's draft.
   *
   * Only where the shop has not already answered. A draft is a convention of
   * the trade, not a finding about this business, so it must never move
   * something somebody typed — what it would have said differently is named
   * underneath instead, with a button.
   */
  const applyTrade = (key) => {
    setTradeKey(key)
    const trade = trades.find((row) => row.key === key)
    if (!trade) {
      setDrafted(null)
      return
    }

    const next = { ...config }
    const filled = []
    const held = []
    DRAFT_FIELDS.forEach((field) => {
      const draft = trade.draft?.[field]
      if (isBlank(draft)) return
      if (isBlank(config[field])) {
        next[field] = draft
        filled.push(field)
      } else if (JSON.stringify(config[field]) !== JSON.stringify(draft)) {
        held.push(field)
      }
    })

    setConfig(next)
    setDrafted({ label: trade.label, draft: trade.draft, filled, held })
  }

  /** Take the draft's version of a field the shop had already answered. */
  const takeDraft = (field) => {
    if (!drafted) return
    const draft = drafted.draft[field]
    const next = { ...config }
    // Lists merge and text replaces. Adding a word to the escalation list
    // never costs anything a shop typed; overwriting a paragraph does, so
    // that one is only ever done on an explicit press.
    next[field] = Array.isArray(draft)
      ? [...new Set([...(config[field] || []), ...draft])]
      : draft
    setConfig(next)
    setDrafted({
      ...drafted,
      filled: [...drafted.filled, field],
      held: drafted.held.filter((key) => key !== field),
    })
  }

  const addPhrase = (phrase) => {
    const existing = config.escalate_on || []
    if (existing.includes(phrase)) return
    setConfig({ ...config, escalate_on: [...existing, phrase] })
    setSuggestions({
      ...suggestions,
      candidates: suggestions.candidates.filter((row) => row.phrase !== phrase),
    })
  }

  const showPreview = async () => {
    setPreviewing(true)
    setError(null)
    try {
      setPreview(await api.previewAgentConfig(config))
    } catch (err) {
      setError(err.message)
    }
    setPreviewing(false)
  }

  const undo = async () => {
    setUndoing(true)
    setError(null)
    setNote(null)
    try {
      const result = await api.undoAgentConfig()
      setConfig(result.agent_config || {})
      setLastChange(result.last_change || null)
      setPreview(null)
      setDrafted(null)
      setNote(
        `Put back: ${describeFields(result.undone || []).toLowerCase() || 'the last change'}.`,
      )
    } catch (err) {
      setError(err.message)
    }
    setUndoing(false)
  }

  const save = async () => {
    setSaving(true)
    setError(null)
    setNote(null)
    try {
      // Deliberately without a timezone. This screen no longer owns it, and
      // sending the one it happens to have loaded would quietly satisfy the
      // server's "set your timezone before setting hours" check with the
      // default nobody chose.
      await api.saveAgentConfig(config)
      setNote(
        fromDocument
          ? 'Saved. Booking is on, and these are the hours appointments are offered in.'
          : 'Saved. New conversations follow these from now on.',
      )
      // Confirmed now, so it is no longer a suggestion. The server drops the
      // stored copy for the same reason.
      setFromDocument(null)
      setDrafted(null)
      setPreview(null)
      // Reloaded rather than assumed: the undo offer has to name the change
      // that was actually recorded, and the server decides what that was.
      const found = await api.getAgentConfig().catch(() => null)
      if (found) {
        setLastChange(found.last_change || null)
        setReadiness(found.booking || null)
      }
    } catch (err) {
      setError(err.message)
    }
    setSaving(false)
  }

  return (
    <section className="space-y-3">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
          <Clock size={14} className="text-accent" />
        </span>
        <div className="min-w-0">
          <h4 className="text-xs font-semibold text-ink">How your agent should behave</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            All optional. Leave it empty and nothing about your agent changes.
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

      {/*
        Shown, not edited. The timezone is asked for in Your business, which
        comes first - it used to be asked for here, after the document upload
        that needs it, so it was reliably unset at the one moment it mattered.
        Keeping an editor in both places is how one of them goes stale.
      */}
      {zone && zone !== 'UTC' ? (
        <p className="text-2xs text-faint">
          Times below are read in <span className="text-dim">{zone}</span>. Change it
          in <span className="text-dim">Your business</span>.
        </p>
      ) : (
        <p className="flex items-start gap-2 rounded-lg bg-warn/10 px-3 py-2 text-2xs text-warn">
          <TriangleAlert size={12} className="mt-0.5 shrink-0" />
          <span>
            Your timezone is not set, so these hours cannot be saved yet — without one
            they would be read as UTC, which is how somebody gets offered an
            appointment in the middle of the night. Set it in{' '}
            <span className="font-semibold">Your business</span>.
          </span>
        </p>
      )}

      {/*
        Whether the agent can book at all. A client ran for its whole life
        unable to take a single appointment - its hours read correctly out of
        a document and never confirmed, its timezone never set - and nothing
        anywhere said so. Every step was working as designed and the chain was
        invisible.
      */}
      {readiness && !readiness.can_book && readiness.blockers?.length > 0 && (
        <div className="rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
          <p className="flex items-start gap-2 font-semibold">
            <TriangleAlert size={12} className="mt-0.5 shrink-0" />
            Your agent cannot book appointments yet.
          </p>
          <ul className="mt-1.5 space-y-1">
            {readiness.blockers.map((blocker) => (
              <li key={blocker.key} className="leading-relaxed">
                {blocker.says} <span className="text-dim">{blocker.fix}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {readiness?.can_book && (
        <p className="flex items-start gap-2 text-2xs text-faint">
          <Check size={12} className="mt-0.5 shrink-0 text-ok" />
          Booking is on, across {readiness.days_open}{' '}
          {readiness.days_open === 1 ? 'day' : 'days'} a week, in{' '}
          <span className="text-dim">{readiness.timezone}</span>.
        </p>
      )}

      {fromDocument?.filling?.length > 0 && (
        <p className="flex items-start gap-2 rounded-lg bg-accent/10 px-3 py-2 text-2xs text-accent">
          <FileText size={12} className="mt-0.5 shrink-0" />
          <span>
            {describeFields(fromDocument.filling)} below{' '}
            {fromDocument.filling.length === 1 ? 'was' : 'were'} read from{' '}
            <span className="font-semibold">
              {namedSources(fromDocument.filling, fromDocument.sources)}
            </span>
            . Nothing is used until you save — check it first, since a document can
            word things in a way this reads differently.
          </span>
        </p>
      )}

      {/*
        The document disagrees with something already saved. Not applied on
        its own: a price list uploaded months later must not quietly move
        opening hours somebody set by hand, and a change nobody noticed is
        the one that ends up promising a customer the wrong thing.
      */}
      {fromDocument?.differing?.length > 0 && (
        <div className="rounded-lg bg-warn/10 px-3 py-2 text-2xs text-warn">
          <p className="flex items-start gap-2">
            <TriangleAlert size={12} className="mt-0.5 shrink-0" />
            <span>
              {namedSources(fromDocument.differing, fromDocument.sources)} states
              different {describeFields(fromDocument.differing).toLowerCase()} from
              what you have saved. Yours is being kept.
            </span>
          </p>
          <button
            type="button"
            onClick={() => {
              const next = { ...config }
              fromDocument.differing.forEach((key) => {
                next[key] = fromDocument.offered[key]
              })
              setConfig(next)
              setFromDocument({
                ...fromDocument,
                filling: [...fromDocument.filling, ...fromDocument.differing],
                differing: [],
              })
            }}
            className="mt-2 rounded-lg border border-warn/40 px-2.5 py-1 font-semibold transition-colors hover:bg-warn/15"
          >
            Use what the document says
          </button>
        </div>
      )}

      <div>
        <span className="eyebrow mb-1.5 block">Opening hours</span>
        <div className="space-y-1">
          {DAYS.map(([key, label]) => {
            const day = hours[key]
            return (
              <div key={key} className="flex items-center gap-2">
                <span className="w-9 shrink-0 text-2xs text-dim">{label}</span>
                <input
                  type="time"
                  value={day?.open || ''}
                  onChange={(e) => setHours(key, { open: e.target.value })}
                  className="min-w-0 flex-1 rounded-lg border border-edge bg-bg px-2 py-1 text-2xs text-ink focus:border-accent/60"
                />
                <span className="text-2xs text-faint">to</span>
                <input
                  type="time"
                  value={day?.close || ''}
                  onChange={(e) => setHours(key, { close: e.target.value })}
                  className="min-w-0 flex-1 rounded-lg border border-edge bg-bg px-2 py-1 text-2xs text-ink focus:border-accent/60"
                />
                <button
                  type="button"
                  onClick={() => clearDay(key)}
                  title="Closed this day"
                  className="shrink-0 rounded px-1.5 py-1 text-2xs text-faint transition-colors hover:text-crit"
                >
                  closed
                </button>
              </div>
            )
          })}
        </div>
        <p className="mt-1.5 text-2xs leading-relaxed text-faint">
          Outside these hours it still answers — it just will not promise that somebody
          will call straight back.
        </p>
      </div>

      <CalendarSubscription />

      <TextList
        label="Services you offer"
        hint="One per line. It will say plainly that anything not listed is not something you do."
        value={lines('services')}
        onChange={(value) => setLines('services', value)}
      />
      <TextList
        label="Areas you serve"
        hint="One per line. Anywhere else, it says so rather than promising to check."
        value={lines('service_areas')}
        onChange={(value) => setLines('service_areas', value)}
      />

      {/*
        The three below are policy rather than fact, so no document fills
        them. A draft by trade is the substitute: text somebody reads and
        deletes from, instead of an empty box somebody has to write into.
      */}
      {trades.length > 0 && (
        <div className="space-y-1.5 rounded-lg border border-edge px-3 py-2.5">
          <label className="block">
            <span className="eyebrow mb-1 block">Start from a draft</span>
            <select
              value={tradeKey}
              onChange={(e) => applyTrade(e.target.value)}
              className="w-full rounded-lg border border-edge bg-bg px-2 py-1.5 text-2xs text-ink focus:border-accent/60"
            >
              <option value="">Pick your trade…</option>
              {trades.map((trade) => (
                <option key={trade.key} value={trade.key}>
                  {trade.label} — {trade.examples}
                </option>
              ))}
            </select>
          </label>
          <p className="text-2xs leading-relaxed text-faint">
            Fills the three below with what a careful business in that trade would
            write. It is a starting point, not a reading of your documents — edit
            it, and nothing takes effect until you save.
          </p>

          {drafted?.filled?.length > 0 && (
            <p className="flex items-start gap-2 text-2xs text-accent">
              <Check size={12} className="mt-0.5 shrink-0" />
              <span>
                {describeFields(drafted.filled)} filled from the{' '}
                {drafted.label.toLowerCase()} draft. Read it before saving.
              </span>
            </p>
          )}

          {drafted?.held?.length > 0 && (
            <div className="text-2xs text-dim">
              <p className="flex items-start gap-2">
                <TriangleAlert size={12} className="mt-0.5 shrink-0 text-warn" />
                <span>
                  You have already written {describeFields(drafted.held).toLowerCase()}.
                  Yours is kept.
                </span>
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {drafted.held.map((field) => (
                  <button
                    key={field}
                    type="button"
                    onClick={() => takeDraft(field)}
                    className="rounded-lg border border-edge px-2 py-1 font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink"
                  >
                    {Array.isArray(drafted.draft[field]) ? 'Add the draft’s' : 'Use the draft’s'}{' '}
                    {(FIELD_NAMES[field] || field).toLowerCase()}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <TextList
        label="Words that should fetch a person"
        hint="One per line. These are added to refunds, complaints and requests for a manager, which always do."
        value={lines('escalate_on')}
        onChange={(value) => setLines('escalate_on', value)}
      />

      <Suggestions
        report={suggestions}
        onAdd={addPhrase}
        onAddAll={() => {
          const phrases = (suggestions?.candidates || []).map((row) => row.phrase)
          setConfig({
            ...config,
            escalate_on: [...new Set([...(config.escalate_on || []), ...phrases])],
          })
          setSuggestions({ ...suggestions, candidates: [] })
        }}
      />

      <label className="block">
        <span className="eyebrow mb-1 block">Never promise</span>
        <textarea
          rows={3}
          value={config.never_promise || ''}
          onChange={(e) => setConfig({ ...config, never_promise: e.target.value })}
          placeholder="Same-day work. Discounts over 10%."
          className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink placeholder:text-faint focus:border-accent/60"
        />
      </label>

      <label className="block">
        <span className="eyebrow mb-1 block">Pricing rules</span>
        <textarea
          rows={3}
          value={config.pricing_rules || ''}
          onChange={(e) => setConfig({ ...config, pricing_rules: e.target.value })}
          placeholder="No quotes under $200. Always mention the callout fee."
          className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink placeholder:text-faint focus:border-accent/60"
        />
      </label>

      {/*
        What the model is actually handed. Until this existed, the only way to
        find out how these fields read once folded into a prompt was to save
        them and message the number - which means finding out from a customer.
      */}
      {preview && (
        <div className="space-y-1.5 rounded-lg border border-edge px-3 py-2.5">
          <span className="eyebrow block">What your agent will be told</span>
          {preview.problems?.length > 0 ? (
            <ul className="space-y-1">
              {preview.problems.map((problem) => (
                <li key={problem} className="flex items-start gap-2 text-2xs text-crit">
                  <TriangleAlert size={12} className="mt-0.5 shrink-0" />
                  {problem}
                </li>
              ))}
            </ul>
          ) : preview.prompt_block ? (
            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-bg px-2.5 py-2 text-2xs leading-relaxed text-dim">
              {preview.prompt_block}
            </pre>
          ) : (
            <p className="text-2xs leading-relaxed text-faint">
              Nothing yet. With none of these filled in your agent is told nothing
              extra and answers exactly as it does today.
            </p>
          )}
          <p className="text-2xs leading-relaxed text-faint">
            This is a preview. Nothing has been saved.
          </p>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          disabled={saving}
          onClick={save}
          className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {saving ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
          Save rules
        </button>

        <button
          type="button"
          disabled={previewing}
          onClick={showPreview}
          className="flex items-center gap-1.5 rounded-lg border border-edge px-3 py-1.5 text-2xs font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink disabled:opacity-40"
        >
          {previewing ? <Loader2 size={12} className="animate-spin" /> : <Eye size={12} />}
          Check it first
        </button>

        {/*
          A wrong rule here is quiet: nothing errors, the agent just starts
          answering under something nobody meant. Being able to step back
          without reconstructing what the form said an hour ago is what makes
          the drafts above safe to try.
        */}
        {lastChange && (
          <button
            type="button"
            disabled={undoing}
            onClick={undo}
            title={`Changed ${describeFields(lastChange.fields).toLowerCase()}`}
            className="flex items-center gap-1.5 rounded-lg border border-edge px-3 py-1.5 text-2xs font-semibold text-dim transition-colors hover:border-warn/50 hover:text-ink disabled:opacity-40"
          >
            {undoing ? <Loader2 size={12} className="animate-spin" /> : <Undo2 size={12} />}
            {lastChange.was_undo ? 'Redo' : 'Undo last save'}
          </button>
        )}
      </div>
    </section>
  )
}

/**
 * Words that kept turning up just before a person took over a conversation.
 *
 * Shown with their evidence rather than as a list to accept, because the
 * count is the argument: "came up before 4 handovers" is checkable, and
 * "suggested" is not. Nothing is added without a press — a trigger that fires
 * on ordinary messages teaches a shop to ignore its alerts, and then the one
 * that mattered is ignored too.
 */
function Suggestions({ report, onAdd, onAddAll }) {
  if (!report || report.unavailable) return null
  const candidates = report.candidates || []
  if (candidates.length === 0) return null

  return (
    <div className="space-y-1.5 rounded-lg border border-edge px-3 py-2.5">
      <p className="flex items-start gap-2 text-2xs text-dim">
        <Lightbulb size={12} className="mt-0.5 shrink-0 text-accent" />
        <span>
          These came up just before somebody at your business stepped into a
          conversation — {report.handovers} of those in your history
          {report.already_caught > 0 && `, ${report.already_caught} already covered`}.
          Add the ones that mean trouble rather than just meaning Tuesday.
        </span>
      </p>

      <ul className="space-y-1">
        {candidates.map((row) => (
          <li key={row.phrase} className="flex items-start gap-2">
            <button
              type="button"
              onClick={() => onAdd(row.phrase)}
              className="mt-px flex shrink-0 items-center gap-1 rounded-lg border border-edge px-1.5 py-0.5 text-2xs font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink"
            >
              <Plus size={10} />
              add
            </button>
            <span className="min-w-0 text-2xs leading-relaxed">
              <span className="font-semibold text-ink">{row.phrase}</span>
              <span className="text-faint">
                {' '}
                — before {row.handovers}{' '}
                {row.handovers === 1 ? 'handover' : 'handovers'}
                {row.ordinary > 0 && `, and ${row.ordinary} ordinary chats`}
              </span>
              {row.examples?.[0] && (
                <span className="mt-0.5 block truncate text-faint">“{row.examples[0]}”</span>
              )}
            </span>
          </li>
        ))}
      </ul>

      {candidates.length > 1 && (
        <button
          type="button"
          onClick={onAddAll}
          className="rounded-lg border border-edge px-2 py-1 text-2xs font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink"
        >
          Add all {candidates.length}
        </button>
      )}
    </div>
  )
}

/**
 * The diary, on the phone the business actually runs its day from.
 *
 * A subscription URL rather than a connected account: every calendar client
 * already knows how to read one, nobody signs into anything, and no consent
 * screen can change underneath it. It is read-only by construction, which
 * keeps the database the one place allowed to say an appointment exists.
 *
 * The link is a secret, so replacing it is offered in exactly those terms —
 * it is how a link that has been forwarded gets taken back.
 */
function CalendarSubscription() {
  const [state, setState] = useState(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    api
      .getCalendarSubscription()
      .then(setState)
      .catch(() => setState({ active: false, urls: null }))
  }, [])

  if (!state) return null

  const run = async (action) => {
    setBusy(true)
    setError(null)
    try {
      setState(await action())
      setCopied(false)
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(state.urls.https)
      setCopied(true)
    } catch {
      setError('Could not copy — select the link and copy it by hand.')
    }
  }

  return (
    <div className="space-y-1.5 rounded-lg border border-edge px-3 py-2.5">
      <p className="flex items-start gap-2 text-2xs text-dim">
        <CalendarDays size={12} className="mt-0.5 shrink-0 text-accent" />
        <span>
          <span className="font-semibold text-ink">Appointments on your phone.</span>{' '}
          Subscribe once and every booking shows up in the calendar you already
          use. No account to connect, and nothing can be changed from there.
        </span>
      </p>

      {error && <p className="text-2xs text-crit">{error}</p>}

      {state.active ? (
        <>
          <a
            href={state.urls.webcal}
            className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-bg transition-opacity hover:opacity-90"
          >
            <CalendarDays size={12} />
            Add to this device
          </a>
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              type="button"
              onClick={copy}
              className="flex items-center gap-1.5 rounded-lg border border-edge px-2 py-1 text-2xs font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink"
            >
              {copied ? <Check size={11} /> : <Copy size={11} />}
              {copied ? 'Copied' : 'Copy link for another phone'}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(api.createCalendarSubscription)}
              className="rounded-lg border border-edge px-2 py-1 text-2xs font-semibold text-dim transition-colors hover:border-warn/50 hover:text-ink disabled:opacity-40"
            >
              Replace link
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => run(api.deleteCalendarSubscription)}
              className="rounded-lg border border-edge px-2 py-1 text-2xs font-semibold text-faint transition-colors hover:border-crit/50 hover:text-crit disabled:opacity-40"
            >
              Turn off
            </button>
          </div>
          <p className="text-2xs leading-relaxed text-faint">
            Anyone with this link can see your appointments — it is the only
            thing standing in front of them. Replacing it stops every phone
            already subscribed, which is how you take one back.
          </p>
        </>
      ) : (
        <button
          type="button"
          disabled={busy}
          onClick={() => run(api.createCalendarSubscription)}
          className="flex items-center gap-1.5 rounded-lg border border-edge px-3 py-1.5 text-2xs font-semibold text-dim transition-colors hover:border-accent/50 hover:text-ink disabled:opacity-40"
        >
          {busy ? <Loader2 size={12} className="animate-spin" /> : <CalendarDays size={12} />}
          Create a calendar link
        </button>
      )}
    </div>
  )
}

function TextList({ label, hint, value, onChange }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1 block">{label}</span>
      <textarea
        rows={3}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink focus:border-accent/60"
      />
      <span className="mt-1 block text-2xs leading-relaxed text-faint">{hint}</span>
    </label>
  )
}
