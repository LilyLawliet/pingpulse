import { useCallback, useEffect, useState } from 'react'
import {
  ChartNoAxesColumn,
  Loader2,
  MessageSquareReply,
  TrendingDown,
  TriangleAlert,
} from 'lucide-react'
import { PageHeader, Segmented } from './ui.jsx'
import { api } from '../api.js'
import { bucketLabel, duration, percent } from '../format.js'

const WINDOWS = [
  ['today', 'Today'],
  ['7d', '7 days'],
  ['30d', '30 days'],
  ['90d', '90 days'],
  ['all', 'All time'],
]

/**
 * How it is going, rather than what is happening right now.
 *
 * The inbox answers the second question and could never answer the first. A
 * column holding a current stage says where every lead is standing; it cannot
 * say how many got as far as qualified, where people stop replying, or whether
 * the agent is actually doing the answering. Those are the questions a shop
 * asks when deciding whether to keep paying for this.
 *
 * Every number here is allowed to be absent. A shop that has never replied has
 * no median reply time, and nought out of nought leads is not a nought per cent
 * conversion rate — it is a question with no answer yet. Both render as an
 * em dash rather than as a zero, because a zero is a claim and these are not.
 */
export default function Analytics() {
  const [window_, setWindow_] = useState('30d')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await api.analytics(window_))
    } catch (err) {
      setError(err.message)
      setData(null)
    }
    setLoading(false)
  }, [window_])

  useEffect(() => {
    load()
  }, [load])

  const funnel = data?.funnel
  const replies = data?.replies
  const traffic = data?.traffic
  const wonLabel =
    funnel?.stages?.filter((stage) => stage.outcome === 'won').slice(-1)[0]?.label || 'Won'

  return (
    <div className="h-full min-h-0 overflow-y-auto" role="region" aria-label="How it is going">
      <div className="mx-auto max-w-6xl">
        <PageHeader
          icon={ChartNoAxesColumn}
          title="Analytics"
          subtitle="Where leads get to, and how fast they get answered"
        >
          <Segmented label="Time span" options={WINDOWS} value={window_} onChange={setWindow_} />
        </PageHeader>

        <div className="space-y-5 px-4 pb-8 sm:px-6 lg:px-8">
          {error && (
            <p className="flex items-start gap-2 rounded-lg bg-crit/10 px-3 py-2 text-2xs text-crit">
              <TriangleAlert size={12} className="mt-0.5 shrink-0" />
              {error}
            </p>
          )}

          {loading && !data && (
            <p className="flex items-center gap-2 py-8 text-2xs text-dim">
              <Loader2 size={13} className="animate-spin" /> Counting…
            </p>
          )}

          {data && (
            <>
              <Headline
                entered={funnel?.entered ?? 0}
                converted={funnel?.converted ?? 0}
                rate={funnel?.conversion_rate}
                median={replies?.median_seconds}
                wonLabel={wonLabel}
              />

              {funnel?.revenue != null && (
                <Revenue funnel={funnel} entered={funnel.entered} />
              )}

              <Funnel funnel={funnel} />

              <Traffic traffic={traffic} />

              <Replies replies={replies} />

              {data.sources?.length > 0 && <Sources sources={data.sources} />}

              <Footnote data={data} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ parts */

function Stat({ label, value, hint }) {
  return (
    <div className="bg-panel px-4 py-3.5">
      <p className="truncate text-xs font-medium text-dim">{label}</p>
      <p className="mt-1.5 text-xl font-semibold leading-none tracking-tight tabular-nums text-ink sm:text-2xl">
        {value}
      </p>
      {hint && <p className="mt-0.5 truncate text-2xs text-faint">{hint}</p>}
    </div>
  )
}

function Headline({ entered, converted, rate, median, wonLabel }) {
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-edge bg-edge shadow-card lg:grid-cols-4">
      <Stat label="Leads who arrived" value={entered} />
      <Stat label={`Reached ${wonLabel.toLowerCase()}`} value={converted} />
      <Stat label="Of everyone who arrived" value={percent(rate)} />
      <Stat
        label="Typical reply time"
        value={duration(median)}
        hint="half are faster than this"
      />
    </div>
  )
}

/**
 * Money, and how much of it is actually known.
 *
 * Only leads somebody has put a figure on are counted, so the count of those
 * is shown next to the total rather than buried. A shop that has priced three
 * of its forty deals should read "3 of 40 have a value", not a revenue number
 * that presents itself as the whole picture — an under-reported total that
 * looks complete is worse than no total at all.
 *
 * The panel is hidden entirely until somebody prices their first lead. An
 * empty revenue tile reading a confident zero is the exact thing this is
 * trying to avoid.
 */
function Revenue({ funnel, entered }) {
  const money = (value) =>
    value == null ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: 0 })

  return (
    <Section
      title="Money"
      hint={`${funnel.priced} of ${entered} ${
        entered === 1 ? 'lead has' : 'leads have'
      } a value on them. Nothing else is counted here.`}
    >
      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-2xl border border-edge bg-edge shadow-card">
        <Stat label="Won" value={money(funnel.revenue)} />
        <Stat label="Still open" value={money(funnel.open_value)} hint="not lost yet" />
        <Stat label="Average won deal" value={money(funnel.average_deal)} />
      </div>
    </Section>
  )
}

