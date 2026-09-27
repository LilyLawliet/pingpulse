import { Component } from 'react'

/**
 * The few pieces every page shares, so they look like one product.
 */

/** A page's title row: what this is, one line on what it is for, and actions. */
export function PageHeader({ icon: Icon, title, subtitle, children }) {
  return (
    <header className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-3 px-4 pb-4 pt-5 sm:px-6 lg:px-8 lg:pt-7">
      {Icon && (
        <span className="hidden h-10 w-10 shrink-0 place-items-center rounded-xl bg-accent/10 text-accent ring-1 ring-inset ring-accent/20 sm:grid">
          <Icon size={18} />
        </span>
      )}
      <div className="min-w-0 flex-1">
        <h2 className="text-lg font-semibold tracking-tight text-ink sm:text-xl">{title}</h2>
        {subtitle && <p className="mt-0.5 text-sm text-dim">{subtitle}</p>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </header>
  )
}

/** A row of mutually exclusive choices, e.g. Today / 7 days / 30 days. */
export function Segmented({ options, value, onChange, label }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map(([key, text]) => (
        <button
          key={key}
          type="button"
          aria-pressed={value === key}
          onClick={() => onChange?.(key)}
          className="seg-item"
        >
          {text}
        </button>
      ))}
    </div>
  )
}

/** A contact's initials in a circle, tinted from their name so a list scans. */
const TINTS = [
  'bg-accent/15 text-accent',
  'bg-customer/15 text-customer',
  'bg-warn/15 text-warn',
  'bg-crit/12 text-crit',
  'bg-edge text-dim',
]

export function Avatar({ text, seed = '', size = 'md', active = false }) {
  let hash = 0
  for (const ch of String(seed || text)) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0
  const tint = active ? 'bg-accent text-on-accent' : TINTS[hash % TINTS.length]
  const box =
    size === 'sm'
      ? 'h-7 w-7 text-[10px]'
      : size === 'lg'
        ? 'h-11 w-11 text-sm'
        : 'h-10 w-10 text-xs'
  return (
    <span
      className={`grid shrink-0 place-items-center rounded-full font-semibold ${box} ${tint}`}
    >
      {text}
    </span>
  )
}

/**
 * One page failing should not blank the whole app.
 *
 * Without this, a panel that throws while rendering takes the sidebar with it
 * and leaves an empty screen with no way to navigate anywhere else.
 */

export class PageBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { failed: null }
  }

  static getDerivedStateFromError(error) {
    return { failed: error }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="grid h-full place-items-center p-6">
        <div className="max-w-sm text-center">
          <p className="text-base font-semibold text-ink">This page could not be shown</p>
          <p className="mt-1.5 text-sm text-dim">
            Something went wrong while loading it. The rest of PingPulse still works.
          </p>
          <button
            type="button"
            className="btn-secondary mt-4"
            onClick={() => this.setState({ failed: null })}
          >
            Try again
          </button>
        </div>
      </div>
    )
  }
}
