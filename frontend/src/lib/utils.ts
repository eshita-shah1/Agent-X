import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatINR(
  value: number,
  options?: {
    showSign?: boolean;
    decimals?: number;
    symbol?: string;
    numberFormat?: 'Indian' | 'International';
  }
): string {
  const {
    showSign = false,
    decimals = 0,
    symbol = '₹',
    numberFormat = 'Indian',
  } = options ?? {};

  const locale = numberFormat === 'Indian' ? 'en-IN' : 'en-US';
  const absFormatted = new Intl.NumberFormat(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(Math.abs(value));

  if (value < 0) {
    return `−${symbol}${absFormatted}`;
  }
  if (showSign && value > 0) {
    return `+${symbol}${absFormatted}`;
  }
  return `${symbol}${absFormatted}`;
}

export function formatPrice(
  value: number,
  decimals = 2,
  numberFormat: 'Indian' | 'International' = 'Indian'
): string {
  const locale = numberFormat === 'Indian' ? 'en-IN' : 'en-US';
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

export function formatPercent(
  value: number,
  options?: { showSign?: boolean; decimals?: number }
): string {
  const { showSign = false, decimals = 2 } = options ?? {};
  const abs = Math.abs(value).toFixed(decimals);
  if (value < 0) return `−${abs}%`;
  if (showSign && value > 0) return `+${abs}%`;
  return `${abs}%`;
}

export function formatCompactLakhs(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 ? '−' : '';
  if (abs >= 100000) {
    return `${sign}₹${(abs / 100000).toFixed(1)}L`;
  }
  if (abs >= 1000) {
    return `${sign}₹${(abs / 1000).toFixed(0)}k`;
  }
  return `${sign}₹${abs}`;
}
