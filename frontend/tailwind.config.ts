import type { Config } from 'tailwindcss';
import tailwindcssAnimate from 'tailwindcss-animate';

const config: Config = {
  darkMode: ['class'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', '-apple-system', 'BlinkMacSystemFont', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        border: '#E4E4E7',
        input: '#E4E4E7',
        ring: '#52647A',
        background: '#F9FAFB',
        foreground: '#18181B',
        primary: {
          DEFAULT: '#52647A',
          foreground: '#FFFFFF',
          hover: '#435367',
        },
        secondary: {
          DEFAULT: '#F4F4F5',
          foreground: '#18181B',
        },
        muted: {
          DEFAULT: '#F4F4F5',
          foreground: '#71717A',
        },
        accent: {
          DEFAULT: '#F1F5F9',
          foreground: '#18181B',
        },
        card: {
          DEFAULT: '#FFFFFF',
          foreground: '#18181B',
        },
        popover: {
          DEFAULT: '#FFFFFF',
          foreground: '#18181B',
        },
        positive: {
          DEFAULT: '#16A34A',
          soft: '#ECFDF5',
          text: '#15803D',
        },
        negative: {
          DEFAULT: '#DC2626',
          soft: '#FEF2F2',
          text: '#DC2626',
        },
        warning: {
          DEFAULT: '#D97706',
          soft: '#FFFBEB',
        },
      },
      borderRadius: {
        lg: '10px',
        md: '8px',
        sm: '6px',
      },
      boxShadow: {
        card: '0 1px 2px 0 rgba(24, 24, 27, 0.03)',
      },
    },
  },
  plugins: [tailwindcssAnimate],
};

export default config;
