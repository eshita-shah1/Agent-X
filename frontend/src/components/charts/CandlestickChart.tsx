import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts';
import { ChevronDown, LineChart as LineChartIcon, Maximize2, Minimize2 } from 'lucide-react';
import { Card } from '@/components/ui/card';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { TimeframeSelector } from '@/components/shared/TimeframeSelector';
import { computeEMA, computeSMA } from '@/lib/marketData';
import { cn, formatPrice } from '@/lib/utils';
import { useAppStore } from '@/store/useAppStore';
import { useCandleSeriesQuery, useInstrumentsQuery } from '@/hooks/useTradingQueries';
import type { CandleBar, ChartTimeframe } from '@/types/trading';

const TIMEFRAMES: ChartTimeframe[] = ['1m', '5m', '15m', '1H', '1D'];

interface CandlestickChartProps {
  height?: number;
  showInstrumentDropdown?: boolean;
  subtitleTag?: string;
  className?: string;
}

function formatVolumeShort(vol: number): string {
  if (vol >= 1_000_000_000) return `${(vol / 1_000_000_000).toFixed(2)}B`;
  if (vol >= 1_000_000) return `${(vol / 1_000_000).toFixed(2)}M`;
  if (vol >= 1_000) return `${(vol / 1_000).toFixed(1)}K`;
  return String(vol);
}

