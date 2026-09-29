import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Bot,
  Check,
  Hand,
  Loader2,
  Save,
  Trash2,
  TriangleAlert,
  User,
  X,
} from 'lucide-react'
import { api } from '../api.js'
import { DEFAULT_STAGES, contactLabel, prettyPhone } from '../format.js'

const FIELDS = [
  ['company', 'Company'],
  ['service_requested', 'Service wanted'],
  ['project_address', 'Address'],
  ['budget', 'Budget'],
  ['timeline', 'Timeline'],
  ['source', 'Came from'],
  ['email', 'Email'],
]

/**
 * Everything known about one lead, and the two controls that matter most.
 *
 * The drawer exists because a conversation is not a lead. Sixty messages tell
 * you what was said; this tells you what the job is, what it is worth and what
 * to do next — the things somebody needs before they can price the work or
 * decide it is not worth pricing.
 *
 * Two things sit at the top rather than buried in it. The summary, because the
 * person opening this has not read the thread and should not have to. And the
 * takeover switch, because the moment you most want it is the moment you are
 * reading a conversation going wrong, and hunting through a settings page for
 * it is time the agent spends replying.
 *
 * Fields the agent filled in and fields a person typed are shown the same way
 * on purpose: by the time you are acting on a lead, where a fact came from
 * matters much less than whether it is right, and anything wrong is editable
 * in place.
 */
