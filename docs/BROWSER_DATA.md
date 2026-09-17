# Browser data architecture

The Telegram Mini App is a browser application. Public Bible content, temporary selection state, and device-local reading history therefore belong in the browser data plane rather than in the Robot process. Small user-critical records additionally reconcile with Telegram Mini App storage, while large public catalogs and chapters remain browser-only. This keeps reading responsive, avoids repeated server work, and preserves useful cross-device continuity without making ordinary reading depend on Robot.

## Doctrine

Every Scripture read — catalogs, chapters, explicit references, and full-text
search — goes from the browser to a public GetBible origin. Robot provides
the authenticated control paths for sessions, preferences, final Post,
contributions, and an explicit bookmark chat backup or restore.

The reading and persistence split is:

- `api.getbible.net/v2` supplies translation catalogs, books, chapter maps, chapter text, and hashes;
- `query.getbible.net/v2` supplies explicit and grouped reference resolution;
- `search.getbible.net/v2` answers full-text search with offset pagination and
  exact totals; results stay in page memory and the browser HTTP cache, never
  IndexedDB;
- `bookmarks.getbible.net/v1` supplies the shared global topic catalogue,
  accepted only when the SHA-256 of `all.json` equals the checksum in
  `index.json`, and kept in IndexedDB with that checksum as validator;
- IndexedDB stores validated public content;
- browser memory owns the current ordered selection;
- scoped browser `localStorage` owns bounded, coordinate-only reading history;
- scoped `localStorage` plus Telegram `DeviceStorage` and `CloudStorage` reconcile
  personal bookmark aggregate v3, colored topics, the full clearable
  recently-used topic order, the active topic, and compact last-read
  coordinates;
- scoped browser `localStorage` plus Telegram `DeviceStorage` reconcile
  device-local global-catalog visibility, per-link exclusions, and legacy
  topic mapping; `CloudStorage` is excluded;
- per-instance, authenticated-user-scoped IndexedDB journals the approved
  contributor's explicit global add/remove intents until an explicit Sync;
- Robot authorizes contributors, accepts bounded idempotent review events,
  keeps the accepted-contribution ledger, and reports when accepted changes
  have appeared in the public catalogue;
- Robot authenticates Telegram, retains compatible reader preferences, accepts
  the final ordered post request, validates it, and sends authoritative
  Scripture or an explicitly requested bookmark backup document to Telegram.

## Ownership boundaries

| Capability | Browser / GetBible API | Robot |
| --- | --- | --- |
| Translation catalog | Yes | No |
| Book catalog | Yes | No |
| Chapter catalog | Yes | No |
| Chapter text | Yes | No |
| Explicit/grouped references | Yes, Query API | No |
| Full-text search | Yes, Search API | No |
| Search pagination | Yes, Search API by offset | No |
| Selection highlighting | Yes | No |
| Select/unselect/reorder/clear | Yes | No |
| Reading history record/remove/clear | Yes | No |
| Bookmark/topic editing and local import/export | Yes | No |
| Bookmark and last-read device/cloud sync | Telegram Mini App storage | No |
| Global catalog visibility and exclusions | Scoped localStorage + Telegram DeviceStorage | No |
| Contributor journal of explicit global intents | Per-instance scoped IndexedDB | Authenticated event intake/review |
| Global topic catalogue | Yes, Bookmarks API, verified and cached in IndexedDB | No; the host watches the same API to report liveness |
| Private-chat bookmark backup/restore transport | Confirm/merge in browser | Yes |
| Telegram authentication | No | Yes |
| Reader preference compatibility | Compact Mini App storage copy | Yes |
| Final Telegram posting | Coordinates submitted once | Yes |

## Request flow

