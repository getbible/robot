import { isCanonicalVerseCoordinate } from "./bible-canon.js";
import { IndexedDbContributionJournal } from "./contribution-journal.js";
import {
  MAX_PUSH_CHUNKS,
  MAX_PUSH_MESSAGE_BYTES,
  PUSH_PROTOCOL_PREFIX,
} from "./contribution-push.js";
import {
  isMiniAppInstanceScope,
  miniAppInstanceScope,
} from "./instance-scope.js";
import {
  contributionReviewDetailsAvailable,
  normalizeContributionStatus,
} from "./model.js";

// Keep the established key while migrating its value in place. A new key
// would strand an unacknowledged v1 global-preference operation.
const STORAGE_PREFIX = "getbible.miniapp.contributions.v1";
const STORAGE_VERSION = 3;
const PROTOCOL_VERSION = 1;
const MAX_BATCH_SIZE = 50; // Compatibility export; snapshot sync is unbatched.
const MAX_EXPLICIT_OPERATIONS = 2_000;
const MAX_SNAPSHOT_ASSIGNMENTS = 10_000;
const MAX_PERSISTED_STATE_BYTES = 1024 * 1024;
const MAX_SYNC_ENVELOPE_BYTES = 1024 * 1024;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const CLIENT_ID_PATTERN = /^[A-Za-z0-9._:-]{1,80}$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const ENGLISH_TOPIC_PATTERN =
  /^(?=[A-Za-z0-9 &'():?-]{2,80}$)(?=.*[A-Za-z])(?!.* {2})[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]$/;

/**
 * Durable local state for the Telegram sendData contribution push.
 *
 * BookmarkStore is the source of truth for personal topics. This class only
 * persists the exact push outbox (envelope plus its encoded GBC1 messages)
 * and explicit global add/remove intents which cannot be reconstructed from
 * BookmarkStore. Explicit intents clear only when a server receipt confirms
 * them through the pull side, so a lost transfer is always resendable.
 */
export class ContributionSync {
  #coreTopicIds;
  #coreTopics;
  #idFactory;
  #initialRaw;
  #journal;
  #key;
  #persistenceFailed = false;
  #persistTail = Promise.resolve();
  #state;
  #storage;

  static async open(options = {}) {
    const instanceScope = options.instanceScope ?? defaultInstanceScope();
    const key = contributionStorageKey(options.scope, instanceScope);
    const journal = options.journal ??
      await IndexedDbContributionJournal.open({ key });
    if (
      !journal ||
      typeof journal.read !== "function" ||
      typeof journal.write !== "function" ||
      typeof journal.remove !== "function"
    ) {
      throw new TypeError("Transactional contribution storage is unavailable.");
    }

    let initialRaw = await journal.read();
    if (initialRaw === null) {
      const legacyStorage = storageLike(options.legacyStorage)
        ? options.legacyStorage
        : browserLocalStorage();
      const legacyKeys = [
        STORAGE_PREFIX + ":" + instanceScope + ":" + options.scope,
        STORAGE_PREFIX + ":" + options.scope,
      ];
      for (const legacyKey of legacyKeys) {
        let candidate = null;
        try {
          candidate = legacyStorage?.getItem(legacyKey) ?? null;
        } catch {
          candidate = null;
        }
        if (!migratableRawState(candidate)) {
          continue;
        }
        await journal.write(candidate);
        initialRaw = candidate;
        try {
          legacyStorage.removeItem(legacyKey);
        } catch {
          // The transactional copy is authoritative after it commits.
        }
        break;
      }
    }

    const sync = new ContributionSync({
      ...options,
      initialStatus: undefined,
      instanceScope,
      storage: null,
      journal,
      initialRaw,
    });
    if (options.initialStatus !== undefined) {
      await sync.seedStatus(options.initialStatus);
    } else {
      await sync.#persist();
    }
    return sync;
  }

  constructor({
    scope,
    coreTopics = [],
    coreTopicIds = [],
    storage = browserLocalStorage(),
    idFactory = defaultToken,
    instanceScope = defaultInstanceScope(),
    journal = null,
    initialRaw = undefined,
    initialStatus = undefined,
  } = {}) {
    if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
      throw new TypeError("An authenticated contribution scope is required.");
    }
    if (
      !Array.isArray(coreTopics) ||
      !Array.isArray(coreTopicIds) ||
      typeof idFactory !== "function" ||
      !isMiniAppInstanceScope(instanceScope)
    ) {
      throw new TypeError("Contribution sync dependencies are invalid.");
    }

    this.#coreTopics = new Map();
    for (const topic of coreTopics) {
      const normalized = normalizeContributionTopic(topic);
      if (this.#coreTopics.has(normalized.id)) {
        throw new TypeError("A core contribution topic is duplicated.");
      }
      this.#coreTopics.set(normalized.id, normalized);
    }
    this.#coreTopicIds = new Set(this.#coreTopics.keys());
    for (const id of coreTopicIds) {
      if (typeof id !== "string" || !SAFE_ID_PATTERN.test(id)) {
        throw new TypeError("A core contribution topic ID is invalid.");
      }
      this.#coreTopicIds.add(id);
    }

    this.#idFactory = idFactory;
    this.#journal = journal;
    this.#key = contributionStorageKey(scope, instanceScope);
    this.#storage = journal ? null : storageLike(storage) ? storage : null;
    this.#initialRaw = initialRaw;
    this.#state = this.#read();
    this.#persistenceFailed = !journal && this.#storage === null;
    if (initialStatus !== undefined) {
      this.#applyStatus(normalizeContributionStatus(initialStatus));
      void this.#persist().catch(() => undefined);
    }
  }

  get canContribute() {
    return this.#state.status.enabled && this.#state.status.can_contribute;
  }

  get disclosureRequired() {
    return this.canContribute && this.#state.status.disclosure_required;
  }

  get contributorState() {
    return this.#state.status.state;
  }

  get status() {
    return cloneContributionStatus(this.#state.status);
  }

  get reviewDetailsAvailable() {
    return contributionReviewDetailsAvailable(this.#state.status);
  }

  get topicOutcomes() {
    return this.status.topics;
  }

  get reviewSummary() {
    return this.status.summary;
  }

  get pendingCount() {
    return this.#state.operations.length + (this.#state.dirty ? 1 : 0);
  }

  get persistenceFailed() {
    return this.#persistenceFailed;
  }

  // Compatibility accessors during a rolling client/server deployment.
  get baselineComplete() {
    return !this.#state.dirty;
  }

  get overflowed() {
    return false;
  }

  get recovering() {
    return false;
  }

  async seedStatus(value) {
    this.#applyStatus(normalizeContributionStatus(value));
    await this.#persist().catch(() => false);
    return this.status;
  }

  /** Personal state is reconciled from the next current BookmarkStore snapshot. */
  captureMutation(_before, after) {
    normalizeDesiredSnapshot(after, this.#coreTopics, this.#coreTopicIds);
    this.#state.dirty = true;
    this.#state.revision += 1;
    return this.#persistCapture(1);
  }

  captureGlobalRemoval(topic, verse, _snapshot) {
    return this.#captureGlobalAssignments(
      "verse_remove",
      [{ topic, verse }],
    );
  }

  captureGlobalAddition(topic, verse, _snapshot) {
    return this.#captureGlobalAssignments(
      "verse_add",
      [{ topic, verse }],
    );
  }

  captureGlobalAdditions(assignments, _snapshot) {
    return this.#captureGlobalAssignments("verse_add", assignments);
  }

  #captureGlobalAssignments(type, assignments) {
    if (!this.canContribute) {
      return 0;
    }
    if (
      !["verse_add", "verse_remove"].includes(type) ||
      !Array.isArray(assignments)
    ) {
      throw new TypeError("Global contribution assignments are invalid.");
    }

    const incoming = new Map();
    for (const assignment of assignments) {
      const supplied = normalizeContributionTopic(assignment?.topic);
      const authoritative = this.#coreTopics.get(supplied.id);
      if (this.#coreTopicIds.has(supplied.id) && !authoritative) {
        throw new TypeError(
          "Authoritative metadata is required for a global contribution topic.",
        );
      }
      const topic = authoritative ?? supplied;
      if (!isCanonicalVerseCoordinate(assignment?.verse)) {
        throw new TypeError("A canonical global bookmark coordinate is required.");
      }
      const verse = {
        book: assignment.verse.book,
        chapter: assignment.verse.chapter,
        verse: assignment.verse.verse,
      };
      incoming.set(operationKey(topic.id, verse), { topic, verse });
    }

    let changed = 0;
    for (const [key, value] of incoming) {
      const index = this.#state.operations.findIndex((operation) =>
        operationKey(operation.topic.local_topic_id, operation.verse) === key
      );
      const current = index >= 0 ? this.#state.operations[index] : null;
      if (current?.type === type && sameOperationPayload(current, value)) {
        continue;
      }
      const operation = {
        client_event_id: this.#nextId("e"),
        type,
        topic: {
          local_topic_id: value.topic.id,
          name: value.topic.name,
          color: value.topic.color,
        },
        verse: value.verse,
      };
      if (index >= 0) {
        this.#state.operations[index] = operation;
      } else {
        if (this.#state.operations.length >= MAX_EXPLICIT_OPERATIONS) {
          throw new RangeError(
            "Too many explicit contribution operations are pending.",
          );
        }
        this.#state.operations.push(operation);
      }
      changed += 1;
    }
    if (changed === 0) {
      return 0;
    }
    this.#state.dirty = true;
    this.#state.revision += 1;
    return this.#persistCapture(changed);
  }

  get outbox() {
    const outbox = this.#state.outbox;
    if (!outbox) {
      return null;
    }
    return {
      sync_id: outbox.envelope.sync_id,
      fingerprint: outbox.fingerprint,
      total: outbox.messages.length,
      attempt_index: outbox.attempt_index,
      sent_all: outbox.attempt_index >= outbox.messages.length,
      disclosure_acknowledged: outbox.envelope.disclosure_acknowledged,
    };
  }

  /**
   * Build (or resume) the durable push outbox for the current desired state.
   *
   * ``encode`` turns one envelope into its GBC1 sendData messages. Identical
   * content resumes the existing outbox byte-for-byte, so re-pushing after a
   * lost transfer replays the same sync identity the server can deduplicate.
   */
  async preparePush(snapshot, { disclosureAcknowledged = false, encode } = {}) {
    if (typeof encode !== "function") {
      throw new TypeError("A push message encoder is required.");
    }
    const desired = normalizeDesiredSnapshot(
      snapshot,
      this.#coreTopics,
      this.#coreTopicIds,
    );
    const operations = this.#state.operations.map(cloneOperation);
    const descriptor = {
      snapshot: desired,
      operations,
      disclosure_acknowledged: disclosureAcknowledged === true,
    };
    const fingerprint = valueFingerprint(descriptor);
    const existing = this.#state.outbox;
    if (
      existing?.fingerprint === fingerprint &&
      outboxMatchesDescriptor(existing.envelope, descriptor)
    ) {
      return this.outbox;
    }
    const envelope = {
      protocol_version: PROTOCOL_VERSION,
      sync_id: this.#nextId("s", fingerprint),
      client_id: this.#state.client_id,
      snapshot: desired,
      operations,
      disclosure_acknowledged: disclosureAcknowledged === true,
    };
    if (utf8Length(JSON.stringify(envelope)) > MAX_SYNC_ENVELOPE_BYTES) {
      throw new RangeError("The contribution snapshot is too large to push.");
    }
    const encoded = await encode(cloneEnvelope(envelope));
    const messages = Array.isArray(encoded?.messages) ? encoded.messages : null;
    if (
      !messages ||
      messages.length < 1 ||
      messages.length > MAX_PUSH_CHUNKS ||
      !messages.every((message) =>
        typeof message === "string" &&
        message.startsWith(PUSH_PROTOCOL_PREFIX + "|") &&
        utf8Length(message) <= MAX_PUSH_MESSAGE_BYTES
      )
    ) {
      throw new TypeError("The encoded push messages are invalid.");
    }
    this.#state.outbox = {
      fingerprint,
      envelope: cloneEnvelope(envelope),
      messages: [...messages],
      attempt_index: 0,
      revision: this.#state.revision,
    };
    // The exact transfer commits durably before the first sendData call.
    // Journal-backed persistence rejects on failure while the localStorage
    // fallback resolves false; both must leave no phantom in-memory outbox.
    let persisted = false;
    try {
      persisted = await this.#persist();
    } finally {
      if (!persisted) {
        this.#state.outbox = null;
      }
    }
    if (!persisted) {
      throw new Error(
        "Contribution retry storage is unavailable; the push was not prepared.",
      );
    }
    return this.outbox;
  }

  /**
   * Durably advance the outbox pointer and return the message to send.
   *
   * The pointer commits before transport so a crash between persist and
   * sendData can only cause a redundant resend, which the bot's chunk
   * staging and sync receipts absorb safely.
   */
  async takeNextPushMessage() {
    const outbox = this.#state.outbox;
    if (!outbox || outbox.attempt_index >= outbox.messages.length) {
      return null;
    }
    const message = outbox.messages[outbox.attempt_index];
    outbox.attempt_index += 1;
    // Journal-backed persistence rejects on failure while the localStorage
    // fallback resolves false; both must roll the pointer back so no chunk
    // is ever skipped by a phantom in-memory advance.
    let persisted = false;
    try {
      persisted = await this.#persist();
    } finally {
      if (!persisted) {
        outbox.attempt_index -= 1;
      }
    }
    if (!persisted) {
      throw new Error(
        "Contribution retry storage is unavailable; the push was not sent.",
      );
    }
    return message;
  }

  /** Undo one pointer advance after a synchronous sendData failure. */
  async rewindPushMessage() {
    const outbox = this.#state.outbox;
    if (!outbox || outbox.attempt_index === 0) {
      return false;
    }
    outbox.attempt_index -= 1;
    await this.#persist().catch(() => false);
    return true;
  }

  /** Resend a fully-sent but unconfirmed transfer from its first message. */
  async restartPush() {
    const outbox = this.#state.outbox;
    if (!outbox) {
      return false;
    }
    outbox.attempt_index = 0;
    await this.#persist().catch(() => false);
    return true;
  }

  /**
   * Settle the outbox against a server receipt observed through the pull side.
   *
   * Explicit operations clear only here: a transfer that never produced a
   * receipt keeps its intents queued, so nothing is lost to a failed push.
   */
  async confirmReceipt(receipt) {
    const outbox = this.#state.outbox;
    if (
      !outbox ||
      typeof receipt?.sync_id !== "string" ||
      receipt.sync_id !== outbox.envelope.sync_id
    ) {
      return false;
    }
    const sentIds = new Set(
      outbox.envelope.operations.map((operation) => operation.client_event_id),
    );
    this.#state.operations = this.#state.operations.filter(
      (operation) => !sentIds.has(operation.client_event_id),
    );
    if (this.#state.revision === outbox.revision) {
      this.#state.dirty = false;
    }
    this.#state.outbox = null;
    // The server receipt is authoritative. If local cleanup cannot persist,
    // the durable outbox remains and a later launch makes the same safe
    // confirmation again; never relabel the confirmed commit as a failure.
    await this.#persist().catch(() => false);
    return true;
  }

  async discardPush() {
    if (!this.#state.outbox) {
      return false;
    }
    this.#state.outbox = null;
    await this.#persist().catch(() => false);
    return true;
  }

  #applyStatus(status) {
    this.#state.status = cloneContributionStatus(status);
  }

  #nextId(kind, suffix = "") {
    const sequence = this.#state.next_sequence;
    if (!Number.isSafeInteger(sequence) || sequence >= Number.MAX_SAFE_INTEGER) {
      throw new RangeError("The contribution retry sequence is exhausted.");
    }
    this.#state.next_sequence += 1;
    const id = [
      this.#state.client_id,
      kind,
      sequence.toString(36),
      ...(suffix ? [suffix] : []),
    ].join(":");
    if (!SAFE_ID_PATTERN.test(id)) {
      throw new RangeError("The contribution request identity is exhausted.");
    }
    return id;
  }

  #persistCapture(count) {
    const persisted = this.#persist();
    if (this.#journal) {
      return persisted.then(() => count);
    }
    void persisted.catch(() => undefined);
    return count;
  }

  #persist() {
    if (!this.#journal && !this.#storage) {
      this.#persistenceFailed = true;
      return Promise.resolve(false);
    }
    const raw = JSON.stringify(this.#state);
    if (utf8Length(raw) > MAX_PERSISTED_STATE_BYTES) {
      this.#persistenceFailed = true;
      return Promise.reject(
        new RangeError("Contribution retry state is too large."),
      );
    }
    if (this.#journal) {
      const write = () => this.#journal.write(raw).then(() => {
        this.#persistenceFailed = false;
        return true;
      });
      const result = this.#persistTail.then(write, write);
      this.#persistTail = result.catch(() => undefined);
      return result.catch((error) => {
        this.#persistenceFailed = true;
        throw error;
      });
    }
    try {
      this.#storage.setItem(this.#key, raw);
      this.#persistenceFailed = false;
      return Promise.resolve(true);
    } catch {
      this.#persistenceFailed = true;
      return Promise.resolve(false);
    }
  }

  #read() {
    const fresh = freshState(this.#idFactory);
    let raw = this.#initialRaw;
    this.#initialRaw = undefined;
    if (raw === undefined) {
      try {
        raw = this.#storage?.getItem(this.#key) ?? null;
      } catch {
        raw = null;
        this.#storage = null;
      }
    }
    if (!raw) {
      return fresh;
    }
    try {
      const parsed = JSON.parse(raw);
      if (validV3State(parsed)) {
        return cloneState(parsed);
      }
      if (validV2State(parsed)) {
        return migrateV2State(parsed, fresh);
      }
      if (parsed?.version === 1) {
        return migrateV1State(parsed, fresh);
      }
      throw new TypeError("Contribution retry state is invalid.");
    } catch {
      if (this.#journal) {
        void this.#journal.remove().catch(() => undefined);
      } else {
        try {
          this.#storage?.removeItem(this.#key);
        } catch {
          this.#storage = null;
        }
      }
      return fresh;
    }
  }
}

