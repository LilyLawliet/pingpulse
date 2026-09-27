import { useState } from 'react'
import { Building2, ChevronsUpDown, Plus, Save, X } from 'lucide-react'
import { api } from '../api.js'

const EMPTY = {
  name: '',
  target_tone: '',
  product_rules: '',
  sales_prompt: '',
  default_currency: 'USD',
  default_language: 'en',
}

const CURRENCIES = ['USD', 'EUR', 'GBP', 'PKR', 'AED', 'SAR', 'INR', 'TRY', 'NGN', 'ZAR']

// An example priced in the currency the shop actually picked. This used to be
// one fixed string in rupees, left over from the first shop it was written
// for, sitting above a dropdown that defaults to USD - so the very first
// thing a new business read was an example that contradicted its own form.
//
// Amounts per currency rather than one set with the code swapped: "USD
// 12,000-45,000" for a pair of shoes is a worse example than the one it
// replaced, and an example nobody believes teaches nothing.
const EXAMPLE_PRICES = {
  USD: ['120', '450', '150'],
  EUR: ['110', '420', '140'],
  GBP: ['95', '360', '120'],
  PKR: ['12,000', '45,000', '15,000'],
  AED: ['450', '1,650', '550'],
  SAR: ['450', '1,700', '560'],
  INR: ['10,000', '38,000', '12,500'],
  TRY: ['4,000', '15,000', '5,000'],
  NGN: ['180,000', '700,000', '230,000'],
  ZAR: ['2,200', '8,300', '2,800'],
}

function sellingExample(currency) {
  const code = EXAMPLE_PRICES[currency] ? currency : 'USD'
  const [low, high, delivery] = EXAMPLE_PRICES[code]
  return (
    `Designer sneakers and heels, ${code} ${low}\u2013${high}. ` +
    `Free delivery over ${code} ${delivery}.`
  )
}

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
export default function OrgSelector({
  organizations,
  selectedId,
  onSelect,
  onSaved,
  // Only "No business yet" once the server has said so; before that, or when
  // it could not be reached, an empty list means nothing has loaded.
  emptyLabel = 'Loading…',
}) {
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
    'w-full rounded-xl border border-edge bg-panel px-3.5 py-2.5 text-sm text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none focus:ring-4 focus:ring-accent/10'

  return (
    <>
      <div className="flex items-center gap-1.5">
        <label className="relative min-w-0 flex-1">
          <span className="sr-only">Business</span>
          <Building2
            size={15}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
          />
          <select
            value={selectedId || ''}
            onChange={(e) => onSelect(e.target.value)}
            className="w-full cursor-pointer appearance-none truncate rounded-xl border border-edge bg-panel py-2 pl-9 pr-8 text-sm font-medium text-ink transition-colors hover:border-edge-hi focus:border-accent/60 focus:outline-none"
          >
            {/* A token can arrive with no business yet. An empty box reads as
                broken; this says what is true and where it gets fixed. */}
            {organizations.length === 0 && <option value="">{emptyLabel}</option>}
            {organizations.map((org) => (
              <option key={org.id} value={org.id}>
                {org.name}
              </option>
            ))}
          </select>
          <ChevronsUpDown
            size={14}
            className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-faint"
          />
        </label>

        <button
          type="button"
          onClick={openCreate}
          title="Add a business"
          aria-label="Add a business"
          className="grid h-[38px] w-[38px] shrink-0 place-items-center rounded-xl border border-edge text-dim transition-colors hover:border-accent/50 hover:text-accent"
        >
          <Plus size={16} />
        </button>
      </div>

      {open && (
        <div className="fixed inset-0 z-50 grid place-items-center scrim p-4">
          <form
            onSubmit={submit}
            className="max-h-[90vh] w-full max-w-xl overflow-auto rounded-2xl border border-edge bg-panel shadow-lift animate-pop"
          >
            <header className="flex items-center gap-2 border-b border-edge px-5 py-4">
              <div>
                <h3 className="text-base font-semibold text-ink">
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
                <span className="mb-1.5 block text-sm font-medium text-ink">Business name</span>
                <input required {...field('name')} className={inputClass} placeholder="Luxe Footwear" />
              </label>

              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-ink">How should it sound?</span>
                <input
                  {...field('target_tone')}
                  className={inputClass}
                  placeholder="Warm, confident, a little playful"
                />
              </label>

              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-ink">What you sell</span>
                <textarea
                  rows={3}
                  {...field('product_rules')}
                  className={inputClass}
                  placeholder={sellingExample(form.default_currency)}
                />
              </label>

              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="mb-1.5 block text-sm font-medium text-ink">Currency</span>
                  <select {...field('default_currency')} className={inputClass}>
                    {CURRENCIES.map((code) => (
                      <option key={code} value={code}>
                        {code}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1.5 block text-sm font-medium text-ink">Replies in</span>
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
                <span className="mb-1.5 block text-sm font-medium text-ink">How it should sell</span>
                <textarea
                  required
                  rows={5}
                  {...field('sales_prompt')}
                  className={inputClass}
                  placeholder="Greet by name, answer the question, always quote a price, and offer to reserve a pair."
                />
              </label>

            </div>

            <footer className="flex items-center gap-3 border-t border-edge px-5 py-4">
              <button
                type="submit"
                disabled={saving}
                className="btn-primary"
              >
                <Save size={14} /> {saving ? 'Saving…' : 'Save and go live'}
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="btn-ghost"
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
