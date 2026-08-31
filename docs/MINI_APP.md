# Telegram Mini App

The GetBible Telegram Mini App is a browser application served by the Robot instance. Its public Scripture data plane is independent from the Robot process: catalogs, chapter text, explicit references, cache validation, temporary verse selection, and device-local history belong in the browser. Compact personal bookmarks and last-read coordinates additionally use Telegram Mini App storage when available. Global-topic preferences use scoped browser storage plus Telegram DeviceStorage, but deliberately remain outside CloudStorage and personal backup. Robot remains the authenticated Telegram control plane, the Librarian search adapter, the bounded relay for an explicit private-chat bookmark backup or restore, and the review boundary for approved contributor events and live global-catalogue revisions.

## Active doctrine

Only full-text search and search pagination use Librarian. Robot also owns the
authenticated control paths for sessions, preference compatibility, final
Post, explicit bookmark chat backup/restore, and trusted contributions.

| Capability | Owner |
| --- | --- |
| Translation catalog and metadata | Browser → `api.getbible.net/v2` |
| Translation books | Browser → `api.getbible.net/v2` |
| Book chapters and hashes | Browser → `api.getbible.net/v2` |
| Chapter text and hashes | Browser → `api.getbible.net/v2` |
| Explicit or grouped references | Browser → `query.getbible.net/v2` |
| Persistent public cache | Browser IndexedDB |
| Selected verse order and highlighting | Browser memory |
| Opened chapter and selected verse history | Scoped browser `localStorage` |
| Personal bookmark aggregate v3, topics, recent topics, and active topic | Scoped `localStorage` + Telegram `DeviceStorage` / `CloudStorage` |
| Global topic visibility, exclusions, and legacy mapping | Scoped `localStorage` + Telegram `DeviceStorage` only |
| Approved-contributor push outbox and explicit global intents | Per-instance, authenticated-user-scoped browser IndexedDB |
| Contribution push transport | Telegram `sendData` → `web_app_data` bot updates |
| Contributor status, staged push bundles, receipts, and live global-catalogue revision | Robot → private per-instance SQLite |
| Compact last-read coordinate | Scoped `localStorage` + Telegram `DeviceStorage` / `CloudStorage`, with Robot preference compatibility |
| Bookmark JSON download/import | Browser |
| Private-chat bookmark backup/restore | Browser confirmation + Robot/Telegram transport |
| Full-text search and pagination | Robot → Librarian |
| Telegram authentication and launch binding | Robot |
| Reader preference compatibility | Robot |
| Final Telegram delivery | Robot |

A normal reader action must never call Robot for translations, books, chapters, chapter text, selecting, unselecting, reordering, clearing, or copying.
For an approved contributor only, explicit global add/remove choices queue
durable intents and an explicit **Push** shares the current snapshot for
review over Telegram's own uplink; that mirror never becomes a dependency of
the local action.

## Runtime flow

```text
Telegram WebView
  ├─ signed initData / preferences / search / post / chat backup → Robot
  ├─ catalogs / chapters / hashes                    → api.getbible.net/v2
  ├─ explicit or grouped references                  → query.getbible.net/v2
  ├─ temporary ordered selection                     → BrowserSelectionStore
  ├─ unique coordinate history                       → scoped local ReadingHistoryStore
  ├─ personal bookmarks / topics / last-read         → local + Telegram storage adapter
  ├─ global topic visibility / exclusions            → scoped localStorage + DeviceStorage
  ├─ approved contribution push (GBC1 chunks)        → Telegram sendData → bot updates
  └─ contributor status / receipt / live overlay     → authenticated Robot API
```

Reader and search results are normalized into one verse descriptor. Coordinate identity is:

```text
translation + book_number + chapter + verse
```

Opaque Librarian tokens and deterministic reader IDs are transport details, not selection identity. A verse selected from search therefore appears selected in the reader, and a second click from either surface removes it.

## Browser selection lifecycle

`BrowserSelectionStore` is the only owner of temporary selected state. It provides bounded add, remove, reorder, clear, snapshot, and final-coordinate operations. Snapshots are defensive copies, and display data cannot mutate the store accidentally.

