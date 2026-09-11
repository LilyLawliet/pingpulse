/**
 * The PingPulse mark and wordmark, in one place.
 *
 * The artwork is brushed silver, cut out of its original backdrop by
 * scripts/make_logo.py, so it sits on the black ground without a tile behind
 * it. No plate, no ring: a plate would be a box around a mark that already has
 * its own edges, and on black the mark reads better with nothing behind it.
 *
 * The wordmark is filled with the same metal as the mark rather than flat white
 * — the pairing is the whole reason the console went black.
 */
export default function BrandMark({ size = 34, strapline = true, className = '' }) {
  return (
    <div className={`flex items-center gap-2.5 ${className}`}>
      <img
        src="./logo-256.png"
        alt="PingPulse"
        width={size}
        height={size}
        // The mark is a fixed square; without this it stretches when the header
        // wraps on a phone.
        className="shrink-0 select-none"
        style={{ width: size, height: size }}
        draggable="false"
      />
      <div className="min-w-0">
        <h1 className="platinum text-[15px] font-bold leading-none tracking-tight">
          PingPulse
        </h1>
        {strapline && (
          <p className="mt-1 hidden text-[10px] uppercase tracking-[0.16em] text-faint sm:block">
            WhatsApp sales agent
          </p>
        )}
      </div>
    </div>
  )
}
