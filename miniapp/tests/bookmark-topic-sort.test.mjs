import assert from "node:assert/strict";
import test from "node:test";

import { sortBookmarkTopics } from "../lib/bookmark-topic-sort.js";

test("sorts a presentation copy without changing persisted topic order", () => {
  const topics = [
    { id: "z", name: "Zulu" },
    { id: "a2", name: "Alpha 10" },
    { id: "a1", name: "alpha 2" },
  ];
  const original = structuredClone(topics);

  const sorted = sortBookmarkTopics(topics, (topic) => topic.name, "en");

  assert.deepEqual(sorted.map((topic) => topic.id), ["a1", "a2", "z"]);
  assert.deepEqual(topics, original);
  assert.notStrictEqual(sorted, topics);
});

test("uses stable ids when localized labels collate equally", () => {
  const sorted = sortBookmarkTopics([
    { id: "second", name: "Faith" },
    { id: "first", name: "faith" },
  ]);
  assert.deepEqual(sorted.map((topic) => topic.id), ["first", "second"]);
});
