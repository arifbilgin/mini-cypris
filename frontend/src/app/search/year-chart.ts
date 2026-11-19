import { DecimalPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { YearCount } from '../api/papers.models';

interface Bar {
  year: number;
  count: number;
  x: number;
  y: number;
  w: number;
  h: number;
  path: string;
  hitX: number;
  hitW: number;
}

const HEIGHT = 200;
const MARGIN = { top: 22, right: 8, bottom: 24, left: 44 };
const MAX_BAR = 24;

/**
 * Papers per publication year, as an inline SVG column chart. Every year from
 * the first to the last is drawn, so gaps show as gaps. Rendered at the real
 * pixel width (ResizeObserver), so text never scales with the screen.
 */
@Component({
  selector: 'app-year-chart',
  imports: [DecimalPipe],
  templateUrl: './year-chart.html',
  styleUrl: './year-chart.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class YearChart {
  readonly years = input.required<YearCount[]>();

  protected readonly height = HEIGHT;
  protected readonly margin = MARGIN;
  protected readonly width = signal(640);
  protected readonly hovered = signal<Bar | null>(null);

  /** Zero-filled, oldest first. */
  protected readonly series = computed(() => {
    const given = this.years();
    if (!given.length) return [];
    const counts = new Map(given.map((y) => [y.year, y.count]));
    const first = Math.min(...counts.keys());
    const last = Math.max(...counts.keys());
    return Array.from({ length: last - first + 1 }, (_, i) => ({
      year: first + i,
      count: counts.get(first + i) ?? 0,
    }));
  });

  protected readonly peak = computed(() =>
    this.series().reduce<YearCount | null>((top, y) => (!top || y.count > top.count ? y : top), null),
  );

  protected readonly yTicks = computed(() => niceTicks(this.peak()?.count ?? 0));

  protected readonly plot = computed(() => {
    const innerW = Math.max(40, this.width() - MARGIN.left - MARGIN.right);
    const innerH = HEIGHT - MARGIN.top - MARGIN.bottom;
    return { innerW, innerH, top: MARGIN.top, bottom: MARGIN.top + innerH };
  });

  protected readonly bars = computed<Bar[]>(() => {
    const series = this.series();
    const ticks = this.yTicks();
    const max = ticks[ticks.length - 1] || 1;
    const { innerW, innerH, bottom } = this.plot();
    const band = innerW / Math.max(1, series.length);
    const gap = Math.min(2, band * 0.25);
    const w = Math.max(1, Math.min(MAX_BAR, band - gap));
    return series.map((s, i) => {
      const h = s.count ? Math.max(1, (s.count / max) * innerH) : 0;
      const x = MARGIN.left + i * band + (band - w) / 2;
      const y = bottom - h;
      return { ...s, x, y, w, h, path: columnPath(x, y, w, h), hitX: MARGIN.left + i * band, hitW: band };
    });
  });

  /** Year labels at least ~44px apart, on round years. */
  protected readonly xTicks = computed(() => {
    const bars = this.bars();
    if (!bars.length) return [];
    const band = this.plot().innerW / bars.length;
    const step = [1, 2, 5, 10, 20, 25, 50, 100].find((s) => s * band >= 44) ?? 200;
    return bars.filter((b) => b.year % step === 0).map((b) => ({ year: b.year, x: b.x + b.w / 2 }));
  });

  protected readonly label = computed(() => {
    const s = this.series();
    const p = this.peak();
    if (!s.length || !p) return 'No papers with a known year';
    return `Papers per year from ${s[0].year} to ${s[s.length - 1].year}. Most papers in ${p.year}: ${p.count}.`;
  });

  protected readonly peakBar = computed(() => this.bars().find((b) => b.year === this.peak()?.year) ?? null);

  constructor() {
    const host = inject(ElementRef<HTMLElement>);
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      const el = host.nativeElement as HTMLElement;
      if (el.clientWidth) this.width.set(el.clientWidth);
      if (typeof ResizeObserver === 'undefined') return;
      const observer = new ResizeObserver(([entry]) => this.width.set(Math.round(entry.contentRect.width)));
      observer.observe(el);
      destroyRef.onDestroy(() => observer.disconnect());
    });
  }

  protected yOf(value: number): number {
    const ticks = this.yTicks();
    const max = ticks[ticks.length - 1] || 1;
    const { innerH, bottom } = this.plot();
    return bottom - (value / max) * innerH;
  }

  protected readonly compact = compact;

  protected tooltipLeft(bar: Bar): number {
    return Math.min(Math.max(bar.x + bar.w / 2, 60), this.width() - 60);
  }
}

/** 0 plus 3–5 round steps (1, 2, 5 × 10^n) reaching at least max. */
export function niceTicks(max: number): number[] {
  if (max <= 0) return [0, 1];
  const raw = max / 4;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, ([1, 2, 5, 10].find((m) => m * pow >= raw) ?? 10) * pow); // counts are whole
  const ticks = [];
  for (let t = 0; t < max + step; t += step) ticks.push(t);
  return ticks;
}

/** A column with 4px rounded top corners and a square foot on the baseline. */
function columnPath(x: number, y: number, w: number, h: number): string {
  if (h <= 0) return '';
  const r = Math.min(4, w / 2, h);
  return [
    `M${x},${y + h}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + w - r}`,
    `Q${x + w},${y} ${x + w},${y + r}`,
    `V${y + h}`,
    'Z',
  ].join(' ');
}

export function compact(n: number): string {
  return n >= 10_000 ? `${Math.round(n / 1000)}K` : n.toLocaleString('en-US');
}