/** Compatibility helpers retained for importers and focused diff tests. */
export function contributionEventsForDiff(before, after) {
  const left = normalizeBookmarkSnapshot(before);
  const right = normalizeBookmarkSnapshot(after);
  const beforeTopics = new Map(left.topics.map((topic) => [topic.id, topic]));
  const afterTopics = new Map(right.topics.map((topic) => [topic.id, topic]));
  const events = [];

  for (const topic of right.topics) {
    const previous = beforeTopics.get(topic.id);
    if (
      isEnglishContributionTopicName(topic.name) &&
      (!previous || previous.name !== topic.name || previous.color !== topic.color)
    ) {
      events.push(topicUpsertEvent(topic));
    }
  }
  const beforeAssignments = assignmentMap(left, beforeTopics);
  const afterAssignments = assignmentMap(right, afterTopics);
  for (const [key, assignment] of beforeAssignments) {
    if (!afterAssignments.has(key)) {
      events.push(verseEvent("verse_remove", assignment));
    }
  }
  for (const [key, assignment] of afterAssignments) {
    if (!beforeAssignments.has(key)) {
      events.push(verseEvent("verse_add", assignment));
    }
  }
  for (const topic of left.topics) {
    if (!afterTopics.has(topic.id) && isEnglishContributionTopicName(topic.name)) {
      events.push({
        type: "topic_delete",
        topic: { local_topic_id: topic.id },
      });
    }
  }
  return events;
}

