import { useState } from 'react'
import { Radio } from 'lucide-react'
import { api, auth } from '../api.js'

/**
 * Sign in, or create an account together with its first business — a new
 * account with no organization has nowhere to put anything, so the two are
 * asked for together.
 */
export default function SignIn({ onSignedIn }) {
  const [mode, setMode] = useState('login')
  const [form, setForm] = useState({
    email: '',
    password: '',
    full_name: '',
    organization_name: '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const creating = mode === 'signup'
  const field = (key) => ({
    value: form[key],
    onChange: (event) => setForm((f) => ({ ...f, [key]: event.target.value })),
  })

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const result = creating
        ? await api.signUp({
            email: form.email,
            password: form.password,
            full_name: form.full_name || null,
            organization_name: form.organization_name || 'My business',
          })
        : await api.logIn({ email: form.email, password: form.password })
      auth.set(result.access_token)
      onSignedIn()
    } catch (err) {
      setError(err.message || 'That did not work. Try again.')
    } finally {
      setBusy(false)
    }
  }

  const inputClass =
    'w-full rounded-lg border border-edge bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-faint focus:border-accent/60'

  return (
    <div className="grid h-full place-items-center p-6">
      <form onSubmit={submit} className="w-full max-w-sm">
        <div className="mb-7 flex items-center gap-2.5">
          <span className="grid h-9 w-9 place-items-center rounded-lg bg-accent/12 ring-1 ring-inset ring-accent/25">
            <Radio size={17} className="text-accent" />
          </span>
          <div>
            <h1 className="text-[15px] font-bold leading-none tracking-tight text-ink">PingPulse</h1>
            <p className="mt-1 text-[10px] uppercase tracking-[0.16em] text-faint">
              WhatsApp sales agent
            </p>
          </div>
        </div>

        <h2 className="text-lg font-semibold text-ink">
          {creating ? 'Create your account' : 'Welcome back'}
        </h2>
        <p className="mb-5 mt-1 text-xs text-dim">
          {creating
            ? 'You can add more businesses once you are in.'
            : 'Sign in to see your conversations.'}
        </p>

        {error && (
          <p className="mb-4 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">{error}</p>
        )}

        <div className="space-y-3">
          <label className="block">
            <span className="eyebrow mb-1.5 block">Email</span>
            <input required type="email" autoComplete="email" {...field('email')} className={inputClass} />
          </label>

          <label className="block">
            <span className="eyebrow mb-1.5 block">Password</span>
            <input
              required
              type="password"
              minLength={8}
              autoComplete={creating ? 'new-password' : 'current-password'}
              {...field('password')}
              className={inputClass}
            />
          </label>

          {creating && (
            <>
              <label className="block">
                <span className="eyebrow mb-1.5 block">Your name</span>
                <input {...field('full_name')} className={inputClass} placeholder="Optional" />
              </label>
              <label className="block">
                <span className="eyebrow mb-1.5 block">Business name</span>
                <input
                  {...field('organization_name')}
                  className={inputClass}
                  placeholder="Irsa's shoe shop"
                />
              </label>
            </>
          )}
        </div>

        <button
          type="submit"
          disabled={busy}
          className="mt-5 w-full rounded-lg bg-accent px-4 py-2.5 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Just a moment…' : creating ? 'Create account' : 'Sign in'}
        </button>

        <button
          type="button"
          onClick={() => {
            setMode(creating ? 'login' : 'signup')
            setError(null)
          }}
          className="mt-3 w-full text-center text-xs text-dim transition-colors hover:text-ink"
        >
          {creating ? 'I already have an account' : 'Create an account instead'}
        </button>
      </form>
    </div>
  )
}