```mermaid
flowchart LR
    T[Telegram WebView] -->|signed init data| S[Robot session API]
    S -->|opaque session + preferences| T
    T -->|translations / books / chapters / chapter text / hashes| A[api.getbible.net/v2]
    T -->|explicit and grouped references| Q[query.getbible.net/v2]
    T -->|full-text search, offset pages| SR[search.getbible.net/v2]
    T -->|topic catalogue: index.json, verified all.json| BK[bookmarks.getbible.net/v1]
    A --> V[VerseSelection]
    SR --> V
    V --> B[BrowserSelectionStore]
    T --> H[Scoped local ReadingHistoryStore]
    T --> M[BookmarkStore]
    M <-->|newest timestamped aggregate| TS[Telegram DeviceStorage / CloudStorage]
    T --> C[Contribution journal]
    C -->|bounded idempotent review events| P
    B -->|final ordered coordinates once| P[Robot protected endpoints]
    M -->|explicit bounded JSON backup| P
    P -->|/bible references, Post text| Q
    P -->|validated Scripture or private backup document| G[Telegram]
```

No select, unselect, reorder, clear, reader navigation, catalog, chapter, explicit-reference, search, or global-catalogue operation may call Robot.

## Reading history

Every successfully opened chapter records its target verse, and every
successfully added selection records that verse. Chapter visits de-duplicate by
book and chapter; selections de-duplicate by their book/chapter/verse
coordinate across translations, and an exact coordinate also coalesces across
event kinds.
Recording a repeat updates and promotes that entry instead of duplicating it.
Each entry contains only its translation, display reference, book name/number,
chapter, verse, event kind, local identifier, and visit time. The store never
persists a verse body, Telegram identity, launch data, session token, search
result, or preference.

The versioned record is unique, newest-first, bounded to 1,000 entries, and
stored under a key derived from the authenticated user scope in browser
`localStorage`. Legacy cross-translation duplicates are compacted when
restored. The History page progressively hydrates initial and near-viewport
display-only verse excerpts from the same bounded public chapter data plane
used by global Bookmarks. Missing coordinates render a localized unavailable
state; transient chapter failures remain request failures and retry after the
browser reconnects. Excerpts are not added to the history record. The reader
reopens the exact coordinate in the currently selected translation, can remove
one entry, or clear the entire record. Clearing removes the storage key. If `localStorage` is
unavailable, history remains available in memory until the page closes.

History never reconciles through Telegram storage and never reaches Robot.
Moving between devices therefore synchronizes personal bookmarks and last-read, but not
the device's trail of previously opened locations.

## Hybrid user storage

The hybrid adapter is intentionally limited to compact personal data:

- at most 100 colored bookmark topics and 800 canonical whole-verse records;
- multiple topic identifiers per verse record, without consuming another verse
  slot;
- the active bookmark topic and a most-recently-used list retaining every
  current topic until explicitly cleared;
- a last-read record containing only translation, book, chapter, verse,
  version, and update timestamp, or a timestamped cleared marker without a
  coordinate.

The personal bookmark aggregate is stored immediately in the authenticated user's
scoped `localStorage` record. At startup, valid local, Telegram
`DeviceStorage`, and Telegram `CloudStorage` candidates are compared by update
time; the newest candidate becomes active and is mirrored to every available
store. Later writes update the local copy synchronously and coalesce bounded
Telegram writes in the background. A client without either Telegram API keeps
working from local storage and presents sync as degraded instead of blocking
the feature. Aggregate version 3 stores full topic identifiers and the bounded
recent list locally and in DeviceStorage; CloudStorage uses compact topic
indexes into the topic manifest and reconstructs assignment and recent-topic
identifiers when reading.

Bookmark identity is canonical book/chapter/verse, not translation. Assigning
the same verse from another translation updates its one record. Assigning it to
another topic extends that record, while repeating an existing assignment is a
no-op. A plus-card after the topic list opens the bounded new-topic form; there
is no separate topic-management editor. Opening a topic exposes its controls in
context: every topic color is user-editable, while only a custom topic name can
enter inline confirm/cancel editing. Curated global names and identities remain
read-only server-owned metadata. **Remove topic** requires confirmation and
removes that user's topic and linked verse assignments. For a global topic this
is a user-local catalogue removal, not deletion of the reviewed definition;
**Add all** restores it. Selections remain independent and ephemeral, and the
public cache remains identity-free.

