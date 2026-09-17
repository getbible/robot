# Telegram Mini App

The GetBible Telegram Mini App is a browser application served by the Robot instance. Its public Scripture data plane is independent from the Robot process: catalogs, chapter text, explicit references, cache validation, temporary verse selection, and device-local history belong in the browser. Compact personal bookmarks and last-read coordinates additionally use Telegram Mini App storage when available. Global-topic preferences use scoped browser storage plus Telegram DeviceStorage, but deliberately remain outside CloudStorage and personal backup. Full-text search and the shared bookmark topic catalogue, like every other Scripture read, go from the browser to a public GetBible origin. Robot remains the authenticated Telegram control plane, the bounded relay for an explicit private-chat bookmark backup or restore, and the review boundary for approved contributor events, whose accepted changes reach the public Bookmarks API through a pull request on the builder repository.

## Active doctrine

Every Scripture read is browser-to-GetBible: catalogues and chapters from the
Main API, references from the Query API, full-text search from the Search
API. Robot owns the authenticated control paths for sessions, preference
compatibility, final Post, explicit bookmark chat backup/restore, and trusted
contributions.

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
| Approved-contributor journal of explicit global add/remove intents | Per-instance, authenticated-user-scoped browser IndexedDB |
| Global topic catalogue | Browser → `bookmarks.getbible.net/v1`, verified by SHA-256 and cached in IndexedDB |
| Contributor status, events, the accepted-contribution ledger, and the last observed public catalogue | Robot → private per-instance SQLite |
| Compact last-read coordinate | Scoped `localStorage` + Telegram `DeviceStorage` / `CloudStorage`, with Robot preference compatibility |
| Bookmark JSON download/import | Browser |
| Private-chat bookmark backup/restore | Browser confirmation + Robot/Telegram transport |
| Full-text search and pagination | Browser → `search.getbible.net/v2` |
| Telegram authentication and launch binding | Robot |
| Reader preference compatibility | Robot |
| Final Telegram delivery | Robot |

A normal reader action must never call Robot for translations, books, chapters, chapter text, selecting, unselecting, reordering, clearing, or copying.
For an approved contributor only, a successful personal topic/bookmark mutation
also marks the contribution mirror for the next explicit Sync; that mirror
never becomes a dependency of the local action.

## Runtime flow

```text
Telegram WebView
  ├─ signed initData / preferences / post / chat backup → Robot
  ├─ catalogs / chapters / hashes                    → api.getbible.net/v2
  ├─ explicit or grouped references                  → query.getbible.net/v2
  ├─ full-text search, page by page                  → search.getbible.net/v2
  ├─ shared topic catalogue, verified and cached     → bookmarks.getbible.net/v1
  ├─ temporary ordered selection                     → BrowserSelectionStore
  ├─ unique coordinate history                       → scoped local ReadingHistoryStore
  ├─ personal bookmarks / topics / last-read         → local + Telegram storage adapter
  ├─ global topic visibility / exclusions            → scoped localStorage + DeviceStorage
  └─ approved contribution outbox and status         → authenticated Robot API
```

Reader and search results are normalized into one verse descriptor. Coordinate identity is:

```text
translation + book_number + chapter + verse
```

Reader verses and search results carry the same deterministic direct selection identity; there is no opaque search token. A verse selected from search therefore appears selected in the reader, and a second click from either surface removes it.

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
- `https://search.getbible.net/v2/`
- `https://bookmarks.getbible.net/v1/`

Requests omit cookies and credentials, never include Telegram data, reject redirects, use `no-referrer`, enforce time and response-size bounds, and validate response coordinates and schemas before use. Search responses are ordinary cacheable `GET`s whose freshness the API declares; a failed search is `application/problem+json` and `429`/`503` carry the wait in `Retry-After`. The topic catalogue's `all.json` is accepted only when its SHA-256 equals the checksum in `index.json`.

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
catalogue is the public Bookmarks API: cached in IndexedDB, revalidated
against `index.json` at most once a day, replaced only by an `all.json` whose
SHA-256 matches, and always network-verified on an explicit **Add all** or
per-topic load. Until a catalogue has loaded the surface reports global topics
as unavailable until online.
Scoped visibility, exclusions, and the legacy numeric-topic mapping reconcile
through localStorage
and Telegram `DeviceStorage`, surviving a discarded Desktop WebView store.
They never consume personal records, enter `CloudStorage`, or enter backups.

