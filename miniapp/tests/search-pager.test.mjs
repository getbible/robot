import assert from "node:assert/strict";
import test from "node:test";

import {
  SEARCH_OFFSET_CEILING,
  SEARCH_PAGE_SIZE,
  SEARCH_PREFETCH_VIEWPORTS,
  SEARCH_RATE_LIMIT_WAIT_SECONDS,
  SEARCH_REACH,
  SEARCH_RESTART_LIMIT,
  SEARCH_RETRY_WAIT_MAX_SECONDS,
  mergeSearchPage,
  prefetchRootMargin,
  retryWaitSeconds,
  searchReach,
  withinPrefetchReach,
} from "../lib/search-pager.js";

function page(overrides = {}) {
  return { kind: "search", has_more: true, returned: SEARCH_PAGE_SIZE, ...overrides };
}

function verse(number) {
  return { selection_id: `gbd_kjv_043_0003_${String(number).padStart(4, "0")}`, verse: number };
}

test("the page size stays inside the API's limit", () => {
  assert.ok(Number.isInteger(SEARCH_PAGE_SIZE));
  assert.ok(SEARCH_PAGE_SIZE >= 1 && SEARCH_PAGE_SIZE <= 100);
  assert.equal(SEARCH_OFFSET_CEILING, 10_000);
});

test("the next page starts where the requested page ended", () => {
  assert.deepEqual(searchReach(page(), 0), { status: SEARCH_REACH.MORE, nextOffset: SEARCH_PAGE_SIZE });
  assert.deepEqual(searchReach(page({ returned: 7 }), 150), { status: SEARCH_REACH.MORE, nextOffset: 157 });
});

test("the list is complete when the API reports no further page", () => {
  assert.deepEqual(searchReach(page({ has_more: false }), 100), { status: SEARCH_REACH.COMPLETE, nextOffset: null });
  assert.deepEqual(searchReach(page({ has_more: false, returned: 0 }), 0), { status: SEARCH_REACH.COMPLETE, nextOffset: null });
  assert.deepEqual(searchReach(page({ has_more: "true" }), 0), { status: SEARCH_REACH.COMPLETE, nextOffset: null });
});

test("a reference answer is complete as delivered", () => {
  assert.deepEqual(searchReach({ kind: "reference", returned: 3, has_more: true }, 0), {
    status: SEARCH_REACH.COMPLETE,
    nextOffset: null,
  });
  assert.deepEqual(searchReach(null, 0), { status: SEARCH_REACH.COMPLETE, nextOffset: null });
});

test("a page that delivers nothing while more is claimed is a stall, not a loop", () => {
  assert.deepEqual(searchReach(page({ returned: 0 }), 200), { status: SEARCH_REACH.STALLED, nextOffset: 200 });
  assert.deepEqual(searchReach(page({ returned: -1 }), 200), { status: SEARCH_REACH.STALLED, nextOffset: 200 });
  assert.deepEqual(searchReach(page({ returned: "50" }), 200), { status: SEARCH_REACH.STALLED, nextOffset: 200 });
});

test("the offset ceiling is the API's: the last page starts at 10 000 and none follows", () => {
  const before = searchReach(page(), SEARCH_OFFSET_CEILING - SEARCH_PAGE_SIZE);
  assert.deepEqual(before, { status: SEARCH_REACH.MORE, nextOffset: SEARCH_OFFSET_CEILING });
  const last = searchReach(page(), SEARCH_OFFSET_CEILING);
  assert.deepEqual(last, { status: SEARCH_REACH.CEILING, nextOffset: null });
  const odd = searchReach(page({ returned: 3 }), SEARCH_OFFSET_CEILING - 2);
  assert.deepEqual(odd, { status: SEARCH_REACH.CEILING, nextOffset: null });
  const exact = searchReach(page({ returned: 2 }), SEARCH_OFFSET_CEILING - 2);
  assert.deepEqual(exact, { status: SEARCH_REACH.MORE, nextOffset: SEARCH_OFFSET_CEILING });
});

test("every offset in a full walk to the ceiling is reachable and strictly increasing", () => {
  const offsets = [];
  let reach = searchReach(page(), 0);
  offsets.push(0);
  while (reach.status === SEARCH_REACH.MORE) {
    offsets.push(reach.nextOffset);
    reach = searchReach(page(), reach.nextOffset);
  }
  assert.equal(reach.status, SEARCH_REACH.CEILING);
  assert.equal(offsets.at(-1), SEARCH_OFFSET_CEILING);
  assert.equal(offsets.length, SEARCH_OFFSET_CEILING / SEARCH_PAGE_SIZE + 1);
  for (let index = 1; index < offsets.length; index += 1) {
    assert.equal(offsets[index] - offsets[index - 1], SEARCH_PAGE_SIZE);
    assert.ok(offsets[index] <= SEARCH_OFFSET_CEILING);
  }
});

test("an invalid requested offset counts as the first page", () => {
  assert.deepEqual(searchReach(page({ returned: 5 }), -3), { status: SEARCH_REACH.MORE, nextOffset: 5 });
  assert.deepEqual(searchReach(page({ returned: 5 }), "12"), { status: SEARCH_REACH.MORE, nextOffset: 5 });
  assert.deepEqual(searchReach(page({ returned: 5 })), { status: SEARCH_REACH.MORE, nextOffset: 5 });
});

