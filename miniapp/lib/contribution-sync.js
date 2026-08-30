import { isCanonicalVerseCoordinate } from "./bible-canon.js";
import { IndexedDbContributionJournal } from "./contribution-journal.js";
import {
  isMiniAppInstanceScope,
  miniAppInstanceScope,
} from "./instance-scope.js";
import {
  contributionReviewDetailsAvailable,
  normalizeContributionStatus,
} from "./model.js";

const STORAGE_PREFIX = "getbible.miniapp.contributions.v1";
const STORAGE_VERSION = 1;
const GLOBAL_EVENT_ORIGIN = "g";
const PERSONAL_EVENT_ORIGIN = "p";
const MAX_BATCH_SIZE = 50;
// Keep ordinary offline churn small enough to coexist with the bookmark record
// and live catalogue under a shared-origin quota. If this journal fills, the
// adapter switches atomically to compact snapshot reconciliation instead of
// dropping the overflowing mutation.
const MAX_OUTBOX_EVENTS = 256;
const MAX_RECOVERY_EXTERNAL_EVENTS = 10_000;
const MAX_RECOVERY_SNAPSHOT_BYTES = 512 * 1024;
const MAX_PERSISTED_STATE_BYTES = 1024 * 1024;
const SCOPE_PATTERN = /^[a-f0-9]{64}$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/;
const COLOR_PATTERN = /^#[a-f0-9]{6}$/;
const ENGLISH_TOPIC_PATTERN =
  /^(?=[A-Za-z0-9 &'():?-]{2,80}$)(?=.*[A-Za-z])(?!.* {2})[A-Za-z0-9][A-Za-z0-9 &'():?-]*[A-Za-z0-9)]$/;

/**
 * Durable, local-first contributor event journal.
 *
 * Personal bookmark writes remain owned by BookmarkStore. This adapter only
 * observes successful before/after snapshots, persists an idempotent event,
 * and retries it independently. A network or server failure can therefore
 * never undo or block the reader's local data.
 */
export class ContributionSync {
  #api;
  #batchPause;
  #clock;
  #coreTopicIds;
  #idFactory;
  #initialRaw;
  #journal;
  #key;
  #lockManager;
  #lockName;
  #statusLockName;
  #syncLockName;
  #maximumOutboxEvents;
  #manualSyncPromise = null;
  #persistenceFailed = false;
  #status;
  #statusTail = Promise.resolve();
  #state;
  #storage;
  #syncPromise = null;
  #validateStoredBaseline = false;

