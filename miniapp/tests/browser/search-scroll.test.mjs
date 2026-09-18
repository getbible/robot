import assert from "node:assert/strict";
import test from "node:test";

import {
  FIRST_SHA,
  OFFSET_CEILING,
  PAGE_SIZE,
  SECOND_SHA,
  cardCount,
  expectedOffsets,
  footText,
  launch,
  offsets,
  openSearch,
  openSearchView,
  scrollToFoot,
  scrollUntilSettled,
  selectionId,
  selectionIds,
  settle,
  waitForCards,
  waitForReach,
  waitForRequests,
} from "./support/search-scroll.mjs";

/**
 * Search results in a real browser load themselves while the reader scrolls:
 * every page the Search API can give is requested in order, at the offset
 * the previous page ended at, until the foot of the list says all results
 * are loaded; a page that did not fill the screen is followed at once; a
 * failed page pauses automatic loading until the reader retries; a browser
 * without IntersectionObserver measures the list on scroll instead; a corpus
 * that changes between pages restarts the search; and the API's offset
 * ceiling is reported rather than silently stopped at.
 */

test("scrolling loads every page in order until the foot says all results are loaded", async (context) => {
  const total = 380;
  const { page, observed, search } = await launch(context, { total });
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  assert.equal(search.requests.length, 1);
  assert.equal(search.requests[0].origin, "https://search.getbible.net");
  assert.equal(search.requests[0].pathname, "/v2/kjv");
  assert.equal(search.requests[0].searchParams.get("q"), "text");
  assert.equal(search.requests[0].searchParams.get("limit"), String(PAGE_SIZE));
  assert.equal(search.requests[0].searchParams.get("offset"), "0");
  // The exact total is on screen from the first page, and the foot of the
  // list counts what is loaded; there is no button, scrolling asks.
  assert.match(await page.locator("#search-summary-meta").innerText(), /^380 verses/);
  await waitForReach(page, "more");
  assert.equal(await footText(page), "50 of 380 verses loaded");
  assert.equal(await page.locator("#load-more").count(), 0);

  // A wheel gesture over the list, as a mouse or trackpad makes, brings the
  // next page without any click.
  await page.mouse.move(195, 600);
  await page.mouse.wheel(0, 20_000);
  await waitForCards(page, PAGE_SIZE * 2);
  assert.equal(search.requests.length, 2);
  assert.equal(offsets(search)[1], PAGE_SIZE);
  await waitForReach(page, "more");
  assert.equal(await footText(page), "100 of 380 verses loaded");

  // And so on to the end, one page per visit to the foot.
  const walk = await scrollUntilSettled(page, { total });
  assert.equal(walk.reach, "complete");
  assert.equal(walk.loaded, total);
  // One page per visit to the foot, each exactly the page size until the last.
  assert.deepEqual(
    walk.trace.map((step) => step.loaded),
    expectedOffsets(total).slice(2).map((offset) => Math.min(total, offset + PAGE_SIZE)),
  );
  assert.equal(await cardCount(page), total);
  assert.equal(await footText(page), "All results loaded: 380 verses.");
  assert.equal(await page.locator("#load-more").count(), 0);
  assert.deepEqual(offsets(search), expectedOffsets(total));
  for (const url of search.requests) {
    assert.equal(url.pathname, "/v2/kjv");
    assert.equal(url.searchParams.get("limit"), String(PAGE_SIZE));
    assert.equal(url.searchParams.get("q"), "text");
  }
  // Every match exactly once, in the API's order.
  const ids = await selectionIds(page);
  assert.equal(ids.length, total);
  assert.equal(new Set(ids).size, total);
  assert.equal(ids[0], selectionId(0));
  assert.equal(ids.at(-1), selectionId(total - 1));
  assert.equal(await page.locator("#search-results .verse-result:first-child mark").textContent(), "text");
  // Nothing more is asked for once everything is loaded, however far the
  // reader scrolls.
  await scrollToFoot(page);
  await page.mouse.wheel(0, 20_000);
  await settle(page);
  assert.equal(search.requests.length, expectedOffsets(total).length);
  assert.deepEqual(observed.pageErrors, []);
  assert.deepEqual(observed.consoleMessages, []);
  assert.deepEqual(observed.failedRequests, []);
});

