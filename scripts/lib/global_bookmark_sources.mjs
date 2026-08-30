import { createHash } from "node:crypto";
import {
  BOOK_CHAPTER_COUNTS,
  isCanonicalVerseCoordinate,
} from "../../miniapp/lib/bible-canon.js";

export { BOOK_CHAPTER_COUNTS };

export const GLOBAL_BOOKMARK_SCHEMA_VERSION = 1;
export const MAX_GLOBAL_BOOKMARK_TOPICS = 100;
export const MAX_GLOBAL_BOOKMARK_ASSIGNMENTS = 10_000;

const TOPIC_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/u;
const ENGLISH_TOPIC_PATTERN =
  /^[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]$/u;

export function parseTopicDocument(source, label = "topic source") {
  const value = parseJson(source, label);
  exactKeys(value, ["schema_version", "catalog_version", "topics"], label);
  if (value.schema_version !== GLOBAL_BOOKMARK_SCHEMA_VERSION) {
    throw new TypeError(`${label} has an unsupported schema_version.`);
  }
  if (!Number.isSafeInteger(value.catalog_version) || value.catalog_version < 1) {
    throw new TypeError(`${label} has an invalid catalog_version.`);
  }
  if (!Array.isArray(value.topics)) {
    throw new TypeError(`${label}.topics must be an array.`);
  }
  const topics = value.topics.map((topic, index) =>
    normalizeSourceTopic(topic, `${label}.topics[${index}]`)
  );
  validateTopicSet(topics, label);
  return {
    schema_version: GLOBAL_BOOKMARK_SCHEMA_VERSION,
    catalog_version: value.catalog_version,
    topics,
  };
}

export function parseContributionBundle(source, label = "contribution bundle") {
  const value = parseJson(source, label);
  exactKeys(value, ["schema_version", "topics", "associations"], label);
  if (value.schema_version !== GLOBAL_BOOKMARK_SCHEMA_VERSION) {
    throw new TypeError(`${label} has an unsupported schema_version.`);
  }
  if (!Array.isArray(value.topics)) {
    throw new TypeError(`${label}.topics must be an array.`);
  }
  const topics = value.topics.map((topic, index) =>
    normalizeBundleTopic(topic, `${label}.topics[${index}]`)
  );
  validateTopicSet(topics, label);

  exactKeys(value.associations, ["add", "remove"], `${label}.associations`);
  if (
    !Array.isArray(value.associations.add) ||
    !Array.isArray(value.associations.remove)
  ) {
    throw new TypeError(`${label} association operations must be arrays.`);
  }
  if (
    value.associations.add.length + value.associations.remove.length >
      MAX_GLOBAL_BOOKMARK_ASSIGNMENTS
  ) {
    throw new TypeError(`${label} has too many association operations.`);
  }
  const add = normalizeAssociationList(
    value.associations.add,
    `${label}.associations.add`,
  );
  const remove = normalizeAssociationList(
    value.associations.remove,
    `${label}.associations.remove`,
  );
  const removed = new Set(remove.map(associationKey));
  const contradiction = add.find((association) => removed.has(associationKey(association)));
  if (contradiction) {
    throw new TypeError(
      `${label} both adds and removes ${describeAssociation(contradiction)}.`,
    );
  }
  return {
    schema_version: GLOBAL_BOOKMARK_SCHEMA_VERSION,
    topics,
    associations: { add, remove },
  };
}