  static async open(options = {}) {
    const instanceScope = options.instanceScope ?? defaultInstanceScope();
    const key = contributionStorageKey(options.scope, instanceScope);
    const journal = options.journal ??
      await IndexedDbContributionJournal.open({ key });
    const lockManager = options.lockManager ?? globalThis.navigator?.locks;
    if (
      !journal ||
      typeof journal.read !== "function" ||
      typeof journal.write !== "function" ||
      typeof journal.remove !== "function" ||
      !lockManager ||
      typeof lockManager.request !== "function"
    ) {
      throw new TypeError("Transactional contribution storage is unavailable.");
    }
    let initialRaw = await journal.read();
    if (initialRaw === null) {
      const legacyStorage = storageLike(options.legacyStorage)
        ? options.legacyStorage
        : browserLocalStorage();
      const legacyKeys = [
        `${STORAGE_PREFIX}:${instanceScope}:${options.scope}`,
        `${STORAGE_PREFIX}:${options.scope}`,
      ];
      for (const legacyKey of legacyKeys) {
        let candidate = null;
        try {
          candidate = legacyStorage?.getItem(legacyKey) ?? null;
        } catch {
          candidate = null;
        }
        if (!validRawState(candidate)) {
          continue;
        }
        // Remove the legacy value only after the transactional copy commits.
        // A failed migration therefore leaves the only durable copy intact.
        await journal.write(candidate);
        initialRaw = candidate;
        try {
          legacyStorage.removeItem(legacyKey);
        } catch {
          // A duplicate legacy copy is harmless; IndexedDB is authoritative.
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
      lockManager,
      initialRaw,
    });
    if (options.initialStatus !== undefined) {
      await sync.seedStatus(options.initialStatus);
    }
    return sync;
  }

  constructor({
    scope,
    api,
    coreTopicIds = [],
    storage = browserLocalStorage(),
    clock = Date.now,
    idFactory = defaultEventToken,
    maximumOutboxEvents = MAX_OUTBOX_EVENTS,
    instanceScope = defaultInstanceScope(),
    journal = null,
    lockManager = null,
    initialRaw = undefined,
    initialStatus = undefined,
    batchPause = null,
  } = {}) {
    if (typeof scope !== "string" || !SCOPE_PATTERN.test(scope)) {
      throw new TypeError("An authenticated contribution scope is required.");
    }
    if (
      !api ||
      typeof api.contributionStatus !== "function" ||
      typeof api.submitContributionEvents !== "function" ||
      typeof api.acknowledgeContributionDisclosure !== "function"
    ) {
      throw new TypeError("A contribution API client is required.");
    }
    if (
      !Array.isArray(coreTopicIds) ||
      coreTopicIds.some((id) => typeof id !== "string" || !SAFE_ID_PATTERN.test(id)) ||
      typeof clock !== "function" ||
      typeof idFactory !== "function" ||
      !Number.isSafeInteger(maximumOutboxEvents) ||
      maximumOutboxEvents < 1 ||
      maximumOutboxEvents > MAX_OUTBOX_EVENTS ||
      !isMiniAppInstanceScope(instanceScope)
    ) {
      throw new TypeError("Contribution sync dependencies are invalid.");
    }
    if (batchPause !== null && typeof batchPause !== "function") {
      throw new TypeError("Contribution sync pacing is invalid.");
    }
    this.#key = contributionStorageKey(scope, instanceScope);
    this.#lockName = `${this.#key}:lock`;
    this.#statusLockName = `${this.#key}:status`;
    this.#syncLockName = `${this.#key}:sync`;
    this.#api = api;
    this.#batchPause = batchPause;
    this.#coreTopicIds = new Set(coreTopicIds);
    this.#journal = journal;
    this.#lockManager = lockManager;
    this.#initialRaw = initialRaw;
    this.#storage = journal ? null : storageLike(storage) ? storage : null;
    this.#persistenceFailed = !journal && this.#storage === null;
    this.#clock = clock;
    this.#idFactory = idFactory;
    this.#maximumOutboxEvents = maximumOutboxEvents;
    this.#state = this.#read();
    this.#persistenceFailed = !journal && this.#storage === null;
    this.#status = statusFromState(this.#state);
    if (initialStatus !== undefined) {
      const status = normalizeContributionStatus(initialStatus);
      this.#applyStatus(status);
      this.#status = cloneContributionStatus(status);
      if (!journal) {
        this.#write();
      }
    }
    // Only a baseline that was already complete when this client opened can
    // hide personal changes made while no contribution journal was running.
    this.#validateStoredBaseline = this.#state.baseline_complete;
  }

  get canContribute() {
    return this.#state.approved;
  }

  get disclosureRequired() {
    return this.#state.disclosure_required;
  }

  get contributorState() {
    return this.#state.contributor_state;
  }

  get status() {
    const status = cloneContributionStatus(this.#status);
    status.state = this.#state.contributor_state;
    status.can_contribute = this.#state.approved;
    status.disclosure_required = this.#state.disclosure_required;
    return status;
  }

  get reviewDetailsAvailable() {
    return contributionReviewDetailsAvailable(this.#status);
  }

  get topicOutcomes() {
    return this.status.topics;
  }

  get reviewSummary() {
    return this.status.summary;
  }

  get baselineComplete() {
    return this.#state.baseline_complete;
  }

  get pendingCount() {
    return this.#state.outbox.length;
  }

  get overflowed() {
    return this.#state.overflowed;
  }

  get recovering() {
    return this.#state.recovery_base !== null;
  }

  get persistenceFailed() {
    return this.#persistenceFailed;
  }

  refreshStatus() {
    return this.#withStatusLock(async () => {
      try {
        const status = normalizeContributionStatus(
          await this.#api.contributionStatus(),
        );
        return this.#seedStatusLocked(status);
      } catch (error) {
        if (isContributionDenied(error)) {
          await this.#suspendContributionsLocked();
        }
        throw error;
      }
    });
  }

  /**
   * Accepts the authenticated status bundled into the session bootstrap.
   * Seeding is deliberately transport-free: callers can decide whether this
   * user should open a journal or make any later contribution request.
   */
  seedStatus(value) {
    const status = normalizeContributionStatus(value);
    return this.#withStatusLock(() => this.#seedStatusLocked(status));
  }

  #seedStatusLocked(status) {
    return this.#withLock(async () => {
      this.#applyStatus(status);
      this.#status = cloneContributionStatus(status);
      await this.#write();
      return this.status;
    });
  }

  #applyStatus(status) {
    const wasApproved = this.#state.approved;
    const approved = status.enabled && status.can_contribute;
    this.#state.approved = approved;
    this.#state.contributor_state = status.state;
    this.#state.disclosure_required = approved && status.disclosure_required;
    if (approved && !wasApproved) {
      // Capturing is intentionally paused while authority is absent. Start a
      // fresh additive baseline when it returns, then drain every preserved
      // inverse/outbox event from before the authority transition.
      this.#state.baseline_complete = false;
      this.#state.baseline_cursor = 0;
      this.#state.baseline_fingerprint = null;
      this.#validateStoredBaseline = false;
    }
  }

  acknowledgeDisclosure() {
    return this.#withStatusLock(async () => {
      const required = await this.#withLock(() =>
        this.#state.approved && this.#state.disclosure_required
      );
      if (!required) {
        return false;
      }
      try {
        const status = normalizeContributionStatus(
          await this.#api.acknowledgeContributionDisclosure(),
        );
        await this.#seedStatusLocked(status);
        return true;
      } catch (error) {
        if (isContributionDenied(error)) {
          await this.#suspendContributionsLocked();
        }
        throw error;
      }
    });
  }

  /**
   * User-requested full synchronization. Unlike the quiet background drain,
   * this operation always revalidates approval first and returns the newest
   * review outcomes. A stale local `canContribute` value can therefore never
   * prevent an approved contributor from starting their first baseline.
   */
  synchronizeNow(snapshot) {
    if (this.#manualSyncPromise) {
      return this.#manualSyncPromise;
    }
    this.#manualSyncPromise = this.#synchronizeNow(snapshot).finally(() => {
      this.#manualSyncPromise = null;
    });
    return this.#manualSyncPromise;
  }

  async #synchronizeNow(snapshot) {
    const initialStatus = await this.refreshStatus();
    if (!initialStatus.can_contribute || initialStatus.disclosure_required) {
      return synchronizationReport(
        { sent: 0, pending: this.pendingCount },
        initialStatus,
      );
    }
    const result = await this.synchronize(snapshot);
    const finalStatus = await this.refreshStatus();
    return synchronizationReport(result, finalStatus);
  }

  /** Records only the mutations that successfully reached BookmarkStore. */
  captureMutation(before, after) {
    if (this.#journal) {
      return this.#withLock(() => this.#captureMutation(before, after));
    }
    return this.#captureMutation(before, after);
  }

  #captureMutation(before, after) {
    if (!this.#state.approved) {
      return 0;
    }
    if (this.#state.recovery_base !== null) {
      const latest = compactRecoverySnapshot(after);
      if (
        this.#state.recovery_fingerprint === null &&
        this.#state.recovery_cursor === 0
      ) {
        // The pre-overflow journal has not drained yet, so the original base
        // still leads directly to the newest local target.
        this.#state.recovery_target = latest;
      } else {
        // Freeze the target whose deterministic batches are in flight and
        // queue one compact follow-up target for all intervening mutations.
        this.#state.recovery_latest = latest;
      }
      return this.#persistCapture(0);
    }
    const events = contributionEventsForDiff(before, after);
    const indexes = new Map(
      this.#state.outbox.map((event, index) => [semanticEventKey(event), index]),
    );
    const newKeys = new Set(
      events
        .map(semanticEventKey)
        .filter((key) => !indexes.has(key)),
    );
    if (
      this.#state.outbox.length + newKeys.size > this.#maximumOutboxEvents
    ) {
      // Freeze the earliest unsent state transition before modifying the
      // journal. Once the existing prefix drains, a deterministic diff from
      // this base to the latest compact target recovers additions, removals,
      // metadata changes, and topic deletions alike.
      this.#state.overflowed = true;
      this.#state.recovery_base = compactRecoverySnapshot(before);
      this.#state.recovery_target = compactRecoverySnapshot(after);
      this.#state.recovery_latest = null;
      this.#state.recovery_cursor = 0;
      this.#state.recovery_fingerprint = null;
      this.#state.recovery_external = emptyRecoveryExternal();
      this.#state.recovery_external_latest = emptyRecoveryExternal();
      this.#state.recovery_sequence += 1;
      return this.#persistCapture(0);
    }
    let accepted = 0;
    for (const event of events) {
      const key = semanticEventKey(event);
      const index = indexes.get(key) ?? -1;
      const queued = {
        ...event,
        client_event_id: this.#nextEventId(PERSONAL_EVENT_ORIGIN),
      };
      if (index >= 0) {
        this.#state.outbox[index] = queued;
        accepted += 1;
      } else if (this.#state.outbox.length < this.#maximumOutboxEvents) {
        indexes.set(key, this.#state.outbox.length);
        this.#state.outbox.push(queued);
        accepted += 1;
      }
    }
    if (accepted > 0 || events.length > accepted) {
      // Keep the in-memory queue so an online retry can still succeed, but do
      // not report a durable capture when the device journal rejected it.
      return this.#persistCapture(accepted);
    }
    return accepted;
  }

  /** Mirrors a successful local hide of one reviewed global assignment. */
  captureGlobalRemoval(topic, verse, snapshot) {
    return this.#captureGlobalAssignments(
      "verse_remove",
      [{ topic, verse }],
      snapshot,
    );
  }

  /** Mirrors an explicit restoration of one previously hidden assignment. */
  captureGlobalAddition(topic, verse, snapshot) {
    return this.#captureGlobalAssignments(
      "verse_add",
      [{ topic, verse }],
      snapshot,
    );
  }

  /** Mirrors a bounded bulk restoration with one transactional checkpoint. */
  captureGlobalAdditions(assignments, snapshot) {
    return this.#captureGlobalAssignments("verse_add", assignments, snapshot);
  }

  #captureGlobalAssignments(type, assignments, snapshot) {
    const operation = () =>
      this.#captureGlobalAssignmentsLocked(type, assignments, snapshot);
    return this.#journal ? this.#withLock(operation) : operation();
  }

  #captureGlobalAssignmentsLocked(type, assignments, snapshot) {
    if (!this.#state.approved) {
      return 0;
    }
    if (
      !["verse_add", "verse_remove"].includes(type) ||
      !Array.isArray(assignments) ||
      assignments.length > MAX_RECOVERY_EXTERNAL_EVENTS
    ) {
      throw new TypeError("Global bookmark contribution assignments are invalid.");
    }
    const eventsByKey = new Map();
    for (const assignment of assignments) {
      const normalizedTopic = normalizeContributionTopic(assignment?.topic);
      const verse = assignment?.verse;
      if (!isCanonicalVerseCoordinate(verse)) {
        throw new TypeError("A canonical global bookmark coordinate is required.");
      }
      const event = verseEvent(type, {
        topic: normalizedTopic,
        verse: {
          book: verse.book,
          chapter: verse.chapter,
          verse: verse.verse,
        },
      });
      eventsByKey.set(semanticEventKey(event), event);
    }
    const events = [...eventsByKey.values()];
    if (events.length === 0) {
      return 0;
    }
    if (this.#state.recovery_base !== null) {
      const target = this.#state.recovery_fingerprint === null
        ? this.#state.recovery_external
        : this.#state.recovery_external_latest;
      addRecoveryExternalEvents(target, events);
      return this.#persistCapture(events.length);
    }

    const indexes = new Map(
      this.#state.outbox.map((event, index) => [semanticEventKey(event), index]),
    );
    const newKeys = events.filter((event) =>
      !indexes.has(semanticEventKey(event))
    ).length;
    if (this.#state.outbox.length + newKeys <= this.#maximumOutboxEvents) {
      for (const event of events) {
        const key = semanticEventKey(event);
        const index = indexes.get(key) ?? -1;
        const queued = {
          ...event,
          client_event_id: this.#nextEventId(GLOBAL_EVENT_ORIGIN),
        };
        if (index >= 0) {
          // A fresh id prevents an acknowledgement for a possibly in-flight
          // inverse event from erasing this later intent.
          this.#state.outbox[index] = queued;
        } else {
          indexes.set(key, this.#state.outbox.length);
          this.#state.outbox.push(queued);
        }
      }
      return this.#persistCapture(events.length);
    }

    const current = compactRecoverySnapshot(snapshot);
    const external = emptyRecoveryExternal();
    addRecoveryExternalEvents(external, events);
    this.#state.overflowed = true;
    this.#state.recovery_base = current;
    this.#state.recovery_target = cloneCompactSnapshot(current);
    this.#state.recovery_latest = null;
    this.#state.recovery_cursor = 0;
    this.#state.recovery_fingerprint = null;
    this.#state.recovery_external = external;
    this.#state.recovery_external_latest = emptyRecoveryExternal();
    this.#state.recovery_sequence += 1;
    return this.#persistCapture(events.length);
  }

  /**
   * Sends a deterministic first baseline and then drains the explicit journal.
   * The baseline includes personal assignments, every topic they reference,
   * and genuinely custom topics; untouched bundled topics are intentionally
   * excluded from the review queue.
   */
  synchronize(snapshot) {
    if (this.#syncPromise) {
      return this.#syncPromise;
    }
    const operation = this.#journal
      ? this.#lockManager.request(this.#syncLockName, async () => {
          // Only one tab may own the network drain. Refresh the shared
          // checkpoint after acquiring that ownership so a waiting tab cannot
          // restart another tab's baseline with a different fingerprint.
          await this.#withLock(() => undefined);
          return this.#synchronize(snapshot);
        })
      : this.#synchronize(snapshot);
    this.#syncPromise = Promise.resolve(operation).finally(() => {
      this.#syncPromise = null;
    });
    return this.#syncPromise;
  }

  async #synchronize(snapshot) {
    // Normalize once before touching the durable checkpoint. Network requests
    // never run while the short journal Web Lock is held: another tab can
    // always capture a local mutation while the sync-owner lock spans a slow
    // or rate-limited request.
    const baseline = baselineContributionEvents(snapshot, this.#coreTopicIds);
    let sent = 0;
    try {
      while (true) {
        const prepared = await this.#withLock(() =>
          this.#prepareBaselineBatch(baseline)
        );
        if (prepared === null) break;
        if (prepared.blocked) {
          return { sent, pending: prepared.pending };
        }
        await this.#api.submitContributionEvents(prepared.batch);
        sent += prepared.batch.length;
        await this.#withLock(() => this.#acknowledgeBaselineBatch(prepared));
        await this.#paceBatch();
      }

      while (true) {
        const batch = await this.#withLock(() => {
          if (!this.#state.approved || this.#state.disclosure_required) {
            return null;
          }
          return this.#state.outbox.slice(0, MAX_BATCH_SIZE).map(cloneEvent);
        });
        if (!batch?.length) break;
        await this.#api.submitContributionEvents(batch);
        sent += batch.length;
        await this.#withLock(async () => {
          const sentIds = new Set(batch.map((event) => event.client_event_id));
          this.#state.outbox = this.#state.outbox.filter(
            (event) => !sentIds.has(event.client_event_id),
          );
          await this.#write();
        });
        await this.#paceBatch();
      }

      while (true) {
        const prepared = await this.#withLock(() =>
          this.#prepareRecoveryBatch()
        );
        if (prepared === null) break;
        if (prepared.blocked) {
          return { sent, pending: prepared.pending };
        }
        await this.#api.submitContributionEvents(prepared.batch);
        sent += prepared.batch.length;
        await this.#withLock(() => this.#acknowledgeRecoveryBatch(prepared));
        await this.#paceBatch();
      }

      const pending = await this.#withLock(() => this.#state.outbox.length);
      return { sent, pending };
    } catch (error) {
      if (isContributionDenied(error)) {
        // The authority failure stops capture/upload immediately, but the
        // journal remains the user's only durable record of unsent intent.
        // Serialize this transition with status GET/PATCH so an older response
        // cannot re-enable contribution after the denial was observed.
        await this.#withStatusLock(() => this.#suspendContributionsLocked());
      }
      throw error;
    }
  }

  async #prepareBaselineBatch(baseline) {
    if (!this.#state.approved || this.#state.disclosure_required) {
      return { blocked: true, pending: this.#state.outbox.length };
    }
    let fingerprint = null;
    if (this.#validateStoredBaseline) {
      this.#validateStoredBaseline = false;
      fingerprint = baselineFingerprint(baseline);
      const reconciled = reconcilePersonalOutbox(this.#state.outbox, baseline);
      if (
        this.#state.baseline_complete &&
        this.#state.baseline_fingerprint !== fingerprint
      ) {
        this.#state.baseline_complete = false;
        this.#state.baseline_cursor = 0;
        this.#state.baseline_fingerprint = null;
      } else if (reconciled) {
        await this.#write();
      }
    }
    if (this.#state.baseline_complete) {
      return null;
    }
    fingerprint ??= baselineFingerprint(baseline);
    if (this.#state.baseline_fingerprint !== fingerprint) {
      this.#state.baseline_fingerprint = fingerprint;
      this.#state.baseline_cursor = 0;
      await this.#write();
    }
    if (this.#state.baseline_cursor < baseline.length) {
      const start = this.#state.baseline_cursor;
      return {
        batch: baseline.slice(start, start + MAX_BATCH_SIZE).map(cloneEvent),
        fingerprint,
        start,
      };
    }
    this.#state.baseline_complete = true;
    this.#state.baseline_cursor = baseline.length;
    pruneBaselineOutcomes(this.#state.outbox, baseline);
    await this.#write();
    return null;
  }

  async #acknowledgeBaselineBatch({ batch, fingerprint, start }) {
    if (
      this.#state.baseline_complete ||
      this.#state.baseline_fingerprint !== fingerprint
    ) {
      return;
    }
    const end = start + batch.length;
    if (this.#state.baseline_cursor === start) {
      this.#state.baseline_cursor = end;
      await this.#write();
    }
  }

  async #prepareRecoveryBatch() {
    if (!this.#state.approved || this.#state.disclosure_required) {
      return { blocked: true, pending: this.#state.outbox.length };
    }
    while (
      this.#state.recovery_base !== null &&
      this.#state.recovery_target !== null
    ) {
      const events = recoveryContributionEvents(
        expandRecoverySnapshot(this.#state.recovery_base),
        expandRecoverySnapshot(this.#state.recovery_target),
        this.#state.recovery_sequence,
        expandRecoveryExternal(this.#state.recovery_external),
      );
      const fingerprint = baselineFingerprint(events);
      if (this.#state.recovery_fingerprint === null) {
        this.#state.recovery_fingerprint = fingerprint;
        this.#state.recovery_cursor = 0;
        await this.#write();
      } else if (this.#state.recovery_fingerprint !== fingerprint) {
        throw new TypeError("The contribution recovery checkpoint is invalid.");
      }
      if (this.#state.recovery_cursor < events.length) {
        const start = this.#state.recovery_cursor;
        return {
          batch: events.slice(start, start + MAX_BATCH_SIZE).map(cloneEvent),
          fingerprint,
          start,
        };
      }
      this.#state.recovery_base = this.#state.recovery_target;
      if (
        this.#state.recovery_latest !== null ||
        this.#state.recovery_external_latest.e.length > 0
      ) {
        this.#state.recovery_target = this.#state.recovery_latest ??
          cloneCompactSnapshot(this.#state.recovery_base);
        this.#state.recovery_latest = null;
        this.#state.recovery_external = this.#state.recovery_external_latest;
        this.#state.recovery_external_latest = emptyRecoveryExternal();
        this.#state.recovery_cursor = 0;
        this.#state.recovery_fingerprint = null;
        this.#state.recovery_sequence += 1;
        await this.#write();
        continue;
      }
      this.#state.recovery_base = null;
      this.#state.recovery_target = null;
      this.#state.recovery_cursor = 0;
      this.#state.recovery_fingerprint = null;
      this.#state.recovery_external = null;
      this.#state.recovery_external_latest = null;
      this.#state.overflowed = false;
      await this.#write();
    }
    return null;
  }

  async #acknowledgeRecoveryBatch({ batch, fingerprint, start }) {
    if (this.#state.recovery_fingerprint !== fingerprint) {
      return;
    }
    const end = start + batch.length;
    if (this.#state.recovery_cursor === start) {
      this.#state.recovery_cursor = end;
      await this.#write();
    }
  }

  async #paceBatch() {
    if (this.#batchPause) {
      await this.#batchPause();
    }
  }

  #nextEventId(origin) {
    if (![PERSONAL_EVENT_ORIGIN, GLOBAL_EVENT_ORIGIN].includes(origin)) {
      throw new TypeError("A contribution event origin is invalid.");
    }
    this.#state.next_sequence += 1;
    const timestamp = Math.max(0, Math.floor(Number(this.#clock()) || 0))
      .toString(36);
    const token = String(this.#idFactory())
      .replace(/[^A-Za-z0-9._:-]/g, "")
      .slice(0, 64);
    const id = `event:${origin}:${timestamp}:${
      this.#state.next_sequence.toString(36)
    }:${
      token || "local"
    }`;
    if (!SAFE_ID_PATTERN.test(id)) {
      throw new TypeError("A contribution event identifier is invalid.");
    }
    return id;
  }

  #persistCapture(value) {
    const persisted = this.#write();
    if (persisted && typeof persisted.then === "function") {
      return persisted.then(
        (saved) => saved ? value : 0,
        () => 0,
      );
    }
    return persisted ? value : 0;
  }

  #withLock(operation) {
    if (!this.#journal) {
      return operation();
    }
    return this.#lockManager.request(this.#lockName, async () => {
      // Retain an unsaved in-memory transition after a rejected IDB write so
      // this open WebView can still drain it. Re-reading the older durable
      // checkpoint here would silently discard the mutation. The contributor
      // continues to see persistenceFailed until a later checkpoint commits.
      if (!this.#persistenceFailed) {
        this.#initialRaw = await this.#journal.read();
        this.#state = this.#read();
      }
      return operation();
    });
  }

  #withStatusLock(operation) {
    if (this.#journal) {
      return this.#lockManager.request(this.#statusLockName, operation);
    }
    // localStorage has no cross-document transaction primitive, but one open
    // client still needs request-order authority: hold this lane across both
    // the network response and its state commit.
    const run = () => Promise.resolve().then(operation);
    const result = this.#statusTail.then(run, run);
    this.#statusTail = result.catch(() => undefined);
    return result;
  }

  #suspendContributionsLocked() {
    return this.#withLock(async () => {
      this.#state.approved = false;
      await this.#write();
    });
  }

  #read() {
    const fresh = freshState();
    try {
      const raw = this.#initialRaw !== undefined
        ? this.#initialRaw
        : this.#storage?.getItem(this.#key);
      this.#initialRaw = undefined;
      if (!raw) {
        return fresh;
      }
      const value = JSON.parse(raw);
      if (!validState(value)) {
        throw new TypeError("Contribution sync state is invalid.");
      }
      return {
        ...fresh,
        approved: value.approved,
        baseline_complete: value.baseline_complete,
        baseline_cursor: value.baseline_cursor,
        baseline_fingerprint: value.baseline_fingerprint,
        contributor_state: value.contributor_state,
        disclosure_required: value.disclosure_required,
        next_sequence: value.next_sequence,
        outbox: value.outbox.map(cloneEvent),
        overflowed: value.overflowed === true,
        recovery_base: cloneCompactSnapshot(value.recovery_base),
        recovery_target: cloneCompactSnapshot(value.recovery_target),
        recovery_latest: cloneCompactSnapshot(value.recovery_latest),
        recovery_cursor: value.recovery_cursor,
        recovery_fingerprint: value.recovery_fingerprint,
        recovery_sequence: value.recovery_sequence,
        recovery_external: cloneRecoveryExternal(value.recovery_external),
        recovery_external_latest: cloneRecoveryExternal(
          value.recovery_external_latest,
        ),
      };
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

  #write() {
    if (!this.#journal && !this.#storage) {
      this.#persistenceFailed = true;
      return false;
    }
    const raw = JSON.stringify(this.#state);
    if (utf8Length(raw) > MAX_PERSISTED_STATE_BYTES) {
      this.#persistenceFailed = true;
      if (this.#journal) {
        return Promise.reject(new RangeError(
          "The contribution journal is too large.",
        ));
      }
      return false;
    }
    if (this.#journal) {
      return this.#journal.write(raw).then(
        () => {
          this.#persistenceFailed = false;
          return true;
        },
        (error) => {
          this.#persistenceFailed = true;
          throw error;
        },
      );
    }
    try {
      this.#storage.setItem(this.#key, raw);
      this.#persistenceFailed = false;
      return true;
    } catch {
      // A quota failure may become recoverable after sent events drain. Keep
      // the storage adapter and retry every later checkpoint.
      this.#persistenceFailed = true;
      return false;
    }
  }
}

