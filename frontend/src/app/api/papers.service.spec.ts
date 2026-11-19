import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { firstValueFrom } from 'rxjs';
import { ApiError } from './papers.models';
import { PapersService } from './papers.service';

describe('PapersService', () => {
  let api: PapersService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    api = TestBed.inject(PapersService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  async function failWith(status: number, body: string | object, headers: Record<string, string> = {}): Promise<ApiError> {
    const result = firstValueFrom(api.search('drone', 10)).catch((e: ApiError) => e);
    http.expectOne((r) => r.url === '/api/search').flush(body, { status, statusText: 'x', headers });
    return (await result) as ApiError;
  }

  it('searches with q and limit as query parameters', async () => {
    const result = firstValueFrom(api.search('drone AND (package OR delivery)', 25));
    const req = http.expectOne((r) => r.url === '/api/search');
    expect(req.request.method).toBe('GET');
    expect(req.request.params.get('q')).toBe('drone AND (package OR delivery)');
    expect(req.request.params.get('limit')).toBe('25');
    req.flush({ total_hits: 3, results: [] });
    expect((await result).total_hits).toBe(3);
  });

  it('asks for the year aggregate by query only', async () => {
    const result = firstValueFrom(api.aggregate('drone'));
    const req = http.expectOne((r) => r.url === '/api/aggregate');
    expect(req.request.params.keys()).toEqual(['q']);
    expect(req.request.params.get('q')).toBe('drone');
    req.flush({ years: [{ year: 2024, count: 5 }], unknown_year: 1 });
    expect((await result).years).toEqual([{ year: 2024, count: 5 }]);
  });

  it('passes the query check message through', async () => {
    const message = "'AND' needs a search term after it (character 8)";
    const err = await failWith(400, { error: 'invalid_query', message, position: 7 });
    expect(err).toEqual({ kind: 'invalid_query', message, position: 7 });
  });

  it('keeps CORE rejections apart from our own checks', async () => {
    const err = await failWith(400, { error: 'core_rejected_query', message: 'CORE rejected the query: TITLE' });
    expect(err.kind).toBe('core_rejected');
    expect(err.message).toContain('TITLE');
  });

  it('turns a 429 into a friendly wait with the seconds from the body', async () => {
    const err = await failWith(429, { error: 'core_rate_limited', retry_after: 42 }, { 'Retry-After': '7' });
    expect(err.kind).toBe('rate_limited');
    expect(err.retryAfter).toBe(42);
    expect(err.message).toContain('about 10 requests a minute');
  });

  it('falls back to the Retry-After header when the body has no seconds', async () => {
    expect((await failWith(429, '', { 'Retry-After': '7' })).retryAfter).toBe(7);
  });

  it('assumes 10 s when a 429 says nothing', async () => {
    expect((await failWith(429, '')).retryAfter).toBe(10);
  });

  it('explains an outage', async () => {
    const err = await failWith(502, { error: 'core_unavailable', message: 'CORE is unavailable right now' });
    expect(err).toEqual({ kind: 'unavailable', message: 'CORE is unavailable right now. Please try again in a minute.' });
  });

  it('explains a network failure', async () => {
    const result = firstValueFrom(api.search('drone', 10)).catch((e: ApiError) => e);
    http.expectOne((r) => r.url === '/api/search').error(new ProgressEvent('error'), { status: 0 });
    expect(((await result) as ApiError).kind).toBe('network');
  });

  it('never shows a raw server error', async () => {
    const err = await failWith(500, '<html>Traceback</html>');
    expect(err).toEqual({ kind: 'unknown', message: 'Something went wrong on our side (HTTP 500).' });
  });
});