export function parseAssociationCsv(source, topics, label = "association CSV") {
  if (typeof source !== "string") {
    throw new TypeError(`${label} must be UTF-8 text.`);
  }
  const topicIds = topicNameMap(topics, label);
  const rows = source.split(/\r?\n/u);
  const associations = [];
  const seen = new Set();
  for (const [index, raw] of rows.entries()) {
    if (raw.length === 0 && index === rows.length - 1) {
      continue;
    }
    if (raw.trim().length === 0) {
      throw new TypeError(`${label} row ${index + 1} is empty.`);
    }
    const columns = raw.split(",");
    if (columns.length !== 2) {
      throw new TypeError(`${label} row ${index + 1} must contain two columns.`);
    }
    const reference = columns[0].trim();
    const topicName = columns[1].trim();
    const match = reference.match(/^([1-9]\d?) ([1-9]\d*):([1-9]\d*)$/u);
    const topicId = topicIds.get(topicName);
    if (!match || !topicId) {
      throw new TypeError(
        `${label} row ${index + 1} contains an unknown reference or topic.`,
      );
    }
    const association = normalizeAssociation({
      topic_id: topicId,
      book: Number(match[1]),
      chapter: Number(match[2]),
      verse: Number(match[3]),
    }, `${label} row ${index + 1}`);
    const key = associationKey(association);
    if (seen.has(key)) {
      throw new TypeError(
        `${label} row ${index + 1} duplicates ${describeAssociation(association)}.`,
      );
    }
    seen.add(key);
    associations.push({ ...association, raw });
  }
  if (associations.length > MAX_GLOBAL_BOOKMARK_ASSIGNMENTS) {
    throw new TypeError(`${label} has too many associations.`);
  }
  validateTopicCoverage(topics, associations, label);
  return associations;
}

export function applyContributionBundle(topicDocument, associations, bundle) {
  const topics = topicDocument.topics.map(cloneTopic);
  const topicsById = new Map(topics.map((topic) => [topic.id, topic]));
  let topicChanges = 0;

  for (const incoming of bundle.topics) {
    const existing = topicsById.get(incoming.id);
    if (!existing) {
      if (incoming.id !== topicSlug(incoming.name)) {
        throw new TypeError(
          `Topic ${JSON.stringify(incoming.id)} must use the stable slug of its English name.`,
        );
      }
      const added = { ...cloneTopic(incoming), default: true };
      topics.push(added);
      topicsById.set(added.id, added);
      topicChanges += 1;
      continue;
    }
    if (existing.name !== incoming.name || existing.color !== incoming.color) {
      throw new TypeError(
        `Topic ${JSON.stringify(incoming.id)} conflicts with its canonical definition.`,
      );
    }
    const aliases = sortedUnique([...existing.aliases, ...incoming.aliases]);
    if (!arraysEqual(existing.aliases, aliases)) {
      existing.aliases = aliases;
      topicChanges += 1;
    }
  }
  topics.sort((left, right) => compareText(left.id, right.id));
  validateTopicSet(topics, "merged topic source");
  if (topics.length > MAX_GLOBAL_BOOKMARK_TOPICS) {
    throw new TypeError(
      `The merged catalogue exceeds ${MAX_GLOBAL_BOOKMARK_TOPICS} topics.`,
    );
  }

  const current = new Map(
    associations.map((association) => [associationKey(association), { ...association }]),
  );
  let removed = 0;
  for (const association of bundle.associations.remove) {
    if (!topicsById.has(association.topic_id)) {
      throw new TypeError(
        `Removal references unknown topic ${JSON.stringify(association.topic_id)}.`,
      );
    }
    if (current.delete(associationKey(association))) {
      removed += 1;
    }
  }

  let added = 0;
  const newAssociations = [];
  for (const association of bundle.associations.add) {
    if (!topicsById.has(association.topic_id)) {
      throw new TypeError(
        `Addition references unknown topic ${JSON.stringify(association.topic_id)}.`,
      );
    }
    const key = associationKey(association);
    if (!current.has(key)) {
      const entry = { ...association, raw: null };
      current.set(key, entry);
      newAssociations.push(entry);
      added += 1;
    }
  }
  if (current.size > MAX_GLOBAL_BOOKMARK_ASSIGNMENTS) {
    throw new TypeError(
      `The merged catalogue exceeds ${MAX_GLOBAL_BOOKMARK_ASSIGNMENTS} associations.`,
    );
  }

  const retained = associations.filter((association) =>
    current.has(associationKey(association))
  );
  newAssociations.sort(compareAssociation);
  const mergedAssociations = [...retained, ...newAssociations];
  validateTopicCoverage(topics, mergedAssociations, "merged catalogue");

  const changed = topicChanges > 0 || added > 0 || removed > 0;
  return {
    topicDocument: {
      schema_version: GLOBAL_BOOKMARK_SCHEMA_VERSION,
      catalog_version: topicDocument.catalog_version + (changed ? 1 : 0),
      topics,
    },
    associations: mergedAssociations,
    changes: { topics: topicChanges, added, removed, changed },
  };
}