export function CandlestickChart({
  height = 360,
  showInstrumentDropdown = true,
  subtitleTag = 'Historical simulation',
  className,
}: CandlestickChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);

  const selectedSymbol = useAppStore((s) => s.selectedInstrumentSymbol);
  const setSelectedInstrument = useAppStore((s) => s.setSelectedInstrument);
  const timeframe = useAppStore((s) => s.chartTimeframe);
  const setTimeframe = useAppStore((s) => s.setChartTimeframe);
  const numberFormat = useAppStore((s) => s.settings.regional.numberFormat);
  const currency = useAppStore((s) => s.settings.regional.currency);

  const { data: instruments = [] } = useInstrumentsQuery();
  const { data } = useCandleSeriesQuery(selectedSymbol, timeframe);

  const [showVolume, setShowVolume] = useState(true);
  const [showSma20, setShowSma20] = useState(false);
  const [showEma50, setShowEma50] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [hoveredBar, setHoveredBar] = useState<CandleBar | null>(null);

  const instrument = data?.instrument;
  const bars = data?.bars ?? [];

  const activeBar = hoveredBar ?? bars[bars.length - 1] ?? null;

  const smaData = useMemo(() => (showSma20 ? computeSMA(bars, 20) : []), [bars, showSma20]);
  const emaData = useMemo(() => (showEma50 ? computeEMA(bars, 50) : []), [bars, showEma50]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || bars.length === 0) return;

    const chart = createChart(el, {
      width: el.clientWidth,
      height: expanded ? Math.max(480, window.innerHeight - 240) : height,
      layout: {
        background: { type: ColorType.Solid, color: '#FFFFFF' },
        textColor: '#71717A',
        fontFamily: 'Inter, -apple-system, BlinkMacSystemFont, sans-serif',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: '#F4F4F5' },
        horzLines: { color: '#F4F4F5' },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: {
          color: '#CBD5E1',
          width: 1,
          style: 2,
          labelBackgroundColor: '#52647A',
        },
        horzLine: {
          color: '#CBD5E1',
          width: 1,
          style: 2,
          labelBackgroundColor: '#52647A',
        },
      },
      rightPriceScale: {
        borderColor: '#F4F4F5',
        scaleMargins: {
          top: 0.1,
          bottom: showVolume ? 0.24 : 0.08,
        },
      },
      timeScale: {
        borderColor: '#E4E4E7',
        timeVisible: timeframe !== '1D',
        secondsVisible: false,
      },
    });

    chartRef.current = chart;

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#16A34A',
      downColor: '#DC2626',
      borderVisible: false,
      wickUpColor: '#16A34A',
      wickDownColor: '#DC2626',
    });

    candleSeries.setData(
      bars.map((b) => ({
        time: b.time as UTCTimestamp,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      }))
    );

    let volumeSeries: ISeriesApi<'Histogram'> | null = null;
    if (showVolume) {
      volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
      });
      chart.priceScale('volume').applyOptions({
        scaleMargins: {
          top: 0.78,
          bottom: 0,
        },
      });
      volumeSeries.setData(
        bars.map((b) => ({
          time: b.time as UTCTimestamp,
          value: b.volume,
          color:
            b.close >= b.open
              ? 'rgba(22, 163, 74, 0.28)'
              : 'rgba(220, 38, 38, 0.26)',
        }))
      );
    }

    if (showSma20 && smaData.length > 0) {
      const smaSeries = chart.addLineSeries({
        color: '#52647A',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      smaSeries.setData(
        smaData.map((d) => ({ time: d.time as UTCTimestamp, value: d.value }))
      );
    }

    if (showEma50 && emaData.length > 0) {
      const emaSeries = chart.addLineSeries({
        color: '#D97706',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      emaSeries.setData(
        emaData.map((d) => ({ time: d.time as UTCTimestamp, value: d.value }))
      );
    }

    chart.timeScale().fitContent();

    chart.subscribeCrosshairMove((param) => {
      if (!param.time) {
        setHoveredBar(null);
        return;
      }
      const ts = Number(param.time);
      const found = bars.find((b) => b.time === ts);
      setHoveredBar(found ?? null);
    });

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.contentRect.width > 0) {
          chart.applyOptions({
            width: entry.contentRect.width,
            height: expanded ? Math.max(480, window.innerHeight - 240) : height,
          });
        }
      }
    });
    observer.observe(el);

    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [bars, timeframe, height, expanded, showVolume, showSma20, showEma50, smaData, emaData]);

  useEffect(() => {
    if (!expanded) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setExpanded(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [expanded]);

  if (!instrument) {
    return (
      <Card className="p-5">
        <div className="h-[360px] animate-pulse rounded-lg bg-[#F9FAFB]" />
      </Card>
    );
  }

  const isPositive = instrument.change >= 0;
  const barPositive = activeBar ? activeBar.close >= activeBar.open : isPositive;

  return (
    <>
      {expanded && (
        <div
          className="fixed inset-0 z-40 bg-zinc-950/35 backdrop-blur-[1px]"
          onClick={() => setExpanded(false)}
        />
      )}
      <Card
        className={cn(
          'overflow-hidden',
          expanded && 'fixed inset-4 z-50 flex flex-col shadow-2xl',
          className
        )}
      >
      {/* Card Top Instrument Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-[#F4F4F5] px-5 py-3.5">
        <div className="flex flex-wrap items-center gap-2.5">
          {showInstrumentDropdown ? (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button
                  type="button"
                  className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-0.5 text-[15px] font-semibold text-[#18181B] transition-colors hover:bg-[#F4F4F5]"
                >
                  {instrument.symbol}
                  <ChevronDown className="h-3.5 w-3.5 text-[#71717A]" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="max-h-72 w-56 overflow-y-auto">
                <DropdownMenuLabel>Indices &amp; Stocks</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {instruments.map((item) => (
                  <DropdownMenuCheckboxItem
                    key={item.symbol}
                    checked={item.symbol === instrument.symbol}
                    onCheckedChange={() => setSelectedInstrument(item.symbol)}
                  >
                    <span className="flex w-full items-center justify-between gap-2">
                      <span className="font-medium">{item.symbol}</span>
                      <span className="text-[11px] text-[#71717A] tabular-nums">
                        {formatPrice(item.lastPrice, 2, numberFormat)}
                      </span>
                    </span>
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          ) : (
            <span className="text-[15px] font-semibold text-[#18181B]">
              {instrument.symbol}
            </span>
          )}

          <span className="text-[12px] text-[#71717A]">
            {instrument.name} · {instrument.exchange}
          </span>
          <span className="rounded bg-[#F4F4F5] px-1.5 py-0.5 text-[11px] font-medium text-[#52525B]">
            {timeframe}
          </span>
        </div>

        <div className="flex items-baseline gap-2.5 tabular-nums">
          <span className="text-[20px] font-semibold tracking-tight text-[#18181B]">
            {formatPrice(instrument.lastPrice, 2, numberFormat)}
          </span>
          <span
            className={cn(
              'text-[12.5px] font-medium',
              isPositive ? 'text-[#16A34A]' : 'text-[#DC2626]'
            )}
          >
            {isPositive ? '+' : '−'}
            {formatPrice(Math.abs(instrument.change), 2, numberFormat)} (
            {isPositive ? '+' : '−'}
            {Math.abs(instrument.changePercent).toFixed(2)}%)
          </span>
        </div>
      </div>

      {/* Timeframe & Indicators Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[#F4F4F5] px-5 py-2.5">
        <div className="flex items-center gap-2">
          <TimeframeSelector
            options={TIMEFRAMES}
            value={timeframe}
            onChange={setTimeframe}
          />
          <span className="h-4 w-px bg-[#E4E4E7]" />

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[12px] font-medium text-[#52525B] transition-colors hover:bg-[#F4F4F5] hover:text-[#18181B]"
              >
                <LineChartIcon className="h-3.5 w-3.5 text-[#71717A]" />
                Indicators
                <ChevronDown className="h-3 w-3 text-[#71717A]" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-48">
              <DropdownMenuLabel>Overlays</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuCheckboxItem
                checked={showVolume}
                onCheckedChange={(v) => setShowVolume(Boolean(v))}
              >
                Volume Histogram
              </DropdownMenuCheckboxItem>
              <DropdownMenuCheckboxItem
                checked={showSma20}
                onCheckedChange={(v) => setShowSma20(Boolean(v))}
              >
                SMA (20)
              </DropdownMenuCheckboxItem>
              <DropdownMenuCheckboxItem
                checked={showEma50}
                onCheckedChange={(v) => setShowEma50(Boolean(v))}
              >
                EMA (50)
              </DropdownMenuCheckboxItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        <div className="flex items-center gap-3">
          <span className="text-[11.5px] text-[#71717A]">{subtitleTag}</span>
          <button
            type="button"
            onClick={() => setExpanded((prev) => !prev)}
            className="rounded-md p-1 text-[#71717A] transition-colors hover:bg-[#F4F4F5] hover:text-[#18181B]"
            aria-label={expanded ? 'Exit fullscreen chart' : 'Expand chart'}
            title={expanded ? 'Exit fullscreen' : 'Expand chart'}
          >
            {expanded ? (
              <Minimize2 className="h-3.5 w-3.5" />
            ) : (
              <Maximize2 className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </div>

      {/* Lightweight Charts Canvas + OHLCV Overlay */}
      <div className={cn('relative w-full px-3 pt-2', expanded && 'flex-1')}>
        {activeBar && (
          <div className="pointer-events-none absolute left-6 top-3 z-10 flex flex-wrap items-center gap-x-4 gap-y-1 rounded bg-white/90 px-1.5 py-0.5 text-[11.5px] tabular-nums backdrop-blur-[2px]">
            <span>
              <span className="text-[#71717A]">O </span>
              <span className="font-medium text-[#18181B]">
                {formatPrice(activeBar.open, 2, numberFormat)}
              </span>
            </span>
            <span>
              <span className="text-[#71717A]">H </span>
              <span className="font-medium text-[#18181B]">
                {formatPrice(activeBar.high, 2, numberFormat)}
              </span>
            </span>
            <span>
              <span className="text-[#71717A]">L </span>
              <span className="font-medium text-[#18181B]">
                {formatPrice(activeBar.low, 2, numberFormat)}
              </span>
            </span>
            <span>
              <span className="text-[#71717A]">C </span>
              <span
                className={cn(
                  'font-medium',
                  barPositive ? 'text-[#16A34A]' : 'text-[#DC2626]'
                )}
              >
                {formatPrice(activeBar.close, 2, numberFormat)}
              </span>
            </span>
            <span>
              <span className="text-[#71717A]">Vol </span>
              <span className="font-medium text-[#18181B]">
                {hoveredBar ? formatVolumeShort(activeBar.volume) : instrument.volumeLabel}
              </span>
            </span>
          </div>
        )}

        {showVolume && (
          <span className="pointer-events-none absolute bottom-14 left-6 z-10 text-[10.5px] font-medium text-[#71717A]">
            Volume
          </span>
        )}

        <div ref={containerRef} className="w-full" />
      </div>

      {/* Bottom Exchange / Currency Strip */}
      <div className="flex items-center justify-between border-t border-[#F4F4F5] px-5 py-2 text-[11px] text-[#71717A]">
        <span>{instrument.exchange}</span>
        <span>{currency}</span>
      </div>
    </Card>
    </>
  );
}
