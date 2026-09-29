import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CalendarDays,
  ChevronLeft,
  ChevronRight,
  Ban,
  Clock,
  Loader2,
  MessageSquare,
  Plus,
  TriangleAlert,
  X,
} from 'lucide-react'
import { PageHeader } from './ui.jsx'
import { api } from '../api.js'

/**
 * The diary: every appointment the agent or a person has made, a week at a
 * time, in the shop's own timezone and against its own opening hours.
 *
 * Nothing here is a second copy of the diary. Each change goes to the same
 * booking service a customer's message does, so a time outside the hours or
 * on top of another appointment is refused here too - and the reason is shown
 * as the server gave it.
 */

const KIND_LABELS = {
  phone: 'Phone call',
  onsite: 'Site visit',
  video: 'Video call',
  other: 'Appointment',
}

const DAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

/** YYYY-MM-DD for this instant in the shop's zone. */
function dayIn(zone, moment = new Date()) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(moment)
}

/** Calendar arithmetic on a YYYY-MM-DD, with no timezone to trip over. */
function addDays(day, count) {
  const [y, m, d] = day.split('-').map(Number)
  const moved = new Date(Date.UTC(y, m - 1, d + count))
  return moved.toISOString().slice(0, 10)
}

function weekdayOf(day) {
  const [y, m, d] = day.split('-').map(Number)
  return (new Date(Date.UTC(y, m - 1, d)).getUTCDay() + 6) % 7 // Monday = 0
}

function mondayOf(day) {
  return addDays(day, -weekdayOf(day))
}

function dayLabel(day, options) {
  const [y, m, d] = day.split('-').map(Number)
  return new Date(Date.UTC(y, m - 1, d, 12)).toLocaleDateString(undefined, {
    timeZone: 'UTC',
    ...options,
  })
}

function clock(moment, zone) {
  return new Date(moment).toLocaleTimeString(undefined, {
    timeZone: zone,
    hour: 'numeric',
    minute: '2-digit',
  })
}

