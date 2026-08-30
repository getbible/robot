import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

import { GLOBAL_BOOKMARK_DATA } from "../lib/global-bookmark-data.js";

const execute = promisify(execFile);
const repositoryRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const importer = resolve(repositoryRoot, "scripts/import_contribution_bundle.mjs");
const generator = resolve(repositoryRoot, "scripts/generate_global_bookmarks.mjs");

test("imports a privacy-safe contribution bundle deterministically", async (t) => {
  const fixture = await createRepositoryFixture(t);
  const bundle = {
    schema_version: 1,
    topics: [{
      id: "prayer-and-fasting",
      name: "Prayer and Fasting",
      color: "#93c5fd",
      aliases: ["Fasting and Prayer"],
    }],
    associations: {
      add: [
        { topic_id: "prayer-and-fasting", book: 40, chapter: 6, verse: 16 },
        { topic_id: "grace", book: 43, chapter: 1, verse: 14 },
      ],
      remove: [
        { topic_id: "grace", book: 56, chapter: 3, verse: 5 },
      ],
    },
  };
  const bundlePath = await writeBundle(fixture.root, bundle);
  const originalTopics = await readFile(fixture.topics, "utf8");
  const originalCsv = await readFile(fixture.csv, "utf8");

  const validation = await runImporter(fixture.root, bundlePath, "--check");
  assert.match(validation.stdout, /Validated contribution bundle/);
  assert.equal(await readFile(fixture.topics, "utf8"), originalTopics);
  assert.equal(await readFile(fixture.csv, "utf8"), originalCsv);

  const imported = await runImporter(fixture.root, bundlePath);
  assert.match(imported.stdout, /1 topic changes, 2 additions, 1 removals/);
  assert.match(imported.stdout, /62 topics and 2156 associations/);
  assert.match(imported.stdout, /catalog version 2/);

  const topicDocument = JSON.parse(await readFile(fixture.topics, "utf8"));
  const newTopic = topicDocument.topics.find(
    (topic) => topic.id === "prayer-and-fasting",
  );
  assert.deepEqual(newTopic, {
    id: "prayer-and-fasting",
    name: "Prayer and Fasting",
    color: "#93c5fd",
    aliases: ["Fasting and Prayer"],
    default: true,
  });
  assert.equal(topicDocument.catalog_version, 2);

  const csv = await readFile(fixture.csv, "utf8");
  assert.doesNotMatch(csv, /^56 3:5 ,Grace\s*$/mu);
  assert.match(csv, /^43 1:14 ,Grace\s*$/mu);
  assert.match(csv, /^40 6:16 ,Prayer and Fasting\s*$/mu);

  const definitions = await import(
    `${pathToFileURL(fixture.definitions).href}?fixture=${Date.now()}`
  );
  const generatedTopic = definitions.CORE_BOOKMARK_TOPIC_DEFINITIONS.find(
    (topic) => topic.id === "prayer-and-fasting",
  );
  assert.deepEqual(generatedTopic, {
    id: "prayer-and-fasting",
    name: "Prayer and Fasting",
    name_key: "bookmark_topics.prayer-and-fasting",
    color: "#93c5fd",
    aliases: ["Fasting and Prayer"],
    default: true,
  });
  assert.equal(
    definitions.ENGLISH_BOOKMARK_TOPIC_MESSAGES[
      "bookmark_topics.prayer-and-fasting"
    ],
    "Prayer and Fasting",
  );

  const generated = await import(
    `${pathToFileURL(fixture.data).href}?fixture=${Date.now()}`
  );
  assert.equal(generated.GLOBAL_BOOKMARK_DATA.catalog_version, 2);
  assert.deepEqual(
    generated.GLOBAL_BOOKMARK_DATA.bookmarks_by_topic[
      "prayer-and-fasting"
    ],
    [[40, 6, 16]],
  );
  assert.equal(
    generated.GLOBAL_BOOKMARK_DATA.bookmarks_by_topic.grace.some(
      (coordinate) => coordinate.join("/") === "56/3/5",
    ),
    false,
  );

  const firstHashes = await fixtureHashes(fixture);
  const repeated = await runImporter(fixture.root, bundlePath);
  assert.match(repeated.stdout, /0 topic changes, 0 additions, 0 removals/);
  assert.match(repeated.stdout, /catalog version 2/);
  assert.deepEqual(await fixtureHashes(fixture), firstHashes);

  await execute(process.execPath, [
    generator,
    "--check",
    fixture.csv,
    fixture.data,
    fixture.topics,
    fixture.definitions,
  ]);
});

