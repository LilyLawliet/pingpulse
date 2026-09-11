import { useEffect, useState } from 'react'
import { Sparkles, X } from 'lucide-react'

/**
 * What has changed, in the words of someone running a shop.
 *
 * Not a changelog. A changelog is written for the people who wrote the code; an
 * operator wants to know what is different about their afternoon. Every entry
 * here is something they can see or do, which is also a useful filter on what
 * belongs: work that changes nothing for them — a backup that now leaves the
 * machine, a test suite in CI — is real and does not go on this list.
 */
const RELEASES = [
  {
    version: '1.3.2',
    date: '11 September 2026',
    items: [
      {
        title: 'It can learn to sound like you',
        body:
          'In your business settings, ask it to read your voice. It looks at the messages ' +
          'you have written to customers yourself and describes how you write — short or ' +
          'warm, formal or casual, the language you use and whether you mix two, how you open ' +
          'and sign off. You read the description, change anything that is wrong, and only ' +
          'then does it take effect. Until you do, ' +
          'nothing about your agent changes.',
      },
      {
        title: 'It can learn what you have already told people',
        body:
          'Your delivery areas, your timings, how you take payment — you have answered ' +
          'these a hundred times in WhatsApp already. It reads those answers, turns them into ' +
          'plain facts, and shows you the list. Tick what is still true and it becomes part of ' +
          'what the agent knows.',
      },
      {
        title: 'It never learns from itself',
        body:
          'Only messages a person at your shop actually typed are used. Anything sent after ' +
          'the agent went live is set aside, because WhatsApp does not record who typed what ' +
          'and we will not teach it your voice out of its own replies.',
      },
    ],
  },
  {
    version: '1.3.1',
    date: '11 September 2026',
    items: [
      {
        title: 'Upload your price list',
        body:
          'Open your business settings and drop in a PDF, Word file or text file — your ' +
          'catalogue, your policies, whatever your customers ask about. The agent starts ' +
          'quoting from it straight away, and only from it: it will never invent a price.',
      },
      {
        title: 'Read your WhatsApp catalogue',
        body:
          'If your WhatsApp Business account already has products in it, we can read them ' +
          'directly — nothing to upload. You see the prices we read before anything is ' +
          'saved, so a decimal point in the wrong place never reaches a customer.',
      },
      {
        title: 'Your replies are marked as yours',
        body:
          'When you take over a conversation, your message now shows as yours rather than ' +
          'the agent’s. Worth having on its own, and it is what will let the agent learn ' +
          'to sound like you.',
      },
    ],
  },
  {
    version: '1.3.0',
    date: '11 September 2026',
    items: [
      {
        title: 'A new look',
        body:
          'The console is black, with the brand mark throughout. Easier on the eyes for the ' +
          'kind of screen that stays open all day.',
      },
      {
        title: 'Works on your phone',
        body:
          'The dashboard now fits a phone properly — chats, the open conversation and your ' +
          'pipeline, one at a time instead of three squeezed together.',
      },
      {
        title: 'One customer, one conversation',
        body:
          'When someone changes their WhatsApp account, they no longer appear twice. ' +
          'Conversations that had split back into one.',
      },
    ],
  },
  {
    version: '1.2.x',
    date: '10 September 2026',
    items: [
      {
        title: 'Nothing gets lost',
        body:
          'If WhatsApp is briefly unreachable, replies queue and go out by themselves. You ' +
          'can see which ones are waiting — nothing is shown as delivered until it is.',
      },
      {
        title: 'Follow-ups you control',
        body:
          'Nudge a quiet conversation in your own words, at a time you choose. If the ' +
          'customer writes back first, it cancels itself.',
      },
      {
        title: 'Updates arrive on their own',
        body:
          'The desktop app updates itself when you open it. No installer to run twice.',
      },
    ],
  },
]

const SEEN_KEY = 'pingpulse.whatsnew'
export const LATEST = RELEASES[0].version

/** Whether this browser has already been shown the newest entry. */
export function hasUnseenUpgrades() {
  try {
    return localStorage.getItem(SEEN_KEY) !== LATEST
  } catch {
    // A private window with storage blocked should not lose the button, and
    // should not nag on every render either. Treat it as seen.
    return false
  }
}

export default function WhatsNew({ onClose }) {
  useEffect(() => {
    try {
      localStorage.setItem(SEEN_KEY, LATEST)
    } catch {
      // Nothing to do — the dot comes back next time, which is harmless.
    }
  }, [])

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm">
      <div className="max-h-[88vh] w-full max-w-lg overflow-auto rounded-2xl border border-edge bg-panel shadow-lift">
        <header className="sticky top-0 flex items-center gap-2.5 border-b border-edge bg-panel px-5 py-4">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-accent/12 ring-1 ring-inset ring-accent/25">
            <Sparkles size={15} className="text-accent" />
          </span>
          <div>
            <h3 className="text-sm font-semibold text-ink">What&rsquo;s new</h3>
            <p className="mt-0.5 text-2xs text-dim">Recent upgrades to your agent</p>
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

        <div className="space-y-6 px-5 py-5">
          {RELEASES.map((release) => (
            <section key={release.version}>
              <div className="mb-3 flex items-baseline gap-2">
                <h4 className="platinum font-mono text-xs font-bold">{release.version}</h4>
                <span className="text-2xs text-faint">{release.date}</span>
              </div>
              <ul className="space-y-3">
                {release.items.map((item) => (
                  <li key={item.title} className="border-l border-edge pl-3.5">
                    <p className="text-xs font-semibold text-ink">{item.title}</p>
                    <p className="mt-1 text-2xs leading-relaxed text-dim">{item.body}</p>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      </div>
    </div>
  )
}
