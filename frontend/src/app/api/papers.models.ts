/** Shapes returned by the Flask API (see README, "API"). */

export interface Paper {
  id: number;
  source: string;
  external_id: string;
  title: string;
  authors: string[];
  year: number | null;
  publisher: string | null;
  doi: string | null;
  pdf_url: string | null;
  abstract: string | null;
  core_url: string | null;
}

export type CacheStatus = 'hit' | 'miss' | 'topup' | 'refresh' | 'stale' | 'partial';

export interface CacheInfo {
  status: CacheStatus;
  fetched_at: number;
  age_seconds: number;
  error: string | null;
}

export interface SearchResponse {
  query: string;
  limit: number;
  total_hits: number;
  count: number;
  results: Paper[];
  cache: CacheInfo;
}

export interface YearCount {
  year: number;
  count: number;
}

export interface AggregateResponse {
  query: string;
  field: 'yearPublished';
  total_hits: number;
  years: YearCount[];
  unknown_year: number;
  implausible_years: Record<string, number>;
  cache: CacheInfo;
}

/** Every failure, already turned into something the UI can show. */
export interface ApiError {
  kind: 'invalid_query' | 'bad_request' | 'core_rejected' | 'rate_limited' | 'unavailable' | 'network' | 'unknown';
  message: string;
  /** Seconds until a retry can succeed (rate_limited only). */
  retryAfter?: number;
  /** 0-based character the query check complained about (invalid_query only). */
  position?: number;
}

/** A request's lifecycle, for loading / empty / error states. */
export type Load<T> =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'ok'; data: T }
  | { state: 'error'; error: ApiError };
