import { useCallback, useEffect, useState } from 'react'
import {
  Check,
  Loader2,
  MapPin,
  MessageSquare,
  ShoppingBag,
  TriangleAlert,
  Wallet,
  X,
} from 'lucide-react'
import { PageHeader, Segmented } from './ui.jsx'
import { api } from '../api.js'

/**
 * The orders customers placed in the chat.
 *
 * An order only appears here after the customer said yes to its summary, so
 * every row is something somebody actually asked to buy, at the prices they
 * were shown. From here a person confirms it, sends it, marks it delivered or
 * paid - and can tell the customer, in a message written from the order
 * itself.
 */

const STATUS = {
  placed: { label: 'New', tone: 'bg-warn/10 text-warn' },
  confirmed: { label: 'Confirmed', tone: 'bg-accent/10 text-accent' },
  dispatched: { label: 'On its way', tone: 'bg-accent/10 text-accent' },
  delivered: { label: 'Delivered', tone: 'bg-panel-2 text-dim' },
  cancelled: { label: 'Cancelled', tone: 'bg-crit/10 text-crit' },
}

// The next step for each status, and what the button says.
const NEXT = {
  placed: ['confirmed', 'Confirm order'],
  confirmed: ['dispatched', 'Mark as sent'],
  dispatched: ['delivered', 'Mark delivered'],
}

const FILTERS = [
  ['', 'All'],
  ['placed', 'New'],
  ['confirmed', 'Confirmed'],
  ['dispatched', 'On its way'],
  ['delivered', 'Delivered'],
]

function when(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString(undefined, {
    day: 'numeric',
    month: 'short',
    hour: 'numeric',
    minute: '2-digit',
  })
}

function money(value, currency) {
  const amount = Number(value)
  const text = amount.toLocaleString(undefined, {
    minimumFractionDigits: amount % 1 ? 2 : 0,
    maximumFractionDigits: 2,
  })
  return currency ? `${currency} ${text}` : text
}