test("reach results are frozen", () => {
  assert.equal(Object.isFrozen(searchReach(page(), 0)), true);
  assert.equal(Object.isFrozen(searchReach(page({ has_more: false }), 0)), true);
});

test("a merged page keeps every verse once and names the ones it added", () => {
  const existing = [verse(1), verse(2), verse(3)];
  const merged = mergeSearchPage(existing, [verse(3), verse(4), verse(4), verse(5)]);
  assert.deepEqual(merged.results.map((item) => item.verse), [1, 2, 3, 4, 5]);
  assert.deepEqual(merged.added.map((item) => item.verse), [4, 5]);
  assert.deepEqual(existing.map((item) => item.verse), [1, 2, 3]);
  assert.equal(Object.isFrozen(merged), true);
});

test("a page of nothing new adds nothing and changes nothing", () => {
  const existing = [verse(1), verse(2)];
  const merged = mergeSearchPage(existing, [verse(2), verse(1)]);
  assert.deepEqual(merged.results.map((item) => item.verse), [1, 2]);
  assert.deepEqual(merged.added, []);
  const first = mergeSearchPage([], [verse(9)]);
  assert.deepEqual(first.added.map((item) => item.verse), [9]);
});

test("the foot of the list is within reach one and a half screens below the visible area", () => {
  const view = { viewBottom: 844, viewHeight: 844 };
  assert.equal(withinPrefetchReach({ footTop: 800, ...view }), true);
  assert.equal(withinPrefetchReach({ footTop: 844 + 844 * SEARCH_PREFETCH_VIEWPORTS, ...view }), true);
  assert.equal(withinPrefetchReach({ footTop: 844 + 844 * SEARCH_PREFETCH_VIEWPORTS + 1, ...view }), false);
  assert.equal(withinPrefetchReach({ footTop: 5_000, ...view }), false);
  assert.equal(withinPrefetchReach({ footTop: 5_000, ...view }, 10), true);
});

test("an unmeasurable list is never within reach", () => {
  assert.equal(withinPrefetchReach({ footTop: Number.NaN, viewBottom: 844, viewHeight: 844 }), false);
  assert.equal(withinPrefetchReach({ footTop: 10, viewBottom: 844, viewHeight: 0 }), false);
  assert.equal(withinPrefetchReach({ footTop: 10, viewBottom: Number.POSITIVE_INFINITY, viewHeight: 844 }), false);
  assert.equal(withinPrefetchReach({ footTop: 10, viewBottom: 844, viewHeight: 844 }, -1), false);
  assert.equal(withinPrefetchReach({}), false);
});

test("the observer margin extends the visible area downwards only", () => {
  assert.equal(prefetchRootMargin(), "0px 0px 150% 0px");
  assert.equal(prefetchRootMargin(2), "0px 0px 200% 0px");
  assert.equal(prefetchRootMargin(0), "0px 0px 0% 0px");
  assert.equal(prefetchRootMargin(-1), "0px 0px 0% 0px");
  assert.equal(prefetchRootMargin(Number.NaN), "0px 0px 0% 0px");
});

test("the retry wait follows Retry-After in whole seconds, bounded and never negative", () => {
  assert.equal(retryWaitSeconds({ retryAfter: 2.2 }), 3);
  assert.equal(retryWaitSeconds({ retryAfter: 30 }), 30);
  assert.equal(retryWaitSeconds({ retryAfter: 9_000 }), SEARCH_RETRY_WAIT_MAX_SECONDS);
  assert.equal(retryWaitSeconds({ retryAfter: 9_000 }, 10), 10);
  assert.equal(retryWaitSeconds({ retryAfter: 0 }), 0);
  assert.equal(retryWaitSeconds({ retryAfter: -4 }), 0);
  assert.equal(retryWaitSeconds({ retryAfter: null }), 0);
  assert.equal(retryWaitSeconds({ retryAfter: "soon" }), 0);
  assert.equal(retryWaitSeconds(null), 0);
  assert.equal(retryWaitSeconds(undefined), 0);
});

test("a rate-limit refusal that names no wait still waits a little", () => {
  assert.equal(retryWaitSeconds({ code: "search_rate_limited", retryAfter: null }), SEARCH_RATE_LIMIT_WAIT_SECONDS);
  assert.equal(retryWaitSeconds({ code: "search_rate_limited" }), SEARCH_RATE_LIMIT_WAIT_SECONDS);
  assert.equal(retryWaitSeconds({ code: "search_rate_limited", retryAfter: 9 }), 9);
  assert.equal(retryWaitSeconds({ code: "search_rate_limited" }, 2), 2);
  assert.equal(retryWaitSeconds({ code: "search_unavailable", retryAfter: null }), 0);
  assert.equal(retryWaitSeconds({ code: "search_rate_limited" }, 0), 0);
  assert.equal(retryWaitSeconds({ retryAfter: 30 }, Number.NaN), 0);
});

test("a search restarts by itself a small, fixed number of times", () => {
  assert.ok(Number.isInteger(SEARCH_RESTART_LIMIT));
  assert.ok(SEARCH_RESTART_LIMIT >= 1 && SEARCH_RESTART_LIMIT <= 3);
});
