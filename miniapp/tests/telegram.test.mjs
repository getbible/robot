import assert from "node:assert/strict";
import test from "node:test";

import { TelegramBridge } from "../lib/telegram.js";

function installDocument() {
  const originalDocument = globalThis.document;
  const properties = new Map();
  globalThis.document = {
    activeElement: null,
    documentElement: {
      dataset: {},
      style: {
        setProperty(name, value) {
          properties.set(name, value);
        },
      },
    },
  };
  return {
    properties,
    restore() {
      if (originalDocument === undefined) {
        delete globalThis.document;
      } else {
        globalThis.document = originalDocument;
      }
    },
  };
}

function fullscreenWebApp(overrides = {}) {
  const handlers = new Map();
  const calls = {
    expand: 0,
    fullscreen: 0,
    ready: 0,
  };
  const webApp = {
    initData: "query_id=test",
    colorScheme: "dark",
    version: "8.0",
    viewportStableHeight: 844,
    safeAreaInset: { top: 24, right: 2, bottom: 18, left: 3 },
    contentSafeAreaInset: { top: 48, right: 4, bottom: 20, left: 5 },
    isVersionAtLeast(version) {
      return version === "8.0";
    },
    onEvent(name, handler) {
      handlers.set(name, handler);
    },
    offEvent(name, handler) {
      if (handlers.get(name) === handler) {
        handlers.delete(name);
      }
    },
    expand() {
      calls.expand += 1;
    },
    requestFullscreen() {
      calls.fullscreen += 1;
    },
    ready() {
      calls.ready += 1;
    },
    ...overrides,
  };
  return { calls, handlers, webApp };
}

test("requests Telegram fullscreen and follows dynamic safe-area changes", () => {
  const mock = installDocument();
  const { calls, handlers, webApp } = fullscreenWebApp();
  const bridge = new TelegramBridge(webApp);

  try {
    assert.equal(bridge.initialize(), true);
    assert.deepEqual(calls, {
      expand: 1,
      fullscreen: 1,
      ready: 1,
    });
    assert.equal(
      mock.properties.get("--bridge-safe-area-inset-top"),
      "24px",
    );
    assert.equal(
      mock.properties.get("--bridge-content-safe-area-inset-bottom"),
      "20px",
    );
    assert.equal(mock.properties.get("--app-height"), "844px");
    assert.equal(globalThis.document.documentElement.dataset.theme, "dark");
    assert.equal(
      globalThis.document.documentElement.dataset.telegramFullscreen,
      "false",
    );

    webApp.viewportStableHeight = 900;
    webApp.isFullscreen = true;
    webApp.safeAreaInset = { top: 40, right: 0, bottom: 32, left: 0 };
    webApp.contentSafeAreaInset = {
      top: 64,
      right: 0,
      bottom: 36,
      left: 0,
    };
    handlers.get("fullscreenChanged")();
    assert.equal(
      globalThis.document.documentElement.dataset.telegramFullscreen,
      "true",
    );
    assert.equal(mock.properties.get("--app-height"), "900px");
    assert.equal(
      mock.properties.get("--bridge-safe-area-inset-top"),
      "40px",
    );
    assert.equal(
      mock.properties.get("--bridge-content-safe-area-inset-bottom"),
      "36px",
    );

    bridge.destroy();
    assert.deepEqual([...handlers.keys()], []);
    assert.equal(
      globalThis.document.documentElement.dataset.telegramFullscreen,
      undefined,
    );
  } finally {
    mock.restore();
  }
});

test("keeps expanded fallback when fullscreen is unsupported or throws", () => {
  const unsupported = installDocument();
  const unsupportedApp = fullscreenWebApp({
    version: "7.10",
    isVersionAtLeast: () => false,
  });
  try {
    assert.equal(
      new TelegramBridge(unsupportedApp.webApp).initialize(),
      true,
    );
    assert.equal(unsupportedApp.calls.expand, 1);
    assert.equal(unsupportedApp.calls.ready, 1);
    assert.equal(unsupportedApp.calls.fullscreen, 0);
  } finally {
    unsupported.restore();
  }

  const rejected = installDocument();
  let rejectedAttempts = 0;
  const rejectedApp = fullscreenWebApp({
    requestFullscreen() {
      rejectedAttempts += 1;
      throw new Error("client rejected fullscreen");
    },
  });
  try {
    assert.equal(new TelegramBridge(rejectedApp.webApp).initialize(), true);
    assert.equal(rejectedApp.calls.expand, 1);
    assert.equal(rejectedApp.calls.ready, 1);
    assert.equal(rejectedAttempts, 1);
  } finally {
    rejected.restore();
  }
});

test("uses the declared API version when the helper is unavailable", () => {
  for (const [version, expectedCalls] of [["8.0", 1], ["7.10", 0]]) {
    const mock = installDocument();
    const { calls, webApp } = fullscreenWebApp({
      version,
      isVersionAtLeast: undefined,
    });
    try {
      assert.equal(new TelegramBridge(webApp).initialize(), true);
      assert.equal(calls.fullscreen, expectedCalls);
    } finally {
      mock.restore();
    }
  }
});

test("does not request fullscreen again when Telegram is already fullscreen", () => {
  const mock = installDocument();
  const { calls, webApp } = fullscreenWebApp({ isFullscreen: true });
  try {
    assert.equal(new TelegramBridge(webApp).initialize(), true);
    assert.equal(calls.expand, 1);
    assert.equal(calls.fullscreen, 0);
  } finally {
    mock.restore();
  }
});

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