Personal records are bounded to 800 canonical verses, and each may belong to
multiple topics without consuming another verse slot. A first-time reader
starts with the catalogue's default topics, seeded once when the catalogue
first loads. A personal bookmark whose verse an enabled global topic also
links is merged into the global row after a day of network-verified coverage,
and the status line reports how many were merged; a cache-only or unavailable
catalogue never removes anything. Global topic definitions
are reviewed catalogue metadata: their names, shown in the reader's locale
with English fallback, cannot be edited,
but their user-facing colors can be changed from topic detail. Custom topic
detail supports the same color control and inline name editing with explicit
confirm and cancel actions. Removing any topic warns that its linked verse
assignments will also be removed. Removing a global topic changes only that
user's catalogue state; **Add all** recreates it from the reviewed definition.
The new-topic plus-card is therefore the only topic-creation area, and the UI
does not duplicate global restoration there. Robot serves no catalogue and
publishes no overlay; the last verified catalogue in IndexedDB stands in when
the Bookmarks API is unreachable.

Immediately below the Global topics controls, an approved contributor sees a
collapsible **Manage Contribution** panel containing synchronization state,
review information, and its sync action. The panel is absent when the
authenticated session has no contribution authority. It is independent of the
new-topic form and remains available without scrolling past the topic list.

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
one-time disclosure that topic and verse-tag changes are shared for review.
Synchronization starts only after that disclosure is acknowledged.