export function contributionEventsForDiff(before, after) {
  const left = normalizeSnapshot(before);
  const right = normalizeSnapshot(after);
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
  const normalized = normalizeSnapshot(snapshot);
  const topics = new Map(normalized.topics.map((topic) => [topic.id, topic]));
  const assignments = assignmentMap(normalized, topics);
  const referencedTopicIds = new Set(
    [...assignments.values()].map((assignment) => assignment.topic.id),
  );
  const events = [];
  for (const topic of normalized.topics) {
    if (
      isEnglishContributionTopicName(topic.name) &&
      (!coreTopicIds.has(topic.id) || referencedTopicIds.has(topic.id))
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
  const name = value.trim().normalize("NFC");
  return ENGLISH_TOPIC_PATTERN.test(name);
}

export const CONTRIBUTION_STORAGE_PREFIX = STORAGE_PREFIX;
export const CONTRIBUTION_BATCH_MAXIMUM = MAX_BATCH_SIZE;
export const CONTRIBUTION_OUTBOX_MAXIMUM = MAX_OUTBOX_EVENTS;

function topicUpsertEvent(topic) {
  return {
    type: "topic_upsert",
    topic: {
      local_topic_id: topic.id,
      name: topic.name,
      color: topic.color,
    },
  };
}

function verseEvent(type, assignment) {
  return {
    type,
    // Verse events carry source context so a first use of an untouched bundled
    // topic is immediately reviewable without proposing a metadata change.
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
    client_event_id: `baseline:${event.type}:${eventFingerprint(event)}`,
    ...event,
  };
}

function recoveryContributionEvents(before, after, sequence, external = []) {
  const events = new Map();
  for (const event of [...contributionEventsForDiff(before, after), ...external]) {
    // An assignment may temporarily exist in both the personal and global
    // layers. The later explicit global action is the user's final intent;
    // identical removals collapse to one server event/client id.
    events.set(semanticEventKey(event), event);
  }
  return [...events.values()].map((event) => ({
    client_event_id: `recovery:${sequence.toString(36)}:${event.type}:${eventFingerprint(event)}`,
    ...event,
  }));
}

function baselineFingerprint(events) {
  return eventFingerprint(events.map((event) => event.client_event_id));
}

function semanticEventKey(event) {
  if (event.type === "topic_upsert" || event.type === "topic_delete") {
    return `topic:${event.topic.local_topic_id}`;
  }
  return `verse:${event.topic.local_topic_id}:${event.verse.book}:${
    event.verse.chapter
  }:${event.verse.verse}`;
}

function reconcilePersonalOutbox(outbox, baseline) {
  const currentKeys = new Set(baseline.map(semanticEventKey));
  const retained = outbox.filter((event) => {
    if (!event.client_event_id.startsWith(`event:${PERSONAL_EVENT_ORIGIN}:`)) {
      // Legacy and explicit global-preference events have no authoritative
      // representation in BookmarkStore, so preserve them without guessing.
      return true;
    }
    if (["topic_upsert", "verse_add"].includes(event.type)) {
      // A fresh baseline will send a still-current positive; otherwise this
      // queued positive was undone while the Mini App journal was closed.
      return false;
    }
    // Keep a negative only while the current personal snapshot still lacks
    // that topic/assignment. A restored item supersedes the stale inverse.
    return !currentKeys.has(semanticEventKey(event));
  });
  if (retained.length === outbox.length) {
    return false;
  }
  outbox.splice(0, outbox.length, ...retained);
  return true;
}

function pruneBaselineOutcomes(outbox, baseline) {
  const represented = new Map(
    baseline
      .filter((event) => ["topic_upsert", "verse_add"].includes(event.type))
      .map((event) => [semanticEventKey(event), eventPayload(event)]),
  );
  const retained = outbox.filter((event) => {
    if (!["topic_upsert", "verse_add"].includes(event.type)) {
      return true;
    }
    return represented.get(semanticEventKey(event)) !== eventPayload(event);
  });
  outbox.splice(0, outbox.length, ...retained);
}

function eventPayload(event) {
  return JSON.stringify({
    type: event.type,
    topic: event.topic,
    ...(event.verse ? { verse: event.verse } : {}),
  });
}

function eventFingerprint(value) {
  const input = JSON.stringify(value);
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < input.length; index += 1) {
    const unit = input.charCodeAt(index);
    left = Math.imul(left ^ unit, 0x01000193) >>> 0;
    right = Math.imul(right ^ unit, 0x85ebca6b) >>> 0;
  }
  return `${left.toString(16).padStart(8, "0")}${
    right.toString(16).padStart(8, "0")
  }`;
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
      assignments.set(
        `${topicId}:${verse.book}:${verse.chapter}:${verse.verse}`,
        { topic, verse },
      );
    }
  }
  return assignments;
}

