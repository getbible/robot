#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import { readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  applyContributionBundle,
  buildNormalizedCatalog,
  parseAssociationCsv,
  parseContributionBundle,
  parseTopicDocument,
  renderAssociationCsv,
  renderGlobalBookmarkData,
  renderTopicDefinitions,
  renderTopicDocument,
  sha256,
} from "./lib/global_bookmark_sources.mjs";

const scriptRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const options = parseArguments(process.argv.slice(2));
const paths = repositoryPaths(options.repositoryRoot ?? scriptRoot);
const [bundleSource, csvSource, topicSource] = await Promise.all([
  readFile(options.bundle, "utf8"),
  readFile(paths.csv, "utf8"),
  readFile(paths.topics, "utf8"),
]);
if (Buffer.byteLength(bundleSource, "utf8") > 2 * 1024 * 1024) {
  throw new TypeError("The contribution bundle exceeds the 2 MiB limit.");
}

const topicDocument = parseTopicDocument(topicSource);
const associations = parseAssociationCsv(csvSource, topicDocument.topics);
const bundle = parseContributionBundle(bundleSource);
const merged = applyContributionBundle(topicDocument, associations, bundle);
const nextTopicSource = renderTopicDocument(merged.topicDocument);
const nextCsvSource = renderAssociationCsv(
  merged.associations,
  merged.topicDocument.topics,
);
const catalog = buildNormalizedCatalog(
  merged.topicDocument,
  merged.associations,
);
const topicFingerprint = sha256(nextTopicSource);
const generatedData = renderGlobalBookmarkData(catalog, {
  csvFingerprint: sha256(nextCsvSource),
  topicFingerprint,
});
const generatedDefinitions = renderTopicDefinitions(
  merged.topicDocument,
  topicFingerprint,
);

if (!options.check) {
  await atomicWriteFiles(new Map([
    [paths.topics, nextTopicSource],
    [paths.csv, nextCsvSource],
    [paths.dataOutput, generatedData.source],
    [paths.definitionsOutput, generatedDefinitions],
  ]));
}

const action = options.check ? "Validated" : "Imported";
process.stdout.write(
  `${action} contribution bundle: ${merged.changes.topics} topic changes, ` +
    `${merged.changes.added} additions, ${merged.changes.removed} removals; ` +
    `${merged.topicDocument.topics.length} topics and ` +
    `${merged.associations.length} associations at catalog version ` +
    `${merged.topicDocument.catalog_version} (${generatedData.fingerprint}).\n`,
);

function parseArguments(arguments_) {
  let check = false;
  let repositoryRoot = null;
  const positional = [];
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    if (argument === "--check") {
      check = true;
      continue;
    }
    if (argument === "--repo-root") {
      const value = arguments_[index + 1];
      if (!value) {
        throw new TypeError("--repo-root requires a directory.");
      }
      repositoryRoot = resolve(value);
      index += 1;
      continue;
    }
    if (argument.startsWith("-")) {
      throw new TypeError(`Unknown option: ${argument}`);
    }
    positional.push(argument);
  }
  if (positional.length !== 1) {
    throw new Error(
      "Usage: import_contribution_bundle.mjs [--check] " +
        "[--repo-root <repository>] <bundle.json>",
    );
  }
  return { check, repositoryRoot, bundle: resolve(positional[0]) };
}

function repositoryPaths(root) {
  return {
    csv: resolve(root, "data/global-bookmarks/tag-verse.csv"),
    topics: resolve(root, "data/global-bookmarks/topics.json"),
    dataOutput: resolve(root, "miniapp/lib/global-bookmark-data.js"),
    definitionsOutput: resolve(root, "miniapp/lib/bookmark-topic-definitions.js"),
  };
}

async function atomicWriteFiles(files) {
  const staged = [];
  try {
    for (const [destination, source] of files) {
      let current = null;
      try {
        current = await readFile(destination, "utf8");
      } catch (error) {
        if (error?.code !== "ENOENT") {
          throw error;
        }
      }
      if (current === source) {
        continue;
      }
      const temporary = `${destination}.tmp-${process.pid}-${randomUUID()}`;
      await writeFile(temporary, source, { encoding: "utf8", flag: "wx" });
      staged.push({ destination, temporary });
    }
    for (const file of staged) {
      await rename(file.temporary, file.destination);
      file.temporary = null;
    }
  } finally {
    await Promise.all(staged
      .filter((file) => file.temporary)
      .map((file) => unlink(file.temporary).catch(() => undefined)));
  }
}