export function baselineContributionEvents(snapshot, coreTopicIds = new Set()) {
  const normalized = normalizeBookmarkSnapshot(snapshot);
  const topics = new Map(normalized.topics.map((topic) => [topic.id, topic]));
  const assignments = assignmentMap(normalized, topics);
  const referenced = new Set(
    [...assignments.values()].map((assignment) => assignment.topic.id),
  );
  const events = [];
  for (const topic of normalized.topics) {
    if (
      isEnglishContributionTopicName(topic.name) &&
      (!coreTopicIds.has(topic.id) || referenced.has(topic.id))
    ) {
      events.push(withBaselineId(topicUpsertEvent(topic)));
    }
  }
  for (const assignment of assignments.values()) {
    events.push(withBaselineId(verseEvent("verse_add", assignment)));
  }
  return events;
}

export function isEnglishContributionTopicName(value) {
  if (typeof value !== "string") {
    return false;
  }
  return ENGLISH_TOPIC_PATTERN.test(value.trim().normalize("NFC"));
}

export const CONTRIBUTION_STORAGE_PREFIX = STORAGE_PREFIX;
export const CONTRIBUTION_BATCH_MAXIMUM = MAX_BATCH_SIZE;
export const CONTRIBUTION_OUTBOX_MAXIMUM = MAX_EXPLICIT_OPERATIONS;
export const CONTRIBUTION_ASSIGNMENT_MAXIMUM = MAX_SNAPSHOT_ASSIGNMENTS;