function normalizeSnapshot(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !Array.isArray(value.topics) ||
    value.topics.length < 1 ||
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
    const topicIdsForBookmark = Array.isArray(bookmark?.topic_ids)
      ? [...new Set(bookmark.topic_ids)]
      : [bookmark?.topic_id];
    if (
      !topicIdsForBookmark.length ||
      topicIdsForBookmark.length > 100 ||
      topicIdsForBookmark.some((id) => !topicIds.has(id)) ||
      !boundedInteger(bookmark?.book, 1, 200) ||
      !boundedInteger(bookmark?.chapter, 1, 1_000) ||
      !boundedInteger(bookmark?.verse, 1, 2_000)
    ) {
      throw new TypeError("A bookmark assignment is invalid.");
    }
    return {
      topic_ids: topicIdsForBookmark,
      book: bookmark.book,
      chapter: bookmark.chapter,
      verse: bookmark.verse,
    };
  });
  return { topics, bookmarks };
}

function statusFromState(state) {
  return normalizeContributionStatus({
    enabled: state.contributor_state !== "unavailable",
    state: state.contributor_state,
    can_contribute: state.approved,
    disclosure_required: state.disclosure_required,
  });
}

function cloneContributionStatus(status) {
  return normalizeContributionStatus(status);
}

function synchronizationReport(result, status) {
  const normalizedStatus = cloneContributionStatus(status);
  return {
    sent: result.sent,
    pending: result.pending,
    review_details_available:
      contributionReviewDetailsAvailable(normalizedStatus),
    status: normalizedStatus,
    topic_outcomes: normalizedStatus.topics.map((topic) => ({
      ...topic,
      ...(topic.canonical_topic
        ? {
            canonical_topic: {
              ...topic.canonical_topic,
              aliases: [...topic.canonical_topic.aliases],
            },
          }
        : {}),
    })),
  };
}

