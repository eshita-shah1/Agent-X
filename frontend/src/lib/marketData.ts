import type { CandleBar, ChartTimeframe, Instrument } from '@/types/trading';

function seededRandom(seed: number): () => number {
  let s = seed % 2147483647;
  if (s <= 0) s += 2147483646;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

function hashSymbol(symbol: string): number {
  let h = 17;
  for (let i = 0; i < symbol.length; i += 1) {
    h = h * 31 + symbol.charCodeAt(i);
  }
  return Math.abs(h);
}

const TIMEFRAME_SECONDS: Record<ChartTimeframe, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '1H': 3600,
  '1D': 86400,
};

const TIMEFRAME_BARS: Record<ChartTimeframe, number> = {
  '1m': 120,
  '5m': 120,
  '15m': 120,
  '1H': 120,
  '1D': 140,
};

export function generateCandleSeries(
  instrument: Instrument,
  timeframe: ChartTimeframe
): CandleBar[] {
  const count = TIMEFRAME_BARS[timeframe];
  const stepSec = TIMEFRAME_SECONDS[timeframe];
  const rng = seededRandom(hashSymbol(instrument.symbol) + stepSec);

  // End near Nov 11, 2026 so calendar labels Apr..Nov match screenshots for 1D
  const endTs = Math.floor(new Date('2026-11-11T15:30:00+05:30').getTime() / 1000);
  const startTs = endTs - (count - 1) * stepSec;

  const targetClose = instrument.lastPrice;
  const startFactor = instrument.changePercent >= 0 ? 0.85 : 1.07;
  const startPrice = targetClose * startFactor;

  const volatilityMap: Record<ChartTimeframe, number> = {
    '1m': 0.0018,
    '5m': 0.0032,
    '15m': 0.0055,
    '1H': 0.009,
    '1D': 0.014,
  };
  const vol = volatilityMap[timeframe];

  // Build unscaled path then gently drift-adjust so final candle lands cleanly on instrument.lastPrice
  const rawCloses: number[] = [startPrice];
  for (let i = 1; i < count; i += 1) {
    const wave =
      Math.sin(i * 0.22) * vol * 0.65 +
      Math.cos(i * 0.09) * vol * 0.45 +
      (rng() - 0.47) * vol * 1.4;
    rawCloses.push(Math.max(10, rawCloses[i - 1] * (1 + wave)));
  }

  const finalRaw = rawCloses[count - 1];
  const totalRatio = targetClose / finalRaw;

  const bars: CandleBar[] = [];
  let prevClose = startPrice;

  for (let i = 0; i < count; i += 1) {
    const progress = i / (count - 1);
    const scale = 1 + (totalRatio - 1) * Math.pow(progress, 0.85);
    const close = Number((rawCloses[i] * scale).toFixed(2));
    const open = i === 0 ? Number((close * 0.994).toFixed(2)) : prevClose;

    const range = Math.abs(close - open) + close * vol * (0.35 + rng() * 0.65);
    const upperWick = range * (0.2 + rng() * 0.45);
    const lowerWick = range * (0.2 + rng() * 0.45);

    const high = Number((Math.max(open, close) + upperWick).toFixed(2));
    const low = Number(Math.max(1, Math.min(open, close) - lowerWick).toFixed(2));

    const baseVolume = instrument.category === 'INDEX' ? 320_000_000 : 8_500_000;
    const impulse = 0.65 + (Math.abs(close - open) / Math.max(1, close * vol)) * 0.7 + rng() * 0.55;
    const volume = Math.round(baseVolume * impulse);

    bars.push({
      time: startTs + i * stepSec,
      open,
      high,
      low,
      close,
      volume,
    });

    prevClose = close;
  }

  // Ensure exact match with instrument snapshot on latest candle
  const last = bars[bars.length - 1];
  if (last) {
    last.close = instrument.lastPrice;
    last.high = Math.max(last.high, last.open, last.close);
    last.low = Math.min(last.low, last.open, last.close);
  }

  return bars;
}

export function computeSMA(bars: CandleBar[], period: number) {
  const result: { time: number; value: number }[] = [];
  for (let i = period - 1; i < bars.length; i += 1) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j += 1) {
      sum += bars[j].close;
    }
    result.push({
      time: bars[i].time,
      value: Number((sum / period).toFixed(2)),
    });
  }
  return result;
}

export function computeEMA(bars: CandleBar[], period: number) {
  if (bars.length === 0) return [];
  const k = 2 / (period + 1);
  const result: { time: number; value: number }[] = [];
  let ema = bars[0].close;
  for (let i = 0; i < bars.length; i += 1) {
    ema = bars[i].close * k + ema * (1 - k);
    if (i >= period - 1) {
      result.push({
        time: bars[i].time,
        value: Number(ema.toFixed(2)),
      });
    }
  }
  return result;
}
