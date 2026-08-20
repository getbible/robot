/**
 * Runs the destructive part of a chat restore in a testable order.
 *
 * The Telegram restore reference is acknowledged only after the user accepts,
 * the backup is merged, and at least one persistence layer reports success.
 */
export async function restoreBookmarkBackup({
  fetchRestore,
  confirmRestore,
  importBackup,
  flushPersistence,
  acknowledgeRestore,
}) {
  requireFunction(fetchRestore, "fetchRestore");
  requireFunction(confirmRestore, "confirmRestore");
  requireFunction(importBackup, "importBackup");
  requireFunction(flushPersistence, "flushPersistence");
  requireFunction(acknowledgeRestore, "acknowledgeRestore");

  const payload = await fetchRestore();
  if (!(await confirmRestore(payload))) {
    return { status: "declined", payload };
  }

  const source = payload?.source ?? {};
  const imported = importBackup(payload?.backup, {
    byteLength: Number.isSafeInteger(source.file_size)
      ? source.file_size
      : null,
  });
  const persistence = await flushPersistence();
  if (persistence && !hasSuccessfulPersistence(persistence)) {
    throw new Error("The restored bookmarks could not be persisted.");
  }

  let acknowledgementError = null;
  try {
    await acknowledgeRestore();
  } catch (error) {
    acknowledgementError = error;
  }
  return {
    status: "restored",
    payload,
    imported,
    acknowledgementError,
  };
}

function hasSuccessfulPersistence(status) {
  return [status.local, status.device, status.cloud].some(
    (value) => ["ready", "synced"].includes(value),
  );
}

function requireFunction(value, name) {
  if (typeof value !== "function") {
    throw new TypeError(`${name} must be a function.`);
  }
}
