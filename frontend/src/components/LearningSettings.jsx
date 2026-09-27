import { useCallback, useEffect, useState } from 'react'
import {
  Check,
  Loader2,
  MessageSquareQuote,
  Sparkles,
  TriangleAlert,
  Undo2,
} from 'lucide-react'
import { api } from '../api.js'

/**
 * Teaching the agent from what the shop has already said.
 *
 * Two separate things come out of the same past conversations and the panel
 * keeps them apart on purpose, because they fail in different ways. The facts
 * change what the agent believes and go into the knowledge base. The voice
 * changes how it sounds and goes into the prompt.
 *
 * Everything here is drafted, shown, and only then applied. A shop is already
 * serving customers when they open this: a description of their own voice that
 * takes effect before they have read it is not an upgrade, it is their agent
 * changing personality mid-conversation. So both halves end in a button that
 * says what it will do, and the voice is editable before it is saved — it is
 * their voice, and they will want to correct it.
 */
export default function LearningSettings() {
  const [sources, setSources] = useState(null)
  const [style, setStyle] = useState('')
  const [examples, setExamples] = useState([])
  const [facts, setFacts] = useState(null)
  const [chosen, setChosen] = useState({})
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [note, setNote] = useState(null)

  const load = useCallback(async () => {
    try {
      const found = await api.learningSources()
      setSources(found)
      setStyle(found.voice?.style || '')
      setExamples(found.voice?.examples || [])
    } catch {
      setSources(null)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const run = async (key, action) => {
    setBusy(key)
    setError(null)
    setNote(null)
    try {
      await action()
    } catch (err) {
      setError(err.message)
    }
    setBusy(null)
  }

  const draftVoice = () =>
    run('voice', async () => {
      const draft = await api.previewVoice()
      setStyle(draft.style)
      setExamples(draft.examples || [])
      setNote(`Drafted from ${draft.based_on} message(s) you wrote. Nothing is live yet.`)
    })

  const applyVoice = () =>
    run('apply', async () => {
      await api.saveVoice(style, examples)
      setNote('Applied. Every reply from now on is written in this voice.')
      await load()
    })

  const revert = () =>
    run('revert', async () => {
      await api.clearVoice()
      setStyle('')
      setExamples([])
      setNote('Back to the default voice.')
      await load()
    })

  const draftFacts = () =>
    run('facts', async () => {
      const found = await api.previewLearnedFacts()
      setFacts(found.facts)
      setChosen(Object.fromEntries((found.facts || []).map((_, index) => [index, true])))
      if (!found.facts?.length) setNote('Nothing durable came out of those conversations.')
    })

  const importFacts = () =>
    run('import', async () => {
      const picked = (facts || []).filter((_, index) => chosen[index])
      const result = await api.importLearnedFacts(picked)
      setNote(
        `Added ${result.imported} fact(s) to what the agent knows` +
          (result.replaced ? `, replacing ${result.replaced} from last time.` : '.'),
      )
      setFacts(null)
      await load()
    })

  if (!sources) return null

  const nothing = sources.replies.total === 0 && sources.exchanges === 0

  return (
    <section className="space-y-4">
      <header className="flex items-start gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-xl bg-accent/10 ring-1 ring-inset ring-accent/20">
          <MessageSquareQuote size={14} className="text-accent" />
        </span>
        <div className="min-w-0">
          <h4 className="text-xs font-semibold text-ink">Learn from your own replies</h4>
          <p className="mt-0.5 text-2xs leading-relaxed text-dim">
            What you have already told customers, turned into what the agent knows and
            how it sounds.
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

      {/* The honest accounting. A shop whose agent has been running for months
          will see a large "set aside" number, and the reason is worth saying
          plainly rather than leaving them to wonder where their messages went. */}
      <div className="rounded-lg border border-edge bg-panel-2/40 px-3 py-2.5 text-2xs text-dim">
        {nothing ? (
          <span>
            Nothing to learn from yet. WhatsApp hands over past chats when a phone is
            first connected — reconnect yours, or reply to a few customers from here.
          </span>
        ) : (
          <span>
            <span className="font-mono text-ink">{sources.replies.total}</span> message(s)
            you wrote, <span className="font-mono text-ink">{sources.exchanges}</span>{' '}
            question(s) you answered.
            {sources.skipped_after_cutoff > 0 && (
              <>
                {' '}
                <span className="font-mono text-faint">
                  {sources.skipped_after_cutoff}
                </span>{' '}
                set aside — sent after the agent went live, so there is no way to tell
                which of them it wrote.
              </>
            )}
          </span>
        )}
      </div>

      {/* ------------------------------ voice ------------------------------ */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="eyebrow">How you write</span>
          {sources.voice && (
            <span className="rounded-md bg-ok/10 px-1.5 py-0.5 text-[11px] text-ok">live</span>
          )}
          <button
            type="button"
            disabled={busy !== null || !sources.enough_for_voice}
            onClick={draftVoice}
            className="ml-auto flex items-center gap-1.5 rounded-lg border border-edge px-2.5 py-1.5 text-2xs text-ink transition-colors hover:bg-panel-2 disabled:opacity-40"
          >
            {busy === 'voice' ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <Sparkles size={12} />
            )}
            {sources.voice ? 'Draft again' : 'Read my voice'}
          </button>
        </div>

        {!sources.enough_for_voice && (
          <p className="text-2xs leading-relaxed text-faint">
            Needs at least {sources.minimum_replies} messages you wrote yourself. You
            have {sources.replies.total}.
          </p>
        )}

        {(style || examples.length > 0) && (
          <>
            <textarea
              rows={5}
              value={style}
              onChange={(event) => setStyle(event.target.value)}
              placeholder="Short, direct sentences. Warm greeting, no sign-off."
              className="w-full rounded-lg border border-edge bg-bg px-3 py-2 text-2xs leading-relaxed text-ink placeholder:text-faint focus:border-accent/60"
            />
            <p className="text-2xs leading-relaxed text-faint">
              Edit this freely — it is a description of your own voice and it is worth
              getting right. It changes how the agent writes, never what it says is
              true; prices and stock come from your catalogue.
            </p>

            {examples.length > 0 && (
              <div className="space-y-1.5">
                <span className="eyebrow">Your own lines, used as examples</span>
                {examples.map((example, index) => (
                  <div
                    key={index}
                    className="flex items-start gap-2 rounded-lg bg-panel-2/60 px-2.5 py-1.5"
                  >
                    <span className="flex-1 text-2xs leading-relaxed text-dim">
                      “{example}”
                    </span>
                    <button
                      type="button"
                      onClick={() => setExamples(examples.filter((_, i) => i !== index))}
                      className="text-2xs text-faint transition-colors hover:text-crit"
                    >
                      remove
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy !== null}
                onClick={applyVoice}
                className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                {busy === 'apply' ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Check size={12} />
                )}
                Use this voice
              </button>
              {sources.voice && (
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={revert}
                  className="flex items-center gap-1.5 rounded-lg border border-edge px-3 py-1.5 text-2xs text-dim transition-colors hover:bg-panel-2 hover:text-ink"
                >
                  <Undo2 size={12} /> Default voice
                </button>
              )}
            </div>
          </>
        )}
      </div>

      {/* ------------------------------ facts ------------------------------ */}
      <div className="space-y-2 border-t border-edge pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="eyebrow">What you have already told customers</span>
          {sources.learned_facts > 0 && (
            <span className="font-mono text-[11px] text-faint">
              {sources.learned_facts} saved
            </span>
          )}
          <button
            type="button"
            disabled={busy !== null || sources.exchanges === 0}
            onClick={draftFacts}
            className="ml-auto flex items-center gap-1.5 rounded-lg border border-edge px-2.5 py-1.5 text-2xs text-ink transition-colors hover:bg-panel-2 disabled:opacity-40"
          >
            {busy === 'facts' ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <Sparkles size={12} />
            )}
            Find facts
          </button>
        </div>

        {facts !== null && facts.length > 0 && (
          <>
            <p className="text-2xs leading-relaxed text-dim">
              Untick anything that is no longer true. Only what you tick is saved, and
              it replaces whatever was saved here before.
            </p>
            <div className="space-y-1.5">
              {facts.map((entry, index) => (
                <label
                  key={index}
                  className="flex cursor-pointer items-start gap-2.5 rounded-lg bg-panel-2/60 px-2.5 py-2"
                >
                  <input
                    type="checkbox"
                    checked={Boolean(chosen[index])}
                    onChange={(event) =>
                      setChosen({ ...chosen, [index]: event.target.checked })
                    }
                    className="mt-0.5 accent-accent"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-[11px] uppercase tracking-wide text-faint">
                      {entry.topic}
                    </span>
                    <span className="block text-2xs leading-relaxed text-ink">
                      {entry.fact}
                    </span>
                  </span>
                </label>
              ))}
            </div>
            <button
              type="button"
              disabled={busy !== null || !Object.values(chosen).some(Boolean)}
              onClick={importFacts}
              className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-2xs font-semibold text-on-accent transition-opacity hover:opacity-90 disabled:opacity-40"
            >
              {busy === 'import' ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Check size={12} />
              )}
              Save these
            </button>
          </>
        )}
      </div>
    </section>
  )
}