export default function Orders({ onOpenConversation }) {
  const [filter, setFilter] = useState('')
  const [orders, setOrders] = useState(null)
  const [error, setError] = useState(null)
  const [open, setOpen] = useState(null)

  const load = useCallback(async () => {
    try {
      setOrders((await api.listOrders(filter || undefined)).orders)
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }, [filter])

  useEffect(() => {
    load()
    const timer = setInterval(load, 30000)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)
    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [load])

  const waiting = (orders || []).filter((o) => o.status === 'placed').length

  return (
    <div className="h-full min-h-0 overflow-y-auto" role="region" aria-label="Orders">
      <div className="mx-auto max-w-5xl">
        <PageHeader
          icon={ShoppingBag}
          title="Orders"
          subtitle={
            orders
              ? waiting
                ? `${waiting} new order${waiting === 1 ? '' : 's'} to confirm`
                : 'Everything placed in the chat, by customers who said yes to the total'
              : 'Everything placed in the chat'
          }
        />
        <div className="space-y-4 px-4 pb-8 sm:px-6 lg:px-8">
          <div className="overflow-x-auto">
            <Segmented label="Show" options={FILTERS} value={filter} onChange={setFilter} />
          </div>

          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          {orders === null && !error && (
            <p className="flex items-center gap-2 py-8 text-xs text-dim">
              <Loader2 size={13} className="animate-spin" /> Loading orders…
            </p>
          )}

          {orders?.length === 0 && (
            <div className="panel items-center px-6 py-10 text-center">
              <ShoppingBag size={22} className="text-faint" />
              <p className="mt-3 text-sm font-medium text-ink">No orders yet</p>
              <p className="mt-1 max-w-sm text-xs leading-relaxed text-dim">
                When a customer chooses something, gives an address and a way to pay, they're shown
                the total and asked to reply YES. That yes puts the order here and alerts you.
              </p>
            </div>
          )}

          {orders?.length > 0 && (
            <ul className="space-y-2">
              {orders.map((order) => (
                <li key={order.id}>
                  <button
                    type="button"
                    onClick={() => setOpen(order)}
                    className="panel w-full flex-row items-center gap-3 px-4 py-3 text-left transition-colors hover:border-edge-hi"
                  >
                    <span className="w-14 shrink-0 font-mono text-xs font-semibold text-ink">
                      #{order.number}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm text-ink">
                        {order.contact?.name || order.contact?.phone_number || 'Customer'}
                      </span>
                      <span className="block truncate text-2xs text-faint">
                        {order.lines.map((l) => `${l.name} × ${l.quantity}`).join(', ')}
                      </span>
                    </span>
                    <span className="hidden shrink-0 text-2xs text-faint sm:block">
                      {when(order.created_at)}
                    </span>
                    <span className="shrink-0 text-right">
                      <span className="block text-sm font-semibold text-ink">{order.total_text}</span>
                      <span className="mt-0.5 flex justify-end gap-1">
                        <span
                          className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                            STATUS[order.status]?.tone
                          }`}
                        >
                          {STATUS[order.status]?.label || order.status}
                        </span>
                        {order.payment_status === 'paid' && (
                          <span className="rounded-full bg-accent/10 px-2 py-0.5 text-[10px] font-semibold text-accent">
                            Paid
                          </span>
                        )}
                      </span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {open && (
        <OrderDialog
          order={open}
          onClose={() => setOpen(null)}
          onSaved={async (next) => {
            setOpen(next)
            await load()
          }}
          onOpenConversation={onOpenConversation}
        />
      )}
    </div>
  )
}

function OrderDialog({ order, onClose, onSaved, onOpenConversation }) {
  const [tell, setTell] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [confirmCancel, setConfirmCancel] = useState(false)

  const change = async (body) => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.changeOrder(order.id, { ...body, tell_customer: tell })
      await onSaved(result.order)
      setConfirmCancel(false)
    } catch (err) {
      setError(err.message)
    }
    setBusy(false)
  }

  const next = NEXT[order.status]
  const live = order.status !== 'cancelled'
  const name = order.contact?.name || order.contact?.phone_number || 'Customer'

  return (
    <div
      className="scrim fixed inset-0 z-50 grid place-items-center p-4"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Order ${order.number}`}
        className="animate-pop max-h-[90vh] w-full max-w-md overflow-y-auto rounded-3xl border border-edge bg-panel p-5 shadow-lift sm:p-6"
      >
        <div className="mb-4 flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h2 className="text-base font-semibold text-ink">
              Order #{order.number}{' '}
              <span
                className={`ml-1 rounded-full px-2 py-0.5 align-middle text-[10px] font-semibold ${
                  STATUS[order.status]?.tone
                }`}
              >
                {STATUS[order.status]?.label || order.status}
              </span>
            </h2>
            <p className="text-xs text-dim">
              {name}
              {order.contact?.phone_number && order.contact?.name ? ` · ${order.contact.phone_number}` : ''} ·{' '}
              {when(order.created_at)}
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="btn-ghost p-1.5">
            <X size={16} />
          </button>
        </div>

        <div className="space-y-4">
          <ul className="divide-y divide-edge rounded-xl border border-edge">
            {order.lines.map((line, i) => (
              <li key={i} className="flex items-baseline gap-2 px-3 py-2 text-xs">
                <span className="min-w-0 flex-1 text-ink">
                  {line.name} <span className="text-faint">× {line.quantity}</span>
                </span>
                <span className="shrink-0 text-ink">{money(line.total, order.currency)}</span>
              </li>
            ))}
            {order.discount && (
              <li className="flex justify-between px-3 py-2 text-xs text-dim">
                <span>Discount</span>
                <span>−{money(order.discount, order.currency)}</span>
              </li>
            )}
            <li className="flex justify-between px-3 py-2 text-xs text-dim">
              <span>Delivery{order.delivery_place ? ` (${order.delivery_place})` : ''}</span>
              <span>
                {order.delivery_fee === null
                  ? 'to confirm'
                  : Number(order.delivery_fee) === 0
                    ? 'free'
                    : money(order.delivery_fee, order.currency)}
              </span>
            </li>
            <li className="flex justify-between px-3 py-2 text-sm font-semibold text-ink">
              <span>Total</span>
              <span>{order.total_text}</span>
            </li>
          </ul>

          <div className="space-y-1.5 text-xs text-dim">
            <p className="flex items-start gap-2">
              <MapPin size={13} className="mt-0.5 shrink-0 text-faint" />
              <span className="text-ink">{order.address || 'No address given'}</span>
            </p>
            <p className="flex items-start gap-2">
              <Wallet size={13} className="mt-0.5 shrink-0 text-faint" />
              <span>
                <span className="text-ink">{order.payment_method || 'Payment to arrange'}</span> ·{' '}
                {order.payment_status === 'paid' ? 'paid' : 'not paid yet'}
              </span>
            </p>
            {order.customer_note && <p className="pl-5 text-dim">Note: {order.customer_note}</p>}
          </div>

          {onOpenConversation && order.contact && (
            <button
              type="button"
              className="btn-secondary w-full py-1.5 text-xs"
              onClick={() => onOpenConversation(order.contact.id)}
            >
              <MessageSquare size={13} /> Open the conversation
            </button>
          )}

          {live && (
            <>
              <label className="flex items-start gap-2 text-xs text-dim">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={tell}
                  onChange={(e) => setTell(e.target.checked)}
                />
                <span>Tell the customer on WhatsApp. The message is written from this order.</span>
              </label>

              <div className="flex flex-wrap gap-2">
                {next && (
                  <button
                    type="button"
                    className="btn-primary flex-1 py-1.5 text-xs"
                    disabled={busy}
                    onClick={() => change({ status: next[0] })}
                  >
                    {busy ? <Loader2 size={12} className="animate-spin" /> : <Check size={12} />}
                    {next[1]}
                  </button>
                )}
                <button
                  type="button"
                  className="btn-secondary flex-1 py-1.5 text-xs"
                  disabled={busy}
                  onClick={() =>
                    change({ payment_status: order.payment_status === 'paid' ? 'unpaid' : 'paid' })
                  }
                >
                  <Wallet size={12} />
                  {order.payment_status === 'paid' ? 'Mark not paid' : 'Mark paid'}
                </button>
              </div>

              {confirmCancel ? (
                <div className="flex gap-2">
                  <button type="button" className="btn-ghost flex-1 text-xs" onClick={() => setConfirmCancel(false)}>
                    Keep it
                  </button>
                  <button
                    type="button"
                    className="btn-primary flex-1 bg-crit py-1.5 text-xs hover:bg-crit"
                    disabled={busy}
                    onClick={() => change({ status: 'cancelled' })}
                  >
                    Cancel order #{order.number}
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="btn-ghost w-full text-xs text-crit"
                  onClick={() => setConfirmCancel(true)}
                >
                  Cancel this order
                </button>
              )}
            </>
          )}

          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