function normalizeDesiredSnapshot(value, coreTopics, coreTopicIds) {
  const normalized = normalizeBookmarkSnapshot(value);
  const referencedIds = new Set();
  for (const bookmark of normalized.bookmarks) {
    if (!isCanonicalVerseCoordinate(bookmark)) {
      continue;
    }
    for (const id of bookmark.topic_ids) {
      referencedIds.add(id);
    }
  }

  const topics = new Map();
  for (const local of normalized.topics) {
    const authoritative = coreTopics.get(local.id);
    if (coreTopicIds.has(local.id)) {
      // Never turn a locally selected color for a global topic into a proposal.
      if (authoritative && referencedIds.has(local.id)) {
        topics.set(local.id, authoritative);
      }
      continue;
    }
    if (isEnglishContributionTopicName(local.name)) {
      topics.set(local.id, local);
    }
  }

  const assignments = new Map();
  for (const bookmark of normalized.bookmarks) {
    if (!isCanonicalVerseCoordinate(bookmark)) {
      continue;
    }
    for (const topicId of bookmark.topic_ids) {
      if (!topics.has(topicId)) {
        continue;
      }
      const assignment = {
        topic_id: topicId,
        book: bookmark.book,
        chapter: bookmark.chapter,
        verse: bookmark.verse,
      };
      assignments.set(topicId + ":" + assignmentKey(assignment), assignment);
      if (assignments.size > MAX_SNAPSHOT_ASSIGNMENTS) {
        throw new RangeError("Too many contribution assignments are present.");
      }
    }
  }
  return {
    topics: [...topics.values()]
      .sort((left, right) => left.id.localeCompare(right.id))
      .map((topic) => ({ ...topic })),
    assignments: [...assignments.values()].sort(compareAssignments),
  };
}