test("a page that does not fill a tall screen is followed at once, and the chain stops out of reach", async (context) => {
  const total = 380;
  const { page, observed, search } = await launch(context, {
    total,
    viewport: { width: 390, height: 8_000 },
  });
  await openSearch(page);
  // Without any scrolling the second page arrives because the foot of the
  // first is within reach of the visible area.
  await waitForCards(page, PAGE_SIZE * 2);
  assert.ok(search.requests.length >= 2);
  // The chain ends as soon as the foot is further than the prefetch
  // distance below the visible area, well short of the whole result set.
  await page.waitForFunction(() => {
    const view = document.querySelector("#search-view").getBoundingClientRect();
    const foot = document.querySelector("#search-more");
    const rect = foot.getBoundingClientRect();
    return foot.dataset.reach === "more" && rect.top - view.bottom > view.height * 1.5;
  });
  await settle(page);
  const requests = search.requests.length;
  assert.ok(requests >= 2 && requests < expectedOffsets(total).length, `requests: ${requests}`);
  assert.equal(await cardCount(page), requests * PAGE_SIZE);
  assert.deepEqual(offsets(search), expectedOffsets(total).slice(0, requests));
  // A scroll to the foot resumes the walk.
  await scrollToFoot(page);
  await waitForCards(page, (requests + 1) * PAGE_SIZE);
  assert.equal(search.requests.length, requests + 1);
  assert.deepEqual(observed.pageErrors, []);
  assert.deepEqual(observed.consoleMessages, []);
});

test("a failed page pauses automatic loading until the reader retries after the announced wait", async (context) => {
  const total = 200;
  const { page, observed, search } = await launch(context, { total });
  search.failure = {
    offset: 100,
    status: 503,
    title: "Service Unavailable",
    code: "busy",
    detail: "The search service is busy.",
    retryAfter: 6,
  };
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  await scrollToFoot(page);
  await waitForCards(page, PAGE_SIZE * 2);
  await scrollToFoot(page);
  await waitForReach(page, "failed");
  assert.equal(search.requests.length, 3);
  assert.equal(offsets(search)[2], 100);
  assert.equal(await cardCount(page), 100);
  const failed = await footText(page);
  assert.match(failed, /More verses could not be loaded\./);
  assert.match(failed, /Try again in about 6 seconds\./);
  const retry = page.locator("#load-more");
  assert.equal(await retry.count(), 1);
  assert.equal(await retry.innerText(), "Try again");
  assert.equal(await retry.isDisabled(), true);
  // Scrolling while paused asks for nothing: the API said to wait.
  await scrollToFoot(page);
  await page.mouse.move(195, 600);
  await page.mouse.wheel(0, 20_000);
  await settle(page);
  assert.equal(search.requests.length, 3);
  // The control opens once the wait has passed, and still nothing is
  // requested until the reader asks.
  await page.waitForFunction(() => {
    const button = document.querySelector("#load-more");
    return button && !button.disabled;
  }, undefined, { timeout: 15_000 });
  await scrollToFoot(page);
  await settle(page);
  assert.equal(search.requests.length, 3);
  search.failure = null;
  await retry.click();
  await waitForCards(page, PAGE_SIZE * 3);
  assert.equal(search.requests.length, 4);
  assert.equal(offsets(search)[3], 100);
  await waitForReach(page, "more");
  assert.equal(await footText(page), "150 of 200 verses loaded");
  assert.equal(await page.locator("#load-more").count(), 0);
  // The retry resumed automatic loading: the next scroll brings the rest.
  await scrollToFoot(page);
  await waitForReach(page, "complete");
  assert.equal(await cardCount(page), total);
  assert.deepEqual(offsets(search), [0, 50, 100, 100, 150]);
  assert.equal(await footText(page), "All results loaded: 200 verses.");
  const ids = await selectionIds(page);
  assert.equal(new Set(ids).size, total);
  assert.deepEqual(observed.pageErrors, []);
});

test("a refusal that would only repeat offers a change of filters instead of a retry", async (context) => {
  const total = 120;
  const { page, observed, search } = await launch(context, { total });
  search.failure = {
    offset: 50,
    status: 400,
    title: "Bad Request",
    code: "request_limit",
    detail: "Too many matches requested.",
  };
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  await scrollToFoot(page);
  await waitForReach(page, "failed");
  assert.equal(search.requests.length, 2);
  const failed = await footText(page);
  assert.match(failed, /More verses could not be loaded\./);
  assert.doesNotMatch(failed, /Try again/);
  assert.equal(await page.locator("#load-more").count(), 0);
  const change = page.locator("#search-more-filters");
  assert.equal(await change.innerText(), "Change filters");
  await scrollToFoot(page);
  await settle(page);
  assert.equal(search.requests.length, 2);
  await change.click();
  await page.waitForFunction(() => document.querySelector("#filters-dialog")?.open === true);
  // Applying the filters starts the search afresh; once the refusal is gone
  // that search walks to its end.
  search.failure = null;
  await page.locator("#filters-form").evaluate((form) => form.requestSubmit());
  await page.waitForFunction(() => document.querySelector("#filters-dialog")?.open === false);
  await waitForRequests(search, 3);
  await waitForReach(page, "more");
  const walk = await scrollUntilSettled(page, { total });
  assert.equal(walk.reach, "complete");
  assert.deepEqual(offsets(search), [0, 50, 0, 50, 100]);
  assert.deepEqual(observed.pageErrors, []);
});

