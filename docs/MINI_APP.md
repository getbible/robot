# Telegram Mini App

The GetBible Telegram Mini App is a browser application served by the Robot instance. Its public Scripture data plane is independent from the Robot process: catalogs, chapter text, explicit references, cache validation, temporary verse selection, and device-local history belong in the browser. Compact personal bookmarks and last-read coordinates additionally use Telegram Mini App storage when available; the global bookmark overlay remains browser-local. Robot remains the authenticated Telegram control plane, the Librarian search adapter, and the bounded relay for an explicit private-chat bookmark backup or restore.

## Active doctrine

Only full-text search and search pagination use Librarian. Robot also owns the
authenticated control paths for sessions, preference compatibility, final
Post, and explicit bookmark chat backup/restore.

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
| Personal bookmark aggregate v2, topics, and active topic | Scoped `localStorage` + Telegram `DeviceStorage` / `CloudStorage` |
| Global bookmark visibility and per-link exclusions | Browser `localStorage` |
| Compact last-read coordinate | Scoped `localStorage` + Telegram `DeviceStorage` / `CloudStorage`, with Robot preference compatibility |
| Bookmark JSON download/import | Browser |
| Private-chat bookmark backup/restore | Browser confirmation + Robot/Telegram transport |
| Full-text search and pagination | Robot → Librarian |
| Telegram authentication and launch binding | Robot |
| Reader preference compatibility | Robot |
| Final Telegram delivery | Robot |

A normal reader action must never call Robot for translations, books, chapters, chapter text, selecting, unselecting, reordering, clearing, or copying.

## Runtime flow