function normalizeBookmarkSnapshot(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !Array.isArray(value.topics) ||
    value.topics.length > 100 ||
    !Array.isArray(value.bookmarks) ||
    value.bookmarks.length > 800
  ) {
    throw new TypeError("A bookmark snapshot is required.");
  }
  const topics = value.topics.map((topic) => {
    const id = String(topic?.id ?? "");
    const name = String(topic?.name ?? "").trim().normalize("NFC");
    const color = String(topic?.color ?? "").toLowerCase();
    if (
      !SAFE_ID_PATTERN.test(id) ||
      name.length < 1 ||
      name.length > 80 ||
      !COLOR_PATTERN.test(color)
    ) {
      throw new TypeError("A bookmark topic is invalid.");
    }
    return { id, name, color };
  });
  const topicIds = new Set(topics.map((topic) => topic.id));
  if (topicIds.size !== topics.length) {
    throw new TypeError("A bookmark topic is invalid.");
  }
  const bookmarks = value.bookmarks.map((bookmark) => {
    const ids = Array.isArray(bookmark?.topic_ids)
      ? [...new Set(bookmark.topic_ids)]
      : [bookmark?.topic_id];
    if (
      ids.length < 1 ||
      ids.length > 100 ||
      ids.some((id) => !topicIds.has(id)) ||
      !boundedInteger(bookmark?.book, 1, 200) ||
      !boundedInteger(bookmark?.chapter, 1, 1_000) ||
      !boundedInteger(bookmark?.verse, 1, 2_000)
    ) {
      throw new TypeError("A bookmark assignment is invalid.");
    }
    return {
      topic_ids: ids,
      book: bookmark.book,
      chapter: bookmark.chapter,
      verse: bookmark.verse,
    };
  });
  return { topics, bookmarks };
}