function isContributionDenied(error) {
  return error?.code === "contribution_not_allowed";
}

function validState(value) {
  const recoverySnapshots = [
    value?.recovery_base,
    value?.recovery_target,
    value?.recovery_latest,
  ];
  const recoveryExternals = [
    value?.recovery_external,
    value?.recovery_external_latest,
  ];
  const snapshotsValid = recoverySnapshots.every((snapshot) =>
    snapshot === null || validCompactSnapshot(snapshot)
  );
  const externalsValid = recoveryExternals.every((external) =>
    external === null || validRecoveryExternal(external)
  );
  const recoveryShapeValid = value?.overflowed === true
    ? value.recovery_base !== null &&
      value.recovery_target !== null &&
      recoveryExternals.every((external) => external !== null)
    : recoverySnapshots.every((snapshot) => snapshot === null) &&
      recoveryExternals.every((external) => external === null) &&
      value?.recovery_cursor === 0 &&
      value?.recovery_fingerprint === null;
  return Boolean(
    value &&
    typeof value === "object" &&
    value.version === STORAGE_VERSION &&
    typeof value.approved === "boolean" &&
    typeof value.baseline_complete === "boolean" &&
    Number.isSafeInteger(value.baseline_cursor) &&
    value.baseline_cursor >= 0 &&
    (
      value.baseline_fingerprint === null ||
      /^[a-f0-9]{16}$/.test(value.baseline_fingerprint)
    ) &&
    typeof value.disclosure_required === "boolean" &&
    typeof value.contributor_state === "string" &&
    Number.isSafeInteger(value.next_sequence) &&
    value.next_sequence >= 0 &&
    Array.isArray(value.outbox) &&
    value.outbox.length <= MAX_OUTBOX_EVENTS &&
    value.outbox.every(validEvent) &&
    typeof value.overflowed === "boolean" &&
    snapshotsValid &&
    externalsValid &&
    recoveryShapeValid &&
    Number.isSafeInteger(value.recovery_cursor) &&
    value.recovery_cursor >= 0 &&
    (
      value.recovery_fingerprint === null ||
      /^[a-f0-9]{16}$/.test(value.recovery_fingerprint)
    ) &&
    Number.isSafeInteger(value.recovery_sequence) &&
    value.recovery_sequence >= 0
  );
}

