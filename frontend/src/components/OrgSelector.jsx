import { useState } from 'react'
import { Building2, Plus, Save, X } from 'lucide-react'
import { api } from '../api.js'
import WhatsAppSettings from './WhatsAppSettings.jsx'

const EMPTY = {
  name: '',
  target_tone: '',
  product_rules: '',
  sales_prompt: '',
  default_currency: 'USD',
  default_language: 'en',
}

const CURRENCIES = ['USD', 'EUR', 'GBP', 'PKR', 'AED', 'SAR', 'INR', 'TRY', 'NGN', 'ZAR']
const LANGUAGES = [
  ['en', 'English'],
  ['ur', 'Urdu'],
  ['ar', 'Arabic'],
  ['fr', 'French'],
  ['es', 'Spanish'],
  ['de', 'German'],
  ['tr', 'Turkish'],
  ['hi', 'Hindi'],
]

/**
 * Business switcher plus the setup sheet: who you are, how you sound, what you
 * sell, and how the agent should push a conversation forward.
 */
export default function OrgSelector({ organizations, selectedId, onSelect, onSaved }) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState(EMPTY)
  const [editingId, setEditingId] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const openCreate = () => {
    setForm(EMPTY)
    setEditingId(null)
    setError(null)
    setOpen(true)
  }

  const openEdit = () => {
    const current = organizations.find((o) => o.id === selectedId)
    if (!current) return openCreate()
    setForm({
      name: current.name || '',
      target_tone: current.target_tone || '',
      product_rules: current.product_rules || '',
      sales_prompt: current.sales_prompt || '',
      default_currency: current.default_currency || 'USD',
      default_language: current.default_language || 'en',
    })
    setEditingId(current.id)
    setError(null)
    setOpen(true)
  }

  const submit = async (event) => {
    event.preventDefault()
    setSaving(true)
    try {
      // Edits always apply to the active organization, which is the one
      // shown in the switcher.
      const saved = editingId
        ? await api.updateActiveOrganization(form)
        : await api.createOrganization(form)
      setOpen(false)
      onSaved(saved)
    } catch {
      setError('That did not save. Check the details and try again.')
    } finally {
      setSaving(false)
    }
  }

  const field = (key) => ({
    value: form[key],
    onChange: (e) => setForm((f) => ({ ...f, [key]: e.target.value })),
  })

  const inputClass =
    'w-full rounded-lg border border-edge bg-bg px-3 py-2 text-[13px] text-ink placeholder:text-faint focus:border-accent/60'

  return (
    <>
      <div className="flex items-center gap-2">
        <div className="relative">
          <Building2
            size={13}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint"
          />
          <select
            value={selectedId || ''}
            onChange={(e) => onSelect(e.target.value)}
            className="appearance-none rounded-lg border border-edge bg-panel py-1.5 pl-7 pr-7 text-xs text-ink"
          >
            {organizations.map((org) => (
              <option key={org.id} value={org.id}>
                {org.name}
              </option>
            ))}
          </select>
        </div>

        <button
          onClick={openEdit}
          disabled={!selectedId}
          className="rounded-lg border border-edge px-2.5 py-1.5 text-xs text-dim transition-colors hover:border-edge-hi hover:text-ink disabled:opacity-40"
        >
          Edit
        </button>
        <button
          onClick={openCreate}
          className="flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-semibold text-bg transition-opacity hover:opacity-90 sm:px-3"
        >
          <Plus size={13} />
          <span className="hidden sm:inline">Add business</span>
          <span className="sr-only sm:hidden">Add business</span>
        </button>
      </div>

      {open && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-black/70 p-4 backdrop-blur-sm">
          <form
            onSubmit={submit}
            className="max-h-[90vh] w-full max-w-xl overflow-auto rounded-2xl border border-edge bg-panel shadow-lift"
          >
            <header className="flex items-center gap-2 border-b border-edge px-5 py-4">
              <div>
                <h3 className="text-sm font-semibold text-ink">
                  {editingId ? 'Edit business' : 'Set up your business'}
                </h3>
                <p className="mt-0.5 text-2xs text-dim">
                  This is what your agent knows when it answers a customer.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="ml-auto rounded-lg p-1 text-faint transition-colors hover:bg-panel-2 hover:text-ink"
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </header>

            <div className="space-y-4 px-5 py-5">
              {error && (
                <p className="rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">{error}</p>
              )}

              <label className="block">
                <span className="eyebrow mb-1.5 block">Business name</span>
                <input required {...field('name')} className={inputClass} placeholder="Luxe Footwear" />
              </label>

              <label className="block">
                <span className="eyebrow mb-1.5 block">How should it sound?</span>
                <input
                  {...field('target_tone')}
                  className={inputClass}
                  placeholder="Warm, confident, a little playful"
                />
              </label>

              <label className="block">
                <span className="eyebrow mb-1.5 block">What you sell</span>
                <textarea
                  rows={3}
                  {...field('product_rules')}
                  className={inputClass}
                  placeholder="Designer sneakers and heels, PKR 12,000–45,000. Free delivery over PKR 15,000."
                />
              </label>

              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="eyebrow mb-1.5 block">Currency</span>
                  <select {...field('default_currency')} className={inputClass}>
                    {CURRENCIES.map((code) => (
                      <option key={code} value={code}>
                        {code}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="eyebrow mb-1.5 block">Replies in</span>
                  <select {...field('default_language')} className={inputClass}>
                    {LANGUAGES.map(([code, label]) => (
                      <option key={code} value={code}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
              </div>

              <label className="block">
                <span className="eyebrow mb-1.5 block">How it should sell</span>
                <textarea
                  required
                  rows={5}
                  {...field('sales_prompt')}
                  className={inputClass}
                  placeholder="Greet by name, answer the question, always quote a price, and offer to reserve a pair."
                />
              </label>

              {/* A channel binds to an organization, so it can only be set up
                  once the business itself exists. */}
              {editingId && (
                <div className="border-t border-edge pt-4">
                  <WhatsAppSettings />
                </div>
              )}
            </div>

            <footer className="flex items-center gap-3 border-t border-edge px-5 py-4">
              <button
                type="submit"
                disabled={saving}
                className="flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-xs font-semibold text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
              >
                <Save size={14} /> {saving ? 'Saving…' : 'Save and go live'}
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="text-xs text-dim transition-colors hover:text-ink"
              >
                Cancel
              </button>
            </footer>
          </form>
        </div>
      )}
    </>
  )
}