```text
Telegram WebView
  ├─ signed initData / preferences / search / post / chat backup → Robot
  ├─ catalogs / chapters / hashes                    → api.getbible.net/v2
  ├─ explicit or grouped references                  → query.getbible.net/v2
  ├─ temporary ordered selection                     → BrowserSelectionStore
  ├─ unique coordinate history                       → scoped local ReadingHistoryStore
  ├─ personal bookmarks / topics / last-read         → local + Telegram storage adapter
  └─ global bookmark visibility / exclusions         → browser localStorage
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
bar keeps only the centered getBible icon. Choosing an entry reopens that exact
translation and coordinate. An entry can be removed individually, and Clear
all removes the complete device-local record.

`ReadingHistoryStore` is versioned, unique and newest-first, and bounded to
1,000 coordinate-only entries in authenticated user-scoped `localStorage`, with
an in-memory fallback.
Chapter visits share translation/book/chapter identity, while verse selections
use their full coordinate; an exact coordinate also coalesces across event
kinds. Revisiting one moves its stable entry to the top. Restoration
compacts older duplicate records. The store never keeps verse bodies, Telegram
data, Robot credentials, or user identity, and none of its mutations call
Robot or Telegram storage. History survives later WebView sessions in that
browser but does not synchronize between devices.

## Navigation and bookmarks

The permanent footer remains the five actions Home, Search, Bible, History, and
Selected. Bookmarks is reached from the Home summary rather than adding a sixth
footer action. Home contains the three primary actions **Search Scripture**,
**Read the Bible**, and **See history**, followed by Selected, History, and
Bookmarks summaries. The translation control remains in Search and Bible; Home,
History, Selected, and Bookmarks show only the centered getBible icon.

Selecting a Bible verse reveals a small keyboard-accessible ellipsis at its
bottom-right; selection alone never opens the menu. Activating the ellipsis
opens the anchored menu, where the complete canonical book/chapter/verse may be
assigned to any unassigned topic or unassigned from one topic without affecting
its others. An existing assignment is a no-op, and the same coordinate in
another translation updates its one personal record instead of duplicating it.
The menu links directly to each assigned topic. Topic detail offers navigation
back to all topics and, when opened from the reader, back to the source verse.

The Bookmarks surface lists personal and global verse links together in one
topic list; global rows carry a **G** marker. One global link may be hidden, or
all global links may be cleared for one topic. Loading that topic or the full
catalog resets its exclusions without duplication. The built-in catalog
contains 2,155 links across 61 topics. Its visibility and exclusions remain in
this browser and never consume, synchronize with, or back up personal records.
A scoped browser-only mapping keeps legacy numeric topics attached to their
canonical global topic after a rename or reload.

Personal records are bounded to 800 canonical verses, and each may belong to
multiple topics without consuming another verse slot. Topic search, add,
rename, recolor, removal, and clear-all remain available. Topic removal warns
that its personal assignments will also be removed. **Restore default tags**
adds only missing defaults and preserves recolors, renames, custom topics, and
personal bookmarks. The global catalog/provider boundary supports a future
authorized publishing source, but authorized-user publishing is not
implemented.

`BookmarkStore` writes personal aggregate version 2 immediately to scoped
`localStorage`. `TelegramBookmarkStorage` compares timestamped candidates from
local storage and Telegram `DeviceStorage` and `CloudStorage`, selects the
newest valid copy, then mirrors it to available stores. Cloud bookmark values
use topic indexes into the synchronized topic manifest and reconstruct full
identifiers on read. Stable item values plus a metadata fingerprint preserve
metadata-last atomicity while avoiding full rewrites, and transient partial
writes receive bounded automatic retries. The active topic and compact
last-read participate; a
timestamped cleared marker prevents an older remote position from reappearing
after a failed sync. Selection, history, the global catalog and exclusions, and
chapters do not. Unsupported Telegram storage leaves the local copy usable and
shows a degraded-sync notice.

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

The public HTML shell is not an authentication boundary. Robot action routes require fresh Telegram-signed `initData`, an owner-bound launch, and an active opaque session. The bot token remains server-side.

The browser is untrusted for final output. It may control display state, but it cannot determine the authoritative text delivered to Telegram. Final output remains bounded, escaped, idempotent, and tied to the originating user, chat, and topic.

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

The public browser cache is identity-free. Robot retains only the compatible
reader preference containing translation and reader coordinates. Device-local
history and hybrid bookmark state live in separate scoped browser/Telegram
stores and never enter the public cache.

In managed Caddy mode the public listener is HTTPS port 443; the Mini App's
own port remains an internal loopback listener. In external mode, Caddy is not
used. The external proxy keeps its public HTTPS port and forwards to the bot
host address and `MINI_APP_PORT` printed by setup.

The generated Caddy route is deny-by-default. It forwards only packaged
shell/assets and documented API paths; every other request receives an empty
`404` without contacting Tornado. Tornado independently recognizes routes and
methods before session lookup, Telegram validation, or rate-limit accounting.
Never replace the generated matchers with a catch-all reverse proxy.

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
10. reading history reopens the exact translation and verse;
11. History remains available from every footer route and revisits move to the
    top without duplication;
12. individual and complete history clearing work locally;
13. Home exposes Search, Bible, and History actions plus Selected, History, and
    Bookmarks summaries, with the route-specific top-bar controls;
14. the compact verse ellipsis alone opens the bookmark menu, and a personal
    verse can belong to multiple colored topics within the 800-record bound;
15. bookmark and last-read state reconcile on a second supported Telegram
    client without synchronizing history or downloaded Scripture;
16. bounded JSON download/import and private-chat backup/restore work, and a
    confirmed restore persists before its one-launch reference is acknowledged;
17. the unified topic list marks global links with **G**, supports per-link hide
    and per-topic/all-catalog reset, and excludes global links from personal
    sync and backup;
18. Post failure preserves selection and successful Post clears it.

## Verification gate

A release is not ready unless permanent CI proves Python 3.10–3.14, the production container, lint, strict typing, branch coverage, dependency and secret scans, CodeQL, public API routing, CSP parity, hash verification, bounded caches, bounded selections, durable local history, bookmark-domain and hybrid-storage invariants, private-chat backup/restore ownership and bounds, real Chromium navigation, graphical select/unselect, cross-source identity, no pre-Post Robot mutation, and authoritative idempotent posting.