function outboxMatchesDescriptor(envelope, descriptor) {
  // Shapes are canonicalized and ordered before this comparison. The hash is
  // only a compact ID suffix; exact equality decides whether a transfer is
  // safe to resume with the same sync identity.
  return JSON.stringify({
    snapshot: envelope.snapshot,
    operations: envelope.operations,
    disclosure_acknowledged: envelope.disclosure_acknowledged,
  }) === JSON.stringify(descriptor);
}

function freshState(idFactory) {
  return {
    version: STORAGE_VERSION,
    client_id: normalizeGeneratedId(idFactory()),
    next_sequence: 0,
    dirty: true,
    revision: 0,
    operations: [],
    outbox: null,
    status: unavailableStatus(),
  };
}

function migrateV1State(value, fresh) {
  fresh.status = normalizeContributionStatus({
    enabled: value.contributor_state !== "unavailable",
    state: typeof value.contributor_state === "string"
      ? value.contributor_state
      : "not_applied",
    can_contribute: value.approved === true,
    disclosure_required:
      value.approved === true && value.disclosure_required === true,
  });
  const candidates = [];
  if (Array.isArray(value.outbox)) {
    candidates.push(...value.outbox.filter((event) =>
      typeof event?.client_event_id === "string" &&
      event.client_event_id.startsWith("event:g:")
    ));
  }
  candidates.push(...legacyRecoveryOperations(value.recovery_external));
  candidates.push(...legacyRecoveryOperations(value.recovery_external_latest));
  for (const candidate of candidates) {
    const operation = normalizeLegacyOperation(candidate, fresh);
    if (!operation) {
      continue;
    }
    const key = operationKey(operation.topic.local_topic_id, operation.verse);
    const index = fresh.operations.findIndex((current) =>
      operationKey(current.topic.local_topic_id, current.verse) === key
    );
    if (index >= 0) {
      fresh.operations[index] = operation;
    } else if (fresh.operations.length < MAX_EXPLICIT_OPERATIONS) {
      fresh.operations.push(operation);
    }
  }
  return fresh;
}

function legacyRecoveryOperations(value) {
  if (!value || !Array.isArray(value.t) || !Array.isArray(value.e)) {
    return [];
  }
  return value.e.flatMap((row) => {
    const legacy = Array.isArray(row) && row.length === 4;
    const operation = legacy ? 0 : row?.[0];
    const topicIndex = legacy ? row?.[0] : row?.[1];
    const topic = value.t[topicIndex];
    const offset = legacy ? 1 : 2;
    return Array.isArray(topic) && topic.length === 3
      ? [{
          type: operation === 1 ? "verse_add" : "verse_remove",
          topic: {
            local_topic_id: topic[0],
            name: topic[1],
            color: topic[2],
          },
          verse: {
            book: row?.[offset],
            chapter: row?.[offset + 1],
            verse: row?.[offset + 2],
          },
        }]
      : [];
  });
}

function normalizeLegacyOperation(value, state) {
  try {
    if (!["verse_add", "verse_remove"].includes(value?.type)) {
      return null;
    }
    const topic = normalizeContributionTopic({
      id: value.topic?.local_topic_id,
      name: value.topic?.name,
      color: value.topic?.color,
    });
    if (!isCanonicalVerseCoordinate(value.verse)) {
      return null;
    }
    let clientEventId = value.client_event_id;
    if (typeof clientEventId !== "string" || !SAFE_ID_PATTERN.test(clientEventId)) {
      clientEventId = [
        state.client_id,
        "e",
        state.next_sequence.toString(36),
      ].join(":");
      state.next_sequence += 1;
    }
    return {
      client_event_id: clientEventId,
      type: value.type,
      topic: {
        local_topic_id: topic.id,
        name: topic.name,
        color: topic.color,
      },
      verse: {
        book: value.verse.book,
        chapter: value.verse.chapter,
        verse: value.verse.verse,
      },
    };
  } catch {
    return null;
  }
}

