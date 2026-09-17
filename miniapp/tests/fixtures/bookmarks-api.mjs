import { createHash } from "node:crypto";

import { globalBookmarkCatalogFromApi } from "../../lib/global-bookmark-catalog.js";

/**
 * A small catalogue in the exact shape the public Bookmarks API v1 publishes.
 *
 * `bookmarksApiDocuments()` renders `all.json` to bytes and derives
 * `index.json` from them, so `index.checksum` is the real SHA-256 of the
 * served body and a client that verifies downloads accepts these documents
 * exactly as it would the production ones.
 */
export const BOOKMARKS_API_TOPICS = Object.freeze([
  topic("biblical-love", "Biblical Love", "#a16207", [
    [43, 3, 16],
    [43, 13, 34],
    [46, 13, 4],
    [62, 4, 8],
  ]),
  topic("blessings-and-curses", "Blessings and Curses", "#fde68a", [
    [5, 28, 1],
    [5, 28, 15],
  ], { aliases: ["Blessings & Curses"] }),
  topic("fear-not", "Fear Not", "#fef08a", [
    [23, 41, 10],
    [43, 14, 27],
  ]),
  topic("gods-judgment", "God's Judgment", "#fb7185", [
    [45, 2, 2],
    [58, 9, 27],
  ], { aliases: ["God's Judgement"] }),
  topic("grace", "Grace", "#bbf7d0", [
    [43, 1, 17],
    [43, 3, 16],
    [45, 5, 20],
    [49, 2, 8],
  ]),
  topic("spiritual-rebirth", "Spiritual Rebirth", "#6ee7b7", [
    [43, 3, 3],
    [43, 3, 4],
    [43, 3, 5],
    [60, 1, 23],
  ]),
  topic("word-of-god", "Word of God", "#7dd3fc", [
    [19, 119, 105],
    [43, 1, 1],
    [58, 4, 12],
  ]),
]);

export const BOOKMARKS_API_LOCALES = Object.freeze({
  af: Object.freeze({
    name: "Afrikaans",
    topics: Object.freeze({
      "biblical-love": "Bybelse liefde",
      "blessings-and-curses": "Seëninge en vloeke",
      "fear-not": "Moenie vrees nie",
      "gods-judgment": "God se oordeel",
      grace: "Genade",
      "spiritual-rebirth": "Geestelike wedergeboorte",
      "word-of-god": "Woord van God",
    }),
  }),
  de: Object.freeze({
    name: "German",
    topics: Object.freeze({
      grace: "Gnade",
      "word-of-god": "Wort Gottes",
    }),
  }),
  "zh-hant": Object.freeze({
    name: null,
    topics: Object.freeze({
      grace: "恩典",
    }),
  }),
});

export function bookmarksApiDocuments({
  topics = BOOKMARKS_API_TOPICS,
  locales = BOOKMARKS_API_LOCALES,
  catalogVersion = 3,
} = {}) {
  const catalogTopics = topics.map((entry) => ({
    id: entry.id,
    name: entry.name,
    color: entry.color,
    aliases: [...entry.aliases],
    default: entry.default,
    verses: entry.verses.map((coordinate) => [...coordinate]),
  }));
  const localeDocuments = {
    en: {
      schema_version: 1,
      locale: "en",
      name: "English",
      topics: Object.fromEntries(
        catalogTopics.map((entry) => [entry.id, entry.name]),
      ),
    },
  };
  for (const [code, locale] of Object.entries(locales)) {
    localeDocuments[code] = {
      schema_version: 1,
      locale: code,
      ...(locale.name === null || locale.name === undefined
        ? {}
        : { name: locale.name }),
      topics: { ...locale.topics },
    };
  }
  const all = {
    schema_version: 1,
    topics: catalogTopics,
    locales: localeDocuments,
  };
  const allBytes = Buffer.from(`${JSON.stringify(all, null, 2)}\n`, "utf8");
  const checksum = createHash("sha256").update(allBytes).digest("hex");
  const index = {
    schema_version: 1,
    catalog_version: catalogVersion,
    checksum,
    counts: {
      topics: catalogTopics.length,
      verses: catalogTopics.reduce((total, entry) => total + entry.verses.length, 0),
      locales: Object.keys(localeDocuments).length,
    },
    resources: {
      index: "index.json",
      catalog: "catalog.json",
      all: "all.json",
      topics: "topics.json",
      topic: "topics/{id}.json",
      locales: "locales.json",
      locale: "locales/{locale}.json",
      checksums: "checksums.json",
    },
    locales: Object.keys(localeDocuments).sort(),
  };
  const indexBytes = Buffer.from(`${JSON.stringify(index, null, 2)}\n`, "utf8");
  return { all, allBytes, checksum, index, indexBytes };
}

export const BOOKMARKS_API_FIXTURE = bookmarksApiDocuments();

export function bookmarksApiCatalog(fixture = BOOKMARKS_API_FIXTURE) {
  return globalBookmarkCatalogFromApi(fixture.all, fixture.index);
}

export function bookmarksApiTopicCount(fixture = BOOKMARKS_API_FIXTURE) {
  return fixture.all.topics.length;
}

export function bookmarksApiAssignmentCount(fixture = BOOKMARKS_API_FIXTURE) {
  return fixture.index.counts.verses;
}

export function bookmarksApiTopicVerseCount(topicId, fixture = BOOKMARKS_API_FIXTURE) {
  const entry = fixture.all.topics.find((candidate) => candidate.id === topicId);
  if (!entry) {
    throw new TypeError(`Unknown fixture topic: ${topicId}.`);
  }
  return entry.verses.length;
}

function topic(id, name, color, verses, { aliases = [], defaultTopic = true } = {}) {
  return Object.freeze({
    id,
    name,
    color,
    aliases: Object.freeze([...aliases]),
    default: defaultTopic,
    verses: Object.freeze(verses.map((coordinate) => Object.freeze([...coordinate]))),
  });
}