function validRawState(raw) {
  if (typeof raw !== "string" || utf8Length(raw) > MAX_PERSISTED_STATE_BYTES) {
    return false;
  }
  try {
    return validState(JSON.parse(raw));
  } catch {
    return false;
  }
}

function validEvent(event) {
  if (
    !event ||
    typeof event !== "object" ||
    !SAFE_ID_PATTERN.test(event.client_event_id) ||
    !["topic_upsert", "topic_delete", "verse_add", "verse_remove"].includes(
      event.type,
    ) ||
    !event.topic ||
    !SAFE_ID_PATTERN.test(event.topic.local_topic_id)
  ) {
    return false;
  }
  if (event.type === "topic_upsert") {
    return hasExactKeys(event, ["client_event_id", "type", "topic"]) &&
      hasExactKeys(event.topic, ["local_topic_id", "name", "color"]) &&
      isEnglishContributionTopicName(event.topic.name) &&
      COLOR_PATTERN.test(event.topic.color);
  }
  if (event.type === "topic_delete") {
    return hasExactKeys(event, ["client_event_id", "type", "topic"]) &&
      hasExactKeys(event.topic, ["local_topic_id"]);
  }
  return Boolean(
    hasExactKeys(event, ["client_event_id", "type", "topic", "verse"]) &&
    hasExactKeys(event.topic, ["local_topic_id", "name", "color"]) &&
    isEnglishContributionTopicName(event.topic.name) &&
    COLOR_PATTERN.test(event.topic.color) &&
    event.verse &&
    hasExactKeys(event.verse, ["book", "chapter", "verse"]) &&
    isCanonicalVerseCoordinate(event.verse),
  );
}