function validV3State(value) {
  try {
    return Boolean(
      value &&
      value.version === STORAGE_VERSION &&
      validCommonState(value) &&
      (value.outbox === null || validOutbox(value.outbox)) &&
      (
        value.outbox === null ||
        value.outbox.envelope.client_id === value.client_id
      )
    );
  } catch {
    return false;
  }
}

function validV2State(value) {
  try {
    return Boolean(
      value &&
      value.version === 2 &&
      validCommonState(value) &&
      (value.inflight === null || validEnvelopeCarrier(value.inflight)) &&
      (
        value.inflight === null ||
        value.inflight.envelope.client_id === value.client_id
      )
    );
  } catch {
    return false;
  }
}

function validCommonState(value) {
  return Boolean(
    CLIENT_ID_PATTERN.test(value.client_id) &&
    Number.isSafeInteger(value.next_sequence) &&
    value.next_sequence >= 0 &&
    typeof value.dirty === "boolean" &&
    Number.isSafeInteger(value.revision) &&
    value.revision >= 0 &&
    Array.isArray(value.operations) &&
    value.operations.length <= MAX_EXPLICIT_OPERATIONS &&
    value.operations.every(validOperation) &&
    Boolean(normalizeContributionStatus(value.status))
  );
}

function validEnvelopeCarrier(value) {
  return Boolean(
    value &&
    typeof value === "object" &&
    /^[a-f0-9]{16}$/.test(value.fingerprint) &&
    value.envelope?.protocol_version === PROTOCOL_VERSION &&
    SAFE_ID_PATTERN.test(value.envelope?.sync_id ?? "") &&
    CLIENT_ID_PATTERN.test(value.envelope?.client_id ?? "") &&
    Array.isArray(value.envelope?.snapshot?.topics) &&
    Array.isArray(value.envelope?.snapshot?.assignments) &&
    Array.isArray(value.envelope?.operations) &&
    typeof value.envelope?.disclosure_acknowledged === "boolean"
  );
}

function validOutbox(value) {
  return Boolean(
    validEnvelopeCarrier(value) &&
    Array.isArray(value.messages) &&
    value.messages.length >= 1 &&
    value.messages.length <= MAX_PUSH_CHUNKS &&
    value.messages.every((message) =>
      typeof message === "string" &&
      message.startsWith(PUSH_PROTOCOL_PREFIX + "|") &&
      utf8Length(message) <= MAX_PUSH_MESSAGE_BYTES
    ) &&
    Number.isSafeInteger(value.attempt_index) &&
    value.attempt_index >= 0 &&
    value.attempt_index <= value.messages.length &&
    Number.isSafeInteger(value.revision) &&
    value.revision >= 0
  );
}

function migrateV2State(value, fresh) {
  fresh.client_id = value.client_id;
  fresh.next_sequence = value.next_sequence;
  // A pending v2 HTTP envelope cannot travel the sendData transport; drop it
  // and keep the state dirty so the next push rebuilds the same content. Its
  // explicit operations are still queued below, so nothing is lost.
  fresh.dirty = value.dirty || value.inflight !== null;
  fresh.revision = value.revision;
  fresh.operations = value.operations.map(cloneOperation);
  fresh.outbox = null;
  fresh.status = cloneContributionStatus(value.status);
  return fresh;
}

function validOperation(value) {
  return Boolean(
    value &&
    SAFE_ID_PATTERN.test(value.client_event_id ?? "") &&
    normalizeLegacyOperation(value, {
      client_id: "validation",
      next_sequence: 0,
    }),
  );
}

function cloneState(value) {
  return {
    version: STORAGE_VERSION,
    client_id: value.client_id,
    next_sequence: value.next_sequence,
    dirty: value.dirty,
    revision: value.revision,
    operations: value.operations.map(cloneOperation),
    outbox: value.outbox === null
      ? null
      : {
          fingerprint: value.outbox.fingerprint,
          envelope: cloneEnvelope(value.outbox.envelope),
          messages: [...value.outbox.messages],
          attempt_index: value.outbox.attempt_index,
          revision: value.outbox.revision,
        },
    status: cloneContributionStatus(value.status),
  };
}

function migratableRawState(raw) {
  if (typeof raw !== "string" || utf8Length(raw) > MAX_PERSISTED_STATE_BYTES) {
    return false;
  }
  try {
    const value = JSON.parse(raw);
    return value?.version === 1 || validV2State(value) || validV3State(value);
  } catch {
    return false;
  }
}

