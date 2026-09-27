import { useState } from 'react'
import BrandMark from './BrandMark.jsx'
import { ArrowRight, KeyRound, Loader2, TriangleAlert } from 'lucide-react'
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
    <div className="grid h-full place-items-center overflow-y-auto p-4 sm:p-6">
      <div className="w-full max-w-[400px]">
        <BrandMark size={44} className="mb-8 justify-center" />

        <form
          onSubmit={submit}
          className="rounded-3xl border border-edge bg-panel p-6 shadow-lift sm:p-8"
        >
          <h2 className="text-xl font-semibold tracking-tight text-ink">Welcome</h2>
          <p className="mb-6 mt-1 text-sm leading-relaxed text-dim">
            Paste the access token we sent you. You only need to do this once on this device.
          </p>

          {error && (
            <p className="mb-4 flex items-start gap-2 rounded-xl bg-crit/10 px-3.5 py-2.5 text-sm text-crit">
              <TriangleAlert size={15} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          <label className="block">
            <span className="mb-1.5 block text-sm font-medium text-ink">Access token</span>
            <div className="relative">
              <KeyRound
                size={16}
                className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-faint"
              />
              <input
                required
                autoFocus
                spellCheck={false}
                autoComplete="off"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                placeholder="pp_live_…"
                className="w-full rounded-xl border border-edge bg-panel py-3 pl-10 pr-3 font-mono text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none focus:ring-4 focus:ring-accent/10"
              />
            </div>
          </label>

          <button type="submit" disabled={busy || !token.trim()} className="btn-primary mt-5 w-full py-3">
            {busy ? (
              <>
                <Loader2 size={16} className="animate-spin" /> Connecting…
              </>
            ) : (
              <>
                Connect <ArrowRight size={16} />
              </>
            )}
          </button>
        </form>

        <p className="mt-6 text-center text-xs leading-relaxed text-faint">
          No token, or yours stopped working? Contact your PingPulse representative.
        </p>
      </div>
    </div>
  )
}