The panel is visible to any approved contributor with a live session. Status
reads authenticate exactly as preferences do: the plain opaque session bearer
issued at the initial session exchange. Event submission requires a second
credential on top of that bearer. An approved contributor receives a
long-lived `contribution_token` — `gbc_` plus 43 URL-safe characters, held
server-side only as a SHA-256 digest, valid for ninety days, at most 16 active
per contributor, and revoked immediately when approval is withdrawn. The
token is delivered only inside ordinary JSON payloads — the session
bootstrap's `contributions` object, `GET
/api/v1/contributions/status?details=1`, and the `status` of every events
response — never in a custom header, and a non-approved user never receives
one. `POST /api/v1/contributions/events` refuses a request that does not
carry both the session bearer and a valid body `contribution_token` with
`403 contribution_not_allowed` before any store work. With both present, the
browser still never infers authority from a cached status flag, and Robot
rechecks the contributor's approved application and acknowledged disclosure
in the durable SQLite store on every event batch.

**Sync now** converts the current personal topic/assignment state into
bounded idempotent contribution events whose `client_event_id`s derive
deterministically from their content (`baseline:<type>:<16-hex>`), appends the
journalled explicit global add/remove intents, and posts them to the
same-origin `POST /api/v1/contributions/events` endpoint in sequential
batches of at most 50 events. Each request is deliberately small — batches
are additionally capped at about 2 KB of JSON — because a small POST is the
one upload shape every deployment's network path has already proven. On HTTP `429` the client waits the announced
`Retry-After` (bounded to 1–60 seconds, at most 5 retries per batch) and
continues, and when a request dies on the wire while smaller requests pass —
a firewall or path-MTU element silently dropping larger uploads — the drip
halves the failed batch, down to one event per request if necessary, so the
contribution seeps to the server one small chunk at a time through any path
that can carry an ordinary API request.
Events carry coordinate identity only, never Scripture text or Telegram
identity, and the one-time disclosure acknowledgement rides the first batch
as an optional `disclosure_acknowledged` field in the same POST body. Each
batch body also carries the current `contribution_token`; the client harvests
the freshest token from every status-bearing response, recovers a missing or
rotated token with one ordinary status request, and shows drip progress
("part X of Y") while a multi-batch synchronization runs. The batches draw on
a dedicated server-side contribution rate budget, configured by
`CONTRIBUTION_RATE_CAPACITY` (default `60`) and
`CONTRIBUTION_RATE_REFILL_PER_SECOND` (default `5.0`) and separate from the
public Mini App and user limits, so a large personal dataset neither starves
nor is starved by public traffic; a batch beyond the budget waits behind
`429` and `Retry-After`, it never fails permanently.

Every response returns the complete result set: `accepted`, `replayed`, and
`event_ids` receipts, the full detailed contributor status, and the
accepted-ledger revision/checksum in `catalog` — `{revision: null,
checksum: null, available: false}` when enrichment is temporarily
unavailable, which never turns a committed batch into an error. The final
batch's response therefore
settles the panel in one round trip: what happened, where the contributor
stands, and how much they have contributed. A redelivered event replays
idempotently per contributor and `client_event_id` under constant-time
payload-digest comparison; a reused ID with different content fails closed
with HTTP `409`.

`BookmarkStore` remains the durable source for current personal state, and
snapshot-derived events are re-created deterministically on every run, so a
crash mid-drip loses nothing: the next Sync resends and the server
deduplicates. A transport failure cannot roll back a bookmark mutation.
Revocation or rejection stops future submission without deleting personal
topics or markings. Personal topics and bookmarks are never altered by any
synchronization outcome: a failed or pending synchronization leaves the
personal topic and its bookmarks untouched, only a topic the host has seen
in the public Bookmarks API catalogue is ever marked **G**, and nothing is
removed by synchronization. The transport is ordinary same-origin HTTPS on
the Mini
App domain and port already in use; it needs no custom header, WebSocket,
long-lived connection, extra listener, or Telegram `sendData` bridge. Status
polls between synchronizations use
`GET /api/v1/contributions/status?details=1`. Robot serves no catalogue
route; the global catalogue is read from `bookmarks.getbible.net/v1`.

Contribution source topic names must be English. The UI explains that rule,
the browser omits invalid topic-name proposals, the server validates them
again, and the operator can still reject or correct a proposal. An accepted
topic keeps its canonical English source name and id; translations live in
the builder repository's locale files and reach readers through the
catalogue's per-locale names. A locale without a translation resolves to the
English name; no foreign-language catalogue is synthesized in this flow.

Acceptance does not publish. **Publish** in the maintainer workflow records
the approved changes in the store's submission ledger and opens a pull request
on `getbible/v1_bookmark_builder`; the upstream merge publishes the Bookmarks
API. Robot then reads `index.json` on its check interval, records the live
catalogue, marks the applied events it contains as live, and queues one
private "contributions live" notice per contributor. The detailed status
reports a topic as published only once it has been seen in that catalogue, so
the panel's **P** marker becomes **G** on the next status refresh after the
upstream publication, never on acceptance.

## Cache integrity

Public content is stored under a versioned `public:v2:` IndexedDB namespace. The cache has bounded record count, bounded total estimated size, bounded per-record size, least-recently-used eviction, and in-flight request coalescing. An in-memory adapter is used when IndexedDB is unavailable.

Every cached scope stores the exact published SHA-1 and is revalidated at least weekly. A changed translation hash invalidates descendants, a changed book hash invalidates chapter descendants, and a changed chapter hash replaces that chapter only.

Chapter acceptance requires the pre-read and post-read `.sha` values to match, SHA-1 over the exact downloaded bytes to equal that value, bounded schema and coordinate validation, and atomic replacement after complete validation. Failed validation never overwrites a previously accepted record.

## Search boundary

`/search` opens the Search page, and the page queries `search.getbible.net/v2` directly with the reader's filters as query parameters. It pages 25 matches at a time by offset, shows the API's exact total, and restarts from the first page if the corpus `sha` changes between pages. The browser normalizes matches into the same verse descriptor and direct selection identity as reader chapters and registers them with the same `BrowserSelectionStore`. Robot serves no search endpoint; search defaults persist through the ordinary preferences endpoint. See [Search](SEARCH.md).

Search failure is isolated from reading, because the search origin is separate from the Main and Query origins. Reading failure is isolated from authentication. Neither may clear a valid Telegram session.

## Post boundary

Post is the only selection synchronization boundary. The browser submits the final ordered selection once. Browser text, book names, references, and UI IDs are display data only. Robot must validate the submitted coordinates and obtain authoritative Scripture before Telegram delivery.

No ordinary click may create server basket state. Legacy per-click basket and Robot Scripture-read routes are not part of the active Mini App contract and must not be referenced by current browser code, tests, or documentation.

## Security boundary

The public HTML shell is not an authentication boundary. Robot validates
Telegram-signed `initData` and the owner-bound launch during the initial session
exchange only. The one-time launch token the robot issued is the proof of a
live tap and the signed data proves who is tapping: Telegram clients reuse
signed launch data from one launch to the next, so a launch the process still
holds always opens the session it promised, whatever earlier session or replay
record that data has, and the data may then be up to a week old. Without a
live launch (a menu launch, an old button, a reload) the signed data must be
fresh, a reload rejoins the exact page's session, and an unknown or lapsed
launch opens a private session at Home. Robot then issues an active opaque
session bearer for ordinary actions; approved-contributor synchronization uses
that same bearer plus a contributor token that only approved contributors ever
receive, after the session exists. Opening the app involves no contributor
token for anyone. The bot token remains server-side.

The browser is untrusted for final output. It may control display state, but it cannot determine the authoritative text delivered to Telegram. Final output remains bounded, escaped, idempotent, and tied to the originating user, chat, and topic.

The browser is also untrusted for contributor authority and global catalogue
publication. Every event batch is authorized again against the approved
application and acknowledged disclosure recorded for the current numeric
Telegram user ID in the durable store. Client-supplied verse text is never
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
| `MINI_APP_SESSION_TTL_SECONDS` | Ninety-day default absolute authenticated-session lifetime |
| `MINI_APP_SESSION_LIMIT` | Bounded active sessions |
| `MINI_APP_SESSIONS_PER_USER` | Bounded sessions per user |
| `MINI_APP_MAX_SELECTIONS` | Browser and final-post selection limit |
| `MINI_APP_TRUSTED_PROXY_CIDRS` | Optional advanced restriction for forwarded client addresses |
| `CONTRIBUTION_STORE_FILE` | Absolute private SQLite path; blank disables contribution endpoints and `/contributor` applications |
| `CONTRIBUTION_CONTRIBUTOR_LIMIT` | Bounded application population |
| `CONTRIBUTION_EVENT_LIMIT` | Bounded retained event journal |
| `CONTRIBUTION_RATE_CAPACITY` | Dedicated contributor event-batch budget, separate from the public limits |
| `CONTRIBUTION_RATE_REFILL_PER_SECOND` | Refill rate of that contribution budget |

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
request receives an empty `404` without contacting Tornado. One exact
bookmark-backup POST matcher applies its 5 MiB limit before the general
64 KiB API matcher; contribution event batches fit the ordinary budget and
need no exception. Tornado remains deny-by-default inside that namespace and
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

Deploy HTML, JavaScript modules, server code, and documentation from one validated commit. Every packaged file, `index.html` and all static modules alike, is served with `no-store`, because Telegram WebViews do not reliably perform conditional revalidation. Some WebViews keep serving a cached module even against `no-store`, so the shell is rendered by the server and loads `app.js`, `boot.js`, and `styles.css` from `build/<fingerprint>/`, a segment derived from the bytes of the complete packaged client tree; every relative import resolves below the same prefix. A launch therefore always downloads the running server's complete module graph, and a WebView can never combine a new shell with old modules because a new shell never names an old address. The `build/<fingerprint>/` segment is a cache key, not a version selector: the running server answers any well-formed fingerprint from its own tree. Telegram's menu button is pointed at `<public URL>/?build=<fingerprint>` for the same reason. A Main Mini App URL configured in `@BotFather` is fixed by Telegram; it is served `no-store`, and direct links carry a unique `tgWebAppStartParam` query.

The shell also loads a dependency-free classic `boot.js` before the module graph. If `boot()` has not been entered by `DOMContentLoaded`, a module failed to download or threw while evaluating, and the watchdog shows the ordinary gate with **Try again** instead of leaving the opening spinner up forever.

Opening personal storage during boot is bounded and fault-isolated. Telegram `CloudStorage` and `DeviceStorage` reads, IndexedDB opens, the public topic catalogue, and the contributor journal each have their own bound, and the whole personal-storage step has a ten-second deadline; a store that does not answer degrades that launch to the scoped browser copy, or to memory, and the reader opens with the bookmark storage warning visible. A topic catalogue that cannot be loaded leaves the Bookmarks surface reporting global topics as unavailable until online. The public translation catalogue is preferred at boot but the robot's own list from the session response stands in when the public origin fails or stalls.

After deployment, verify:

1. cold reader load uses the public Main API;
2. warm reader load uses IndexedDB and hash policy correctly;
3. explicit references use Query API;
4. search goes directly to the Search API and issues no Robot request;
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
19. an unapproved or revoked user cannot create server contribution events,
    while an approved user's Sync drips sequential bounded event batches whose
    redelivered events replay without duplication, whose final response
    settles the panel's status, and which keep every local mutation on server
    failure;
20. the global topics come from `bookmarks.getbible.net/v1`, a topic's name
    follows the reader's locale with English fallback, a contributed topic is
    marked **G** only after the host has seen it in the public catalogue, and
    the last verified cached catalogue stands in when the API is unreachable;
21. Post failure preserves selection and successful Post clears it.

## Verification gate

A release is not ready unless permanent CI proves Python 3.10–3.14, the production container, lint, strict typing, branch coverage, dependency and secret scans, CodeQL, public API routing, CSP parity, hash verification, bounded caches, bounded selections, durable local history, bookmark-domain and hybrid-storage invariants, private-chat backup/restore ownership and bounds, contributor authorization/idempotency/privacy, Bookmarks API checksum verification and cache-first fallback, English topic-name fallback, real Chromium navigation, graphical select/unselect, cross-source identity, no pre-Post Robot mutation, and authoritative idempotent posting.
