import { ApiError } from "./api.js";

const SAFE_REQUEST_REFERENCE = /^[A-Za-z0-9._:-]{1,128}$/;
const SHORT_REQUEST_REFERENCE_LENGTH = 16;

/**
 * Convert contributor-sync failures into safe, actionable UI categories.
 *
 * Server messages can contain implementation detail and local exceptions can
 * contain bookmark data. Neither is rendered. A server-issued opaque request
 * identifier is the only error value that may cross into the presentation.
 */
export function contributionErrorPresentation(error, { catalog = false } = {}) {
  const reference = contributionRequestReference(error);
  let messageKey = catalog
    ? "bookmarks.contribution_sync_catalog_error"
    : "bookmarks.contribution_sync_error";

  if (
    error instanceof ApiError &&
    (
      [404, 405].includes(error.status) ||
      ["not_found", "method_not_allowed"].includes(error.code)
    )
  ) {
    messageKey = "bookmarks.contribution_sync_update_error";
  } else if (!catalog && error instanceof ApiError) {
    if (error.status === 429 || error.code === "rate_limited") {
      messageKey = "bookmarks.contribution_sync_retry_wait";
    } else if (
      error.status === 400 ||
      ["invalid_contribution", "invalid_request"].includes(error.code)
    ) {
      messageKey = "bookmarks.contribution_sync_invalid_data";
    } else if (
      error.status >= 500 ||
      ["contributions_unavailable", "invalid_response"].includes(error.code)
    ) {
      messageKey = "bookmarks.contribution_sync_server_error";
    }
  } else if (
    !catalog &&
    (error instanceof TypeError || error instanceof RangeError)
  ) {
    messageKey = "bookmarks.contribution_sync_local_error";
  }

  return {
    messageKey,
    values: reference === null ? {} : { reference },
  };
}

export function contributionRequestReference(error) {
  const value = error instanceof ApiError ? error.requestId : null;
  if (typeof value !== "string" || !SAFE_REQUEST_REFERENCE.test(value)) {
    return null;
  }
  return value.slice(0, SHORT_REQUEST_REFERENCE_LENGTH);
}