test("treats an empty accepted bundle as a byte-for-byte no-op", async (t) => {
  const fixture = await createRepositoryFixture(t);
  const bundlePath = await writeBundle(fixture.root, emptyBundle());
  const before = await fixtureHashes(fixture);

  const imported = await runImporter(fixture.root, bundlePath);

  assert.match(imported.stdout, /0 topic changes, 0 additions, 0 removals/);
  assert.match(imported.stdout, /catalog version 1/);
  assert.deepEqual(await fixtureHashes(fixture), before);
});

test("adds a reviewed spelling alias without replacing canonical metadata", async (t) => {
  const fixture = await createRepositoryFixture(t);
  const bundlePath = await writeBundle(fixture.root, {
    schema_version: 1,
    topics: [{
      id: "grace",
      name: "Grace",
      color: "#bbf7d0",
      aliases: ["Unmerited Favor"],
    }],
    associations: { add: [], remove: [] },
  });

  await runImporter(fixture.root, bundlePath);
  const topicDocument = JSON.parse(await readFile(fixture.topics, "utf8"));
  const grace = topicDocument.topics.find((topic) => topic.id === "grace");

  assert.equal(grace.name, "Grace");
  assert.equal(grace.color, "#bbf7d0");
  assert.deepEqual(grace.aliases, ["Unmerited Favor"]);
  assert.equal(topicDocument.catalog_version, 2);

  await runImporter(fixture.root, bundlePath);
  assert.equal(
    JSON.parse(await readFile(fixture.topics, "utf8")).catalog_version,
    2,
  );
});

test("rejects private fields, collisions, unstable English topics, and bad refs", async (t) => {
  const invalidBundles = [
    {
      label: "private attribution",
      bundle: {
        ...emptyBundle(),
        contributor_id: 123456,
      },
      error: /missing or unsupported fields/i,
    },
    {
      label: "non-English topic text",
      bundle: {
        ...emptyBundle(),
        topics: [{
          id: "oracion",
          name: "Oración",
          color: "#93c5fd",
          aliases: [],
        }],
      },
      error: /English topic name/i,
    },
    {
      label: "unstable slug",
      bundle: {
        ...emptyBundle(),
        topics: [{
          id: "fasting",
          name: "Prayer and Fasting",
          color: "#93c5fd",
          aliases: [],
        }],
      },
      error: /stable slug/i,
    },
    {
      label: "canonical name collision",
      bundle: {
        ...emptyBundle(),
        topics: [{
          id: "prayer-and-fasting",
          name: "Prayer and Fasting",
          color: "#93c5fd",
          aliases: ["Grace"],
        }],
      },
      error: /reuses topic name/i,
    },
    {
      label: "existing metadata conflict",
      bundle: {
        ...emptyBundle(),
        topics: [{
          id: "grace",
          name: "Grace",
          color: "#000000",
          aliases: [],
        }],
      },
      error: /conflicts with its canonical definition/i,
    },
    {
      label: "book chapter bound",
      bundle: {
        ...emptyBundle(),
        associations: {
          add: [{ topic_id: "grace", book: 65, chapter: 2, verse: 1 }],
          remove: [],
        },
      },
      error: /canonical Bible coordinate/i,
    },
    {
      label: "contradictory operation",
      bundle: {
        ...emptyBundle(),
        associations: {
          add: [{ topic_id: "grace", book: 1, chapter: 1, verse: 1 }],
          remove: [{ topic_id: "grace", book: 1, chapter: 1, verse: 1 }],
        },
      },
      error: /both adds and removes/i,
    },
    {
      label: "empty new topic",
      bundle: {
        ...emptyBundle(),
        topics: [{
          id: "prayer-and-fasting",
          name: "Prayer and Fasting",
          color: "#93c5fd",
          aliases: [],
        }],
      },
      error: /without a verse association/i,
    },
    {
      label: "removal empties a canonical topic",
      bundle: {
        ...emptyBundle(),
        associations: {
          add: [],
          remove: GLOBAL_BOOKMARK_DATA.bookmarks_by_topic.sodomy.map(
            ([book, chapter, verse]) => ({
              topic_id: "sodomy",
              book,
              chapter,
              verse,
            }),
          ),
        },
      },
      error: /without a verse association/i,
    },
  ];

  for (const invalid of invalidBundles) {
    await t.test(invalid.label, async (subtest) => {
      const fixture = await createRepositoryFixture(subtest);
      const bundlePath = await writeBundle(fixture.root, invalid.bundle);
      const before = await fixtureSourceHashes(fixture);
      await assert.rejects(
        runImporter(fixture.root, bundlePath, "--check"),
        (error) => invalid.error.test(`${error.stderr}\n${error.message}`),
      );
      assert.deepEqual(await fixtureSourceHashes(fixture), before);
    });
  }
});