export default function LeadProfileDrawer({
  contact,
  stages = DEFAULT_STAGES,
  onClose,
  onSaved,
  onDeleted,
}) {
  const [draft, setDraft] = useState({})
  const [saving, setSaving] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)

  const valuesOf = useCallback((person) => {
    const next = {}
    FIELDS.forEach(([key]) => {
      next[key] = person[key] || ''
    })
    next.name = person.name || ''
    next.notes = person.notes || ''
    // Arrives as a string, because a decimal that round-trips through a float
    // is a decimal that eventually loses a penny.
    next.deal_value = person.deal_value == null ? '' : String(person.deal_value)
    next.pipeline_stage = person.pipeline_stage
    return next
  }, [])

  // What the draft was last filled from, to tell an edit from a refresh.
  const shown = useRef({ id: null, values: {} })

  const reset = useCallback(() => {
    if (!contact) return
    const next = valuesOf(contact)
    shown.current = { id: contact.id, values: next }
    setDraft(next)
    setError(null)
    setNote(null)
  }, [contact, valuesOf])

  // The contact list is re-read on every message and every save, and each
  // re-read is a new object for the same person. That used to reset the
  // form, so notes being typed vanished whenever a customer wrote. Now a new
  // person resets it; the same person's fresh data fills in only the fields
  // nobody has touched, and the "Saved." note stays.
  useEffect(() => {
    if (!contact) return
    if (shown.current.id !== contact.id) {
      reset()
      return
    }
    const fresh = valuesOf(contact)
    const before = shown.current.values
    shown.current = { id: contact.id, values: fresh }
    setDraft((draft) => {
      const merged = { ...draft }
      for (const key of Object.keys(fresh)) {
        if ((draft[key] ?? '') === (before[key] ?? '')) merged[key] = fresh[key]
      }
      return merged
    })
  }, [contact, reset, valuesOf])

  if (!contact) return null

  const dirty = Object.entries(draft).some(([key, value]) => (contact[key] || '') !== value)

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      // Only what actually changed. Sending the whole object would overwrite a
      // field the agent filled in a second ago with what was on screen when
      // the drawer opened.
      const changed = {}
      Object.entries(draft).forEach(([key, value]) => {
        if ((contact[key] || '') !== value) changed[key] = value === '' ? null : value
      })
      if (Object.keys(changed).length) {
        await api.updateContact(contact.id, changed)
        onSaved?.()
      }
      setNote('Saved.')
    } catch (err) {
      setError(err.message)
    }
    setSaving(false)
  }

  const toggleAgent = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.setTakeover(contact.id, !contact.ai_enabled)
      onSaved?.()
      setNote(
        contact.ai_enabled
          ? 'You have this conversation. The agent will not reply.'
          : 'The agent is answering again.',
      )
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end scrim"
      // A click on the dimmed area outside closes it, as it does everywhere else.
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <aside className="animate-slide-right flex h-full w-full max-w-md flex-col border-l border-edge bg-panel shadow-lift">
        <header className="flex items-start gap-2.5 border-b border-edge px-5 py-4">
          <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
            <User size={15} className="text-accent" />
          </span>
          <div className="min-w-0 flex-1">
            <h3 className="truncate text-sm font-semibold text-ink">
              {contactLabel(contact)}
            </h3>
            <p className="mt-0.5 font-mono text-2xs text-dim">
              {prettyPhone(contact.phone_number)}
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded-lg p-1 text-faint transition-colors hover:bg-panel-2 hover:text-ink"
          >
            <X size={16} />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
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

          {contact.opt_out && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" />
              This person asked to stop being messaged. Nothing will be sent to them,
              including anything you type.
            </p>
          )}

          {/* Why it is first: whoever opened this has not read the thread. */}
          {contact.summary && (
            <section>
              <span className="eyebrow mb-1.5 block">Where this stands</span>
              <p className="rounded-lg bg-panel-2/60 px-3 py-2.5 text-[13px] leading-relaxed text-ink">
                {contact.summary}
              </p>
            </section>
          )}

          {contact.next_action && (
            <section>
              <span className="eyebrow mb-1.5 block">Next</span>
              <p className="rounded-lg bg-warn/10 px-3 py-2 text-[13px] leading-relaxed text-warn">
                {contact.next_action}
              </p>
            </section>
          )}

          {/* The switch you want at the moment you are reading a conversation
              going wrong — not on a settings page two clicks away. */}
          <section className="rounded-lg border border-edge bg-panel-2/40 p-3">
            <div className="flex items-center gap-2.5">
              <span
                className={`grid h-7 w-7 shrink-0 place-items-center rounded-lg ${
                  contact.ai_enabled ? 'bg-accent/12 text-accent' : 'bg-warn/12 text-warn'
                }`}
              >
                {contact.ai_enabled ? <Bot size={14} /> : <Hand size={14} />}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold text-ink">
                  {contact.ai_enabled ? 'The agent is answering' : 'You have this one'}
                </p>
                <p className="mt-0.5 text-2xs leading-relaxed text-dim">
                  {contact.ai_enabled
                    ? 'Take it over and nothing is generated here until you hand it back.'
                    : 'Messages still arrive and are shown. Nothing is sent automatically.'}
                </p>
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={toggleAgent}
                className={`shrink-0 rounded-lg px-2.5 py-1.5 text-2xs font-semibold transition-opacity hover:opacity-90 disabled:opacity-40 ${
                  contact.ai_enabled ? 'bg-warn text-on-accent' : 'bg-accent text-on-accent'
                }`}
              >
                {busy ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : contact.ai_enabled ? (
                  'Take over'
                ) : (
                  'Hand back'
                )}
              </button>
            </div>
          </section>

          {Object.keys(contact.qualification || {}).length > 0 && (
            <section>
              <span className="eyebrow mb-1.5 block">What the agent found out</span>
              <dl className="space-y-1">
                {Object.entries(contact.qualification).map(([key, value]) => (
                  <div key={key} className="flex gap-2 text-2xs">
                    <dt className="w-28 shrink-0 capitalize text-faint">
                      {key.replace(/_/g, ' ')}
                    </dt>
                    <dd className="min-w-0 flex-1 text-ink">{value}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}

          <section className="space-y-2.5">
            <span className="eyebrow block">The lead</span>

            <label className="block">
              <span className="mb-1 block text-2xs text-faint">Name</span>
              <input
                value={draft.name || ''}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-xs text-ink focus:border-accent/60"
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-2xs text-faint">Stage</span>
              <select
                value={draft.pipeline_stage || ''}
                onChange={(e) => setDraft({ ...draft, pipeline_stage: e.target.value })}
                className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-xs text-ink focus:border-accent/60"
              >
                {stages.map((stage) => (
                  <option key={stage.key} value={stage.key}>
                    {stage.label}
                  </option>
                ))}
              </select>
            </label>

            {FIELDS.map(([key, label]) => (
              <label key={key} className="block">
                <span className="mb-1 block text-2xs text-faint">{label}</span>
                <input
                  value={draft[key] || ''}
                  onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                  className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-xs text-ink focus:border-accent/60"
                />
              </label>
            ))}

            <label className="block">
              <span className="mb-1 block text-2xs text-faint">What it is worth</span>
              <input
                type="number"
                min="0"
                step="0.01"
                inputMode="decimal"
                value={draft.deal_value ?? ''}
                onChange={(e) => setDraft({ ...draft, deal_value: e.target.value })}
                className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-xs text-ink focus:border-accent/60"
              />
              <span className="mt-1 block text-2xs leading-relaxed text-faint">
                In your own currency. Only what you put here counts towards the revenue
                figures — the agent never guesses this, because a number on a dashboard
                gets acted on.
              </span>
            </label>

            <label className="block">
              <span className="mb-1 block text-2xs text-faint">Notes</span>
              <textarea
                rows={3}
                value={draft.notes || ''}
                onChange={(e) => setDraft({ ...draft, notes: e.target.value })}
                className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-xs leading-relaxed text-ink focus:border-accent/60"
              />
            </label>
          </section>

          <DeleteContact contact={contact} onDeleted={onDeleted} />
        </div>

        <footer className="flex items-center gap-2 border-t border-edge px-5 py-3.5">
          <button
            type="button"
            disabled={saving || !dirty}
            onClick={save}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {saving ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
            Save
          </button>
          {dirty && (
            <button
              type="button"
              onClick={reset}
              className="rounded-lg px-3 py-2 text-xs text-dim transition-colors hover:text-ink"
            >
              Discard
            </button>
          )}
        </footer>
      </aside>
    </div>
  )
}

/**
 * Removing somebody from the dashboard, for good.
 *
 * Two presses, and the second one says exactly what goes: the contact, every
 * message, their appointments and their history on the board. The backend
 * deletes all of it together and there is no copy to restore from, so the
 * confirmation names the person rather than asking "are you sure".
 *
 * If they message again they arrive as somebody new - which is also why this
 * is not the way to stop messaging a person; asking to stop is recorded on the
 * contact, and deleting it would forget that they asked.
 */
function DeleteContact({ contact, onDeleted }) {
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const name = contactLabel(contact)

  useEffect(() => {
    setAsking(false)
    setError(null)
  }, [contact.id])

  const remove = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.deleteContact(contact.id)
      onDeleted?.(contact.id)
    } catch (err) {
      setError(err.message || 'That contact could not be deleted.')
      setBusy(false)
    }
  }

  return (
    <section className="rounded-lg border border-crit/25 p-3">
      <p className="text-xs font-semibold text-ink">Delete this contact</p>
      <p className="mt-0.5 text-2xs leading-relaxed text-dim">
        Removes {name} and everything recorded about them from PingPulse.
      </p>

      {contact.opt_out && (
        <p className="mt-2 text-2xs leading-relaxed text-warn">
          They asked not to be messaged. Deleting them forgets that they asked, so if they
          write again they will be treated as somebody new.
        </p>
      )}

      {error && <p className="mt-2 text-2xs text-crit">{error}</p>}

      {asking ? (
        <div className="mt-3 space-y-2 rounded-lg bg-crit/10 p-3">
          <p className="text-2xs leading-relaxed text-crit">
            <span className="font-semibold">This cannot be undone.</span> {name}, every message
            in this conversation, their appointments and their history on the board are deleted
            now. If they message again they arrive as a new contact.
          </p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={remove}
              className="flex items-center gap-1.5 rounded-lg bg-crit px-3 py-1.5 text-xs font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {busy ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
              Delete {name}
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setAsking(false)}
              className="rounded-lg px-3 py-1.5 text-xs text-dim transition-colors hover:text-ink"
            >
              Keep them
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setAsking(true)}
          className="mt-3 flex items-center gap-1.5 rounded-lg border border-crit/30 px-3 py-1.5 text-xs font-semibold text-crit transition-colors hover:bg-crit/10"
        >
          <Trash2 size={13} /> Delete contact
        </button>
      )}
    </section>
  )
}
