import { GLOBAL_BOOKMARK_DATA } from "./global-bookmark-data.js";
import {
  CORE_BOOKMARK_TOPIC_DEFINITIONS,
  isLegacyBookmarkTopicId,
} from "./bookmark-topic-definitions.js";

const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const GLOBAL_SOURCE = "global";
const GLOBAL_TRANSLATION_FALLBACK = "kjv";
const SUPPORTED_SCHEMA_VERSION = 1;
const MAX_GLOBAL_BOOKMARK_ASSIGNMENTS = 10_000;

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
 * Immutable, translation-independent topic-to-verse associations.
 *
 * The catalogue deliberately owns no user state. A separate browser-local
 * preference records which topic overlays are visible. A future authenticated
 * publisher can therefore replace the catalogue provider without changing
 * personal bookmark storage, Telegram sync, or backups.
 */
export class GlobalBookmarkCatalog {
  #assignmentsById = new Map();
  #assignmentsByTopic = new Map();
  #assignmentsByVerse = new Map();
  #bookmarkIds;
  #bookNames;
  #topics;
  #topicsById = new Map();

  constructor({
    data = GLOBAL_BOOKMARK_DATA,
    topics = CORE_BOOKMARK_TOPIC_DEFINITIONS,
    bookNames = BOOK_NAMES,
  } = {}) {
    if (
      !data ||
      typeof data !== "object" ||
      Array.isArray(data) ||
      data.schema_version !== SUPPORTED_SCHEMA_VERSION ||
      !data.bookmarks_by_topic ||
      typeof data.bookmarks_by_topic !== "object" ||
      Array.isArray(data.bookmarks_by_topic)
    ) {
      throw new TypeError("The global bookmark catalogue is invalid.");
    }
    if (!Array.isArray(topics) || !Array.isArray(bookNames)) {
      throw new TypeError("The global bookmark catalogue metadata is invalid.");
    }
    this.version = data.schema_version;
    this.#bookNames = Object.freeze(bookNames.map((name) => boundedText(name, 80)));
    this.#topics = Object.freeze(topics.map((definition) =>
      Object.freeze(normalizeTopicDefinition(definition))
    ));
    for (const definition of this.#topics) {
      if (this.#topicsById.has(definition.id)) {
        throw new TypeError("The global bookmark catalogue has duplicate topics.");
      }
      this.#topicsById.set(definition.id, definition);
    }

    const dataTopicIds = Object.keys(data.bookmarks_by_topic);
    if (
      dataTopicIds.length !== this.#topics.length ||
      dataTopicIds.some((id) => !this.#topicsById.has(id))
    ) {
      throw new TypeError("The global bookmark catalogue topics do not match.");
    }
    for (const definition of this.#topics) {
      const coordinates = data.bookmarks_by_topic[definition.id];
      if (!Array.isArray(coordinates) || coordinates.length === 0) {
        throw new TypeError("A global bookmark topic has no verse associations.");
      }
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
          topic_id: definition.id,
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
      this.#assignmentsByTopic.set(
        definition.id,
        Object.freeze(topicAssignments),
      );
    }
    this.assignmentCount = this.#assignmentsById.size;
    this.uniqueVerseCount = this.#assignmentsByVerse.size;
    this.#bookmarkIds = Object.freeze([...this.#assignmentsById.keys()].sort());
    Object.freeze(this);
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
    return this.#assignmentsByTopic.get(canonicalId).map((assignment) =>
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

export const GLOBAL_BOOKMARK_CATALOG = new GlobalBookmarkCatalog();
export const GLOBAL_BOOKMARK_SOURCE = GLOBAL_SOURCE;
export const GLOBAL_BOOKMARK_CATALOG_VERSION = GLOBAL_BOOKMARK_CATALOG.version;
export const GLOBAL_BOOKMARK_TOPIC_DEFINITIONS = Object.freeze(
  GLOBAL_BOOKMARK_CATALOG.topicDefinitions().map(freezeTopicDefinition),
);
export const DEFAULT_BOOKMARK_TOPIC_DEFINITIONS = Object.freeze(
  GLOBAL_BOOKMARK_CATALOG
    .topicDefinitions({ defaultsOnly: true })
    .map(freezeTopicDefinition),
);

function normalizeTopicDefinition(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  const id = boundedText(value.id, 128);
  const name = boundedText(value.name, 80).normalize("NFC");
  const nameKey = boundedText(
    value.name_key ?? `bookmark_topics.${id}`,
    180,
  );
  const color = boundedText(value.color, 7).toLowerCase();
  const aliases = Array.isArray(value.aliases)
    ? value.aliases.map((alias) => boundedText(alias, 80).normalize("NFC"))
    : [];
  if (
    !ID_PATTERN.test(id) ||
    !/^bookmark_topics\.[a-z0-9]+(?:-[a-z0-9]+)*$/.test(nameKey) ||
    !COLOR_PATTERN.test(color)
  ) {
    throw new TypeError("A global bookmark topic is invalid.");
  }
  return {
    id,
    name,
    name_key: nameKey,
    color,
    aliases: Object.freeze(aliases),
    default: value.default !== false,
  };
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
    chapter > 1_000 ||
    !Number.isInteger(verse) ||
    verse < 1 ||
    verse > 2_000
  ) {
    throw new TypeError("A global bookmark coordinate is invalid.");
  }
  return { book, chapter, verse };
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
  };
}

function freezeTopicDefinition(value) {
  return Object.freeze({
    ...value,
    aliases: Object.freeze([...value.aliases]),
  });
}