### Global catalogue

The global topic-to-verse links come from the public Bookmarks API at
`https://bookmarks.getbible.net/v1`; the robot ships no copy. They appear in
the same topic list as personal records and carry a **G**
marker.
Their cards hydrate verse text for the currently selected translation from the
bounded public chapter data plane; that text is not persisted as a bookmark.
Compact **Add all** and **Remove all** controls precede topic search; per-topic
add/remove and per-link hiding remain available. Topic visibility, exclusions,
and canonical mapping for renamed legacy numeric topics are timestamped under
the hashed account scope and reconciled between browser `localStorage` and
Telegram `DeviceStorage`. This restores the selection when a Telegram Desktop
WebView discards its browser storage while keeping the state device-local:
`CloudStorage` is never read or written. Loading one topic or the full catalog
restores its hidden links without duplication. Global links and these
preferences never enter the personal aggregate or backup documents.

Loading is cache-first. A cached catalogue younger than a day is used as is.
Otherwise the browser reads `index.json` (schema version, catalogue version,
the SHA-256 of `all.json`, and counts); when the checksum equals the cached
validator the cache is marked checked, and when it differs `all.json` is
downloaded, accepted only if its SHA-256 equals that checksum, validated
against the catalogue rules (topic ids, names, colours, aliases, 66-book
coordinate bounds, sorted unique verses), normalised, and stored in the
identity-free public cache with the checksum as validator. Explicit **Add all**
and per-topic loads require the network and revalidate; a malformed, oversized,
or unavailable response keeps the last verified cached catalogue, and when
nothing is cached the surface reports global topics as unavailable until
online. Topic names are shown in the reader's locale from the catalogue's
per-locale names, falling back to the base language and then to English.

A first-time reader starts with no topics; the catalogue's default topics are
seeded into personal storage once per scope, recorded by catalogue version in
the preference record, so a later catalogue never reseeds them. A personal
bookmark whose coordinate an enabled global topic also links is recorded as
covered on every successful network refresh; once that coverage has been
stable for a day, the next network-verified **Add all** or per-topic load
removes the personal row, the global row stands in for it, and the status line
reports how many bookmarks were merged. A cache-only or unavailable catalogue
never removes anything.

### Trusted contribution mirror

Only a server-approved Telegram identity can mirror changes. After the
one-time disclosure is acknowledged, **Sync now** converts the current
personal topic/assignment state into bounded idempotent events with
deterministic content-derived IDs, appends the journalled explicit global
add/remove intents, and drips them to Robot in sequential
session-authenticated batches of at most 50 events, pausing for the server's
`Retry-After` on a `429`. Topic source names use the repository's English
grammar; missing locale strings continue to fall back to that English source.

`BookmarkStore` remains the durable current-state source; only the explicit
global intents need the journal, because snapshot-derived events are
re-created on every run and deduplicated server-side by their stable IDs. A
redelivered event replays idempotently, and a reused ID with different
content is rejected. Every response returns the receipt counts with the
complete contributor status and the accepted-ledger revision/checksum, so the
final batch settles the panel. A contributed topic is marked **P** until the
host has seen it in the public Bookmarks API catalogue, then **G**; acceptance
alone never changes the marker. Transport failure does not modify personal bookmarks
and leaves the same idempotent events available for the next Sync; no
capability token, WebSocket, or extra port participates.

### Portable recovery

The Bookmarks page can download and merge a bounded personal JSON file. New
documents are version 4 and represent multi-topic assignment with bounded
`colorIndexes` into the color array; version 1, 2, and 3 documents remain
importable. The page can also send the
validated JSON to the authenticated user's private bot chat.
That document has an owner-bound Restore callback that remains useful on
another Telegram client. Pressing it in the owner's private chat creates a
fresh short-lived, one-time Mini App launch containing only bounded Telegram
file metadata. The browser retrieves the document, asks the user to confirm,
merges and flushes it, then acknowledges the restore reference. The document
message remains in chat for future recovery. Robot does not store or log the
backup body in a database or Mini App session.

