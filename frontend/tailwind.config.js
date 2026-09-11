/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        panel: 'var(--panel)',
        'panel-2': 'var(--panel-2)',
        edge: 'var(--edge)',
        'edge-hi': 'var(--edge-hi)',
        ink: 'var(--ink)',
        dim: 'var(--ink-dim)',
        faint: 'var(--ink-faint)',
        platinum: 'var(--platinum)',
        'platinum-dim': 'var(--platinum-dim)',
        accent: 'var(--accent)',
        'accent-deep': 'var(--accent-deep)',
        customer: 'var(--customer)',
        warn: 'var(--warn)',
        crit: 'var(--crit)',
      },
      fontFamily: {
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
      boxShadow: {
        lift: '0 10px 30px -12px rgba(0, 0, 0, 0.85)',
        // A raised surface on black is described by light along its top edge,
        // not by a shadow underneath it.
        rim: 'inset 0 1px 0 rgba(255, 255, 255, 0.06)',
      },
    },
  },
  plugins: [],
}
