import assert from "node:assert/strict";
import test from "node:test";

import { ScriptureExcerptResolver } from "../lib/scripture-excerpt-resolver.js";

function target(id, chapter, verse, translation = "kjv") {
  return { id, translation, book: 43, chapter, verse };
}

function chapterVerses({ translation, book, chapter }) {
  return [1, 2, 3].map((verse) => ({
    selection_id: `gbd_${translation}_${book}_${chapter}_${verse}`,
    translation,
    reference: `John ${chapter}:${verse}`,
    book_number: book,
    book_name: "John",
    chapter,
    verse,
    text: `${translation.toUpperCase()} text ${chapter}:${verse}`,
  }));
}

test("coalesces one batch and delegates later reads to the shared loader", async () => {
  const calls = [];
  const resolver = new ScriptureExcerptResolver({
    loadChapter: async (request) => {
      calls.push(request);
      return chapterVerses(request);
    },
  });

  const first = await resolver.resolve([
    target("history_one", 3, 1),
    target("history_two", 3, 2),
  ]);
  assert.equal(calls.length, 1);
  const repeated = await resolver.resolve([target("history_three", 3, 3)]);

  assert.equal(calls.length, 2);
  assert.equal(first[0].text, "KJV text 3:1");
  assert.equal(first[0].status, "ready");
  assert.equal(first[1].reference, "John 3:2");
  assert.equal(repeated[0].book_name, "John");
});

test("bounds cold chapter concurrency and isolates unavailable verses", async () => {
  let active = 0;
  let peak = 0;
  const releases = [];
  const resolver = new ScriptureExcerptResolver({
    maximumConcurrency: 2,
    loadChapter: async (request) => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise((resolve) => releases.push(resolve));
      active -= 1;
      if (request.chapter === 4) {
        throw new Error("offline cache miss");
      }
      return chapterVerses(request);
    },
  });

  const pending = resolver.resolve([
    target("one", 1, 1),
    target("two", 2, 1),
    target("three", 3, 1),
    target("four", 4, 1),
  ]);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(peak, 2);
  while (releases.length > 0) {
    releases.shift()();
    await new Promise((resolve) => setImmediate(resolve));
  }
  const results = await pending;

  assert.equal(peak, 2);
  assert.equal(results[0].text, "KJV text 1:1");
  assert.deepEqual(results[3], { id: "four", status: "error" });
});

test("distinguishes an unavailable coordinate from a chapter load failure", async () => {
  const resolver = new ScriptureExcerptResolver({
    loadChapter: async (request) => {
      if (request.chapter === 4) {
        throw new Error("temporary network failure");
      }
      return chapterVerses(request);
    },
  });

  assert.deepEqual(
    await resolver.resolve([
      target("missing", 3, 99),
      target("failed", 4, 1),
    ]),
    [
      { id: "missing", status: "unavailable" },
      { id: "failed", status: "error" },
    ],
  );
});

test("rejects invalid dependencies and ignores malformed targets", async () => {
  assert.throws(
    () => new ScriptureExcerptResolver(),
    /chapter loader/,
  );
  assert.throws(
    () => new ScriptureExcerptResolver({
      loadChapter: async () => [],
      maximumConcurrency: 0,
    }),
    /concurrency/,
  );

  const resolver = new ScriptureExcerptResolver({
    loadChapter: async () => {
      throw new Error("must not run");
    },
  });
  assert.deepEqual(await resolver.resolve([null, { id: "bad id" }]), [null, null]);
  await assert.rejects(resolver.resolve(null), /must be an array/);
});

test("one cancelled view cannot poison a shared request for a fresh view", async () => {
  const controller = new AbortController();
  const calls = [];
  const releases = new Map();
  const resolver = new ScriptureExcerptResolver({
    maximumConcurrency: 1,
    loadChapter: async (request) => {
      calls.push(request.chapter);
      await new Promise((resolve) => {
        releases.set(request.chapter, resolve);
      });
      return chapterVerses(request);
    },
  });

  const stale = resolver.resolve([
    target("one", 1, 1),
    target("two", 2, 1),
  ], { signal: controller.signal });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [1]);
  controller.abort();
  const fresh = resolver.resolve([target("fresh", 2, 2)]);
  releases.get(1)();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [1, 2]);
  releases.get(2)();

  assert.deepEqual(await stale, [null, null]);
  assert.equal((await fresh)[0].text, "KJV text 2:2");
  assert.deepEqual(calls, [1, 2]);
});

test("does not start unshared queued chapters after a view is cancelled", async () => {
  const controller = new AbortController();
  const calls = [];
  let release;
  const resolver = new ScriptureExcerptResolver({
    maximumConcurrency: 1,
    loadChapter: async (request) => {
      calls.push(request.chapter);
      await new Promise((resolve) => {
        release = resolve;
      });
      return chapterVerses(request);
    },
  });

  const pending = resolver.resolve([
    target("one", 1, 1),
    target("two", 2, 1),
    target("three", 3, 1),
  ], { signal: controller.signal });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  release();

  assert.deepEqual(await pending, [null, null, null]);
  assert.deepEqual(calls, [1]);
});

test("a fresh view replaces an entry that already began cancellation", async () => {
  const controller = new AbortController();
  const calls = [];
  let releaseFirst;
  const resolver = new ScriptureExcerptResolver({
    maximumConcurrency: 1,
    loadChapter: async (request) => {
      calls.push(request.chapter);
      if (request.chapter === 1) {
        await new Promise((resolve) => {
          releaseFirst = resolve;
        });
      }
      return chapterVerses(request);
    },
  });

  const stale = resolver.resolve([
    target("active", 1, 1),
    target("abandoned", 2, 1),
  ], { signal: controller.signal });
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  releaseFirst();
  // Enter the queued entry's liveness-check/rejection window before the old
  // promise has necessarily completed its finally chain.
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  const fresh = resolver.resolve([target("fresh", 2, 2)]);

  assert.deepEqual(await stale, [null, null]);
  assert.equal((await fresh)[0].text, "KJV text 2:2");
  assert.deepEqual(calls, [1, 2]);
});