## Shared verse contract

Reader verses and Search API results are normalized into the same `VerseSelection` interface:

```text
selection_id   Stable UI identity
translation    Translation code
reference      Display reference
book_number    Canonical book number
book_name      Display book name
chapter        Positive chapter number
verse          Positive verse number
text           Browser display text
terms          Optional search metadata
highlights     Optional search highlights
```

Coordinate identity is `translation + book_number + chapter + verse`, and both sources carry the same deterministic direct selection identity. A verse selected from search must appear selected when the same verse is opened in the reader, and vice versa.

## Browser selection lifecycle

1. A reader chapter or Search API response produces `VerseSelection` records.
2. The browser selection store adds or removes records by coordinate identity.
3. The UI derives `aria-pressed`, highlight styling, range boundaries, counters, copy output, and ordering from that store.
4. No Robot request occurs while the selection changes.
5. Post submits the final ordered coordinates once.
6. Robot resolves authoritative Scripture before sending it to Telegram.
7. A failed Post leaves browser state intact; a successful Post clears it.

Browser text, names, and references are display data only and never posting authority.

## Public API origins

The browser transport has four fixed HTTPS origins:

- `https://api.getbible.net/v2/` for mappings, Scripture, and matching `.sha` resources;
- `https://query.getbible.net/v2/` for explicit and grouped Bible-reference resolution;
- `https://search.getbible.net/v2/` for full-text search;
- `https://bookmarks.getbible.net/v1/` for the shared topic catalogue (`index.json` and `all.json`).

The Content Security Policy allows only these four external connection origins in addition to the Mini App origin. Requests omit credentials, disable redirects, send no Telegram data, and use `no-referrer`. Catalogue, chapter, and topic-catalogue requests do not rely on HTTP cache state; search responses are ordinary cacheable `GET`s whose freshness the API declares, and they are never written to IndexedDB. A failed search is `application/problem+json`; `429` and `503` are retried after the announced `Retry-After`, while `400` and `404` are raised at once.

### Waiting for a chapter

A chapter is downloaded, not computed, so the only question a deadline can honestly ask is whether the response is still arriving. The transport bounds a read by inactivity: the stall timer is rearmed by every chunk of body, so a transfer that is still progressing is never abandoned for taking a long time, however slow the connection or however long the chapter. A second, much larger bound exists only so a connection that trickles forever cannot hold a request open without end.

A single wall clock over the whole read asked the wrong question and answered it wrongly on a slow phone: it abandoned transfers that were progressing and told the reader Scripture could not be loaded, when it was still coming.

Reads that fail for a reason that may not repeat — a stalled transfer, a dropped connection, a 429 or 5xx — are retried a bounded number of times with exponential backoff. A refusal that will repeat — a 404, an oversized body, a malformed payload, a checksum mismatch — is raised on the first attempt and never retried.

## Persistent cache

Public GetBible content is stored in IndexedDB under the versioned `public:v2:` namespace. The verified Bookmarks API catalogue is one record in the same namespace, keyed `bookmarks:all`, with the index checksum as its validator and the time of the last successful check. An in-memory implementation is used only when IndexedDB is unavailable.

The cache is bounded by record count, total estimated payload size, per-record
size, least-recently-used eviction, and in-flight request coalescing. Only
identity-free public payloads may enter this cache. Telegram init data, session
tokens, user IDs, preferences, search results, selections, reading history,
bookmarks, last-read records, and posting state are excluded. History and the
hybrid bookmark adapter use separate bounded scoped stores and never enter the
public cache.

### Revalidation and invalidation

Every cached scope is revalidated at least weekly. A changed translation hash invalidates book descendants; a changed book hash invalidates chapter descendants; a changed chapter hash replaces that chapter. Chapter JSON is accepted only when the pre-read and post-read hashes match, SHA-1 over the exact bytes matches that hash, and the payload passes bounded schema and coordinate validation.