function cloneEvent(event) {
  return {
    ...event,
    topic: { ...event.topic },
    ...(event.verse ? { verse: { ...event.verse } } : {}),
  };
}

function freshState() {
  return {
    version: STORAGE_VERSION,
    approved: false,
    baseline_complete: false,
    baseline_cursor: 0,
    baseline_fingerprint: null,
    contributor_state: "not_applied",
    disclosure_required: false,
    next_sequence: 0,
    outbox: [],
    overflowed: false,
    recovery_base: null,
    recovery_target: null,
    recovery_latest: null,
    recovery_cursor: 0,
    recovery_fingerprint: null,
    recovery_sequence: 0,
    recovery_external: null,
    recovery_external_latest: null,
  };
}

function compactRecoverySnapshot(value) {
  const snapshot = normalizeSnapshot(value);
  const topicIndexes = new Map(
    snapshot.topics.map((topic, index) => [topic.id, index]),
  );
  const compact = {
    t: snapshot.topics.map((topic) => [topic.id, topic.name, topic.color]),
    b: snapshot.bookmarks.map((bookmark) => [
      bookmark.book,
      bookmark.chapter,
      bookmark.verse,
      bookmark.topic_ids.map((topicId) => topicIndexes.get(topicId)),
    ]),
  };
  if (utf8Length(JSON.stringify(compact)) > MAX_RECOVERY_SNAPSHOT_BYTES) {
    throw new RangeError("The contribution recovery snapshot is too large.");
  }
  return compact;
}

function expandRecoverySnapshot(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !Array.isArray(value.t) ||
    !Array.isArray(value.b) ||
    Object.keys(value).length !== 2
  ) {
    throw new TypeError("The contribution recovery snapshot is invalid.");
  }
  const topics = value.t.map((row) => {
    if (!Array.isArray(row) || row.length !== 3) {
      throw new TypeError("The contribution recovery snapshot is invalid.");
    }
    return { id: row[0], name: row[1], color: row[2] };
  });
  const bookmarks = value.b.map((row) => {
    if (
      !Array.isArray(row) ||
      row.length !== 4 ||
      !Array.isArray(row[3]) ||
      row[3].some((index) =>
        !Number.isInteger(index) || index < 0 || index >= topics.length
      )
    ) {
      throw new TypeError("The contribution recovery snapshot is invalid.");
    }
    return {
      book: row[0],
      chapter: row[1],
      verse: row[2],
      topic_ids: row[3].map((index) => topics[index].id),
    };
  });
  return normalizeSnapshot({ topics, bookmarks });
}