test("enforces the 100-topic runtime boundary on the merged catalogue", async (t) => {
  const acceptedFixture = await createRepositoryFixture(t);
  const acceptedBundle = topicCapacityBundle(39);
  const acceptedPath = await writeBundle(acceptedFixture.root, acceptedBundle);
  const accepted = await runImporter(
    acceptedFixture.root,
    acceptedPath,
    "--check",
  );
  assert.match(accepted.stdout, /100 topics/);

  const rejectedFixture = await createRepositoryFixture(t);
  const rejectedBundle = topicCapacityBundle(40);
  const rejectedPath = await writeBundle(rejectedFixture.root, rejectedBundle);
  await assert.rejects(
    runImporter(rejectedFixture.root, rejectedPath, "--check"),
    (error) => /exceeds 100 topics/i.test(`${error.stderr}\n${error.message}`),
  );
});

function emptyBundle() {
  return {
    schema_version: 1,
    topics: [],
    associations: { add: [], remove: [] },
  };
}

function topicCapacityBundle(count) {
  const topics = Array.from({ length: count }, (_, index) => {
    const number = String(index + 1).padStart(2, "0");
    return {
      id: `accepted-topic-${number}`,
      name: `Accepted Topic ${number}`,
      color: "#93c5fd",
      aliases: [],
    };
  });
  return {
    schema_version: 1,
    topics,
    associations: {
      add: topics.map((topic, index) => ({
        topic_id: topic.id,
        book: 19,
        chapter: 119,
        verse: index + 1,
      })),
      remove: [],
    },
  };
}

async function createRepositoryFixture(t) {
  const root = await mkdtemp(resolve(tmpdir(), "getbible-catalog-import-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const dataDirectory = resolve(root, "data/global-bookmarks");
  const miniappDirectory = resolve(root, "miniapp/lib");
  await Promise.all([
    mkdir(dataDirectory, { recursive: true }),
    mkdir(miniappDirectory, { recursive: true }),
  ]);
  await writeFile(resolve(root, "package.json"), '{"type":"module"}\n', "utf8");
  const fixture = {
    root,
    topics: resolve(dataDirectory, "topics.json"),
    csv: resolve(dataDirectory, "tag-verse.csv"),
    definitions: resolve(miniappDirectory, "bookmark-topic-definitions.js"),
    data: resolve(miniappDirectory, "global-bookmark-data.js"),
  };
  await Promise.all([
    copyFile(
      resolve(repositoryRoot, "data/global-bookmarks/topics.json"),
      fixture.topics,
    ),
    copyFile(
      resolve(repositoryRoot, "data/global-bookmarks/tag-verse.csv"),
      fixture.csv,
    ),
    copyFile(
      resolve(repositoryRoot, "miniapp/lib/bookmark-topic-definitions.js"),
      fixture.definitions,
    ),
    copyFile(
      resolve(repositoryRoot, "miniapp/lib/global-bookmark-data.js"),
      fixture.data,
    ),
  ]);
  return fixture;
}

async function writeBundle(root, bundle) {
  const path = resolve(root, "bundle.json");
  await writeFile(path, `${JSON.stringify(bundle, null, 2)}\n`, "utf8");
  return path;
}

async function runImporter(root, bundle, ...options) {
  return execute(process.execPath, [
    importer,
    ...options,
    "--repo-root",
    root,
    bundle,
  ]);
}

async function fixtureHashes(fixture) {
  return Promise.all([
    fixture.topics,
    fixture.csv,
    fixture.definitions,
    fixture.data,
  ].map(fileHash));
}

async function fixtureSourceHashes(fixture) {
  return Promise.all([fixture.topics, fixture.csv].map(fileHash));
}

async function fileHash(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}