A failed validation never replaces a previously accepted record.

## Failure isolation

- A public API failure affects reading only and never invalidates Telegram authentication.
- A Search API failure affects search only; the search origin is separate from the Main and Query origins and from Robot.
- A browser selection operation cannot fail because Robot is unavailable.
- A failed final Post preserves the complete ordered browser selection for retry.
- A malformed or tampered final selection is rejected by Robot before Telegram output.
- Telegram storage failure degrades bookmark/last-read sync to whichever valid
  local or Telegram store remains available; it does not invalidate Scripture
  content, history, or selection.
- Contribution journal, network, or rate-limit failure never rolls back a local
  bookmark mutation; a `429` paces the drip, redelivered events replay
  idempotently on the next Sync, and an unavailable journal is disclosed as
  memory-only.
- A malformed, oversized, or unavailable Bookmarks API response never replaces
  a verified cached catalogue; an `all.json` whose SHA-256 differs from the
  index checksum is rejected; without any cached catalogue the surface reports
  global topics as unavailable until online, and no personal bookmark is
  merged.
- A chat restore transport, validation, or confirmation failure before merge
  leaves current bookmarks unchanged. If persistence or acknowledgement fails
  after merge, the imported merge remains available and the chat document can
  be retried. Local JSON download/import remains independent of chat transport.

## Compatibility and removal policy

Legacy Robot endpoints for translations, books, chapters, Scripture reads, and per-click basket mutation are deprecated compatibility surfaces. Current Mini App assets must not call them. Once the supported mixed-version deployment window ends, those routes, tests, documentation, and dormant helpers must be removed together in one release.

No documentation or test may present legacy routes as the active design.

## Verification

The release gate must prove:

- catalog, chapter, explicit/grouped reference, full-text search, and topic-catalogue traffic goes directly to the GetBible APIs;
- the topic catalogue is accepted only when the SHA-256 of `all.json` equals the checksum in `index.json`, revalidates at most daily unless explicitly pulled, and a personal bookmark is merged into a global link only after a day of network-verified coverage;
- no Robot endpoint serves search, and the removed search routes answer `404`;
- select, unselect, reorder, and clear issue no Robot request;
- reader and search records share coordinate identity;
- selected verses remain highlighted after chapter navigation and source changes;
- a second click unselects immediately;
- final Post submits one ordered selection payload and preserves state on failure;
- Robot re-resolves authoritative Scripture before Telegram output;
- cache hashes, bounds, invalidation, and CSP origin parity remain enforced;
- cold and warm real-browser flows pass;
- successful chapter opens and selections record unique scoped, durable local
  history, promoting revisited coordinates to the front;
- History remains available from every bottom-navigation surface, restores
  translation and coordinates, supports individual removal, and fully resets;
  opening the History page, recording, removal, and reset issue no Robot
  request. Restoring an entry may persist the normal reader preference, but
  uses no history or Scripture-content route;
- bookmark topic operations, canonical cross-translation deduplication,
  multi-topic assignment within the 800-record bound, v4 export with v1/v2/v3
  import, and aggregate-v3 reconciliation with compact cloud topic and recent
  indexes
  remain deterministic under partial API failure;
- the unified global/personal list, **G** marker, per-link hide, and
  per-topic/all-catalog reset remain browser-local and absent from personal
  sync and backup;
- approved contribution disclosure, session-authenticated bounded event
  batches, idempotent per-event replay, rate-limit pacing, and explicit global
  removals remain local-first;
- the Bookmarks API catalogue is verified by SHA-256 against its index,
  revalidated at most daily unless explicitly pulled, kept in the identity-free
  public cache, never replaced by a malformed or mismatched document, and
  reported as unavailable rather than substituted when nothing is cached;
  topic names fall back to English; a personal bookmark is merged into a
  global link only after a day of network-verified coverage;
- private-chat backup is owner-bound and bounded, restore uses a fresh
  one-time launch, the confirmed merge persists before acknowledgement, and no
  backup body enters Robot database/session/log storage.