function validCompactSnapshot(value) {
  try {
    expandRecoverySnapshot(value);
    return utf8Length(JSON.stringify(value)) <= MAX_RECOVERY_SNAPSHOT_BYTES;
  } catch {
    return false;
  }
}

function cloneCompactSnapshot(value) {
  if (value === null) {
    return null;
  }
  return {
    t: value.t.map((row) => [...row]),
    b: value.b.map((row) => [row[0], row[1], row[2], [...row[3]]]),
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

function emptyRecoveryExternal() {
  return { t: [], e: [] };
}

function addRecoveryExternalEvents(value, events) {
  if (
    !value ||
    !Array.isArray(value.t) ||
    !Array.isArray(value.e) ||
    !Array.isArray(events)
  ) {
    throw new TypeError("The external contribution recovery is invalid.");
  }
  // Canonicalize persisted v1 removal-only rows before merging the newest
  // intent. The five-column form adds an operation bit while still accepting
  // an interrupted four-column recovery written by an earlier client.
  expandRecoveryExternal(value);
  const topicIndexes = new Map(value.t.map((row, index) => [row[0], index]));
  const merged = new Map();
  for (const row of value.e) {
    const normalized = row.length === 4
      ? [0, row[0], row[1], row[2], row[3]]
      : [...row];
    const topic = value.t[normalized[1]];
    merged.set(
      [topic[0], normalized[2], normalized[3], normalized[4]].join(":"),
      normalized,
    );
  }
  for (const event of events) {
    if (
      !["verse_add", "verse_remove"].includes(event?.type) ||
      !isEnglishContributionTopicName(event.topic?.name) ||
      !COLOR_PATTERN.test(event.topic?.color ?? "") ||
      !SAFE_ID_PATTERN.test(event.topic?.local_topic_id ?? "") ||
      !isCanonicalVerseCoordinate(event.verse)
    ) {
      throw new TypeError("The external contribution recovery is invalid.");
    }
    let topicIndex = topicIndexes.get(event.topic.local_topic_id);
    if (topicIndex === undefined) {
      if (value.t.length >= 100) {
        throw new RangeError("Too many external recovery topics are pending.");
      }
      topicIndex = value.t.length;
      topicIndexes.set(event.topic.local_topic_id, topicIndex);
      value.t.push([
        event.topic.local_topic_id,
        event.topic.name,
        event.topic.color,
      ]);
    } else {
      value.t[topicIndex] = [
        event.topic.local_topic_id,
        event.topic.name,
        event.topic.color,
      ];
    }
    const key = [
      event.topic.local_topic_id,
      event.verse.book,
      event.verse.chapter,
      event.verse.verse,
    ].join(":");
    merged.set(key, [
      event.type === "verse_add" ? 1 : 0,
      topicIndex,
      event.verse.book,
      event.verse.chapter,
      event.verse.verse,
    ]);
  }
  if (merged.size > MAX_RECOVERY_EXTERNAL_EVENTS) {
    throw new RangeError("Too many external recovery events are pending.");
  }
  value.e = [...merged.values()];
  if (utf8Length(JSON.stringify(value)) > MAX_RECOVERY_SNAPSHOT_BYTES) {
    throw new RangeError("The external contribution recovery is too large.");
  }
}

function expandRecoveryExternal(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.keys(value).length !== 2 ||
    !Array.isArray(value.t) ||
    value.t.length > 100 ||
    !Array.isArray(value.e) ||
    value.e.length > MAX_RECOVERY_EXTERNAL_EVENTS
  ) {
    throw new TypeError("The external contribution recovery is invalid.");
  }
  const topicIds = new Set();
  const topics = value.t.map((row) => {
    if (!Array.isArray(row) || row.length !== 3) {
      throw new TypeError("The external contribution recovery is invalid.");
    }
    const topic = normalizeContributionTopic({
      id: row[0],
      name: row[1],
      color: row[2],
    });
    if (topicIds.has(topic.id)) {
      throw new TypeError("The external contribution recovery is invalid.");
    }
    topicIds.add(topic.id);
    return topic;
  });
  const seen = new Set();
  return value.e.map((row) => {
    const legacy = row?.length === 4;
    const operation = legacy ? 0 : row?.[0];
    const topicIndex = legacy ? row?.[0] : row?.[1];
    const book = legacy ? row?.[1] : row?.[2];
    const chapter = legacy ? row?.[2] : row?.[3];
    const verse = legacy ? row?.[3] : row?.[4];
    if (
      !Array.isArray(row) ||
      (!legacy && row.length !== 5) ||
      ![0, 1].includes(operation) ||
      !Number.isInteger(topicIndex) ||
      topicIndex < 0 ||
      topicIndex >= topics.length ||
      !isCanonicalVerseCoordinate({
        book,
        chapter,
        verse,
      })
    ) {
      throw new TypeError("The external contribution recovery is invalid.");
    }
    const key = [topicIndex, book, chapter, verse].join(":");
    if (seen.has(key)) {
      throw new TypeError("The external contribution recovery is invalid.");
    }
    seen.add(key);
    return verseEvent(operation === 1 ? "verse_add" : "verse_remove", {
      topic: topics[topicIndex],
      verse: { book, chapter, verse },
    });
  });
}

function validRecoveryExternal(value) {
  try {
    expandRecoveryExternal(value);
    return utf8Length(JSON.stringify(value)) <= MAX_RECOVERY_SNAPSHOT_BYTES;
  } catch {
    return false;
  }
}

function cloneRecoveryExternal(value) {
  if (value === null) {
    return null;
  }
  return {
    t: value.t.map((row) => [...row]),
    e: value.e.map((row) => [...row]),
  };
}

function utf8Length(value) {
  return new TextEncoder().encode(value).byteLength;
}

function defaultEventToken() {
  return globalThis.crypto?.randomUUID?.() ??
    `${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

function boundedInteger(value, minimum, maximum) {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}

function hasExactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length &&
    actual.every((key, index) => key === wanted[index]);
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
  return `${STORAGE_PREFIX}:${instanceScope}:${scope}`;
}