test("without IntersectionObserver the list is measured on scroll and still loads to the end", async (context) => {
  const total = 130;
  const { page, observed, search } = await launch(context, {
    total,
    intersectionObserver: false,
  });
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  assert.equal(await page.evaluate(() => typeof window.IntersectionObserver), "undefined");
  await waitForReach(page, "more");
  await settle(page);
  assert.equal(search.requests.length, 1);
  const walk = await scrollUntilSettled(page, { total });
  assert.equal(walk.reach, "complete");
  assert.deepEqual(walk.trace.map((step) => step.loaded), [100, 130]);
  assert.equal(await cardCount(page), total);
  assert.deepEqual(offsets(search), [0, 50, 100]);
  assert.equal(await footText(page), "All results loaded: 130 verses.");
  await scrollToFoot(page);
  await settle(page);
  assert.equal(search.requests.length, 3);
  assert.deepEqual(observed.pageErrors, []);
  assert.deepEqual(observed.consoleMessages, []);
});

test("a corpus that changes between pages restarts the search from the first page", async (context) => {
  const total = 80;
  const { page, observed, search } = await launch(context, { total });
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  search.sha = SECOND_SHA;
  await scrollToFoot(page);
  // The second page came from another corpus: the list is rebuilt from
  // offset zero on the new corpus, so the first page shows again and a
  // further scroll ends the search on that corpus alone.
  await page.waitForFunction(() => (
    document.querySelector("#toast-region")?.innerText.includes("This translation changed")
  ));
  const walk = await scrollUntilSettled(page, { total });
  assert.equal(walk.reach, "complete");
  assert.equal(await cardCount(page), total);
  assert.deepEqual(offsets(search), [0, 50, 0, 50]);
  const ids = await selectionIds(page);
  assert.equal(new Set(ids).size, total);
  assert.equal(await footText(page), "All results loaded: 80 verses.");
  assert.deepEqual(observed.pageErrors, []);
});

test("a single page that holds everything closes the list without a second request", async (context) => {
  const total = 7;
  const { page, search } = await launch(context, { total });
  await openSearch(page);
  await waitForReach(page, "complete");
  assert.equal(await cardCount(page), total);
  assert.equal(await footText(page), "All results loaded: 7 verses.");
  assert.match(await page.locator("#search-summary-meta").innerText(), /^7 verses/);
  await scrollToFoot(page);
  await settle(page);
  assert.equal(search.requests.length, 1);
  // A new search clears the foot with the list.
  await page.locator("#clear-search").click();
  assert.equal(await page.locator("#search-more").isHidden(), true);
  assert.equal(await cardCount(page), 0);
});

test("the API's offset ceiling ends the walk with an honest count instead of a silent stop", async (context) => {
  const total = 10_200;
  const { page, observed, search } = await launch(context, { total });
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  assert.match(await page.locator("#search-summary-meta").innerText(), /^10200 verses/);
  const walk = await scrollUntilSettled(page, { total });
  assert.equal(walk.reach, "ceiling");
  const loaded = OFFSET_CEILING + PAGE_SIZE;
  assert.equal(walk.loaded, loaded);
  assert.equal(walk.trace.length, OFFSET_CEILING / PAGE_SIZE);
  assert.ok(
    walk.trace.every((step, index) => step.loaded === (index + 2) * PAGE_SIZE),
    "every visit to the foot brought exactly one page",
  );
  assert.equal(await cardCount(page), loaded);
  assert.equal(
    await footText(page),
    `Showing the first ${loaded} of ${total} matching verses. Narrow the search to reach the rest.`,
  );
  assert.equal(await page.locator("#load-more").count(), 0);
  assert.deepEqual(offsets(search), expectedOffsets(total));
  assert.equal(offsets(search).at(-1), OFFSET_CEILING);
  await scrollToFoot(page);
  await settle(page);
  assert.equal(search.requests.length, OFFSET_CEILING / PAGE_SIZE + 1);
  const ids = await selectionIds(page);
  assert.equal(new Set(ids).size, loaded);
  assert.equal(ids.at(-1), selectionId(loaded - 1));
  assert.deepEqual(observed.pageErrors, []);
  assert.deepEqual(observed.consoleMessages, []);
});

