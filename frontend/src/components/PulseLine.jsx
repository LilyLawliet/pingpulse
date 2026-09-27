import { useEffect, useRef } from 'react'

/**
 * Message throughput as a scrolling trace — the console's heartbeat.
 * Each event pushes a spike into the buffer; the line decays back to the
 * baseline when traffic stops, so a quiet channel reads as quiet.
 */
export default function PulseLine({ beat, width = 132, height = 30 }) {
  const canvasRef = useRef(null)
  const samplesRef = useRef(new Array(70).fill(0))
  const pendingRef = useRef(0)

  useEffect(() => {
    if (beat > 0) pendingRef.current = 1
  }, [beat])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.scale(dpr, dpr)

    let frame
    let tick = 0
    // Read from the theme tokens so the trace matches light and dark alike.
    let accent = '52 211 153'
    let edge = '37 42 51'
    const readColours = () => {
      const css = getComputedStyle(document.documentElement)
      accent = css.getPropertyValue('--accent').trim() || accent
      edge = css.getPropertyValue('--edge').trim() || edge
    }
    readColours()

    const render = () => {
      tick += 1
      if (tick % 60 === 0) readColours()
      if (tick % 3 === 0) {
        const samples = samplesRef.current
        const spike = pendingRef.current
        pendingRef.current = 0
        samples.push(spike ? 1 : Math.max(0, samples[samples.length - 1] * 0.72 - 0.02))
        samples.shift()
      }

      const samples = samplesRef.current
      ctx.clearRect(0, 0, width, height)

      // Baseline
      ctx.strokeStyle = `rgb(${edge})`
      ctx.lineWidth = 1
      ctx.beginPath()
      ctx.moveTo(0, height - 4.5)
      ctx.lineTo(width, height - 4.5)
      ctx.stroke()

      const step = width / (samples.length - 1)
      const yFor = (v) => height - 4.5 - v * (height - 11)

      // Fill under the trace
      ctx.beginPath()
      ctx.moveTo(0, height - 4.5)
      samples.forEach((v, i) => ctx.lineTo(i * step, yFor(v)))
      ctx.lineTo(width, height - 4.5)
      ctx.closePath()
      ctx.fillStyle = `rgb(${accent} / 0.14)`
      ctx.fill()

      // The trace itself
      ctx.beginPath()
      samples.forEach((v, i) => (i === 0 ? ctx.moveTo(0, yFor(v)) : ctx.lineTo(i * step, yFor(v))))
      ctx.strokeStyle = `rgb(${accent})`
      ctx.lineWidth = 1.5
      ctx.lineJoin = 'round'
      ctx.stroke()

      // Emphasised leading endpoint
      const last = samples[samples.length - 1]
      ctx.beginPath()
      ctx.arc(width - 1.5, yFor(last), last > 0.05 ? 2.6 : 1.6, 0, Math.PI * 2)
      ctx.fillStyle = `rgb(${accent})`
      ctx.fill()

      frame = requestAnimationFrame(render)
    }

    frame = requestAnimationFrame(render)
    return () => cancelAnimationFrame(frame)
  }, [width, height])

  return <canvas ref={canvasRef} style={{ width, height }} aria-hidden="true" />
}
