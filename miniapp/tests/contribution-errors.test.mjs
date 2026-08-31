import assert from "node:assert/strict";
import test from "node:test";

import { ApiError } from "../lib/api.js";
import {
  contributionErrorPresentation,
  contributionRequestReference,
} from "../lib/contribution-errors.js";
import { ENGLISH_MESSAGES } from "../lib/messages.en.js";

test("contributor errors use safe actionable categories", () => {
  const cases = [
    {
      error: new ApiError("private rejected field", {
        code: "invalid_contribution",
        status: 400,
      }),
      key: "bookmarks.contribution_sync_invalid_data",
    },
    {
      error: new ApiError("private database detail", {
        code: "contributions_unavailable",
        status: 503,
      }),
      key: "bookmarks.contribution_sync_server_error",
    },
    {
      error: new ApiError("private proxy response", {
        code: "not_found",
        status: 404,
      }),
      key: "bookmarks.contribution_sync_update_error",
    },
    {
      error: new ApiError("private proxy response", {
        code: "method_not_allowed",
        status: 405,
      }),
      key: "bookmarks.contribution_sync_update_error",
    },
    {
      error: new ApiError("private limit detail", {
        code: "rate_limited",
        status: 429,
      }),
      key: "bookmarks.contribution_sync_retry_wait",
    },
    {
      error: new TypeError("private bookmark value"),
      key: "bookmarks.contribution_sync_local_error",
    },
  ];

  for (const { error, key } of cases) {
    assert.deepEqual(contributionErrorPresentation(error), {
      messageKey: key,
      values: {},
    });
  }
});

test("contributor errors expose only a short safe request reference", () => {
  const error = new ApiError("never rendered", {
    code: "invalid_contribution",
    status: 400,
    requestId: "request_0123456789abcdef",
  });
  assert.equal(contributionRequestReference(error), "request_01234567");
  assert.deepEqual(contributionErrorPresentation(error), {
    messageKey: "bookmarks.contribution_sync_invalid_data",
    values: { reference: "request_01234567" },
  });

  error.requestId = "unsafe request value";
  assert.equal(contributionRequestReference(error), null);
  assert.deepEqual(contributionErrorPresentation(error).values, {});
});

test("catalog failures keep the independently actionable catalog category", () => {
  const error = new ApiError("private upstream response", {
    code: "invalid_response",
    status: 503,
    requestId: "catalog-1234",
  });
  assert.deepEqual(contributionErrorPresentation(error, { catalog: true }), {
    messageKey: "bookmarks.contribution_sync_catalog_error",
    values: { reference: "catalog-1234" },
  });
});

test("a missing catalog route reports an incomplete server update", () => {
  const error = new ApiError("private proxy page", {
    code: "not_found",
    status: 404,
  });
  assert.deepEqual(contributionErrorPresentation(error, { catalog: true }), {
    messageKey: "bookmarks.contribution_sync_update_error",
    values: {},
  });
});

test("contributor failure guidance never requires an app lifecycle workaround", () => {
  const keys = [
    "bookmarks.contribution_sync_invalid_data",
    "bookmarks.contribution_sync_server_error",
    "bookmarks.contribution_sync_update_error",
    "bookmarks.contribution_sync_local_error",
    "bookmarks.contribution_sync_error",
    "bookmarks.contribution_sync_catalog_error",
  ];
  for (const key of keys) {
    assert.doesNotMatch(
      ENGLISH_MESSAGES[key],
      /\b(?:reopen|restart|close and open|when you are online)\b/i,
    );
  }
});
