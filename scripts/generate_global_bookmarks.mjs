#!/usr/bin/env node

import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  buildNormalizedCatalog,
  parseAssociationCsv,
  parseTopicDocument,
  renderGlobalBookmarkData,
  renderTopicDefinitions,
  sha256,
} from "./lib/global_bookmark_sources.mjs";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const arguments_ = process.argv.slice(2);
let check = false;
if (arguments_[0] === "--check") {
  check = true;
  arguments_.shift();
}

let paths;
if (arguments_.length === 0) {
  paths = defaultPaths(repositoryRoot);
} else if (arguments_.length === 2) {
  // Preserve the historical focused-generation interface while sourcing the
  // canonical topic metadata from its repository-native JSON document.
  paths = {
    ...defaultPaths(repositoryRoot),
    csv: resolve(arguments_[0]),
    dataOutput: resolve(arguments_[1]),
    definitionsOutput: null,
  };
} else if (arguments_.length === 4) {
  paths = {
    csv: resolve(arguments_[0]),
    dataOutput: resolve(arguments_[1]),
    topics: resolve(arguments_[2]),
    definitionsOutput: resolve(arguments_[3]),
  };
} else {
  throw new Error(
    "Usage: generate_global_bookmarks.mjs [--check] " +
      "[<tag-verse.csv> <global-bookmark-data.js> " +
      "[<topics.json> <bookmark-topic-definitions.js>]]",
  );
}

const [csvSource, topicSource] = await Promise.all([
  readFile(paths.csv, "utf8"),
  readFile(paths.topics, "utf8"),
]);
const topicDocument = parseTopicDocument(topicSource);
const associations = parseAssociationCsv(csvSource, topicDocument.topics);
const catalog = buildNormalizedCatalog(topicDocument, associations);
const csvFingerprint = sha256(csvSource);
const topicFingerprint = sha256(topicSource);
const generatedData = renderGlobalBookmarkData(catalog, {
  csvFingerprint,
  topicFingerprint,
});
const generatedDefinitions = renderTopicDefinitions(
  topicDocument,
  topicFingerprint,
);

const outputs = new Map([[paths.dataOutput, generatedData.source]]);
if (paths.definitionsOutput) {
  outputs.set(paths.definitionsOutput, generatedDefinitions);
}
if (check) {
  const stale = await Promise.all([...outputs].map(async ([path, expected]) =>
    await readFile(path, "utf8") !== expected
  ));
  if (stale.some(Boolean)) {
    throw new Error(
      "Generated global bookmark files are stale; run " +
        "node scripts/generate_global_bookmarks.mjs.",
    );
  }
} else {
  await Promise.all([...outputs].map(([path, source]) =>
    writeFile(path, source, "utf8")
  ));
}

process.stdout.write(
  `${check ? "Verified" : "Generated"} ${associations.length} associations ` +
    `across ${topicDocument.topics.length} topics ` +
    `(catalog version ${topicDocument.catalog_version}, ` +
    `${generatedData.fingerprint}).\n`,
);

function defaultPaths(root) {
  return {
    csv: resolve(root, "data/global-bookmarks/tag-verse.csv"),
    topics: resolve(root, "data/global-bookmarks/topics.json"),
    dataOutput: resolve(root, "miniapp/lib/global-bookmark-data.js"),
    definitionsOutput: resolve(root, "miniapp/lib/bookmark-topic-definitions.js"),
  };
}
