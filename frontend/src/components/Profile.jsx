import { useState } from 'react'
import { Check, Copy, Eye, EyeOff, KeyRound, LogOut, X } from 'lucide-react'
import { Avatar } from './ui.jsx'
import { initialsOf } from '../format.js'

/** "pp_live_" and the last four, with the middle hidden. */
function masked(token) {
  if (!token) return ''
  const prefix = token.startsWith('pp_live_') ? 'pp_live_' : ''
  return `${prefix}${'•'.repeat(16)}${token.slice(-4)}`
}

function when(iso) {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' })
}

/**
 * The sidebar row that says who is signed in.
 *
 * The name is the one the access token was issued to, not the business: one
 * person can run several businesses from the same token.
 */
export function ProfileButton({ session, onOpen }) {
  const name = session?.client_name || 'Your profile'
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-panel-2"
    >
      <Avatar size="sm" seed={name} text={initialsOf(session?.client_name, '') || '?'} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-ink">{name}</span>
        <span className="block text-2xs text-faint">Profile and access token</span>
      </span>
    </button>
  )
}

/**
 * Who this is, and the token they signed in with.
 *
 * The token is the only way in - there is no password to fall back on - so it
 * is shown here for the moment somebody needs it on another device, hidden
 * until asked for so it is not on screen for anyone looking over a shoulder.
 */
export default function Profile({ session, token, onClose, onSignOut }) {
  const [shown, setShown] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState(null)
  const expires = when(session?.expires_at)

  const copy = async () => {
    setError(null)
    try {
      await navigator.clipboard.writeText(token)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Clipboard access is refused in some browsers and in the desktop shell's
      // older builds. Showing it lets it be selected and copied by hand.
      setShown(true)
      setError('Could not copy. Select the token and copy it by hand.')
    }
  }

  return (
    <div
      className="scrim fixed inset-0 z-50 grid place-items-center p-4"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="profile-title"
        className="animate-pop w-full max-w-md rounded-3xl border border-edge bg-panel p-6 shadow-lift sm:p-7"
      >
        <div className="flex items-start gap-3">
          <Avatar
            size="lg"
            seed={session?.client_name || ''}
            text={initialsOf(session?.client_name, '') || '?'}
          />
          <div className="min-w-0 flex-1">
            <h2 id="profile-title" className="truncate text-lg font-semibold text-ink">
              {session?.client_name || 'Your profile'}
            </h2>
            <p className="text-sm text-dim">The name your access token was issued to</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="btn-ghost p-1.5">
            <X size={18} />
          </button>
        </div>

        <div className="mt-6">
          <p className="mb-1.5 flex items-center gap-1.5 text-sm font-medium text-ink">
            <KeyRound size={14} className="text-faint" /> Access token
          </p>
          <div className="flex gap-2">
            <input
              readOnly
              value={shown ? token || '' : masked(token)}
              onFocus={(event) => shown && event.target.select()}
              aria-label="Access token"
              className="min-w-0 flex-1 rounded-xl border border-edge bg-panel-2 px-3.5 py-2.5 font-mono text-xs text-ink focus:border-accent/60 focus:outline-none"
            />
            <button
              type="button"
              onClick={() => setShown((was) => !was)}
              className="btn-secondary shrink-0 px-3"
              aria-label={shown ? 'Hide token' : 'Show token'}
              title={shown ? 'Hide' : 'Show'}
            >
              {shown ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
            <button
              type="button"
              onClick={copy}
              disabled={!token}
              className="btn-secondary shrink-0 px-3"
              aria-label="Copy token"
              title="Copy"
            >
              {copied ? <Check size={15} className="text-ok" /> : <Copy size={15} />}
            </button>
          </div>
          {error && <p className="mt-2 text-2xs text-crit">{error}</p>}
          <p className="mt-2 text-2xs leading-relaxed text-faint">
            This is what signs you in, so keep it private: anyone who has it can open your
            dashboard. Signing in on another computer uses one of your licence&rsquo;s seats.
          </p>
        </div>

        {expires && (
          <p className="mt-5 rounded-xl bg-panel-2/60 px-3.5 py-2.5 text-xs text-dim">
            Valid until <span className="font-semibold text-ink">{expires}</span>
          </p>
        )}

        <div className="mt-6 flex justify-end">
          <button type="button" onClick={onSignOut} className="btn-secondary">
            <LogOut size={15} /> Sign out
          </button>
        </div>
      </div>
    </div>
  )
}
