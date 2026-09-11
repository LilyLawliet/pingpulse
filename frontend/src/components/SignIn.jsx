import { useState } from 'react'
import BrandMark from './BrandMark.jsx'
import { KeyRound } from 'lucide-react'
import { api, auth } from '../api.js'

/**
 * Connect with an access token.
 *
 * There is no email, no password and no sign-up: a client is issued a token
 * out of band and pastes it here once. The app stores it and sends it as a
 * bearer token from then on, so this screen is only seen on first run or after
 * a token is revoked.
 */
export default function SignIn({ onSignedIn }) {
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // Trim first: a token copied out of an email or a chat window very often
      // arrives with a trailing space or newline attached.
      const cleaned = token.trim()
      const session = await api.logIn(cleaned)
      // Store what the server accepted rather than what was pasted.
      auth.set(session.token || cleaned)
      onSignedIn()
    } catch (err) {
      setError(err.message || 'That token was not accepted.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid h-full place-items-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm">
        <BrandMark size={56} className="mb-7" />

        <h2 className="text-lg font-semibold text-ink">Enter your access token</h2>
        <p className="mb-5 mt-1 text-xs text-dim">
          Paste the token we sent you. You only need to do this once.
        </p>

        {error && (
          <p className="mb-4 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">{error}</p>
        )}

        <label className="block">
          <span className="eyebrow mb-1.5 block">Access token</span>
          <div className="relative">
            <KeyRound
              size={14}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            />
            <input
              required
              autoFocus
              spellCheck={false}
              autoComplete="off"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="pp_live_…"
              className="w-full rounded-lg border border-edge bg-bg py-2 pl-9 pr-3 font-mono text-[12px] text-ink placeholder:text-faint focus:border-accent/60"
            />
          </div>
        </label>

        <button
          type="submit"
          disabled={busy || !token.trim()}
          className="mt-5 w-full rounded-lg bg-accent px-4 py-2.5 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Connecting…' : 'Connect'}
        </button>

        <p className="mt-4 text-center text-[11px] leading-relaxed text-faint">
          Do not have a token, or yours has stopped working?
          <br />
          Contact your PingPulse representative.
        </p>
      </form>
    </div>
  )
}