function clockText(hhmm) {
  const [h, m] = hhmm.split(':').map(Number)
  return new Date(Date.UTC(2024, 0, 1, h, m)).toLocaleTimeString(undefined, {
    timeZone: 'UTC',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export default function Calendar({ contacts = [], onOpenSetup, onOpenConversation }) {
  const [start, setStart] = useState(null)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [creating, setCreating] = useState(null)
  const [blocking, setBlocking] = useState(null)
  const [showCancelled, setShowCancelled] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      setData(await api.listAppointments(start, 7))
    } catch (err) {
      setError(err.message)
    }
    setLoading(false)
  }, [start])

  useEffect(() => {
    load()
    // The agent books while this is open. A minute is soon enough to see it,
    // and coming back to the tab refreshes straight away.
    const timer = setInterval(load, 60000)
    const onFocus = () => load()
    window.addEventListener('focus', onFocus)
    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [load])

  const zone = data?.timezone || 'UTC'
  const today = dayIn(zone)
  // The first load has no start: the server answers from today, and the
  // week shown is the one today falls in.
  const weekStart = start || (data ? mondayOf(data.start) : null)
  useEffect(() => {
    if (!start && data) setStart(mondayOf(data.start))
  }, [start, data])

  const days = useMemo(
    () => (weekStart ? Array.from({ length: 7 }, (_, i) => addDays(weekStart, i)) : []),
    [weekStart],
  )

  const byDay = useMemo(() => {
    const out = {}
    for (const row of data?.appointments || []) {
      if (row.status !== 'confirmed' && !showCancelled) continue
      const key = dayIn(zone, new Date(row.starts_at))
      ;(out[key] ||= []).push(row)
    }
    return out
  }, [data, zone, showCancelled])

  const cancelledCount = (data?.appointments || []).filter((r) => r.status !== 'confirmed').length
  const liveCount = (data?.appointments || []).filter(
    (r) => r.status === 'confirmed' && r.kind !== 'blocked',
  ).length
  const blockers = data?.readiness?.blockers || []
  const canBook = data?.readiness?.can_book

  const range =
    days.length > 0
      ? `${dayLabel(days[0], { day: 'numeric', month: 'short' })} – ${dayLabel(days[6], {
          day: 'numeric',
          month: 'short',
          year: 'numeric',
        })}`
      : ''

  const saved = async () => {
    setSelected(null)
    setCreating(null)
    setBlocking(null)
    await load()
  }

  return (
    <div className="h-full min-h-0 overflow-y-auto" role="region" aria-label="Calendar">
      <div className="mx-auto max-w-7xl">
        <PageHeader
          icon={CalendarDays}
          title="Calendar"
          subtitle={
            data
              ? `${liveCount} booked this week · ${zone.replace(/_/g, ' ')} time`
              : 'Every appointment, booked by the agent or by you'
          }
        />

        <div className="space-y-4 px-4 pb-8 sm:px-6 lg:px-8">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-medium text-ink">{range}</p>
            <div className="flex items-center gap-2">
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  className="btn-ghost p-1.5"
                  aria-label="Previous week"
                  disabled={!weekStart}
                  onClick={() => setStart(addDays(weekStart, -7))}
                >
                  <ChevronLeft size={16} />
                </button>
                <button
                  type="button"
                  className="btn-secondary px-3 py-1.5 text-xs"
                  disabled={!data}
                  onClick={() => setStart(mondayOf(today))}
                >
                  Today
                </button>
                <button
                  type="button"
                  className="btn-ghost p-1.5"
                  aria-label="Next week"
                  disabled={!weekStart}
                  onClick={() => setStart(addDays(weekStart, 7))}
                >
                  <ChevronRight size={16} />
                </button>
              </div>
              <button
                type="button"
                className="btn-secondary px-3 py-1.5 text-xs"
                disabled={!data}
                title="Mark time you're busy, so the agent never offers it"
                onClick={() => setBlocking({ day: days.includes(today) ? today : days[0] })}
              >
                <Ban size={14} /> Block out time
              </button>
              <button
                type="button"
                className="btn-primary px-3 py-1.5 text-xs"
                disabled={!canBook}
                title={canBook ? undefined : 'Set your opening hours first'}
                onClick={() => setCreating({ day: days.includes(today) ? today : days[0] })}
              >
                <Plus size={14} /> New appointment
              </button>
            </div>
          </div>

          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          {blockers.length > 0 && (
            <div className="space-y-2 rounded-xl border border-warn/30 bg-warn/5 px-4 py-3">
              <p className="text-sm font-medium text-ink">
                {canBook ? 'Worth checking' : 'The agent cannot book anyone yet'}
              </p>
              {blockers.map((blocker) => (
                <div key={blocker.key} className="flex flex-wrap items-center gap-2 text-xs text-dim">
                  <span className="flex-1">{blocker.says}</span>
                  <button
                    type="button"
                    className="btn-secondary px-2.5 py-1 text-xs"
                    onClick={() => onOpenSetup?.(blocker.key === 'timezone' ? 'timezone' : 'hours')}
                  >
                    {blocker.fix.replace(/\.$/, '')}
                  </button>
                </div>
              ))}
            </div>
          )}

          {loading && !data && (
            <p className="flex items-center gap-2 py-8 text-xs text-dim">
              <Loader2 size={13} className="animate-spin" /> Opening the diary…
            </p>
          )}

          {data && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-7">
              {days.map((day) => {
                const hours = data.hours?.[DAY_NAMES[weekdayOf(day)]]
                const rows = byDay[day] || []
                const past = day < today
                return (
                  <section
                    key={day}
                    aria-label={dayLabel(day, { weekday: 'long', day: 'numeric', month: 'long' })}
                    className={`panel p-0 ${day === today ? 'ring-1 ring-accent/40' : ''} ${
                      past ? 'opacity-70' : ''
                    }`}
                  >
                    <div className="border-b border-edge px-3 py-2">
                      <p className="flex items-baseline justify-between gap-2">
                        <span className="text-xs font-semibold text-ink">
                          {dayLabel(day, { weekday: 'short' })}
                        </span>
                        <span
                          className={`text-xs ${day === today ? 'font-semibold text-accent' : 'text-dim'}`}
                        >
                          {dayLabel(day, { day: 'numeric', month: 'short' })}
                        </span>
                      </p>
                      <p className="mt-0.5 text-2xs text-faint">
                        {hours ? `${clockText(hours.open)} – ${clockText(hours.close)}` : 'Closed'}
                      </p>
                    </div>
                    <div className="min-h-[2.75rem] space-y-1.5 p-2 lg:min-h-[4.5rem]">
                      {rows.map((row) => (
                        <AppointmentCard
                          key={row.id}
                          row={row}
                          zone={zone}
                          onOpen={() => setSelected(row)}
                        />
                      ))}
                      {rows.length === 0 && hours && !past && canBook && (
                        <button
                          type="button"
                          onClick={() => setCreating({ day })}
                          className="flex w-full items-center justify-center gap-1 rounded-lg border border-dashed border-edge py-2 text-2xs text-faint transition-colors hover:border-accent/40 hover:text-accent"
                        >
                          <Plus size={11} /> Book
                        </button>
                      )}
                    </div>
                  </section>
                )
              })}
            </div>
          )}

          {cancelledCount > 0 && (
            <label className="flex items-center gap-2 text-xs text-dim">
              <input
                type="checkbox"
                checked={showCancelled}
                onChange={(e) => setShowCancelled(e.target.checked)}
              />
              Show {cancelledCount} cancelled or moved this week
            </label>
          )}
        </div>
      </div>

      {selected && (
        <AppointmentDialog
          row={selected}
          zone={zone}
          kinds={data?.kinds}
          onClose={() => setSelected(null)}
          onSaved={saved}
          onOpenConversation={onOpenConversation}
        />
      )}
      {blocking && (
        <BlockDialog
          initialDay={blocking.day < today ? today : blocking.day}
          today={today}
          hours={data?.hours}
          onClose={() => setBlocking(null)}
          onSaved={saved}
        />
      )}
      {creating && (
        <NewAppointmentDialog
          initialDay={creating.day}
          today={today}
          zone={zone}
          contacts={contacts}
          kinds={data?.kinds || []}
          defaultKind={data?.default_kind}
          onClose={() => setCreating(null)}
          onSaved={saved}
        />
      )}
    </div>
  )
}

