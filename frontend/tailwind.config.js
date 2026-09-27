/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'rgb(var(--bg) / <alpha-value>)',
        panel: 'rgb(var(--panel) / <alpha-value>)',
        'panel-2': 'rgb(var(--panel-2) / <alpha-value>)',
        edge: 'rgb(var(--edge) / <alpha-value>)',
        'edge-hi': 'rgb(var(--edge-hi) / <alpha-value>)',
        ink: 'rgb(var(--ink) / <alpha-value>)',
        dim: 'rgb(var(--ink-dim) / <alpha-value>)',
        faint: 'rgb(var(--ink-faint) / <alpha-value>)',
        platinum: 'rgb(var(--platinum) / <alpha-value>)',
        'platinum-dim': 'rgb(var(--platinum-dim) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        'accent-deep': 'rgb(var(--accent-deep) / <alpha-value>)',
        customer: 'rgb(var(--customer) / <alpha-value>)',
        ok: 'rgb(var(--ok) / <alpha-value>)',
        warn: 'rgb(var(--warn) / <alpha-value>)',
        crit: 'rgb(var(--crit) / <alpha-value>)',
        'on-accent': 'rgb(var(--on-accent) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', '"Segoe UI"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      // Tailwind's opacity scale has no 8 or 12, and a modifier it does not
      // recognise generates no rule at all rather than an error - so the
      // panels asking for bg-accent/12 were rendering with no fill.
      opacity: {
        8: '0.08',
        12: '0.12',
      },
      fontSize: {
        '2xs': ['0.75rem', { lineHeight: '1.125rem' }],
        xs: ['0.8125rem', { lineHeight: '1.25rem' }],
      },
      boxShadow: {
        lift: 'var(--shadow-lift)',
        card: 'var(--shadow-card)',
        rim: 'var(--shadow-card)',
      },
    },
  },
  plugins: [],
}
