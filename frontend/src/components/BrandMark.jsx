/**
 * The PingPulse mark and wordmark, in one place.
 *
 * The artwork is brushed silver cut out of its backdrop, so it sits on a small
 * dark tile: without one the silver disappears against the light theme.
 */
export default function BrandMark({ size = 34, strapline = true, className = '' }) {
  return (
    <div className={`flex items-center gap-2.5 ${className}`}>
      <span
        className="grid shrink-0 place-items-center rounded-[28%] bg-[#0b0d11] ring-1 ring-inset ring-white/10"
        style={{ width: size, height: size }}
      >
        <img
          src="./logo-256.png"
          alt=""
          className="select-none"
          style={{ width: size * 0.78, height: size * 0.78 }}
          draggable="false"
        />
      </span>
      <div className="min-w-0">
        <h1 className="text-[15px] font-bold leading-none tracking-tight text-ink">PingPulse</h1>
        {strapline && (
          <p className="mt-1 truncate text-[11px] font-medium text-faint">WhatsApp sales agent</p>
        )}
      </div>
    </div>
  )
}