The following behavior is local and synchronous:

- graphical selected styling and `aria-pressed`;
- selection-range start/end styling;
- select and unselect;
- ordering and removal;
- counters and navigation badges;
- clipboard output;
- persistence of selected state while navigating chapters during the active WebView session.

A failed Robot request cannot undo a browser selection. A failed final Post leaves the complete ordered selection intact for retry. A successful Post clears it.

## Public API transport

The browser transport accepts only these fixed HTTPS origins:

- `https://api.getbible.net/v2/`
- `https://query.getbible.net/v2/`

Requests omit cookies and credentials, never include Telegram data, reject redirects, use `no-referrer`, enforce time and response-size bounds, and validate response coordinates and schemas before use.

Both CSP enforcement layers must contain the same allowlist:

- the `Content-Security-Policy` meta element in `miniapp/index.html`;
- the response header emitted by `MiniAppStaticHandler`.

## Reading history

The bottom navigation exposes History permanently on Home, Search, Bible,
History, and Selected. History is a normal, keyboard-accessible page, so the
same footer remains visible and usable. Its heading and empty state follow the
Selected page, including the centered **Open the Bible** action, while the top
bar keeps only the centered getBible icon. The first and near-viewport entries
hydrate independently through the bounded public chapter data plane; a missing
coordinate shows a localized unavailable state, while a transient request
failure retries after connectivity returns. Each entry reopens its exact
coordinate in the currently selected translation. An entry can be removed
individually, and Clear all removes the complete device-local record.

`ReadingHistoryStore` is versioned, unique and newest-first, and bounded to
1,000 coordinate-only entries in authenticated user-scoped `localStorage`, with
an in-memory fallback.
Chapter visits share book/chapter identity across translations, while verse
selections use their full book/chapter/verse coordinate; an exact coordinate
also coalesces across event kinds. Revisiting one moves its stable entry to the top. Restoration
compacts older duplicate records. The store never keeps verse bodies, Telegram
data, Robot credentials, or user identity, and none of its mutations call
Robot or Telegram storage. History survives later WebView sessions in that
browser but does not synchronize between devices.

## Navigation and bookmarks

The permanent footer remains the five actions Home, Search, Bible, History, and
Selected. Bookmarks is reached from the Home summary rather than adding a sixth
footer action. Home contains the two primary actions **Search Scripture** and
**Read the Bible**, followed by Selected, History, and Bookmarks summaries; the
History summary appears only when populated. The translation control remains in Search and Bible; Home,
History, Selected, and Bookmarks show only the centered getBible icon.

Selecting a Bible verse reveals a small keyboard-accessible ellipsis at its
bottom-right; selection alone never opens the menu. Activating the ellipsis
opens the anchored menu, where the complete canonical book/chapter/verse may be
assigned to any unassigned topic or unassigned from one topic without affecting
its others. An existing assignment is a no-op, and the same coordinate in
another translation updates its one personal record instead of duplicating it.
The menu links directly to each assigned topic. Topic detail offers navigation
back to all topics and, when opened from the reader, back to the source verse.
The assignment picker places a clearable **Recently used** group in most-recent
order above the complete locale-aware alphabetical topic list. It retains every
topic used until the user clears it (within the 100-topic product limit), and a
recent topic also remains in the complete list intentionally. The main topic
list uses only the alphabetical presentation and never rewrites persisted topic
order merely to sort the screen. A plus-card at the end of that list expands
the name/color form for creating one topic; topic editing is kept in the opened
topic instead of a second manager list.

The Bookmarks surface lists personal and global verse links together in one
topic list; global rows carry a **G** marker and progressively hydrate visible
verse text for the currently selected translation without persisting that text.
Compact **Add all** and **Remove all** controls appear before search with a
disclosure explaining that global topics are curated verse sets. One global
link may be hidden, or all global links may be removed for one topic. Loading
that topic or the full catalog resets its exclusions without duplication. The
built-in catalogue contains the repository's reviewed topic-to-verse links.
Scoped visibility, exclusions, and the legacy numeric-topic mapping reconcile
through localStorage
and Telegram `DeviceStorage`, surviving a discarded Desktop WebView store.
They never consume personal records, enter `CloudStorage`, or enter backups.

