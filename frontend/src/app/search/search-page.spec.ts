import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Router, provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { AggregateResponse, Paper, SearchResponse } from '../api/papers.models';
import { SearchPage } from './search-page';

const CACHE = { status: 'miss' as const, fetched_at: 0, age_seconds: 0, error: null };

function paper(id: number, extra: Partial<Paper> = {}): Paper {
  return {
    id,
    source: 'core',
    external_id: String(id),
    title: `Paper ${id}`,
    authors: ['Ada Lovelace', 'Alan Turing', 'Grace Hopper', 'Edsger Dijkstra'],
    year: 2021,
    publisher: 'Test Press',
    doi: '10.1/x',
    pdf_url: 'https://example.org/p.pdf',
    abstract: 'Short abstract.',
    core_url: `https://core.ac.uk/works/${id}`,
    ...extra,
  };
}

function found(results: Paper[], total = 12): SearchResponse {
  return { query: 'drone', limit: 10, total_hits: total, count: results.length, results, cache: CACHE };
}

const YEARS: AggregateResponse = {
  query: 'drone',
  field: 'yearPublished',
  total_hits: 12,
  years: [
    { year: 2020, count: 4 },
    { year: 2022, count: 6 },
  ],
  unknown_year: 2,
  implausible_years: { '9999': 1 },
  cache: CACHE,
};