export function renderTopicDocument(document) {
  const topics = document.topics.map((topic) => ({
      id: topic.id,
      name: topic.name,
      color: topic.color,
      aliases: [...topic.aliases],
      default: topic.default,
    }));
  const lines = [
    "{",
    `  \"schema_version\": ${GLOBAL_BOOKMARK_SCHEMA_VERSION},`,
    `  \"catalog_version\": ${document.catalog_version},`,
    "  \"topics\": [",
    ...topics.map((topic, index) => {
      const rendered = `{ \"id\": ${JSON.stringify(topic.id)}, ` +
        `\"name\": ${JSON.stringify(topic.name)}, ` +
        `\"color\": ${JSON.stringify(topic.color)}, ` +
        `\"aliases\": ${JSON.stringify(topic.aliases)}, ` +
        `\"default\": ${topic.default} }`;
      return `    ${rendered}${index === topics.length - 1 ? "" : ","}`;
    }),
    "  ]",
    "}",
    "",
  ];
  return lines.join("\n");
}

export function renderAssociationCsv(associations, topics) {
  const topicsById = new Map(topics.map((topic) => [topic.id, topic]));
  const rows = associations.map((association) => {
    if (typeof association.raw === "string") {
      return association.raw;
    }
    const topic = topicsById.get(association.topic_id);
    if (!topic) {
      throw new TypeError("Cannot render an association for an unknown topic.");
    }
    return `${association.book} ${association.chapter}:${association.verse} ,${topic.name} `;
  });
  return `${rows.join("\n")}\n`;
}

export function buildNormalizedCatalog(topicDocument, associations) {
  const groups = new Map(topicDocument.topics.map((topic) => [topic.id, []]));
  for (const association of associations) {
    const coordinates = groups.get(association.topic_id);
    if (!coordinates) {
      throw new TypeError("An association references an unknown topic.");
    }
    coordinates.push([
      association.book,
      association.chapter,
      association.verse,
    ]);
  }
  for (const coordinates of groups.values()) {
    coordinates.sort(compareCoordinate);
  }
  const bookmarksByTopic = Object.fromEntries(
    [...groups.entries()].sort(([left], [right]) => compareText(left, right)),
  );
  return {
    schema_version: GLOBAL_BOOKMARK_SCHEMA_VERSION,
    catalog_version: topicDocument.catalog_version,
    bookmarks_by_topic: bookmarksByTopic,
  };
}

export function renderGlobalBookmarkData(catalog, { csvFingerprint, topicFingerprint }) {
  const normalizedFingerprint = sha256(JSON.stringify(catalog));
  const lines = [
    "// Generated by scripts/generate_global_bookmarks.mjs; do not edit by hand.",
    `// Source CSV SHA-256: ${csvFingerprint}`,
    `// Topic source SHA-256: ${topicFingerprint}`,
    `// Normalized catalogue SHA-256: ${normalizedFingerprint}`,
    "const BOOKMARKS_BY_TOPIC = {",
  ];
  for (const [topicId, coordinates] of Object.entries(catalog.bookmarks_by_topic)) {
    lines.push(`  ${JSON.stringify(topicId)}: [`);
    for (const coordinate of coordinates) {
      lines.push(`    Object.freeze(${JSON.stringify(coordinate)}),`);
    }
    lines.push("  ],");
  }
  lines.push(
    "};",
    "",
    "for (const coordinates of Object.values(BOOKMARKS_BY_TOPIC)) {",
    "  Object.freeze(coordinates);",
    "}",
    "",
    "export const GLOBAL_BOOKMARK_DATA = Object.freeze({",
    `  schema_version: ${catalog.schema_version},`,
    `  catalog_version: ${catalog.catalog_version},`,
    "  bookmarks_by_topic: Object.freeze(BOOKMARKS_BY_TOPIC),",
    "});",
    "",
  );
  return {
    source: lines.join("\n"),
    fingerprint: normalizedFingerprint,
  };
}