function assignmentMap(snapshot, topics) {
  const assignments = new Map();
  for (const bookmark of snapshot.bookmarks) {
    if (!isCanonicalVerseCoordinate(bookmark)) {
      continue;
    }
    for (const topicId of bookmark.topic_ids) {
      const topic = topics.get(topicId);
      if (!topic || !isEnglishContributionTopicName(topic.name)) {
        continue;
      }
      const verse = {
        book: bookmark.book,
        chapter: bookmark.chapter,
        verse: bookmark.verse,
      };
      assignments.set(topicId + ":" + assignmentKey(verse), { topic, verse });
    }
  }
  return assignments;
}

function topicUpsertEvent(topic) {
  return {
    type: "topic_upsert",
    topic: { local_topic_id: topic.id, name: topic.name, color: topic.color },
  };
}

function verseEvent(type, assignment) {
  return {
    type,
    topic: {
      local_topic_id: assignment.topic.id,
      name: assignment.topic.name,
      color: assignment.topic.color,
    },
    verse: { ...assignment.verse },
  };
}

function withBaselineId(event) {
  return {
    client_event_id:
      "baseline:" + event.type + ":" + valueFingerprint(event),
    ...event,
  };
}

function normalizeContributionTopic(value) {
  const id = String(value?.id ?? value?.local_topic_id ?? "");
  const name = String(value?.name ?? "").trim().normalize("NFC");
  const color = String(value?.color ?? "").toLowerCase();
  if (
    !SAFE_ID_PATTERN.test(id) ||
    !isEnglishContributionTopicName(name) ||
    !COLOR_PATTERN.test(color)
  ) {
    throw new TypeError("An English contribution topic is required.");
  }
  return { id, name, color };
}

function cloneContributionStatus(value) {
  return normalizeContributionStatus(value);
}

function cloneOperation(operation) {
  return {
    client_event_id: operation.client_event_id,
    type: operation.type,
    topic: { ...operation.topic },
    verse: { ...operation.verse },
  };
}

function cloneEnvelope(envelope) {
  return {
    protocol_version: envelope.protocol_version,
    sync_id: envelope.sync_id,
    client_id: envelope.client_id,
    snapshot: {
      topics: envelope.snapshot.topics.map((topic) => ({ ...topic })),
      assignments: envelope.snapshot.assignments.map((assignment) => ({
        ...assignment,
      })),
    },
    operations: envelope.operations.map(cloneOperation),
    disclosure_acknowledged: envelope.disclosure_acknowledged,
  };
}

function sameOperationPayload(operation, value) {
  return operation.topic.name === value.topic.name &&
    operation.topic.color === value.topic.color;
}

function operationKey(topicId, verse) {
  return topicId + ":" + assignmentKey(verse);
}

function assignmentKey(value) {
  return value.book + ":" + value.chapter + ":" + value.verse;
}

function compareAssignments(left, right) {
  return left.topic_id.localeCompare(right.topic_id) ||
    left.book - right.book ||
    left.chapter - right.chapter ||
    left.verse - right.verse;
}

function valueFingerprint(value) {
  const input = JSON.stringify(value);
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < input.length; index += 1) {
    const unit = input.charCodeAt(index);
    left = Math.imul(left ^ unit, 0x01000193) >>> 0;
    right = Math.imul(right ^ unit, 0x85ebca6b) >>> 0;
  }
  return left.toString(16).padStart(8, "0") +
    right.toString(16).padStart(8, "0");
}

function normalizeGeneratedId(value) {
  const token = String(value ?? "")
    .replace(/[^A-Za-z0-9._:-]/g, "")
    .slice(0, 80);
  if (!CLIENT_ID_PATTERN.test(token)) {
    throw new TypeError("The contribution client identity is invalid.");
  }
  return token;
}

function defaultToken() {
  return globalThis.crypto?.randomUUID?.() ??
    Math.random().toString(36).slice(2) + Date.now().toString(36);
}

function unavailableStatus() {
  return normalizeContributionStatus(undefined);
}

function utf8Length(value) {
  return new TextEncoder().encode(value).byteLength;
}

function boundedInteger(value, minimum, maximum) {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}

function storageLike(value) {
  return Boolean(
    value &&
    typeof value.getItem === "function" &&
    typeof value.setItem === "function" &&
    typeof value.removeItem === "function",
  );
}

function browserLocalStorage() {
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

function defaultInstanceScope() {
  try {
    return miniAppInstanceScope();
  } catch {
    return "0".repeat(16);
  }
}

function contributionStorageKey(scope, instanceScope) {
  if (
    typeof scope !== "string" ||
    !SCOPE_PATTERN.test(scope) ||
    !isMiniAppInstanceScope(instanceScope)
  ) {
    throw new TypeError("A contribution storage scope is required.");
  }
  return STORAGE_PREFIX + ":" + instanceScope + ":" + scope;
}