describe('SearchPage', () => {
  let harness: RouterTestingHarness;
  let http: HttpTestingController;

  async function open(url = '/') {
    TestBed.configureTestingModule({
      providers: [provideRouter([{ path: '', component: SearchPage }]), provideHttpClient(), provideHttpClientTesting()],
    });
    harness = await RouterTestingHarness.create();
    http = TestBed.inject(HttpTestingController);
    await harness.navigateByUrl(url, SearchPage);
  }

  const el = () => harness.routeNativeElement as HTMLElement;
  const text = () => el().textContent?.replace(/\s+/g, ' ') ?? '';
  const searchReq = () => http.expectOne((r) => r.url === '/api/search');
  const chartReq = () => http.expectOne((r) => r.url === '/api/aggregate');
  const noChartReq = () => http.expectNone((r) => r.url === '/api/aggregate');

  function fill(q: string, limit: string | number) {
    const qi = el().querySelector<HTMLInputElement>('#q')!;
    const li = el().querySelector<HTMLInputElement>('#limit')!;
    qi.value = q;
    qi.dispatchEvent(new Event('input'));
    li.value = String(limit);
    li.dispatchEvent(new Event('input'));
  }

  function submit() {
    el().querySelector('form')!.dispatchEvent(new Event('submit'));
    harness.detectChanges();
  }

  function button(label: string): HTMLButtonElement {
    const b = [...el().querySelectorAll('button')].find((x) => x.textContent?.includes(label));
    if (!b) throw new Error(`no button "${label}"`);
    return b;
  }

  afterEach(() => http.verify());

  it('starts blank with examples and makes no request', async () => {
    await open();
    expect(text()).toContain('Try one of these');
    expect(el().querySelector('app-year-chart')).toBeNull();
  });

  it('checks the fields before searching', async () => {
    await open();
    fill('   ', 10);
    submit();
    expect(text()).toContain('Enter a search query.');
    expect(el().querySelector('#q')!.getAttribute('aria-invalid')).toBe('true');

    for (const bad of [0, 101, 2.5, '']) {
      fill('drone', bad);
      submit();
      expect(text()).toContain('Choose a whole number of papers from 1 to 100.');
    }
    http.expectNone((r) => r.url === '/api/search');
  });

  it('searches from the URL, lists the papers, then charts all matches', async () => {
    await open('/?q=drone&limit=5');
    const req = searchReq();
    expect(req.request.params.get('q')).toBe('drone');
    expect(req.request.params.get('limit')).toBe('5');
    harness.detectChanges();
    expect(text()).toContain('Searching CORE');

    req.flush(found([paper(1), paper(2, { doi: null, pdf_url: null, authors: ['Solo'] })]));
    harness.detectChanges();
    expect(text()).toContain('12 papers match');
    expect(text()).toContain('showing the top 2');
    expect(text()).toContain('Ada Lovelace, Alan Turing, Grace Hopper +1 more');

    const cards = el().querySelectorAll('.paper');
    expect(cards.length).toBe(2);
    const first = cards[0] as HTMLElement;
    expect(first.querySelector('h3 a')!.getAttribute('href')).toBe('https://doi.org/10.1/x');
    expect(first.textContent).toContain('2021');
    expect(first.textContent).toContain('PDF');
    const second = cards[1] as HTMLElement;
    expect(second.querySelector('h3 a')!.getAttribute('href')).toBe('https://core.ac.uk/works/2'); // no DOI
    expect(second.textContent).not.toContain('PDF');
    expect(text()).toContain('Counting papers per year');

    const chart = chartReq();
    expect(chart.request.params.get('q')).toBe('drone');
    chart.flush(YEARS);
    harness.detectChanges();
    expect(el().querySelectorAll('app-year-chart .bars path').length).toBe(3); // 2021 is zero-filled
    expect(text()).toContain('2 of 12 papers');
    expect(text()).toContain('dated 9999');
  });

  it('puts a submitted search in the URL', async () => {
    await open();
    fill('drone AND uav', 20);
    submit();
    await harness.fixture.whenStable();
    const req = searchReq();
    expect(req.request.params.get('q')).toBe('drone AND uav');
    expect(req.request.params.get('limit')).toBe('20');
    expect(TestBed.inject(Router).url).toBe('/?q=drone%20AND%20uav&limit=20');
    req.flush(found([], 0));
  });

  it('keeps the chart when only the count changes', async () => {
    await open('/?q=drone&limit=5');
    searchReq().flush(found([paper(1)]));
    chartReq().flush(YEARS);
    await harness.navigateByUrl('/?q=drone&limit=20');
    expect(searchReq().request.params.get('limit')).toBe('20');
    noChartReq();
  });

  it('shows the query check message under the query field', async () => {
    await open('/?q=(drone%20AND');
    const message = "'AND' needs a search term after it (character 8)";
    searchReq().flush({ error: 'invalid_query', message, position: 7 }, { status: 400, statusText: 'Bad Request' });
    harness.detectChanges();
    expect(el().querySelector('#q-error')!.textContent).toContain(message);
    expect(el().querySelector('#q')!.getAttribute('aria-invalid')).toBe('true');
    expect(el().querySelector('.notice')).toBeNull();
    noChartReq();
  });

  it('says so when nothing matches, and skips the chart', async () => {
    await open('/?q=zzzz');
    searchReq().flush(found([], 0));
    harness.detectChanges();
    expect(text()).toContain('No papers match');
    expect(el().querySelector('.chart-card')).toBeNull();
    noChartReq();
  });

  it('counts down a rate limit, then retries', async () => {
    await open('/?q=drone');
    vi.useFakeTimers();
    try {
      searchReq().flush(
        { error: 'core_rate_limited', retry_after: 3, message: 'x' },
        { status: 429, statusText: 'Too Many Requests' },
      );
      vi.advanceTimersByTime(0);
      harness.detectChanges();
      expect(text()).toContain('Rate limited');
      expect(text()).toContain('about 10 requests a minute');
      expect(button('Retry in 3 s').disabled).toBe(true);

      vi.advanceTimersByTime(3000);
      harness.detectChanges();
      const retry = button('Retry now');
      expect(retry.disabled).toBe(false);
      retry.click();
      expect(searchReq().request.params.get('q')).toBe('drone');
    } finally {
      vi.useRealTimers();
    }
  });

  it('lets the chart retry on its own', async () => {
    await open('/?q=drone');
    searchReq().flush(found([paper(1)]));
    chartReq().flush({ error: 'core_rate_limited', retry_after: 20 }, { status: 429, statusText: 'x' });
    harness.detectChanges();
    expect(text()).toContain('The chart has to wait');
    expect(el().querySelectorAll('.paper').length).toBe(1); // results stay
    button('Load the chart').click();
    chartReq().flush(YEARS);
    harness.detectChanges();
    expect(el().querySelector('app-year-chart')).toBeTruthy();
  });

  it('shows an outage with a way to try again', async () => {
    await open('/?q=drone');
    searchReq().flush({ error: 'core_unavailable', message: 'CORE is unavailable right now' }, { status: 502, statusText: 'x' });
    harness.detectChanges();
    expect(text()).toContain('Search failed');
    expect(text()).toContain('CORE is unavailable right now. Please try again in a minute.');
    button('Try again').click();
    searchReq().flush(found([paper(1)]));
    chartReq().flush(YEARS);
  });

  it('runs an example query', async () => {
    await open();
    button('drone AND (package OR delivery)').click();
    await harness.fixture.whenStable();
    expect(searchReq().request.params.get('q')).toBe('drone AND (package OR delivery)');
  });

  it('clamps long abstracts behind Show more', async () => {
    await open('/?q=drone');
    searchReq().flush(found([paper(1, { abstract: 'word '.repeat(80) })]));
    chartReq().flush(YEARS);
    harness.detectChanges();
    const abstract = el().querySelector('.abstract')!;
    expect(abstract.classList).toContain('clamped');
    button('Show more').click();
    harness.detectChanges();
    expect(abstract.classList).not.toContain('clamped');
    expect(button('Show less').getAttribute('aria-expanded')).toBe('true');
  });
});