function AppointmentCard({ row, zone, onOpen }) {
  const live = row.status === 'confirmed'
  if (row.kind === 'blocked') {
    return (
      <button
        type="button"
        onClick={onOpen}
        className={`w-full rounded-lg border border-dashed px-2 py-1.5 text-left transition-colors ${
          live ? 'border-edge-hi bg-panel-2 hover:border-dim' : 'border-edge text-faint line-through'
        }`}
      >
        <p className="flex items-center gap-1 text-2xs font-semibold text-dim">
          <Ban size={10} className="shrink-0 text-faint" />
          {clock(row.starts_at, zone)} – {clock(row.ends_at, zone)}
        </p>
        <p className="truncate text-xs text-dim">{row.notes || 'Blocked out'}</p>
      </button>
    )
  }
  return (
    <button
      type="button"
      onClick={onOpen}
      className={`w-full rounded-lg border px-2 py-1.5 text-left transition-colors ${
        live
          ? 'border-accent/25 bg-accent/5 hover:border-accent/50'
          : 'border-edge bg-panel-2/40 text-faint line-through hover:border-edge-hi'
      }`}
    >
      <p className="flex items-center gap-1 text-2xs font-semibold text-ink">
        <Clock size={10} className="shrink-0 text-faint" />
        {clock(row.starts_at, zone)}
        {!live && <span className="ml-auto text-[10px] font-normal no-underline">{row.status}</span>}
      </p>
      <p className="truncate text-xs text-ink">
        {row.contact?.name || row.contact?.phone_number || 'Customer'}
      </p>
      <p className="truncate text-2xs text-faint">
        {KIND_LABELS[row.kind] || 'Appointment'} · {row.source === 'operator' ? 'by you' : 'by the agent'}
      </p>
    </button>
  )
}