export function renderTopicDefinitions(topicDocument, topicFingerprint) {
  const lines = [
    "/**",
    " * Generated by scripts/generate_global_bookmarks.mjs from the canonical",
    ` * English topic source (SHA-256: ${topicFingerprint}).`,
    " *",
    " * Stable ids and English names remain storage/import migration authority;",
    " * localized names are display-only and are never synchronized. Missing",
    " * translations intentionally fall back to these English messages.",
    " */",
    "const LEGACY_NUMERIC_TOPIC_ID_PATTERN = /^[1-9]\\d{0,2}$/;",
    "",
    "export const CORE_BOOKMARK_TOPIC_DEFINITIONS = Object.freeze([",
  ];
  for (const definition of topicDocument.topics) {
    const arguments_ = [
      JSON.stringify(definition.id),
      JSON.stringify(definition.name),
      JSON.stringify(definition.color),
    ];
    if (definition.aliases.length > 0 || !definition.default) {
      arguments_.push(JSON.stringify(definition.aliases));
    }
    if (!definition.default) {
      arguments_.push("false");
    }
    lines.push(`  topic(${arguments_.join(", ")}),`);
  }
  lines.push(
    "]);",
    "",
    "export const ENGLISH_BOOKMARK_TOPIC_MESSAGES = Object.freeze(",
    "  Object.fromEntries(CORE_BOOKMARK_TOPIC_DEFINITIONS.map((definition) => [",
    "    definition.name_key,",
    "    definition.name,",
    "  ])),",
    ");",
    "",
    "export function isLegacyBookmarkTopicId(value) {",
    "  return typeof value === \"string\" &&",
    "    LEGACY_NUMERIC_TOPIC_ID_PATTERN.test(value);",
    "}",
    "",
    "function topic(id, name, color, aliases = [], defaultTopic = true) {",
    "  return Object.freeze({",
    "    id,",
    "    name,",
    "    name_key: `bookmark_topics.${id}`,",
    "    color,",
    "    aliases: Object.freeze([...aliases]),",
    "    default: defaultTopic,",
    "  });",
    "}",
    "",
  );
  return lines.join("\n");
}

export function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