Personal records are bounded to 800 canonical verses, and each may belong to
multiple topics without consuming another verse slot. Global topic definitions
are reviewed server-owned metadata: their localized names cannot be edited,
but their user-facing colors can be changed from topic detail. Custom topic
detail supports the same color control and inline name editing with explicit
confirm and cancel actions. Removing any topic warns that its linked verse
assignments will also be removed. Removing a global topic changes only that
user's catalogue state; **Add all** recreates it from the reviewed definition.
The new-topic plus-card is therefore the only topic-creation area, and the UI
does not duplicate global restoration there. The global catalogue provider
merges a validated, reviewed per-instance overlay over the bundled catalogue.
The bundled source remains available when the overlay request, validation, or
local cache fails.

Immediately below the Global topics controls, an approved contributor sees a
collapsible **Manage Contribution** panel containing synchronization state,
review information, and its **Push** and **Pull** actions. The panel is absent
when the authenticated session has no contribution authority. It is
independent of the new-topic form and remains available without scrolling past
the topic list.

`BookmarkStore` writes personal aggregate version 3 immediately to scoped
`localStorage`. `TelegramBookmarkStorage` compares timestamped candidates from
local storage and Telegram `DeviceStorage` and `CloudStorage`, selects the
newest valid copy, then mirrors it to available stores. Cloud bookmark values
use topic indexes into the synchronized topic manifest and reconstruct full
identifiers on read. Stable item values plus a metadata fingerprint preserve
metadata-last atomicity while avoiding full rewrites, and transient partial
writes receive bounded automatic retries. The active topic and compact
last-read participate; a
  timestamped cleared marker prevents an older remote position from reappearing
  after a failed sync. Global visibility and exclusions use their separate
  DeviceStorage-only adapter; selection, history, the catalog itself, and
  chapters do not enter Telegram storage. Unsupported Telegram storage leaves
  the local copy usable and shows a degraded-sync notice.

The Bookmarks page offers bounded personal JSON **Download** and **Import**,
plus **Back up to chat**. New documents are version 4 and use bounded
`colorIndexes` into the color array for multi-topic assignment; version 1, 2,
and 3 documents remain importable. The chat
action passes a validated document through an
authenticated, idempotent endpoint to the user's private bot chat. Telegram's
document message carries an owner-bound Restore callback. A callback from the
owner's private chat creates a fresh, short-lived, one-time launch that opens
Bookmarks, retrieves and validates the Telegram file, asks before merging,
flushes the result to persistent storage, and acknowledges that launch. The
original document remains in chat. Robot sessions and persistence keep only
delivery/file metadata and never a backup body; structured logs exclude the
document.

### Trusted contribution mirror

The hidden private Telegram `/contributor` command is the only application
entry point. It submits the signed numeric Telegram ID for operator review;
the Mini App never treats a username, client flag, or hidden control as
authorization. After approval, the next authenticated Mini App launch shows a
one-time disclosure that topic and verse-tag changes are shared for review,
and `/contributor` attaches a persistent reply-keyboard button, **Push
contribution**, to the contributor's private chat. That button's `web_app` URL
carries `?context=push`, and only a Mini App launched from such a
reply-keyboard button can call `Telegram.WebApp.sendData()`.

**Push** runs in that launch, which lands on the Bookmarks contribution panel.
It builds one bounded envelope — protocol version 1, stable `sync_id` and
`client_id`, the current personal topic/assignment snapshot, pending explicit
operations, and the disclosure acknowledgement — serializes it,
deflate-compresses it when that is smaller, base64url-encodes it, and splits
it into numbered chunks

```text
GBC1|<sync_id>|<index>|<count>|<d|j>|<sha256-hex-of-plaintext>|<payload>
```

