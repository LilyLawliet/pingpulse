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
        lift: '0 10px 30px -12px rgba(0, 0, 0, 0.7)',
      },
    },
  },
  plugins: [],
}