/**
 * Bars rather than a trapezoid.
 *
 * The classic funnel shape looks the part and is hard to read: the eye
 * compares the widths of two sloping edges, and at nine stages on a laptop the
 * bottom three are slivers. Bars off a common left edge compare by length,
 * which is the one thing people read accurately.
 */
function Funnel({ funnel }) {
  const stages = funnel?.stages || []
  const top = stages[0]?.reached || 0

  if (stages.length === 0) {
    return (
      <Section title="Where leads get to" hint="Every column on your board counts as an exit.">
        <p className="rounded-lg bg-panel-2/40 px-3 py-2.5 text-2xs text-dim">
          Your board has no stages a lead can progress through — every column is
          marked as lost or unqualified. Mark at least one as in progress and
          this fills in.
        </p>
      </Section>
    )
  }

  return (
    <Section
      title="Where leads get to"
      hint="Everyone who reached each stage, including those who went further."
    >
      {top === 0 ? (
        <p className="rounded-lg bg-panel-2/40 px-3 py-2.5 text-2xs text-dim">
          No leads arrived in this window. Try a wider one.
        </p>
      ) : (
        <div className="space-y-2">
          {stages.map((stage) => (
            <div key={stage.key}>
              <div className="flex items-baseline gap-2">
                <span className="min-w-0 flex-1 truncate text-2xs text-ink">
                  {stage.label}
                </span>
                <span className="font-mono text-2xs tabular-nums text-ink">
                  {stage.reached}
                </span>
                <span className="w-9 shrink-0 text-right font-mono text-2xs tabular-nums text-faint">
                  {percent(top ? stage.reached / top : null)}
                </span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-panel-2">
                <div
                  className="h-full rounded-full bg-accent transition-all"
                  style={{ width: `${top ? (stage.reached / top) * 100 : 0}%` }}
                />
              </div>
              {stage.dropped > 0 && (
                <p className="mt-1 flex items-center gap-1 text-2xs text-faint">
                  <TrendingDown size={10} className="shrink-0" />
                  {stage.dropped} stopped before this
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {funnel?.exits?.some((exit) => exit.count > 0) && (
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-edge pt-2.5">
          {funnel.exits
            .filter((exit) => exit.count > 0)
            .map((exit) => (
              <span key={exit.key} className="text-2xs text-dim">
                <span className="font-mono tabular-nums text-crit">{exit.count}</span>{' '}
                {exit.label.toLowerCase()}
              </span>
            ))}
          <span className="text-2xs text-faint">
            — counted separately, because stopping is not a step further along
          </span>
        </div>
      )}
    </Section>
  )
}

/**
 * Stacked bars, drawn straight into an SVG.
 *
 * `preserveAspectRatio="none"` lets one unit per bucket stretch to whatever
 * width the panel has, which is exactly what is wanted for rectangles and
 * exactly wrong for text — so every label lives in HTML underneath rather than
 * inside the SVG, where it would be squashed along with the bars.
 */
function Traffic({ traffic }) {
  const points = traffic?.points || []
  const totals = traffic?.totals || {}
  const grain = traffic?.grain || 'day'

  if (points.length === 0) {
    return (
      <Section title="Messages" hint="Nothing has come in or gone out yet.">
        <p className="rounded-lg bg-panel-2/40 px-3 py-2.5 text-2xs text-dim">
          Nothing to chart yet.
        </p>
      </Section>
    )
  }

  const tallest = Math.max(1, ...points.map((p) => p.inbound + p.outbound))
  const mostLeads = Math.max(1, ...points.map((p) => p.new_contacts))
  const ticks = axisTicks(points)

  return (
    <Section
      title="Messages"
      hint={`${totals.inbound ?? 0} in, ${totals.outbound ?? 0} out, ${
        totals.new_contacts ?? 0
      } new ${totals.new_contacts === 1 ? 'lead' : 'leads'}.`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pb-1.5">
        <Key className="bg-customer" label="From customers" />
        <Key className="bg-accent" label="From you" />
        <Key className="bg-warn" label="New leads" />
      </div>

      <svg
        viewBox={`0 0 ${points.length} 100`}
        preserveAspectRatio="none"
        className="h-28 w-full sm:h-36"
        role="img"
        aria-label="Messages over time"
      >
        {points.map((point, index) => {
          const inbound = (point.inbound / tallest) * 100
          const outbound = (point.outbound / tallest) * 100
          return (
            <g key={point.bucket}>
              <title>
                {`${bucketLabel(point.bucket, grain)} — ${point.inbound} in, ${
                  point.outbound
                } out`}
              </title>
              <rect
                x={index + 0.12}
                width={0.76}
                y={100 - inbound}
                height={inbound}
                className="fill-customer"
              />
              <rect
                x={index + 0.12}
                width={0.76}
                y={100 - inbound - outbound}
                height={outbound}
                className="fill-accent"
              />
            </g>
          )
        })}
      </svg>

      {totals.new_contacts > 0 && (
        <svg
          viewBox={`0 0 ${points.length} 100`}
          preserveAspectRatio="none"
          className="mt-2 h-8 w-full"
          role="img"
          aria-label="New leads over time"
        >
          {points.map((point, index) => {
            const height = (point.new_contacts / mostLeads) * 100
            return (
              <g key={point.bucket}>
                <title>
                  {`${bucketLabel(point.bucket, grain)} — ${point.new_contacts} new`}
                </title>
                <rect
                  x={index + 0.12}
                  width={0.76}
                  y={100 - height}
                  height={height}
                  className="fill-warn"
                />
              </g>
            )
          })}
        </svg>
      )}

      <div className="mt-1 flex justify-between text-2xs text-faint">
        {ticks.map((tick) => (
          <span key={tick}>{bucketLabel(tick, grain)}</span>
        ))}
      </div>
    </Section>
  )
}

/** First, middle and last, so a dense axis stays readable at phone width. */
function axisTicks(points) {
  if (points.length === 0) return []
  if (points.length <= 2) return points.map((p) => p.bucket)
  const middle = points[Math.floor(points.length / 2)]
  return [points[0], middle, points[points.length - 1]].map((p) => p.bucket)
}

function Key({ className, label }) {
  return (
    <span className="flex items-center gap-1.5 text-2xs text-dim">
      <span className={`h-2 w-2 rounded-sm ${className}`} />
      {label}
    </span>
  )
}

function Replies({ replies }) {
  const by = replies?.by || { agent: 0, operator: 0 }
  const total = by.agent + by.operator

  return (
    <Section
      title="How fast people get answered"
      hint="Measured per conversation turn, not per message — somebody typing three lines in a row waited once."
    >
      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-2xl border border-edge bg-edge shadow-card">
        <Stat label="Typical" value={duration(replies?.median_seconds)} />
        <Stat label="Slowest 10%" value={duration(replies?.p90_seconds)} />
        <Stat label="First reply" value={duration(replies?.first_median_seconds)} />
      </div>

      {total > 0 ? (
        <div className="mt-2.5">
          <div className="flex items-baseline justify-between text-2xs">
            <span className="text-dim">Who answered</span>
            <span className="font-mono tabular-nums text-accent">
              {percent(replies?.agent_share)} the agent
            </span>
          </div>
          <div className="mt-1 flex h-1.5 overflow-hidden rounded-full bg-panel-2">
            <div
              className="h-full bg-accent"
              style={{ width: `${(by.agent / total) * 100}%` }}
            />
            <div
              className="h-full bg-customer"
              style={{ width: `${(by.operator / total) * 100}%` }}
            />
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
            <Key className="bg-accent" label={`${by.agent} by the agent`} />
            <Key className="bg-customer" label={`${by.operator} by you`} />
          </div>
        </div>
      ) : (
        <p className="mt-2.5 flex items-start gap-2 rounded-lg bg-panel-2/40 px-3 py-2.5 text-2xs text-dim">
          <MessageSquareReply size={12} className="mt-0.5 shrink-0" />
          Nobody has been answered in this window yet. Messages you send without
          being asked — a follow-up nudge, say — are not counted here, because
          nobody was waiting on them.
        </p>
      )}
    </Section>
  )
}

function Sources({ sources }) {
  const most = Math.max(1, ...sources.map((row) => row.count))
  return (
    <Section title="Where leads came from" hint="Only the ones with a source recorded.">
      <div className="space-y-1.5">
        {sources.map((row) => (
          <div key={row.source} className="flex items-center gap-2">
            <span className="w-28 shrink-0 truncate text-2xs text-ink">{row.source}</span>
            <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-panel-2">
              <div
                className="h-full rounded-full bg-customer"
                style={{ width: `${(row.count / most) * 100}%` }}
              />
            </div>
            <span className="w-6 shrink-0 text-right font-mono text-2xs tabular-nums text-dim">
              {row.count}
            </span>
          </div>
        ))}
      </div>
    </Section>
  )
}

/**
 * What these numbers cannot tell you, said out loud.
 *
 * Stage history only starts when a lead first moves after this shipped. A
 * funnel built on that alone would understate every conversation that came
 * before, so the shape above reads current stages too — but the honest thing
 * is to say where the record begins rather than let the chart imply it goes
 * back forever.
 */
function Footnote({ data }) {
  const tracked = data?.movement?.tracked_from
  return (
    <p className="border-t border-edge pt-3 text-2xs leading-relaxed text-faint">
      Days start and end in {data?.timezone || 'UTC'}.{' '}
      {tracked
        ? `Stage moves have been recorded since ${new Date(tracked).toLocaleDateString()} — ${
            data.movement.stage_changes
          } in this window. Leads from before then still count where they stand, but the route they took is not known.`
        : 'No stage moves recorded yet — leads count where they stand now, and the route each one takes will be recorded from the next move onwards.'}
      {data?.traffic?.truncated &&
        ' There were more messages in this window than one chart can hold, so the totals are capped.'}
    </p>
  )
}

function Section({ title, hint, children }) {
  return (
    <section className="rounded-2xl border border-edge bg-panel p-4 shadow-card sm:p-5">
      <h4 className="text-sm font-semibold text-ink">{title}</h4>
      {hint && <p className="mb-3 mt-0.5 text-xs leading-relaxed text-dim">{hint}</p>}
      {!hint && <div className="mb-3" />}
      {children}
    </section>
  )
}