export function topicSlug(name) {
  return String(name)
    .toLowerCase()
    .replace(/['’]/gu, "")
    .replace(/[^a-z0-9]+/gu, "-")
    .replace(/^-|-$/gu, "");
}

function normalizeSourceTopic(value, label) {
  exactKeys(value, ["id", "name", "color", "aliases", "default"], label);
  const topic = normalizeTopic(value, label);
  if (typeof value.default !== "boolean") {
    throw new TypeError(`${label}.default must be a boolean.`);
  }
  return { ...topic, default: value.default };
}

function normalizeBundleTopic(value, label) {
  exactKeys(value, ["id", "name", "color", "aliases"], label);
  return normalizeTopic(value, label);
}

function normalizeTopic(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object.`);
  }
  const id = boundedString(value.id, 80, `${label}.id`);
  const name = englishTopicName(value.name, `${label}.name`);
  const color = boundedString(value.color, 7, `${label}.color`);
  if (!TOPIC_ID_PATTERN.test(id)) {
    throw new TypeError(`${label}.id must be a stable topic slug.`);
  }
  if (!COLOR_PATTERN.test(color)) {
    throw new TypeError(`${label}.color must be a lowercase six-digit hex color.`);
  }
  if (!Array.isArray(value.aliases) || value.aliases.length > 20) {
    throw new TypeError(`${label}.aliases must be an array with at most 20 entries.`);
  }
  const aliases = sortedUnique(value.aliases.map((alias, index) =>
    englishTopicName(alias, `${label}.aliases[${index}]`)
  ));
  if (aliases.some((alias) => normalizedTopicName(alias) === normalizedTopicName(name))) {
    throw new TypeError(`${label} repeats its canonical name as an alias.`);
  }
  return { id, name, color, aliases };
}

function validateTopicSet(topics, label) {
  if (topics.length > MAX_GLOBAL_BOOKMARK_TOPICS) {
    throw new TypeError(
      `${label} exceeds ${MAX_GLOBAL_BOOKMARK_TOPICS} topics.`,
    );
  }
  const ids = new Set();
  const names = new Map();
  for (const topic of topics) {
    if (ids.has(topic.id)) {
      throw new TypeError(`${label} duplicates topic id ${JSON.stringify(topic.id)}.`);
    }
    ids.add(topic.id);
    for (const name of [topic.name, ...topic.aliases]) {
      const normalized = normalizedTopicName(name);
      const owner = names.get(normalized);
      if (owner) {
        throw new TypeError(
          `${label} reuses topic name ${JSON.stringify(name)} for ${owner} and ${topic.id}.`,
        );
      }
      names.set(normalized, topic.id);
    }
  }
}

function topicNameMap(topics, label) {
  validateTopicSet(topics, label);
  return new Map(
    topics.flatMap((topic) =>
      [topic.name, ...topic.aliases].map((name) => [name, topic.id])
    ),
  );
}

function normalizeAssociationList(values, label) {
  if (values.length > MAX_GLOBAL_BOOKMARK_ASSIGNMENTS) {
    throw new TypeError(`${label} has too many entries.`);
  }
  const seen = new Set();
  return values.map((value, index) => {
    const association = normalizeAssociation(value, `${label}[${index}]`);
    const key = associationKey(association);
    if (seen.has(key)) {
      throw new TypeError(`${label} duplicates ${describeAssociation(association)}.`);
    }
    seen.add(key);
    return association;
  }).sort(compareAssociation);
}

function normalizeAssociation(value, label) {
  exactKeys(value, ["topic_id", "book", "chapter", "verse"], label);
  const topicId = boundedString(value.topic_id, 80, `${label}.topic_id`);
  if (!TOPIC_ID_PATTERN.test(topicId)) {
    throw new TypeError(`${label}.topic_id is invalid.`);
  }
  if (!isCanonicalVerseCoordinate(value)) {
    throw new TypeError(`${label} is not a canonical Bible coordinate.`);
  }
  return {
    topic_id: topicId,
    book: value.book,
    chapter: value.chapter,
    verse: value.verse,
  };
}

function validateTopicCoverage(topics, associations, label) {
  const covered = new Set(associations.map((association) => association.topic_id));
  const empty = topics.find((topic) => !covered.has(topic.id));
  if (empty) {
    throw new TypeError(
      `${label} leaves topic ${JSON.stringify(empty.id)} without a verse association.`,
    );
  }
}

function parseJson(source, label) {
  if (typeof source !== "string") {
    throw new TypeError(`${label} must be UTF-8 JSON text.`);
  }
  try {
    return JSON.parse(source);
  } catch (error) {
    throw new TypeError(`${label} is not valid JSON.`, { cause: error });
  }
}

function exactKeys(value, expected, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object.`);
  }
  const actual = Object.keys(value).sort(compareText);
  const wanted = [...expected].sort(compareText);
  if (!arraysEqual(actual, wanted)) {
    throw new TypeError(`${label} has missing or unsupported fields.`);
  }
}

function boundedString(value, maximum, label) {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > maximum ||
    value !== value.normalize("NFC") ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    throw new TypeError(`${label} is invalid.`);
  }
  return value;
}

function englishTopicName(value, label) {
  const name = boundedString(value, 80, label);
  if (
    name.length < 2 ||
    name !== name.trim() ||
    /\s{2,}/u.test(name) ||
    !ENGLISH_TOPIC_PATTERN.test(name) ||
    !/[A-Za-z]/u.test(name)
  ) {
    throw new TypeError(`${label} must be a bounded English topic name.`);
  }
  return name;
}

function normalizedTopicName(value) {
  return value.toLowerCase().replace(/\s+/gu, " ");
}

function associationKey(value) {
  return `${value.topic_id}/${value.book}/${value.chapter}/${value.verse}`;
}

function describeAssociation(value) {
  return `${value.topic_id} ${value.book} ${value.chapter}:${value.verse}`;
}

function cloneTopic(topic) {
  return { ...topic, aliases: [...topic.aliases] };
}

function sortedUnique(values) {
  const sorted = [...values].sort(compareText);
  return sorted.filter((value, index) => index === 0 || value !== sorted[index - 1]);
}

function arraysEqual(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function compareText(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function compareAssociation(left, right) {
  return compareText(left.topic_id, right.topic_id) ||
    left.book - right.book ||
    left.chapter - right.chapter ||
    left.verse - right.verse;
}

function compareCoordinate(left, right) {
  return left[0] - right[0] || left[1] - right[1] || left[2] - right[2];
}
