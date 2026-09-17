import { BOOKMARKS_API_FIXTURE } from "../../fixtures/bookmarks-api.mjs";

const corsHeaders = { "access-control-allow-origin": "*" };

export const bookmarksApiPattern = /^https:\/\/bookmarks\.getbible\.net\/v1\/.+/;

/**
 * Serves the public Bookmarks API v1 to a page from the shared fixture. The
 * bytes are the ones the fixture hashed, so `index.checksum` really is the
 * SHA-256 of the `all.json` body the browser receives. `documents` can be
 * swapped between requests to publish a new catalogue; `unavailable` answers
 * every request with 503 so the client's offline state can be observed.
 */
export async function installBookmarksApiRoute(page, {
  fixture = BOOKMARKS_API_FIXTURE,
  unavailable = false,
} = {}) {
  const state = { fixture, unavailable, requests: [] };
  await page.route(bookmarksApiPattern, (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/v1\//, "");
    state.requests.push(path);
    if (state.unavailable) {
      return route.fulfill({
        status: 503,
        contentType: "text/plain",
        headers: corsHeaders,
        body: "unavailable",
      });
    }
    const bodies = {
      "index.json": state.fixture.indexBytes,
      "all.json": state.fixture.allBytes,
    };
    if (!Object.hasOwn(bodies, path)) {
      return route.fulfill({
        status: 404,
        contentType: "text/plain",
        headers: corsHeaders,
        body: "not found",
      });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: corsHeaders,
      body: bodies[path],
    });
  });
  return state;
}