test("a translation whose content keeps changing is restarted a bounded number of times, then left to the reader", async (context) => {
  const total = 200;
  const { page, observed, search } = await launch(context, { total });
  // The first page keeps coming from the old corpus, as a stale HTTP cache
  // would serve it, while every later page comes from the new one.
  search.shaFor = (offset) => (offset === 0 ? FIRST_SHA : SECOND_SHA);
  await openSearch(page);
  await waitForCards(page, PAGE_SIZE);
  for (let round = 1; round <= 3; round += 1) {
    await scrollToFoot(page);
    if (round < 3) {
      // The mismatching second page restarts the search from its first page.
      await waitForRequests(search, 1 + round * 2);
      await page.waitForFunction((expected) => (
        document.querySelectorAll("#search-results .verse-result").length === expected &&
        document.querySelector("#search-more")?.dataset.reach === "more"
      ), PAGE_SIZE);
    } else {
      await waitForReach(page, "stalled");
    }
  }
  assert.deepEqual(offsets(search), [0, 50, 0, 50, 0, 50]);
  assert.match(
    await page.locator("#toast-region").innerText(),
    /This translation changed while you were reading/,
  );
  assert.equal(await cardCount(page), PAGE_SIZE);
  assert.match(await footText(page), /^50 of 200 verses loaded/);
  const more = page.locator("#load-more");
  assert.equal(await more.innerText(), "Load more");
  assert.equal(await more.isDisabled(), false);
  // Scrolling no longer chases the changing corpus.
  await scrollToFoot(page);
  await settle(page);
  assert.equal(search.requests.length, 6);
  // The reader's own request is honoured once more, and again bounded.
  await more.click();
  await waitForRequests(search, 8);
  await page.waitForFunction((expected) => (
    document.querySelectorAll("#search-results .verse-result").length === expected &&
    document.querySelector("#search-more")?.dataset.reach === "more"
  ), PAGE_SIZE);
  await settle(page);
  assert.deepEqual(offsets(search), [0, 50, 0, 50, 0, 50, 50, 0]);
  assert.deepEqual(observed.pageErrors, []);
});

test("later pages keep the criteria the search started with, even after a visit to the reader", async (context) => {
  const total = 120;
  const { page, observed, search } = await launch(context, { total });
  await openSearchView(page);
  await page.locator("#open-filters").click();
  await page.waitForFunction(() => document.querySelector("#filters-dialog")?.open === true);
  const john = page.locator('#filter-books input[value="43"]');
  await john.waitFor();
  await john.check();
  await page.locator("#filters-form").evaluate((form) => form.requestSubmit());
  await page.waitForFunction(() => document.querySelector("#filters-dialog")?.open === false);
  await page.locator("#search-query").fill("text");
  await page.locator("#search-query").press("Enter");
  await waitForCards(page, PAGE_SIZE);
  assert.equal(search.requests.length, 1);
  assert.deepEqual(search.requests[0].searchParams.getAll("book"), ["43"]);
  // Opening a result in the reader rewrites the live filters (it clears the
  // book restriction) and coming back must not change what the next page
  // is asked for.
  await page.locator("#search-results .verse-result:first-child .verse-context").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "bible" &&
    document.querySelector("#bible-search-return")?.hidden === false
  ));
  await page.locator("#bible-search-return").click();
  await page.waitForFunction(() => (
    document.querySelector("#app")?.dataset.activeRoute === "search"
  ));
  // The view restores its remembered scroll position a frame after it is
  // shown again; a reader's scroll comes after that, so this one does too.
  await settle(page);
  assert.equal(await cardCount(page), PAGE_SIZE);
  await scrollToFoot(page);
  await waitForCards(page, PAGE_SIZE * 2);
  assert.equal(search.requests.length, 2);
  assert.equal(search.requests[1].searchParams.get("offset"), "50");
  assert.deepEqual(search.requests[1].searchParams.getAll("book"), ["43"]);
  for (const name of ["words", "match", "scope", "case_sensitive", "diacritics", "sort", "q"]) {
    assert.equal(
      search.requests[1].searchParams.get(name),
      search.requests[0].searchParams.get(name),
      name,
    );
  }
  const walk = await scrollUntilSettled(page, { total });
  assert.equal(walk.reach, "complete");
  assert.ok(search.requests.every((url) => url.searchParams.getAll("book").join() === "43"));
  assert.deepEqual(observed.pageErrors, []);
});
