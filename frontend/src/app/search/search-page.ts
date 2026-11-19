import { DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  AbstractControl,
  FormControl,
  FormGroup,
  ReactiveFormsModule,
  ValidationErrors,
  Validators,
} from '@angular/forms';
import { ActivatedRoute, ParamMap, Router } from '@angular/router';
import { Observable, Subject, catchError, map, of, startWith, switchMap, takeWhile, timer } from 'rxjs';
import { AggregateResponse, ApiError, Load, Paper, SearchResponse } from '../api/papers.models';
import { DEFAULT_LIMIT, MAX_LIMIT, MAX_QUERY_LENGTH, PapersService } from '../api/papers.service';
import { YearChart } from './year-chart';

interface SearchRequest {
  q: string;
  limit: number;
}

const notBlank = (c: AbstractControl): ValidationErrors | null =>
  typeof c.value === 'string' && !c.value.trim() ? { required: true } : null;

const wholeNumber = (c: AbstractControl): ValidationErrors | null =>
  c.value === null || c.value === '' || Number.isInteger(Number(c.value)) ? null : { whole: true };

/**
 * The whole app: a boolean query plus a result count, the matching papers, and
 * a chart of ALL matches per year. The query lives in the URL (?q=&limit=), so
 * searches can be shared, bookmarked and walked with Back/Forward.
 */
@Component({
  selector: 'app-search-page',
  imports: [ReactiveFormsModule, DecimalPipe, YearChart],
  templateUrl: './search-page.html',
  styleUrl: './search-page.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SearchPage {
  private readonly api = inject(PapersService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  protected readonly maxLimit = MAX_LIMIT;
  protected readonly examples = [
    'drone AND (package OR delivery)',
    '"machine learning" AND NOT survey',
    'title:(battery OR "solid state") AND lithium',
  ];

  protected readonly form = new FormGroup({
    q: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, notBlank, Validators.maxLength(MAX_QUERY_LENGTH)],
    }),
    limit: new FormControl<number | null>(DEFAULT_LIMIT, [
      Validators.required,
      Validators.min(1),
      Validators.max(MAX_LIMIT),
      wholeNumber,
    ]),
  });

  protected readonly results = signal<Load<SearchResponse>>({ state: 'idle' });
  protected readonly chart = signal<Load<AggregateResponse>>({ state: 'idle' });
  /** Seconds left before a rate-limited search can be retried. */
  protected readonly retryIn = signal(0);
  protected readonly expanded = signal<ReadonlySet<number>>(new Set());

  /** A server-side complaint about the query, shown under the query field. */
  protected readonly queryError = computed(() => {
    const r = this.results();
    return r.state === 'error' && ['invalid_query', 'core_rejected', 'bad_request'].includes(r.error.kind)
      ? r.error.message
      : null;
  });

  private current: SearchRequest | null = null;
  private chartQuery: string | null = null;
  private readonly searches = new Subject<SearchRequest>();
  private readonly charts = new Subject<string>();
  private readonly countdowns = new Subject<number>();

  constructor() {
    this.searches
      .pipe(
        switchMap((req) => load(this.api.search(req.q, req.limit)).pipe(map((state) => ({ req, state })))),
        takeUntilDestroyed(),
      )
      .subscribe(({ req, state }) => this.onResults(req, state));

    this.charts
      .pipe(
        switchMap((q) => load(this.api.aggregate(q))),
        takeUntilDestroyed(),
      )
      .subscribe((state) => this.chart.set(state));

    this.countdowns
      .pipe(
        switchMap((seconds) => timer(0, 1000).pipe(map((i) => seconds - i), takeWhile((s) => s >= 0))),
        takeUntilDestroyed(),
      )
      .subscribe((s) => this.retryIn.set(s));

    this.route.queryParamMap.pipe(takeUntilDestroyed()).subscribe((params) => this.onUrl(params));
  }

  protected submit(): void {
    this.form.markAllAsTouched();
    if (this.form.invalid) return;
    const req = { q: this.form.controls.q.value.trim(), limit: Number(this.form.controls.limit.value) };
    if (this.current && req.q === this.current.q && req.limit === this.current.limit) {
      this.run(req); // same URL: the router would ignore it, so search again directly
    } else {
      this.router.navigate([], { queryParams: req });
    }
  }

  protected useExample(q: string): void {
    this.form.controls.q.setValue(q);
    this.submit();
  }

  protected retry(): void {
    if (this.current) this.run(this.current);
  }

  protected retryChart(): void {
    if (this.chartQuery) this.charts.next(this.chartQuery);
  }

  protected toggle(paper: Paper): void {
    const next = new Set(this.expanded());
    if (!next.delete(paper.id)) next.add(paper.id);
    this.expanded.set(next);
  }

  protected link(p: Paper): string | null {
    return p.doi ? doiUrl(p.doi) : p.core_url;
  }

  protected doiUrl = doiUrl;

  protected authors(p: Paper): string {
    if (p.authors.length <= 3) return p.authors.join(', ');
    return `${p.authors.slice(0, 3).join(', ')} +${p.authors.length - 3} more`;
  }

  protected isLong(p: Paper): boolean {
    return (p.abstract?.length ?? 0) > 260;
  }

  protected ago(seconds: number): string {
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
    return `${Math.floor(seconds / 86400)} d ago`;
  }

  protected implausible(agg: AggregateResponse): string {
    return Object.keys(agg.implausible_years)
      .filter((k) => k)
      .sort()
      .join(', ');
  }

  // ----- plumbing -------------------------------------------------------------

  private onUrl(params: ParamMap): void {
    const q = (params.get('q') ?? '').trim();
    if (!q) {
      this.current = null;
      this.results.set({ state: 'idle' });
      this.chart.set({ state: 'idle' });
      this.chartQuery = null;
      return;
    }
    const n = Number(params.get('limit') ?? DEFAULT_LIMIT);
    const limit = Number.isInteger(n) && n >= 1 && n <= MAX_LIMIT ? n : DEFAULT_LIMIT;
    this.form.setValue({ q, limit });
    this.run({ q, limit });
  }

  private run(req: SearchRequest): void {
    this.current = req;
    this.expanded.set(new Set());
    if (req.q !== this.chartQuery) {
      this.chartQuery = null;
      this.chart.set({ state: 'idle' });
    }
    this.searches.next(req);
  }

  private onResults(req: SearchRequest, state: Load<SearchResponse>): void {
    this.results.set(state);
    if (state.state === 'error' && state.error.kind === 'rate_limited') {
      this.countdowns.next(state.error.retryAfter ?? 10);
    }
    // The chart is per query: a new count needs no new chart. It is fetched
    // after the search so its total is a cache hit, and skipped for zero matches.
    const chartFailed = this.chart().state === 'error';
    if (state.state === 'ok' && state.data.total_hits > 0 && (req.q !== this.chartQuery || chartFailed)) {
      this.chartQuery = req.q;
      this.charts.next(req.q);
    }
  }
}

function load<T>(source: Observable<T>): Observable<Load<T>> {
  return source.pipe(
    map((data): Load<T> => ({ state: 'ok', data })),
    catchError((error: ApiError) => of<Load<T>>({ state: 'error', error })),
    startWith<Load<T>>({ state: 'loading' }),
  );
}

function doiUrl(doi: string): string {
  return /^https?:\/\//i.test(doi) ? doi : `https://doi.org/${doi.replace(/^doi:\s*/i, '')}`;
}
