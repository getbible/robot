import assert from "node:assert/strict";
import test from "node:test";

import { TelegramBridge } from "../lib/telegram.js";

test("dismisses the mobile keyboard by blurring the active search input", () => {
  const originalDocument = globalThis.document;
  let blurCount = 0;
  globalThis.document = {
    activeElement: {
      matches(selector) {
        assert.match(selector, /input/);
        return true;
      },
      blur() {
        blurCount += 1;
      },
    },
  };

  try {
    new TelegramBridge({}).dismissKeyboard();
  } finally {
    if (originalDocument === undefined) {
      delete globalThis.document;
    } else {
      globalThis.document = originalDocument;
    }
  }

  assert.equal(blurCount, 1);
});

test("does not blur non-editable active elements", () => {
  const originalDocument = globalThis.document;
  let blurCount = 0;
  globalThis.document = {
    activeElement: {
      matches: () => false,
      blur() {
        blurCount += 1;
      },
    },
  };

  try {
    new TelegramBridge({}).dismissKeyboard();
  } finally {
    if (originalDocument === undefined) {
      delete globalThis.document;
    } else {
      globalThis.document = originalDocument;
    }
  }

  assert.equal(blurCount, 0);
});
