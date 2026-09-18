import { uniqueVerses } from "./model.js";

/**
 * Paging arithmetic for the Mini App's full-text search.
 *
 * The Search API answers one page per request. It echoes `offset` and
 * `limit`, counts the matches it delivered in `returned`, reports the exact
 * number of matching verses in `total`, and says in `has_more` whether any
 * follow. It accepts an offset of at most `SEARCH_OFFSET_CEILING`, so a search
 * with more matches than the ceiling can hold is never paged to its end and
 * the reader has to be told so.
 *
 * Pages are requested automatically while the reader scrolls, so the
 * arithmetic here is deliberately monotonic: the next page starts at the
 * offset that was asked for plus the matches the API delivered, never at a
 * count of what survived deduplication on the screen. A page that delivers
 * nothing cannot advance, and is reported as a stall rather than asked for
 * again and again.
 */

/** Matches requested per page; the API accepts at most 100. */
export const SEARCH_PAGE_SIZE = 50;

/** The largest offset the Search API accepts. */
export const SEARCH_OFFSET_CEILING = 10_000;

/**
 * How far below the visible part of the list the next page is requested,
 * in multiples of the visible height. Scrolling never has to reach the very
 * end of what is loaded before the next page is on its way.
 */
export const SEARCH_PREFETCH_VIEWPORTS = 1.5;

/** The longest wait honoured from a `Retry-After` answer before a retry, in seconds. */
export const SEARCH_RETRY_WAIT_MAX_SECONDS = 120;

/** The wait applied to a rate-limit refusal that named none, in seconds. */
export const SEARCH_RATE_LIMIT_WAIT_SECONDS = 5;

/**
 * How many times a search restarts by itself because the translation's
 * content changed between pages, before the reader is asked to continue.
 */
export const SEARCH_RESTART_LIMIT = 2;

/** Where a result list stands after the last page received. */
export const SEARCH_REACH = Object.freeze({
  /** Another page can be requested at `nextOffset`. */
  MORE: "more",
  /** The API reported no further page: every matching verse is loaded. */
  COMPLETE: "complete",
  /** Matches remain, but the API accepts no larger offset. */
  CEILING: "ceiling",
  /** The API says matches remain but delivered nothing to advance by. */
  STALLED: "stalled",
});

function nonNegativeInteger(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

/**
 * Where the list stands after `page` was received for `requestedOffset`.
 *
 * Returns `{ status, nextOffset }`: `nextOffset` is the offset the next page
 * starts at while more can be requested (or asked for again after a stall),
 * and `null` once the list is complete or the API's ceiling is reached.
 */
export function searchReach(page, requestedOffset = 0) {
  const offset = nonNegativeInteger(requestedOffset) ?? 0;
  if (!page || page.kind !== "search" || page.has_more !== true) {
    return Object.freeze({ status: SEARCH_REACH.COMPLETE, nextOffset: null });
  }
  const returned = nonNegativeInteger(page.returned) ?? 0;
  if (returned === 0) {
    return Object.freeze({ status: SEARCH_REACH.STALLED, nextOffset: offset });
  }
  const nextOffset = offset + returned;
  if (nextOffset > SEARCH_OFFSET_CEILING) {
    return Object.freeze({ status: SEARCH_REACH.CEILING, nextOffset: null });
  }
  return Object.freeze({ status: SEARCH_REACH.MORE, nextOffset });
}

/**
 * The list after a page is appended: `results` holds every verse once, in
 * arrival order, and `added` holds the verses this page contributed, so the
 * screen can append exactly those instead of rebuilding the whole list.
 */
export function mergeSearchPage(existing, incoming) {
  const results = uniqueVerses(existing, incoming);
  return Object.freeze({ results, added: results.slice(existing.length) });
}

/**
 * Whether the foot of the list is close enough to the visible area for the
 * next page to be requested: at most `viewports` visible heights below the
 * bottom edge. Used when the browser offers no IntersectionObserver.
 */
export function withinPrefetchReach(
  { footTop, viewBottom, viewHeight },
  viewports = SEARCH_PREFETCH_VIEWPORTS,
) {
  if (
    ![footTop, viewBottom, viewHeight, viewports].every(Number.isFinite) ||
    viewHeight <= 0 ||
    viewports < 0
  ) {
    return false;
  }
  return footTop - viewBottom <= viewHeight * viewports;
}

/**
 * The IntersectionObserver root margin that extends the visible area of the
 * scrolling list downwards by `viewports` of its own height.
 */
export function prefetchRootMargin(viewports = SEARCH_PREFETCH_VIEWPORTS) {
  const percent = Number.isFinite(viewports) && viewports > 0
    ? Math.round(viewports * 100)
    : 0;
  return `0px 0px ${percent}% 0px`;
}

/**
 * Seconds to wait before a failed page is retried, from the wait the API
 * announced: whole seconds, never negative, never longer than `maximum`.
 * A rate-limit refusal that named no wait still gets a short one.
 */
export function retryWaitSeconds(error, maximum = SEARCH_RETRY_WAIT_MAX_SECONDS) {
  const bound = Number.isFinite(maximum) && maximum > 0 ? maximum : 0;
  const seconds = Math.ceil(Number(error?.retryAfter));
  if (Number.isFinite(seconds) && seconds > 0) {
    return Math.min(seconds, bound);
  }
  if (error?.code === "search_rate_limited") {
    return Math.min(SEARCH_RATE_LIMIT_WAIT_SECONDS, bound);
  }
  return 0;
}
