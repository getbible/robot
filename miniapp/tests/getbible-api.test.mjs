import assert from "node:assert/strict";
import test from "node:test";

import { GetBibleApi } from "../lib/getbible-api.js";
import {
  BrowserPublicCache,
  MemoryPublicStore,
} from "../lib/public-cache.js";

class FakeTransport {
  constructor() {
    this.calls = [];
  }

  async sha(path) {
    this.calls.push(["sha", path]);
    if (path === "kjv.sha") {
      return "1".repeat(40);
    }
    if (path === "kjv/43.sha") {
      return "2".repeat(40);
    }
    if (path === "kjv/44.sha") {
      return "3".repeat(40);
    }
    throw new Error(`Unexpected sha: ${path}`);
  }

  async json(path) {
    this.calls.push(["json", path]);
    if (path === "translations.json") {
      return {
        kjv: {
          abbreviation: "kjv",
          translation: "King James Version",
          language: "English",
          lang: "en",
        },
      };
    }
    if (path === "kjv/books.json") {
      return {
        43: { nr: 43, abbreviation: "kjv", name: "John", sha: "2".repeat(40) },
        44: { nr: 44, abbreviation: "kjv", name: "Acts", sha: "3".repeat(40) },
      };
    }
    if (path === "kjv/43/chapters.json") {
      return {
        3: { chapter: 3, verses: 36 },
        4: { chapter: 4, verses: 54 },
      };
    }
    if (path === "kjv/44/chapters.json") {
      return { 1: { chapter: 1, verses: 26 } };
    }
    throw new Error(`Unexpected json: ${path}`);
  }

  async consistentJson(jsonPath, shaPath) {
    this.calls.push(["consistent", jsonPath, shaPath]);
    return {
      sha: "4".repeat(40),
      payload: {
        abbreviation: "kjv",
        translation: "King James Version",
        book_nr: 43,
        book_name: "John",
        chapter: 4,
        name: "John 4",
        verses: [
          { verse: 1, text: "When therefore the Lord knew." },
          { verse: 2, text: "Though Jesus himself baptized not." },
        ],
      },
    };
  }

  async query(translation, references) {
    this.calls.push(["query", translation, references]);
    return {
      abbreviation: translation,
      books: [{
        book_nr: 43,
        book_name: "John",
        chapter: 3,
        verses: [{ verse: 16, name: "John 3:16", text: "Love" }],
      }],
    };
  }
}

function createApi() {
  let now = 1_000;
  const transport = new FakeTransport();
  const cache = new BrowserPublicCache({
    store: new MemoryPublicStore(),
    now: () => now,
  });
  return {
    api: new GetBibleApi({
      transport,
      cache,
      now: () => now,
      revalidateAfterMs: 60_000,
    }),
    transport,
    advance(milliseconds) {
      now += milliseconds;
    },
  };
}

test("fresh public catalogs are served from persistent cache", async () => {
  const { api, transport } = createApi();

  const first = await api.translations();
  const second = await api.translations();

  assert.deepEqual(second, first);
  assert.deepEqual(
    transport.calls.filter(([kind]) => kind === "json"),
    [["json", "translations.json"]],
  );
});

test("stale scope with unchanged checksum is touched without redownload", async () => {
  const { api, transport, advance } = createApi();
  await api.books("kjv");
  transport.calls.length = 0;
  advance(60_001);

  const books = await api.books("kjv");

  assert.equal(books.items.length, 2);
  assert.deepEqual(transport.calls, [["sha", "kjv.sha"]]);
});

test("chapter reads remain browser-direct and calculate adjacent navigation", async () => {
  const { api, transport } = createApi();

  const scripture = await api.chapter("kjv", 43, 4, 2);
  const callsAfterFirst = transport.calls.length;
  const cached = await api.chapter("kjv", 43, 4, 1);

  assert.equal(scripture.target_verse, 2);
  assert.deepEqual(scripture.navigation.previous, {
    book: 43,
    book_name: "John",
    chapter: 3,
  });
  assert.deepEqual(scripture.navigation.next, {
    book: 44,
    book_name: "Acts",
    chapter: 1,
  });
  assert.equal(cached.target_verse, 1);
  assert.equal(transport.calls.length, callsAfterFirst);
  assert.ok(
    transport.calls.some(([kind, path]) =>
      kind === "consistent" && path === "kjv/43/4.json"),
  );
});

test("reference resolution uses query API without persisting search content", async () => {
  const { api, transport } = createApi();

  const target = await api.resolveReference("kjv", "John 3:16");

  assert.equal(target.book_number, 43);
  assert.equal(target.verse, 16);
  assert.deepEqual(
    transport.calls.at(-1),
    ["query", "kjv", "John 3:16"],
  );
});