function Dialog({ title, onClose, children }) {
  return (
    <div
      className="scrim fixed inset-0 z-50 grid place-items-center p-4"
      onMouseDown={(event) => event.target === event.currentTarget && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="animate-pop max-h-[90vh] w-full max-w-md overflow-y-auto rounded-3xl border border-edge bg-panel p-5 shadow-lift sm:p-6"
      >
        <div className="mb-4 flex items-start gap-3">
          <h2 className="min-w-0 flex-1 text-base font-semibold text-ink">{title}</h2>
          <button type="button" onClick={onClose} aria-label="Close" className="btn-ghost p-1.5">
            <X size={16} />
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

/** The free start times on one day, straight from the diary. */
function SlotPicker({ day, kind, zone, value, onChange }) {
  const [slots, setSlots] = useState(null)
  const [hours, setHours] = useState('')
  const [error, setError] = useState(null)

  useEffect(() => {
    let live = true
    setSlots(null)
    setError(null)
    api
      .freeTimes(day, kind)
      .then((found) => {
        if (!live) return
        setSlots(found.slots)
        setHours(found.hours)
      })
      .catch((err) => live && setError(err.message))
    return () => {
      live = false
    }
  }, [day, kind])

  if (error) return <p className="text-xs text-crit">{error}</p>
  if (slots === null)
    return (
      <p className="flex items-center gap-2 text-xs text-dim">
        <Loader2 size={12} className="animate-spin" /> Checking the diary…
      </p>
    )
  if (slots.length === 0)
    return (
      <p className="text-xs text-dim">
        Nothing free that day{hours ? ` (${hours.replace(/^open /, 'open ')})` : ''}.
      </p>
    )
  return (
    <div className="flex flex-wrap gap-1.5">
      {slots.map((slot) => (
        <button
          key={slot}
          type="button"
          aria-pressed={value === slot}
          onClick={() => onChange(slot)}
          className={`rounded-lg border px-2 py-1 text-xs transition-colors ${
            value === slot
              ? 'border-accent bg-accent text-on-accent'
              : 'border-edge text-ink hover:border-accent/50'
          }`}
        >
          {clock(slot, zone)}
        </button>
      ))}
    </div>
  )
}

function TellCustomer({ checked, onChange }) {
  return (
    <label className="flex items-start gap-2 text-xs text-dim">
      <input
        type="checkbox"
        className="mt-0.5"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>
        Tell the customer on WhatsApp. The message is written from the calendar entry, so it says
        exactly the day and time saved here.
      </span>
    </label>
  )
}

function AppointmentDialog({ row, zone, onClose, onSaved, onOpenConversation }) {
  const live = row.status === 'confirmed'
  const [mode, setMode] = useState(null) // 'move' | 'cancel'
  const [day, setDay] = useState(dayIn(zone, new Date(row.starts_at)))
  const [slot, setSlot] = useState(null)
  const [tell, setTell] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const act = async (work) => {
    setBusy(true)
    setError(null)
    try {
      await work()
      await onSaved()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  const name = row.contact?.name || row.contact?.phone_number || 'Customer'

  if (row.kind === 'blocked') {
    return (
      <Dialog title="Blocked out" onClose={onClose}>
        <div className="space-y-4">
          <p className={`text-sm text-ink ${live ? '' : 'line-through'}`}>
            {row.description.charAt(0).toUpperCase() + row.description.slice(1)}
          </p>
          {row.notes && <p className="whitespace-pre-wrap text-xs text-dim">{row.notes}</p>}
          <p className="text-xs text-dim">
            {live
              ? 'The agent offers nobody a time inside this.'
              : 'Given back: this time can be booked again.'}
          </p>
          {live && (
            <button
              type="button"
              className="btn-secondary w-full py-1.5 text-xs"
              disabled={busy}
              onClick={() => act(() => api.cancelAppointment(row.id, false))}
            >
              {busy && <Loader2 size={12} className="animate-spin" />}
              Unblock - make this time bookable again
            </button>
          )}
          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
              <TriangleAlert size={13} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}
        </div>
      </Dialog>
    )
  }

  return (
    <Dialog title={name} onClose={onClose}>
      <div className="space-y-4">
        <div className="space-y-1 text-sm">
          <p className={`text-ink ${live ? '' : 'line-through'}`}>
            {row.description.charAt(0).toUpperCase() + row.description.slice(1)}
          </p>
          <p className="text-xs text-dim">
            {live ? 'Booked' : row.status === 'cancelled' ? 'Cancelled' : row.status}{' '}
            {row.source === 'operator' ? 'by you' : 'by the agent'}
            {row.contact?.phone_number ? ` · ${row.contact.phone_number}` : ''}
          </p>
          {row.notes && <p className="whitespace-pre-wrap text-xs text-dim">{row.notes}</p>}
        </div>

        {onOpenConversation && row.contact && (
          <button
            type="button"
            className="btn-secondary w-full py-1.5 text-xs"
            onClick={() => onOpenConversation(row.contact.id)}
          >
            <MessageSquare size={13} /> Open the conversation
          </button>
        )}

        {live && mode === null && (
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-secondary flex-1 py-1.5 text-xs"
              onClick={() => setMode('move')}
            >
              Move
            </button>
            <button
              type="button"
              className="btn-secondary flex-1 py-1.5 text-xs text-crit"
              onClick={() => setMode('cancel')}
            >
              Cancel appointment
            </button>
          </div>
        )}

        {live && mode === 'move' && (
          <div className="space-y-3">
            <label className="block text-xs font-medium text-ink">
              New day
              <input
                type="date"
                value={day}
                min={dayIn(zone)}
                onChange={(e) => {
                  setDay(e.target.value)
                  setSlot(null)
                }}
                className="mt-1 block w-full rounded-xl border border-edge bg-panel-2 px-3 py-2 text-sm text-ink"
              />
            </label>
            {day && <SlotPicker day={day} kind={row.kind} zone={zone} value={slot} onChange={setSlot} />}
            <TellCustomer checked={tell} onChange={setTell} />
            <div className="flex gap-2">
              <button type="button" className="btn-ghost flex-1 text-xs" onClick={() => setMode(null)}>
                Back
              </button>
              <button
                type="button"
                className="btn-primary flex-1 py-1.5 text-xs"
                disabled={!slot || busy}
                onClick={() => act(() => api.moveAppointment(row.id, slot, tell))}
              >
                {busy && <Loader2 size={12} className="animate-spin" />}
                Move to {slot ? clock(slot, zone) : '…'}
              </button>
            </div>
          </div>
        )}

        {live && mode === 'cancel' && (
          <div className="space-y-3">
            <p className="text-xs text-dim">
              The time becomes free for other customers. The entry stays in the calendar, marked
              cancelled.
            </p>
            <TellCustomer checked={tell} onChange={setTell} />
            <div className="flex gap-2">
              <button type="button" className="btn-ghost flex-1 text-xs" onClick={() => setMode(null)}>
                Keep it
              </button>
              <button
                type="button"
                className="btn-primary flex-1 bg-crit py-1.5 text-xs hover:bg-crit"
                disabled={busy}
                onClick={() => act(() => api.cancelAppointment(row.id, tell))}
              >
                {busy && <Loader2 size={12} className="animate-spin" />}
                Cancel it
              </button>
            </div>
          </div>
        )}

        {error && (
          <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            {error}
          </p>
        )}
      </div>
    </Dialog>
  )
}

function NewAppointmentDialog({
  initialDay,
  today,
  zone,
  contacts,
  kinds,
  defaultKind,
  onClose,
  onSaved,
}) {
  const [contactId, setContactId] = useState('')
  const [search, setSearch] = useState('')
  const [day, setDay] = useState(initialDay < today ? today : initialDay)
  const [kind, setKind] = useState(defaultKind || kinds[0] || 'other')
  const [slot, setSlot] = useState(null)
  const [location, setLocation] = useState('')
  const [notes, setNotes] = useState('')
  const [tell, setTell] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const matching = useMemo(() => {
    const needle = search.trim().toLowerCase()
    const list = needle
      ? contacts.filter(
          (c) =>
            (c.name || '').toLowerCase().includes(needle) || (c.phone_number || '').includes(needle),
        )
      : contacts
    return list.slice(0, 50)
  }, [contacts, search])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.bookAppointment({
        contact_id: contactId,
        starts_at: slot,
        kind,
        location: location || null,
        notes: notes || null,
        tell_customer: tell,
      })
      await onSaved()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  const field =
    'mt-1 block w-full rounded-xl border border-edge bg-panel-2 px-3 py-2 text-sm text-ink focus:border-accent/60 focus:outline-none'

  return (
    <Dialog title="New appointment" onClose={onClose}>
      <div className="space-y-3">
        <label className="block text-xs font-medium text-ink">
          Customer
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name or number"
            className={field}
          />
        </label>
        <select
          value={contactId}
          onChange={(e) => setContactId(e.target.value)}
          aria-label="Choose the customer"
          size={Math.min(5, Math.max(2, matching.length))}
          className={`${field} mt-0`}
        >
          {matching.map((contact) => (
            <option key={contact.id} value={contact.id}>
              {contact.name ? `${contact.name} · ${contact.phone_number}` : contact.phone_number}
            </option>
          ))}
        </select>
        {contacts.length === 0 && (
          <p className="text-2xs text-faint">
            Customers appear here once they have messaged you on WhatsApp.
          </p>
        )}

        <div className="grid grid-cols-2 gap-2">
          <label className="block text-xs font-medium text-ink">
            Day
            <input
              type="date"
              value={day}
              min={today}
              onChange={(e) => {
                setDay(e.target.value)
                setSlot(null)
              }}
              className={field}
            />
          </label>
          <label className="block text-xs font-medium text-ink">
            Kind
            <select
              value={kind}
              onChange={(e) => {
                setKind(e.target.value)
                setSlot(null)
              }}
              className={field}
            >
              {kinds.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k] || k}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div>
          <p className="mb-1.5 text-xs font-medium text-ink">Free times</p>
          {day && <SlotPicker day={day} kind={kind} zone={zone} value={slot} onChange={setSlot} />}
        </div>

        <label className="block text-xs font-medium text-ink">
          Where <span className="font-normal text-faint">(optional)</span>
          <input value={location} onChange={(e) => setLocation(e.target.value)} className={field} />
        </label>
        <label className="block text-xs font-medium text-ink">
          Notes <span className="font-normal text-faint">(only you see these)</span>
          <textarea
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            className={field}
          />
        </label>

        <TellCustomer checked={tell} onChange={setTell} />

        {error && (
          <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            {error}
          </p>
        )}

        <button
          type="button"
          className="btn-primary w-full py-2 text-sm"
          disabled={!contactId || !slot || busy}
          onClick={submit}
        >
          {busy && <Loader2 size={13} className="animate-spin" />}
          Book {slot ? `${dayLabel(day, { weekday: 'short', day: 'numeric', month: 'short' })}, ${clock(slot, zone)}` : ''}
        </button>
      </div>
    </Dialog>
  )
}

/** Half-hour marks through a whole day, as HH:MM, for picking a block by hand. */
const HALF_HOURS = Array.from({ length: 49 }, (_, i) => {
  const h = Math.floor(i / 2)
  return `${String(h).padStart(2, '0')}:${i % 2 ? '30' : '00'}`
})

function BlockDialog({ initialDay, today, hours, onClose, onSaved }) {
  const openFor = (day) => hours?.[DAY_NAMES[weekdayOf(day)]]
  const first = openFor(initialDay)
  const [day, setDay] = useState(initialDay)
  const [wholeDay, setWholeDay] = useState(false)
  const [from, setFrom] = useState(first?.open || '09:00')
  const [to, setTo] = useState(first?.close || '17:00')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const field =
    'mt-1 block w-full rounded-xl border border-edge bg-panel-2 px-3 py-2 text-sm text-ink focus:border-accent/60 focus:outline-none'

  const submit = async () => {
    setBusy(true)
    setError(null)
    // Written as the shop's own clock time; the server reads it in the shop's zone.
    const starts = `${day}T${wholeDay ? '00:00' : from}`
    const ends = wholeDay || to === '24:00' ? `${addDays(day, 1)}T00:00` : `${day}T${to}`
    try {
      await api.blockTime({ starts_at: starts, ends_at: ends, note: note.trim() || null })
      await onSaved()
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <Dialog title="Block out time" onClose={onClose}>
      <div className="space-y-3">
        <p className="text-xs leading-relaxed text-dim">
          For when you're busy - a day off, an errand, a meeting booked somewhere else. The agent
          won't offer any time inside it.
        </p>
        <label className="block text-xs font-medium text-ink">
          Day
          <input
            type="date"
            value={day}
            min={today}
            onChange={(e) => {
              setDay(e.target.value)
              const hoursThen = openFor(e.target.value)
              if (hoursThen) {
                setFrom(hoursThen.open)
                setTo(hoursThen.close)
              }
            }}
            className={field}
          />
        </label>
        <label className="flex items-center gap-2 text-xs text-ink">
          <input type="checkbox" checked={wholeDay} onChange={(e) => setWholeDay(e.target.checked)} />
          The whole day
        </label>
        {!wholeDay && (
          <div className="grid grid-cols-2 gap-2">
            <label className="block text-xs font-medium text-ink">
              From
              <select value={from} onChange={(e) => setFrom(e.target.value)} className={field}>
                {HALF_HOURS.slice(0, -1).map((t) => (
                  <option key={t} value={t}>
                    {clockText(t)}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-xs font-medium text-ink">
              Until
              <select value={to} onChange={(e) => setTo(e.target.value)} className={field}>
                {HALF_HOURS.slice(1).map((t) => (
                  <option key={t} value={t}>
                    {t === '24:00' ? 'Midnight' : clockText(t)}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
        <label className="block text-xs font-medium text-ink">
          Note <span className="font-normal text-faint">(only you see this)</span>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Dentist, stock take, day off…"
            className={field}
          />
        </label>
        {error && (
          <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-xs text-crit">
            <TriangleAlert size={13} className="mt-0.5 shrink-0" />
            {error}
          </p>
        )}
        <button
          type="button"
          className="btn-primary w-full py-2 text-sm"
          disabled={busy || (!wholeDay && to <= from)}
          onClick={submit}
        >
          {busy && <Loader2 size={13} className="animate-spin" />}
          Block it out
        </button>
      </div>
    </Dialog>
  )
}
