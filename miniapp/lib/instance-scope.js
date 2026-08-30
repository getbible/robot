const INSTANCE_PATTERN = /^[a-f0-9]{16}$/;

/**
 * Namespaces server-owned browser state by the relative Robot API path.
 * localStorage and IndexedDB are origin-wide, while supported deployments may
 * host independent Robot instances below different paths on that origin.
 */
export function miniAppInstanceScope(
  baseUrl = globalThis.document?.baseURI ?? globalThis.location?.href,
) {
  let path;
  try {
    path = new URL("api/v1/", String(baseUrl)).pathname;
  } catch {
    throw new TypeError("A Mini App instance URL is required.");
  }
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < path.length; index += 1) {
    const unit = path.charCodeAt(index);
    left = Math.imul(left ^ unit, 0x01000193) >>> 0;
    right = Math.imul(right ^ unit, 0x85ebca6b) >>> 0;
  }
  return `${left.toString(16).padStart(8, "0")}${
    right.toString(16).padStart(8, "0")
  }`;
}

export function isMiniAppInstanceScope(value) {
  return typeof value === "string" && INSTANCE_PATTERN.test(value);
}
