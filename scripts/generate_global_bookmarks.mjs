#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const TAG_IDS = new Map([
  ["Adultery", "adultery"],
  ["Authority of the Bible", "authority-of-the-bible"],
  ["Baptism", "baptism"],
  ["Biblical Love", "biblical-love"],
  ["Blessings & Curses", "blessings-and-curses"],
  ["Christian Clothing", "christian-clothing"],
  ["Christian Offices", "christian-offices"],
  ["Communion", "communion"],
  ["Conditional Security", "conditional-security"],
  ["Dating", "dating"],
  ["Dietary Guidance", "dietary-guidance"],
  ["Discipline", "discipline"],
  ["Education", "education"],
  ["Effective Prayer", "effective-prayer"],
  ["Family Planning", "family-planning"],
  ["Fear Not", "fear-not"],
  ["First Day", "first-day"],
  ["Flattery", "flattery"],
  ["Free Will", "free-will"],
  ["God's Judgement", "gods-judgment"],
  ["Grace", "grace"],
  ["Home Church", "home-church"],
  ["Immutability", "immutability"],
  ["Jesus Christ's Deity", "jesus-christs-deity"],
  ["Jesus Christ's Humanity", "jesus-christs-humanity"],
  ["Leadership", "leadership"],
  ["Longevity", "longevity"],
  ["Man's Role", "mans-role"],
  ["Marriage", "marriage"],
  ["Music's Influence", "musics-influence"],
  ["No Fellowship", "no-fellowship"],
  ["No One is Good", "no-one-is-good"],
  ["Nonresistance", "non-resistance"],
  ["Not Under the Law", "not-under-the-law"],
  ["Obey God's Commandments", "obey-gods-commandments"],
  ["Obey Government Laws", "obey-government-laws"],
  ["Omnipotence", "omnipotent"],
  ["Omnipresence", "omnipresent"],
  ["Omniscience", "omniscient"],
  ["Orderly Home", "orderly-home"],
  ["Ordinance", "ordinances"],
  ["Prince of this World", "prince-of-this-world"],
  ["Providence", "providence"],
  ["Renewing of the Mind", "renewing-of-the-mind"],
  ["Repentance", "repentance"],
  ["Saved by Faith", "saved-by-faith"],
  ["Sodomy", "sodomy"],
  ["Spirit of Prophecy", "spirit-of-prophecy"],
  ["Spiritual Gifts", "spiritual-gifts"],
  ["Spiritual Judgement", "spiritual-judgment"],
  ["Spiritual Rebirth", "spiritual-rebirth"],
  ["Temptation", "temptation"],
  ["What is Life", "what-is-life"],
  ["Wine", "wine"],
  ["Wisdom Cause", "wisdom-cause"],
  ["Wisdom Fruit", "wisdom-fruit"],
  ["Wisdom Origin", "wisdom-origin"],
  ["Wisdom Value", "wisdom-value"],
  ["Woman's Role", "womans-role"],
  ["Word of God", "word-of-god"],
  ["Worldly Wisdom", "worldly-wisdom"],
]);

// Protestant canon order, matching the numeric book identifiers used by the
// mini app and source CSV. Keeping this table beside the generator prevents a
// malformed chapter from becoming a bundled link that can never be opened.
const BOOK_CHAPTER_COUNTS = Object.freeze([
  50, 40, 27, 36, 34, 24, 21, 4, 31, 24, 22, 25, 29, 36, 10, 13, 10,
  42, 150, 31, 12, 8, 66, 52, 5, 48, 12, 14, 3, 9, 1, 4, 7, 3, 3, 3, 2,
  14, 4, 28, 16, 24, 21, 28, 16, 16, 13, 6, 6, 4, 4, 5, 3, 6, 4, 3, 1,
  13, 5, 5, 3, 5, 1, 1, 1, 22,
]);
const MAX_VERSE_NUMBER = 2_000;

const [inputArgument, outputArgument] = process.argv.slice(2);
if (!inputArgument || !outputArgument) {
  throw new Error(
    "Usage: generate_global_bookmarks.mjs <tag-verse.csv> <global-bookmark-data.js>",
  );
}

const inputPath = resolve(inputArgument);
const outputPath = resolve(outputArgument);
const source = await readFile(inputPath);
const sourceFingerprint = createHash("sha256").update(source).digest("hex");
const rows = source.toString("utf8")
  .split(/\r?\n/u)
  .filter((line) => line.length > 0);
const groups = new Map([...TAG_IDS.values()].map((id) => [id, []]));
const associations = new Set();

for (const [index, row] of rows.entries()) {
  const columns = row.split(",");
  if (columns.length !== 2) {
    throw new Error(`Row ${index + 1} must contain exactly two columns.`);
  }
  const reference = columns[0].trim();
  const tag = columns[1].trim();
  const match = reference.match(/^([1-9]\d?) ([1-9]\d*):([1-9]\d*)$/u);
  const topicId = TAG_IDS.get(tag);
  if (!match || !topicId) {
    throw new Error(`Row ${index + 1} contains an unknown reference or tag.`);
  }
  const coordinate = match.slice(1).map(Number);
  if (coordinate[0] > BOOK_CHAPTER_COUNTS.length) {
    throw new Error(`Row ${index + 1} contains an unsupported book number.`);
  }
  if (coordinate[1] > BOOK_CHAPTER_COUNTS[coordinate[0] - 1]) {
    throw new Error(`Row ${index + 1} contains an unsupported chapter number.`);
  }
  if (coordinate[2] > MAX_VERSE_NUMBER) {
    throw new Error(`Row ${index + 1} contains an unsupported verse number.`);
  }
  const key = `${topicId}/${coordinate.join("/")}`;
  if (associations.has(key)) {
    throw new Error(`Row ${index + 1} duplicates ${key}.`);
  }
  associations.add(key);
  groups.get(topicId).push(coordinate);
}

for (const coordinates of groups.values()) {
  coordinates.sort((left, right) =>
    left[0] - right[0] || left[1] - right[1] || left[2] - right[2]
  );
}

const bookmarksByTopic = Object.fromEntries(
  [...groups.entries()].sort(([left], [right]) => left.localeCompare(right)),
);
const normalized = {
  schema_version: 1,
  bookmarks_by_topic: bookmarksByTopic,
};
const fingerprint = createHash("sha256")
  .update(JSON.stringify(normalized))
  .digest("hex");
const lines = [
  "// Generated by scripts/generate_global_bookmarks.mjs; do not edit by hand.",
  `// Source CSV SHA-256: ${sourceFingerprint}`,
  `// Normalized catalogue SHA-256: ${fingerprint}`,
  "const BOOKMARKS_BY_TOPIC = {",
];
for (const [topicId, coordinates] of Object.entries(bookmarksByTopic)) {
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
  "  schema_version: 1,",
  "  bookmarks_by_topic: Object.freeze(BOOKMARKS_BY_TOPIC),",
  "});",
  "",
);

await writeFile(outputPath, lines.join("\n"), "utf8");
process.stdout.write(
  `Generated ${associations.size} associations across ${groups.size} topics (${fingerprint}).\n`,
);
