import { BOOK_CHAPTER_COUNTS } from "./bible-canon.js";
import { isLegacyBookmarkTopicId } from "./bookmark-store.js";

const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
// The public Bookmarks API v1 contract: ids, English names, colours, aliases
// and locale codes are validated exactly as the API publishes them so a
// document that would not pass the builder never reaches the reader.
const CANONICAL_TOPIC_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const ENGLISH_TOPIC_PATTERN = /^[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const CHECKSUM_PATTERN = /^[a-f0-9]{64}$/;
const LOCALE_PATTERN = /^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/;
const GLOBAL_SOURCE = "global";
const GLOBAL_TRANSLATION_FALLBACK = "kjv";
const SUPPORTED_SCHEMA_VERSION = 1;
const MAX_TOPIC_ID_LENGTH = 80;
const MAX_TOPIC_NAME_LENGTH = 80;
const MAX_TRANSLATED_NAME_LENGTH = 120;
const MAX_LOCALE_LENGTH = 16;
const MAX_TOPIC_ALIASES = 20;
const MAX_GLOBAL_BOOKMARK_TOPICS = 1_000;
const MAX_GLOBAL_BOOKMARK_ASSIGNMENTS = 100_000;
const MAX_GLOBAL_BOOKMARK_LOCALES = 500;
const MAX_VERSE = 2_000;

const BOOK_NAMES = Object.freeze([
  "Genesis",
  "Exodus",
  "Leviticus",
  "Numbers",
  "Deuteronomy",
  "Joshua",
  "Judges",
  "Ruth",
  "1 Samuel",
  "2 Samuel",
  "1 Kings",
  "2 Kings",
  "1 Chronicles",
  "2 Chronicles",
  "Ezra",
  "Nehemiah",
  "Esther",
  "Job",
  "Psalms",
  "Proverbs",
  "Ecclesiastes",
  "Song of Solomon",
  "Isaiah",
  "Jeremiah",
  "Lamentations",
  "Ezekiel",
  "Daniel",
  "Hosea",
  "Joel",
  "Amos",
  "Obadiah",
  "Jonah",
  "Micah",
  "Nahum",
  "Habakkuk",
  "Zephaniah",
  "Haggai",
  "Zechariah",
  "Malachi",
  "Matthew",
  "Mark",
  "Luke",
  "John",
  "Acts",
  "Romans",
  "1 Corinthians",
  "2 Corinthians",
  "Galatians",
  "Ephesians",
  "Philippians",
  "Colossians",
  "1 Thessalonians",
  "2 Thessalonians",
  "1 Timothy",
  "2 Timothy",
  "Titus",
  "Philemon",
  "Hebrews",
  "James",
  "1 Peter",
  "2 Peter",
  "1 John",
  "2 John",
  "3 John",
  "Jude",
  "Revelation",
]);

/**
 * Immutable, translation-independent topic-to-verse associations published
 * by the public Bookmarks API.
 *
 * The catalogue deliberately owns no user state. A separate browser-local
 * preference records which topics are visible on this device, and the
 * catalogue is only ever replaced whole by a document whose SHA-256 the API's
 * own index vouched for. Nothing is bundled with the application any more:
 * `EMPTY_GLOBAL_BOOKMARK_CATALOG` stands in until a verified document exists.
 */
export class GlobalBookmarkCatalog {
  #assignmentsById = new Map();
  #assignmentsByTopic = new Map();
  #assignmentsByVerse = new Map();
  #bookmarkIds;
  #bookNames;
  #topics;
  #topicsById = new Map();

  constructor(document, { bookNames = BOOK_NAMES } = {}) {
    if (
      !document ||
      typeof document !== "object" ||
      Array.isArray(document) ||
      document.schema_version !== SUPPORTED_SCHEMA_VERSION ||
      !Number.isSafeInteger(document.catalog_version) ||
      document.catalog_version < 0 ||
      !Array.isArray(document.topics) ||
      document.topics.length > MAX_GLOBAL_BOOKMARK_TOPICS ||
      (
        document.checksum !== undefined &&
        document.checksum !== null &&
        (
          typeof document.checksum !== "string" ||
          !CHECKSUM_PATTERN.test(document.checksum)
        )
      )
    ) {
      throw new TypeError("The global bookmark catalogue is invalid.");
    }
    if (!Array.isArray(bookNames) || bookNames.length > BOOK_CHAPTER_COUNTS.length) {
      throw new TypeError("The global bookmark catalogue metadata is invalid.");
    }
    this.version = document.catalog_version;
    this.checksum = document.checksum ?? null;
    this.#bookNames = Object.freeze(bookNames.map((name) => boundedText(name, 80)));
    this.#topics = Object.freeze(
      document.topics
        .map((definition) => Object.freeze(normalizeTopicDefinition(definition)))
        .sort((left, right) => left.id.localeCompare(right.id, "en")),
    );
    for (const definition of this.#topics) {
      if (this.#topicsById.has(definition.id)) {
        throw new TypeError("The global bookmark catalogue has duplicate topics.");
      }
      this.#topicsById.set(definition.id, definition);
    }
    for (const definition of document.topics) {
      const coordinates = definition.verses;
      if (!Array.isArray(coordinates)) {
        throw new TypeError("A global bookmark topic has invalid verse associations.");
      }
      const topicId = boundedText(definition.id, MAX_TOPIC_ID_LENGTH);
      const topicAssignments = [];
      const seen = new Set();
      for (const coordinate of coordinates) {
        if (this.#assignmentsById.size >= MAX_GLOBAL_BOOKMARK_ASSIGNMENTS) {
          throw new TypeError("The global bookmark catalogue is too large.");
        }
        const normalized = normalizeCoordinate(coordinate, this.#bookNames.length);
        const coordinateKey = verseKey(normalized);
        if (seen.has(coordinateKey)) {
          throw new TypeError("The global bookmark catalogue has duplicate entries.");
        }
        seen.add(coordinateKey);
        const assignment = Object.freeze({
          topic_id: topicId,
          ...normalized,
        });
        const id = globalBookmarkId(assignment);
        if (this.#assignmentsById.has(id)) {
          throw new TypeError("The global bookmark catalogue has duplicate entries.");
        }
        this.#assignmentsById.set(id, assignment);
        topicAssignments.push(assignment);
        const verseAssignments = this.#assignmentsByVerse.get(coordinateKey) ?? [];
        verseAssignments.push(assignment);
        this.#assignmentsByVerse.set(coordinateKey, verseAssignments);
      }
      // Sorted, unique coordinates are what the API promises; the order is
      // repeated here so a document that arrives unsorted still renders the
      // same list as one that does.
      topicAssignments.sort(compareAssignments);
      this.#assignmentsByTopic.set(topicId, Object.freeze(topicAssignments));
    }
    for (const assignments of this.#assignmentsByVerse.values()) {
      assignments.sort((left, right) =>
        left.topic_id.localeCompare(right.topic_id, "en")
      );
    }
    this.assignmentCount = this.#assignmentsById.size;
    this.uniqueVerseCount = this.#assignmentsByVerse.size;
    this.#bookmarkIds = Object.freeze([...this.#assignmentsById.keys()].sort());
    Object.freeze(this);
  }

  get topicCount() {
    return this.#topics.length;
  }

  bookmarkIds() {
    return this.#bookmarkIds;
  }

  hasBookmarkId(id) {
    return typeof id === "string" && this.#assignmentsById.has(id);
  }

  topicDefinitions({ defaultsOnly = false } = {}) {
    return this.#topics
      .filter((definition) => !defaultsOnly || definition.default)
      .map(cloneTopicDefinition);
  }

  resolveTopics(localTopics, topicMappings = null) {
    if (!Array.isArray(localTopics)) {
      throw new TypeError("Bookmark topics are invalid.");
    }
    const available = localTopics.map((local) => ({
      id: boundedText(local?.id, 128),
      name: boundedText(local?.name, 80),
    }));
    const mappedTopicIds = normalizeTopicMappings(topicMappings);
    const used = new Set();
    const resolved = new Map();
    for (const definition of this.#topics) {
      const mappedLocalId = mappedTopicIds.get(definition.id);
      let match = mappedLocalId
        ? available.find((local) =>
          local.id === mappedLocalId && !used.has(local.id)
        )
        : null;
      if (!match) {
        match = available.find((local) =>
          local.id === definition.id && !used.has(local.id)
        );
      }
      if (!match) {
        const names = new Set(
          [definition.name, ...definition.aliases].map(normalizedTopicName),
        );
        match = available.find((local) =>
          isLegacyBookmarkTopicId(local.id) &&
          !used.has(local.id) &&
          names.has(normalizedTopicName(local.name))
        );
      }
      if (match) {
        used.add(match.id);
        resolved.set(definition.id, match.id);
      }
    }
    return resolved;
  }

  canonicalTopicId(localTopicId, localTopics, topicMappings = null) {
    for (const [canonicalId, resolvedLocalId] of this.resolveTopics(
      localTopics,
      topicMappings,
    )) {
      if (resolvedLocalId === localTopicId) {
        return canonicalId;
      }
    }
    return null;
  }

  topicDefinition(canonicalTopicId) {
    const definition = this.#topicsById.get(canonicalTopicId);
    return definition ? cloneTopicDefinition(definition) : null;
  }

  topicDefinitionForLocalTopic(
    localTopicId,
    localTopics,
    topicMappings = null,
  ) {
    const canonicalTopicId = this.canonicalTopicId(
      localTopicId,
      localTopics,
      topicMappings,
    );
    return canonicalTopicId
      ? this.topicDefinition(canonicalTopicId)
      : null;
  }

  bookmarksForTopic(localTopicId, localTopics, topicMappings = null) {
    const canonicalId = this.canonicalTopicId(
      localTopicId,
      localTopics,
      topicMappings,
    );
    if (!canonicalId) {
      return [];
    }
    return this.bookmarksForCanonicalTopic(canonicalId, localTopicId);
  }

  /**
   * Returns one already-resolved canonical topic without repeating the
   * canonical-to-local matching pass. Renderers that resolve the whole topic
   * map once can therefore classify every global coordinate in linear time.
   */
  bookmarksForCanonicalTopic(canonicalTopicId, localTopicId) {
    if (
      typeof canonicalTopicId !== "string" ||
      !CANONICAL_TOPIC_ID_PATTERN.test(canonicalTopicId) ||
      !this.#topicsById.has(canonicalTopicId) ||
      typeof localTopicId !== "string" ||
      !ID_PATTERN.test(localTopicId)
    ) {
      throw new TypeError("Global bookmark topic identifiers are invalid.");
    }
    return this.#assignmentsByTopic.get(canonicalTopicId).map((assignment) =>
      this.#bookmark(assignment, localTopicId)
    );
  }

  bookmarksForVerse(verse, localTopics, topicMappings = null) {
    const coordinate = normalizeCoordinate([
      verse?.book ?? verse?.book_number,
      verse?.chapter,
      verse?.verse,
    ], this.#bookNames.length);
    const resolved = this.resolveTopics(localTopics, topicMappings);
    return (this.#assignmentsByVerse.get(verseKey(coordinate)) ?? [])
      .map((assignment) => {
        const localTopicId = resolved.get(assignment.topic_id);
        return localTopicId ? this.#bookmark(assignment, localTopicId) : null;
      })
      .filter(Boolean);
  }

  bookmarkById(id, localTopics, topicMappings = null) {
    const assignment = this.#assignmentsById.get(id);
    if (!assignment) {
      return null;
    }
    const localTopicId = this.resolveTopics(
      localTopics,
      topicMappings,
    ).get(assignment.topic_id);
    return localTopicId ? this.#bookmark(assignment, localTopicId) : null;
  }

  assignmentCountForTopics(localTopics, topicMappings = null) {
    const resolved = this.resolveTopics(localTopics, topicMappings);
    let count = 0;
    for (const canonicalId of resolved.keys()) {
      count += this.#assignmentsByTopic.get(canonicalId)?.length ?? 0;
    }
    return count;
  }

  assignmentCountForCanonicalTopics(canonicalTopicIds) {
    if (!Array.isArray(canonicalTopicIds)) {
      throw new TypeError("Global bookmark topic identifiers are invalid.");
    }
    let count = 0;
    for (const canonicalId of new Set(canonicalTopicIds)) {
      if (!this.#topicsById.has(canonicalId)) {
        continue;
      }
      count += this.#assignmentsByTopic.get(canonicalId)?.length ?? 0;
    }
    return count;
  }

  #bookmark(assignment, localTopicId) {
    const bookName = this.#bookNames[assignment.book - 1];
    return {
      id: globalBookmarkId(assignment),
      source: GLOBAL_SOURCE,
      catalog_topic_id: assignment.topic_id,
      topic_id: localTopicId,
      translation: GLOBAL_TRANSLATION_FALLBACK,
      reference: `${bookName} ${assignment.chapter}:${assignment.verse}`,
      book: assignment.book,
      book_name: bookName,
      chapter: assignment.chapter,
      verse: assignment.verse,
      text: "",
      created_at: 0,
      updated_at: 0,
    };
  }
}

export const GLOBAL_BOOKMARK_SOURCE = GLOBAL_SOURCE;
export const GLOBAL_BOOKMARK_CATALOG_SCHEMA_VERSION = SUPPORTED_SCHEMA_VERSION;
export const MAX_GLOBAL_BOOKMARK_CATALOG_TOPICS = MAX_GLOBAL_BOOKMARK_TOPICS;
export const MAX_GLOBAL_BOOKMARK_CATALOG_ASSIGNMENTS = MAX_GLOBAL_BOOKMARK_ASSIGNMENTS;

/**
 * The catalogue the application holds before a verified API document exists:
 * version 0, no topics, no assignments. Every reader of the catalogue can rely
 * on its shape, so "no catalogue yet" is a state rather than a null check.
 */
export const EMPTY_GLOBAL_BOOKMARK_CATALOG = new GlobalBookmarkCatalog({
  schema_version: SUPPORTED_SCHEMA_VERSION,
  catalog_version: 0,
  checksum: null,
  topics: [],
});

/**
 * Turns the API's `all.json` and `index.json` into the plain catalogue
 * document the constructor accepts: one entry per topic carrying its verse
 * coordinates and every published translated name. The result is
 * JSON-compatible so a verified download can be stored as-is and reopened
 * later without touching the network.
 */
export function globalBookmarkCatalogDocumentFromApi(all, index) {
  if (
    !all ||
    typeof all !== "object" ||
    Array.isArray(all) ||
    all.schema_version !== SUPPORTED_SCHEMA_VERSION ||
    !Array.isArray(all.topics) ||
    all.topics.length > MAX_GLOBAL_BOOKMARK_TOPICS ||
    !all.locales ||
    typeof all.locales !== "object" ||
    Array.isArray(all.locales)
  ) {
    throw new TypeError("The Bookmarks API catalogue document is invalid.");
  }
  if (
    !index ||
    typeof index !== "object" ||
    Array.isArray(index) ||
    index.schema_version !== SUPPORTED_SCHEMA_VERSION ||
    !Number.isSafeInteger(index.catalog_version) ||
    index.catalog_version < 1 ||
    typeof index.checksum !== "string" ||
    !CHECKSUM_PATTERN.test(index.checksum)
  ) {
    throw new TypeError("The Bookmarks API index document is invalid.");
  }
  const localeEntries = Object.entries(all.locales);
  if (localeEntries.length > MAX_GLOBAL_BOOKMARK_LOCALES) {
    throw new TypeError("The Bookmarks API catalogue document is invalid.");
  }
  const namesByTopic = new Map();
  for (const [code, locale] of localeEntries) {
    if (
      typeof code !== "string" ||
      code.length > MAX_LOCALE_LENGTH ||
      !LOCALE_PATTERN.test(code) ||
      !locale ||
      typeof locale !== "object" ||
      Array.isArray(locale) ||
      locale.schema_version !== SUPPORTED_SCHEMA_VERSION ||
      locale.locale !== code ||
      !locale.topics ||
      typeof locale.topics !== "object" ||
      Array.isArray(locale.topics)
    ) {
      throw new TypeError("A Bookmarks API locale document is invalid.");
    }
    for (const [topicId, name] of Object.entries(locale.topics)) {
      if (
        typeof name !== "string" ||
        name.trim().length === 0 ||
        name.length > MAX_TRANSLATED_NAME_LENGTH
      ) {
        throw new TypeError("A Bookmarks API locale document is invalid.");
      }
      const names = namesByTopic.get(topicId) ?? {};
      names[code] = name;
      namesByTopic.set(topicId, names);
    }
  }
  return {
    schema_version: SUPPORTED_SCHEMA_VERSION,
    catalog_version: index.catalog_version,
    checksum: index.checksum,
    topics: all.topics.map((topic) => {
      if (!topic || typeof topic !== "object" || Array.isArray(topic)) {
        throw new TypeError("A Bookmarks API topic is invalid.");
      }
      const names = { ...(namesByTopic.get(topic.id) ?? {}) };
      if (typeof names.en !== "string") {
        names.en = topic.name;
      }
      return {
        id: topic.id,
        name: topic.name,
        color: topic.color,
        aliases: Array.isArray(topic.aliases) ? [...topic.aliases] : topic.aliases,
        default: topic.default,
        names,
        verses: Array.isArray(topic.verses)
          ? topic.verses.map((coordinate) =>
            Array.isArray(coordinate) ? [...coordinate] : coordinate
          )
          : topic.verses,
      };
    }),
  };
}

export function globalBookmarkCatalogFromApi(all, index) {
  return new GlobalBookmarkCatalog(globalBookmarkCatalogDocumentFromApi(all, index));
}

/**
 * The name a topic shows in one interface locale: the exact locale, then its
 * base language, then the API's English name, then the canonical name. The
 * API never infers a language or fills gaps, so the fallback lives here.
 */
export function globalBookmarkTopicName(definition, locale) {
  if (!definition || typeof definition !== "object") {
    return "";
  }
  const names = definition.names && typeof definition.names === "object"
    ? definition.names
    : {};
  const normalizedLocale = typeof locale === "string"
    ? locale.trim().toLocaleLowerCase("en")
    : "";
  const candidates = [normalizedLocale];
  const base = normalizedLocale.split("-")[0];
  if (base && base !== normalizedLocale) {
    candidates.push(base);
  }
  candidates.push("en");
  for (const candidate of candidates) {
    if (
      candidate &&
      Object.hasOwn(names, candidate) &&
      typeof names[candidate] === "string" &&
      names[candidate].trim().length > 0
    ) {
      return names[candidate];
    }
  }
  return typeof definition.name === "string" ? definition.name : "";
}

function normalizeTopicDefinition(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  const id = boundedText(value.id, MAX_TOPIC_ID_LENGTH);
  const name = boundedText(value.name, MAX_TOPIC_NAME_LENGTH).normalize("NFC");
  const color = boundedText(value.color, 7).toLowerCase();
  if (
    !CANONICAL_TOPIC_ID_PATTERN.test(id) ||
    !ENGLISH_TOPIC_PATTERN.test(name) ||
    !COLOR_PATTERN.test(color) ||
    typeof value.default !== "boolean" ||
    !Array.isArray(value.aliases) ||
    value.aliases.length > MAX_TOPIC_ALIASES
  ) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  const aliases = value.aliases.map((alias) =>
    boundedText(alias, MAX_TOPIC_NAME_LENGTH).normalize("NFC")
  );
  if (
    aliases.some((alias) => !ENGLISH_TOPIC_PATTERN.test(alias)) ||
    new Set(aliases.map((alias) => alias.toLocaleLowerCase("en"))).size !==
      aliases.length
  ) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  return {
    id,
    name,
    // Retained for callers that still address a topic by its message key;
    // the translated names now travel with the definition itself.
    name_key: `bookmark_topics.${id}`,
    color,
    aliases: Object.freeze(aliases),
    default: value.default,
    names: Object.freeze(normalizeTopicNames(value.names, name)),
  };
}

function normalizeTopicNames(value, englishName) {
  const names = { en: englishName };
  if (value === undefined || value === null) {
    return names;
  }
  if (typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  const entries = Object.entries(value);
  if (entries.length > MAX_GLOBAL_BOOKMARK_LOCALES) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  for (const [code, name] of entries) {
    if (
      typeof code !== "string" ||
      code.length > MAX_LOCALE_LENGTH ||
      !LOCALE_PATTERN.test(code)
    ) {
      throw new TypeError("A global bookmark topic is invalid.");
    }
    names[code] = boundedText(name, MAX_TRANSLATED_NAME_LENGTH).normalize("NFC");
  }
  return names;
}

function normalizeCoordinate(value, bookCount) {
  if (!Array.isArray(value) || value.length !== 3) {
    throw new TypeError("A global bookmark coordinate is invalid.");
  }
  const [book, chapter, verse] = value;
  if (
    !Number.isInteger(book) ||
    book < 1 ||
    book > bookCount ||
    !Number.isInteger(chapter) ||
    chapter < 1 ||
    chapter > BOOK_CHAPTER_COUNTS[book - 1] ||
    !Number.isInteger(verse) ||
    verse < 1 ||
    verse > MAX_VERSE
  ) {
    throw new TypeError("A global bookmark coordinate is invalid.");
  }
  return { book, chapter, verse };
}

function compareAssignments(left, right) {
  return left.book - right.book ||
    left.chapter - right.chapter ||
    left.verse - right.verse;
}

function globalBookmarkId(assignment) {
  return `global_${assignment.topic_id}_${assignment.book}_${assignment.chapter}_${assignment.verse}`;
}

function verseKey(value) {
  return `${value.book}/${value.chapter}/${value.verse}`;
}

function normalizedTopicName(value) {
  return String(value)
    .trim()
    .normalize("NFC")
    .toLocaleLowerCase()
    .replace(/[’]/gu, "'")
    .replace(/\s+/gu, " ");
}

function normalizeTopicMappings(value) {
  if (value === null || value === undefined) {
    return new Map();
  }
  const entries = value instanceof Map
    ? [...value]
    : value && typeof value === "object" && !Array.isArray(value)
      ? Object.entries(value)
      : null;
  if (!entries || entries.length > 100) {
    throw new TypeError("Global bookmark topic mappings are invalid.");
  }
  const mappings = new Map();
  const localTopicIds = new Set();
  for (const [canonicalValue, localValue] of entries) {
    const canonicalId = boundedText(canonicalValue, 128);
    const localId = boundedText(localValue, 128);
    if (
      !ID_PATTERN.test(canonicalId) ||
      !ID_PATTERN.test(localId) ||
      mappings.has(canonicalId) ||
      localTopicIds.has(localId)
    ) {
      throw new TypeError("Global bookmark topic mappings are invalid.");
    }
    mappings.set(canonicalId, localId);
    localTopicIds.add(localId);
  }
  return mappings;
}

function boundedText(value, maximum) {
  if (typeof value !== "string") {
    throw new TypeError("Global bookmark text is invalid.");
  }
  const normalized = value.trim();
  if (normalized.length === 0 || normalized.length > maximum) {
    throw new TypeError("Global bookmark text is invalid.");
  }
  return normalized;
}

function cloneTopicDefinition(value) {
  return {
    id: value.id,
    name: value.name,
    name_key: value.name_key,
    color: value.color,
    aliases: [...value.aliases],
    default: value.default,
    names: { ...value.names },
  };
}
