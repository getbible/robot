import assert from "node:assert/strict";
import test from "node:test";

import {
  BrowserSelectionError,
  BrowserSelectionStore,
} from "../lib/selection-store.js";

const JOHN_316 = {
  selection_id: "gbd_kjv_043_0003_0016",
  translation: "kjv",
  reference: "John 3:16",
  book_number: 43,
  book_name: "John",
  chapter: 3,
  verse: 16,
  text: "For God so loved the world.",
  terms: [],
  highlights: [],
};

const SEARCH_JOHN_316 = {
  ...JOHN_316,
  selection_id: "OpaqueSearchSelectionToken123",
  terms: ["world"],
};

test("reader and search records share coordinate identity", () => {
  const store = new BrowserSelectionStore();
  store.registerMany([JOHN_316, SEARCH_JOHN_316]);

  assert.equal(store.add(JOHN_316.selection_id).count, 1);
  assert.equal(store.add(SEARCH_JOHN_316.selection_id).count, 1);
  assert.equal(store.snapshot().items[0].selection_id, JOHN_316.selection_id);
});

test("selection removal works from either source token", () => {
  const store = new BrowserSelectionStore();
  store.registerMany([JOHN_316, SEARCH_JOHN_316]);
  store.add(JOHN_316.selection_id);

  assert.equal(store.remove(SEARCH_JOHN_316.selection_id).count, 0);
});

test("snapshots are defensive and selection order is explicit", () => {
  const store = new BrowserSelectionStore();
  const second = {
    ...JOHN_316,
    selection_id: "gbd_kjv_043_0003_0017",
    reference: "John 3:17",
    verse: 17,
    text: "For God sent not his Son into the world.",
  };
  store.registerMany([JOHN_316, second]);
  store.add(JOHN_316.selection_id);
  store.add(second.selection_id);

  const first = store.snapshot();
  first.items[0].text = "tampered";
  assert.equal(store.snapshot().items[0].text, JOHN_316.text);

  const reordered = store.reorder([
    second.selection_id,
    JOHN_316.selection_id,
  ]);
  assert.deepEqual(
    reordered.items.map((item) => item.verse),
    [17, 16],
  );
});

test("capacity is bounded and coordinates omit display text", () => {
  const store = new BrowserSelectionStore({ maximum: 1 });
  const second = {
    ...JOHN_316,
    selection_id: "gbd_kjv_043_0003_0017",
    reference: "John 3:17",
    verse: 17,
    text: "For God sent not his Son into the world.",
  };
  store.registerMany([JOHN_316, second]);
  store.add(JOHN_316.selection_id);
  assert.throws(
    () => store.add(second.selection_id),
    (error) => error instanceof BrowserSelectionError,
  );
  assert.deepEqual(store.coordinates(), [{
    translation: "kjv",
    book_number: 43,
    chapter: 3,
    verse: 16,
  }]);
});
