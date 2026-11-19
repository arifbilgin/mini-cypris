import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, throwError } from 'rxjs';
import { AggregateResponse, ApiError, SearchResponse } from './papers.models';

export const MAX_LIMIT = 100;
export const DEFAULT_LIMIT = 10;
export const MAX_QUERY_LENGTH = 1000;

/** The Flask API in front of CORE. Errors come out as ApiError, never raw HTTP. */
@Injectable({ providedIn: 'root' })
export class PapersService {
  private readonly http = inject(HttpClient);

  search(q: string, limit: number): Observable<SearchResponse> {
    const params = new HttpParams().set('q', q).set('limit', limit);
    return this.http.get<SearchResponse>('/api/search', { params }).pipe(catchError(fail));
  }

  /** Papers per year across ALL matches of q, not just the returned page. */
  aggregate(q: string): Observable<AggregateResponse> {
    const params = new HttpParams().set('q', q);
    return this.http.get<AggregateResponse>('/api/aggregate', { params }).pipe(catchError(fail));
  }
}

function fail(res: HttpErrorResponse): Observable<never> {
  return throwError(() => toApiError(res));
}

export function toApiError(res: HttpErrorResponse): ApiError {
  const body = typeof res.error === 'object' && res.error !== null ? res.error : {};
  const message: string | undefined = typeof body.message === 'string' ? body.message : undefined;

  if (res.status === 0) {
    return { kind: 'network', message: 'Could not reach the server. Check your connection and try again.' };
  }
  if (res.status === 429) {
    const header = Number(res.headers?.get('Retry-After'));
    const retryAfter = Number.isFinite(body.retry_after) ? body.retry_after : header > 0 ? header : 10;
    return {
      kind: 'rate_limited',
      retryAfter,
      message: 'CORE allows about 10 requests a minute, so this search has to wait a moment.',
    };
  }
  switch (body.error) {
    case 'invalid_query':
      return { kind: 'invalid_query', message: message ?? 'Check the query syntax.', position: body.position };
    case 'bad_request':
      return { kind: 'bad_request', message: message ?? 'Check the search fields.' };
    case 'core_rejected_query':
      return { kind: 'core_rejected', message: message ?? 'CORE could not run this query.' };
  }
  if (res.status === 502 || res.status === 503 || res.status === 504) {
    return {
      kind: 'unavailable',
      message: `${message ?? 'CORE is unavailable right now'}. Please try again in a minute.`,
    };
  }
  return { kind: 'unknown', message: `Something went wrong on our side (HTTP ${res.status}).` };
}