of at most 4096 bytes each and at most 64 per transfer. The envelope contains
coordinate identity only, never Scripture text or Telegram identity. Each
chunk goes through one `sendData()` call, which closes the Mini App; Telegram
delivers it to the bot as a `web_app_data` service message on the bot's
ordinary polling or webhook update channel. Push therefore has zero inbound
surface: no inbound port, Caddy route, WebSocket, or bearer token
participates, and behind a firewall that exposes only 443 for the Mini App, a
push in polling mode needs nothing at all.

The bot consumes each service message: it rate-limits the sender, stages the
chunk durably and idempotently per chunk index in the private SQLite store,
deletes the service message from the chat, and keeps one edited progress
message for a multi-part transfer ("Received part i of n — tap Push
contribution to send the next part"). When the transfer completes, exactly one
caller assembles the bundle, base64url-decodes it, decompresses it under a
1 MiB plaintext bound that refuses decompression bombs, verifies the SHA-256
digest of the plaintext, and commits through the unchanged atomic
`ContributionStore.synchronize_snapshot` path: snapshot, operations,
disclosure, derived moderation events, and the durable receipt in one SQLite
transaction. Approval and disclosure are rechecked on every staging and commit
write, stale incomplete bundles expire after 24 hours, and the bot then edits
or sends one confirmation in the chat.

The client side is lossless. The envelope and its encoded messages persist as
a durable outbox before the first `sendData` call, so an interrupted transfer
resends the identical bytes under the same `sync_id`, and the server answers
an exact replay with the stored durable receipt without duplicating anything.
Explicit global add/remove intents clear only when the server's receipt is
observed. `BookmarkStore` remains the durable source for current personal
state: a failed transfer cannot roll back a bookmark mutation, and revocation
or rejection stops future submission without deleting personal topics or
markings.

**Pull** sits next to **Push** in the Manage contribution card. It refreshes
contributor status through `GET /api/v1/contributions/status?details=1`,
confirms any pending push receipt through
`GET /api/v1/contributions/receipt?sync_id=…` (settling the outbox), strictly
refreshes the live catalogue over the network, and performs the Add-all
semantics. Published contributor topics reclassify from personal (**P**) to
global (**G**) through the existing mapping machinery; personal verses outside
the global set keep rendering untouched. There is no background status
polling: status refreshes at session bootstrap and on Pull. These reads use
the plain opaque Mini App session bearer issued at the initial session
exchange — no separate contribution credential exists.

Contribution source topic names must be English. The UI explains that rule,
the browser omits invalid topic-name proposals, the server validates them
again, and the operator can still reject or correct a proposal. Accepted
repository topics receive the stable key `bookmark_topics.<english-slug>` plus
their canonical English source. A locale with no new translation resolves to
that English name; no foreign-language catalogue is synthesized in this flow.

The reviewed catalogue endpoint returns only canonical topic metadata,
coordinate additions/removals, a server revision, checksum, and ETag. It
contains no contributor identity. A bounded per-instance,
authenticated-scope cache may retain that envelope. An authenticated valid
`200` replaces it even after a database restore, `304` retains it unchanged,
and failed or malformed loading uses the bundled catalogue. Publishing a
live revision changes the current instance immediately and survives restart,
while repository branch publication remains a separate operator step.

## Cache integrity

Public content is stored under a versioned `public:v2:` IndexedDB namespace. The cache has bounded record count, bounded total estimated size, bounded per-record size, least-recently-used eviction, and in-flight request coalescing. An in-memory adapter is used when IndexedDB is unavailable.

Every cached scope stores the exact published SHA-1 and is revalidated at least weekly. A changed translation hash invalidates descendants, a changed book hash invalidates chapter descendants, and a changed chapter hash replaces that chapter only.

Chapter acceptance requires the pre-read and post-read `.sha` values to match, SHA-1 over the exact downloaded bytes to equal that value, bounded schema and coordinate validation, and atomic replacement after complete validation. Failed validation never overwrites a previously accepted record.

## Search boundary

`/search` is the sole content-discovery path that uses Robot and Librarian. Robot returns bounded normalized verse descriptors and paging metadata. The browser registers those descriptors with the same `BrowserSelectionStore` used by reader chapters.

Search failure is isolated from reading. Reading failure is isolated from authentication. Neither may clear a valid Telegram session.

## Post boundary

Post is the only selection synchronization boundary. The browser submits the final ordered selection once. Browser text, book names, references, and UI IDs are display data only. Robot must validate the submitted coordinates and obtain authoritative Scripture before Telegram delivery.

No ordinary click may create server basket state. Legacy per-click basket and Robot Scripture-read routes are not part of the active Mini App contract and must not be referenced by current browser code, tests, or documentation.

## Security boundary

The public HTML shell is not an authentication boundary. Robot validates fresh
Telegram-signed `initData` and the owner-bound launch during the initial session
exchange only. It then issues an active opaque session bearer for ordinary
actions and, only for a currently approved contributor, a separate revocable
contributor capability. The bot token remains server-side.

The browser is untrusted for final output. It may control display state, but it cannot determine the authoritative text delivered to Telegram. Final output remains bounded, escaped, idempotent, and tied to the originating user, chat, and topic.

The browser is also untrusted for contributor authority and global catalogue
publication. Every synchronization validates the durable capability and
current approved numeric Telegram user ID. Client-supplied verse text is never
a review authority; the terminal reviewer fetches and validates the configured
translation directly from `query.getbible.net` and defers when it is
unavailable.

Do not trust `Referer`, `User-Agent`, an obscure URL, or client IP as authentication. Do not expose the bot token, session token, or Telegram init data to GetBible API origins or browser persistent caches.

## Listeners

| Listener | Default | Exposure |
| --- | ---: | --- |
| Health/readiness/metrics | `127.0.0.1:8081` | private only |
| Telegram webhook | private IP, port `9001` by default | exact private webhook path; remote-proxy access is firewall-restricted to the proxy source |
| Mini App | `127.0.0.1:9201` | HTTPS reverse proxy only |

Polling and the Mini App can run together. The Mini App listener is unrelated to Telegram update delivery.

## Production configuration

Setup and maintenance must be performed through `setup.sh` or `getbible-robot`, not by editing generated listener or Caddy files directly.

```bash
sudo getbible-robot miniapp production
sudo getbible-robot status production
sudo getbible-robot doctor production
```

| Variable | Rule |
| --- | --- |
| `MINI_APP_ENABLED` | Managed deployment toggle |
| `MINI_APP_PUBLIC_URL` | Absolute HTTPS URL without credentials, query, or fragment |
| `MINI_APP_LISTEN` | Loopback by default; operator-selected backend address in external proxy mode |
| `MINI_APP_PORT` | Unique per instance |
| `MINI_APP_INIT_DATA_MAX_AGE_SECONDS` | Short Telegram authentication window |
| `MINI_APP_LAUNCH_TTL_SECONDS` | Short owner-bound launch lifetime |
| `MINI_APP_SESSION_TTL_SECONDS` | Three-hour default absolute authenticated-session lifetime |
| `MINI_APP_SESSION_LIMIT` | Bounded active sessions |
| `MINI_APP_SESSIONS_PER_USER` | Bounded sessions per user |
| `MINI_APP_MAX_SEARCHES_PER_SESSION` | Bounded Librarian result snapshots |
| `MINI_APP_MAX_SELECTIONS` | Browser and final-post selection limit |
| `MINI_APP_TRUSTED_PROXY_CIDRS` | Optional advanced restriction for forwarded client addresses |
| `CONTRIBUTION_STORE_FILE` | Absolute private SQLite path; blank disables contribution endpoints and `/contributor` applications |
| `CONTRIBUTION_CONTRIBUTOR_LIMIT` | Bounded application population |
| `CONTRIBUTION_EVENT_LIMIT` | Bounded retained event journal |

The public browser cache is identity-free. Robot retains only the compatible
reader preference containing translation and reader coordinates. Device-local
history and hybrid bookmark state live in separate scoped browser/Telegram
stores and never enter the public cache.

In managed Caddy mode the public listener is HTTPS port 443; the Mini App's
own port remains an internal loopback listener. In external mode, Caddy is not
used. The external proxy keeps its public HTTPS port and forwards to the bot
host address and `MINI_APP_PORT` printed by setup.

The generated Caddy route is deny-by-default. It forwards only packaged
shell/assets and the bounded Mini App `/api/v1/*` namespace; every other
request receives an empty `404` without contacting Tornado. Exact backup and
contribution-sync matchers apply 5 MiB and 1 MiB limits before the general
64 KiB API matcher. Tornado remains deny-by-default inside that namespace and
recognizes routes and methods before authentication or rate-limit accounting.
This prefix prevents a newly deployed API from being stranded behind a stale
endpoint allow-list without exposing a broad site catch-all.

Caddy's normal reverse-proxy behavior preserves `Authorization` and `Origin`;
the generated route does not strip or reconstruct them. Authenticated requests
do not depend on `X-Telegram-Init-Data`: raw launch data is consumed by the
initial session exchange and is not a per-request proxy credential.

Posting is one bounded authenticated action. The browser sends ordered
coordinate identities and an idempotency key. Robot validates every coordinate
against authoritative catalogs and derives references server-side; it never
trusts browser-provided Scripture text or references. A multi-verse post
therefore consumes one action instead of a burst of basket synchronization
requests.

## Deployment consistency

Deploy HTML, JavaScript modules, server code, and documentation from one validated commit. `index.html` is served with `no-store`; static modules revalidate through ETags. This prevents a Telegram WebView from combining a new shell with old modules.

After deployment, verify:

1. cold reader load uses the public Main API;
2. warm reader load uses IndexedDB and hash policy correctly;
3. explicit references use Query API;
4. search is the only Scripture-discovery path through Robot/Librarian;
5. selecting highlights the verse number and body immediately;
6. selecting the same verse from search and reader does not duplicate it;
7. a second click unselects it;
8. navigation preserves selected styling;
9. no Robot basket or Scripture request occurs before Post;
10. reading history shows verse text and reopens the exact verse in the
    currently selected translation;
11. History remains available from every footer route and revisits move to the
    top without duplication;
12. individual and complete history clearing work locally;
13. Home exposes Search and Bible actions plus conditional Selected, History,
    and Bookmarks summaries, with the route-specific top-bar controls;
14. the compact verse ellipsis alone opens the bookmark menu, and a personal
    verse can belong to multiple colored topics within the 800-record bound;
15. bookmark and last-read state reconcile on a second supported Telegram
    client without synchronizing history or downloaded Scripture;
16. bounded JSON download/import and private-chat backup/restore work, and a
    confirmed restore persists before its one-launch reference is acknowledged;
17. the unified topic list marks global links with **G**, supports per-link hide
    and per-topic/all-catalog reset, and excludes global links from personal
    sync and backup;
18. topic management is alphabetical, while verse assignment shows a clearable
    recent group followed by the full alphabetical list;
19. an unapproved user never receives a contributor capability or creates
    server contribution events, while an approved user's one-request snapshot
    sync commits atomically, replays the same receipt after an ambiguous
    response, and keeps every local mutation on server failure;
20. a newly published live topic appears on the same instance, uses its English
    source when the active locale lacks a translation, and falls back to the
    bundled catalogue when overlay validation fails;
21. Post failure preserves selection and successful Post clears it.

## Verification gate

A release is not ready unless permanent CI proves Python 3.10–3.14, the production container, lint, strict typing, branch coverage, dependency and secret scans, CodeQL, public API routing, CSP parity, hash verification, bounded caches, bounded selections, durable local history, bookmark-domain and hybrid-storage invariants, private-chat backup/restore ownership and bounds, contributor authorization/idempotency/privacy, live-catalogue fallback, English topic fallback, real Chromium navigation, graphical select/unselect, cross-source identity, no pre-Post Robot mutation, and authoritative idempotent posting.
