import { GLOBAL_BOOKMARK_DATA } from "./global-bookmark-data.js";
import {
  CORE_BOOKMARK_TOPIC_DEFINITIONS,
  isLegacyBookmarkTopicId,
} from "./bookmark-topic-definitions.js";
import { BOOK_CHAPTER_COUNTS } from "./bible-canon.js";

const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
const CANONICAL_TOPIC_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,79}$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const GLOBAL_SOURCE = "global";
const GLOBAL_TRANSLATION_FALLBACK = "kjv";
const SUPPORTED_SCHEMA_VERSION = 1;
const MAX_GLOBAL_BOOKMARK_ASSIGNMENTS = 10_000;
const ENGLISH_TOPIC_PATTERN =
  /^(?=[A-Za-z0-9 &'():?-]{2,80}$)(?=.*[A-Za-z])(?!.* {2})[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]$/;

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
 * preference records which topic overlays are visible, while the authenticated
 * live provider can merge reviewed server deltas over this bundled fallback
 * without changing personal bookmark storage, Telegram sync, or backups.
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
      (
        data.catalog_version !== undefined &&
        (
          !Number.isSafeInteger(data.catalog_version) ||
          data.catalog_version < 1
        )
      ) ||
      !data.bookmarks_by_topic ||
      typeof data.bookmarks_by_topic !== "object" ||
      Array.isArray(data.bookmarks_by_topic)
    ) {
      throw new TypeError("The global bookmark catalogue is invalid.");
    }
    if (!Array.isArray(topics) || !Array.isArray(bookNames)) {
      throw new TypeError("The global bookmark catalogue metadata is invalid.");
    }
    // Older generated catalogues used the schema version as their content
    // revision. Keep those fixtures/imports readable while allowing accepted
    // contributions to advance the catalogue independently of its format.
    this.version = data.catalog_version ?? data.schema_version;
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

/**
 * Applies a server-published, cumulative contribution overlay to the bundled
 * catalogue. The bundled asset remains the authoritative offline fallback;
 * the overlay contains only reviewed metadata and coordinate deltas.
 */
export function globalBookmarkCatalogWithOverlay(value, revision = 0) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !hasExactKeys(value, ["schema_version", "topics", "associations"]) ||
    value.schema_version !== SUPPORTED_SCHEMA_VERSION ||
    !Array.isArray(value.topics) ||
    !value.associations ||
    typeof value.associations !== "object" ||
    !hasExactKeys(value.associations, ["add", "remove"]) ||
    !Array.isArray(value.associations.add) ||
    !Array.isArray(value.associations.remove) ||
    value.associations.add.length + value.associations.remove.length >
      MAX_GLOBAL_BOOKMARK_ASSIGNMENTS ||
    !Number.isSafeInteger(revision) ||
    revision < 0
  ) {
    throw new TypeError("The global bookmark catalogue overlay is invalid.");
  }
  if (value.topics.length > 39) {
    throw new TypeError("The global bookmark catalogue overlay has too many topics.");
  }

  const definitions = new Map(
    CORE_BOOKMARK_TOPIC_DEFINITIONS.map((definition) => [
      definition.id,
      normalizeTopicDefinition(definition),
    ]),
  );
  const overlayTopicIds = new Set();
  const overlayTopicNames = new Set(
    [...definitions.values()].flatMap((definition) =>
      [definition.name, ...definition.aliases].map((candidate) =>
        candidate.toLocaleLowerCase("en").trim().replace(/\s+/gu, " ")
      )
    ),
  );
  for (const valueTopic of value.topics) {
    if (
      !valueTopic ||
      typeof valueTopic !== "object" ||
      Array.isArray(valueTopic) ||
      !hasExactKeys(valueTopic, ["id", "name", "color", "aliases"]) ||
      typeof valueTopic.name !== "string" ||
      !ENGLISH_TOPIC_PATTERN.test(valueTopic.name) ||
      !CANONICAL_TOPIC_ID_PATTERN.test(valueTopic.id ?? "") ||
      !Array.isArray(valueTopic.aliases) ||
      valueTopic.aliases.length > 20 ||
      valueTopic.aliases.some((alias) =>
        typeof alias !== "string" || !ENGLISH_TOPIC_PATTERN.test(alias)
      ) ||
      new Set(valueTopic.aliases.map((alias) => alias.toLocaleLowerCase("en")))
        .size !== valueTopic.aliases.length
    ) {
      throw new TypeError("A global bookmark overlay topic is invalid.");
    }
    const normalized = normalizeTopicDefinition({
      ...valueTopic,
      name_key: `bookmark_topics.${valueTopic?.id ?? ""}`,
      default: definitions.get(valueTopic?.id)?.default ?? true,
    });
    if (overlayTopicIds.has(normalized.id)) {
      throw new TypeError("The global bookmark catalogue overlay has duplicate topics.");
    }
    overlayTopicIds.add(normalized.id);
    const bundled = definitions.get(normalized.id);
    if (bundled) {
      // A deployed repository update can make an older cumulative overlay
      // repeat this now-bundled definition, including metadata which the PR
      // corrected. The repository copy is authoritative; ignore the stale
      // metadata while retaining its association deltas and later live topics.
      continue;
    }
    for (const candidate of [valueTopic.name, ...valueTopic.aliases]) {
      const normalizedName = candidate.toLocaleLowerCase("en")
        .trim()
        .replace(/\s+/gu, " ");
      if (overlayTopicNames.has(normalizedName)) {
        throw new TypeError("The global bookmark overlay reuses a topic name.");
      }
      overlayTopicNames.add(normalizedName);
    }
    definitions.set(normalized.id, normalized);
  }
  if (definitions.size > 100) {
    throw new TypeError("The global bookmark catalogue overlay has too many topics.");
  }

  const coordinates = new Map(
    Object.entries(GLOBAL_BOOKMARK_DATA.bookmarks_by_topic).map(
      ([topicId, topicCoordinates]) => [
        topicId,
        new Set(topicCoordinates.map((coordinate) => coordinate.join("/"))),
      ],
    ),
  );
  for (const topicId of definitions.keys()) {
    if (!coordinates.has(topicId)) {
      coordinates.set(topicId, new Set());
    }
  }

  const removed = new Set();
  for (const association of value.associations.remove) {
    const normalized = normalizeOverlayAssociation(association, definitions);
    const key = `${normalized.topic_id}:${normalized.coordinate.join("/")}`;
    if (removed.has(key)) {
      throw new TypeError("The global bookmark catalogue overlay has duplicate removals.");
    }
    removed.add(key);
    coordinates.get(normalized.topic_id).delete(normalized.coordinate.join("/"));
  }
  const added = new Set();
  for (const association of value.associations.add) {
    const normalized = normalizeOverlayAssociation(association, definitions);
    const key = `${normalized.topic_id}:${normalized.coordinate.join("/")}`;
    if (added.has(key)) {
      throw new TypeError("The global bookmark catalogue overlay has duplicate additions.");
    }
    if (removed.has(key)) {
      throw new TypeError("The global bookmark catalogue overlay has conflicting entries.");
    }
    added.add(key);
    coordinates.get(normalized.topic_id).add(normalized.coordinate.join("/"));
  }

  const data = {
    schema_version: SUPPORTED_SCHEMA_VERSION,
    catalog_version: GLOBAL_BOOKMARK_CATALOG_VERSION + revision,
    bookmarks_by_topic: Object.fromEntries(
      [...definitions.keys()].sort().map((topicId) => [
        topicId,
        [...coordinates.get(topicId)]
          .map((coordinate) => coordinate.split("/").map(Number))
          .sort(compareCoordinates),
      ]),
    ),
  };
  return new GlobalBookmarkCatalog({
    data,
    topics: [...definitions.values()].sort((left, right) =>
      left.id.localeCompare(right.id, "en")
    ),
  });
}

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

function normalizeOverlayAssociation(value, definitions) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !hasExactKeys(value, ["topic_id", "book", "chapter", "verse"])
  ) {
    throw new TypeError("A global bookmark overlay association is invalid.");
  }
  const topicId = boundedText(value.topic_id, 80);
  if (!CANONICAL_TOPIC_ID_PATTERN.test(topicId) || !definitions.has(topicId)) {
    throw new TypeError("A global bookmark overlay association is invalid.");
  }
  const coordinate = normalizeCoordinate(
    [value.book, value.chapter, value.verse],
    BOOK_NAMES.length,
  );
  if (coordinate.chapter > BOOK_CHAPTER_COUNTS[coordinate.book - 1]) {
    throw new TypeError("A global bookmark overlay association is invalid.");
  }
  return {
    topic_id: topicId,
    coordinate: [coordinate.book, coordinate.chapter, coordinate.verse],
  };
}

function compareCoordinates(left, right) {
  return left[0] - right[0] || left[1] - right[1] || left[2] - right[2];
}

function hasExactKeys(value, expected) {
  const keys = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return keys.length === wanted.length &&
    keys.every((key, index) => key === wanted[index]);
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
