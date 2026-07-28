# Architecture

## Request path

```text
Telegram update → command → direct-reference fast path or signed Mini App launch
Mini App HTTPS → verified initData + user-bound launch token
  → command/API rate limit and owner/session validation
  → strict reference, search, and selection validation
  → bounded ScriptureService capacity wait
  → upstream circuit breaker
  → fixed-size worker pool
  → hardened GetBible Librarian client
  → https://api.getbible.net
  → validated result shape
  → escaped HTML and percent-encoded getbible.life link
  → UTF-16-aware, message-count-bounded Telegram chunks
```

Handlers never call synchronous repository code on the event-loop thread.
`ScriptureService` owns the Librarian client, separate fixed reference/search
executors and semaphores, timeout behavior, circuit state, and aggregate
metrics.

## Trust boundaries

Telegram command arguments are untrusted. The robot reconstructs them only from `context.args`, applies a length limit, splits a bounded number of references, and fully parses every reference before repository access.

Repository JSON is also untrusted. Librarian caps response bytes and validates JSON and checksums. The renderer validates the minimum chapter and verse shape again before formatting.

The guided reference picker additionally reads the v2 translation and books catalogs through a bounded navigation client. Catalog responses are size-limited and structurally validated. A selected full-book navigation response must match the SHA-1 published by its books index before chapter or verse controls are built.

Telegram output is generated only by `modules.renderer`:

- repository text is HTML escaped;
- URL path components are percent encoded;
- the configured web base is validated at startup;
- message length is measured in Telegram UTF-16 code units;
- blocks are split without cutting generated HTML entities;
- one command may produce only a configured number of messages.

## URL separation

```text
Machine-readable data: https://api.getbible.net
Human-facing links:    https://getbible.life
```

`GETBIBLE_API_BASE_URL` is passed to Librarian and the bounded navigation catalog client as their repository source. `GETBIBLE_WEB_BASE_URL` is passed only to the Scripture renderer. These roles must not be conflated.

## Translation resolution

An ordinary reference is parsed locally with the Telegram user's saved
translation first, falling back to the configured application default.
Therefore `John 3:16` never causes a speculative network lookup for a
translation named `3:16`.

Only when the complete input is not a valid reference does the robot consider the final whitespace-delimited token as a translation abbreviation. The candidate must match the complete abbreviation grammar. The preceding reference is validated locally before the candidate is repository-checked. Explicit `kjv` remains supported.

An empty `/bible` does not enter the reference parser and never falls back to a
configured verse. It creates a short-lived, owner-bound launch and opens the
Mini App at translation, testament, book, chapter, and verse/range navigation.
The basket can combine separate verses and ranges across chapters/books and
compacts overlapping or adjacent intervals before final validation. Only the
final server-resolved Scripture is posted into the originating chat and forum
topic.

## Search and confirmation flow

`/search <words>` launches the Mini App directly into results constructed with
Librarian's defaults and the user's saved translation. An empty `/search`
launches the full search and filter screen. For detected CJK scripts, default
whole-word matching is adapted to substring matching because those scripts do
not depend on whitespace word boundaries.

Librarian returns exact totals, grouped verse data, and ordered match metadata.
The server validates that every match points to a verse in the grouped results,
retains only the configured bounded result set, and returns complete wrapping
verse cards. Search filters, paging, selection, and review remain inside the
Mini App; they do not produce chat messages. No ordinary message is sent until
the owner presses **Post selected**.

Selected match metadata is converted back into compressed canonical references.
The final post performs a normal Librarian `select()` call and passes through the
existing renderer. Displayed result text is therefore never the authoritative
post payload.

## Interactive session model

The bot issues an opaque, high-entropy launch token rather than placing
references, queries, chat identifiers, or verse text in the URL. The Mini App
server accepts it only together with fresh Telegram-signed `initData`, verifies
the signature using the bot token, validates the authentication time, and binds
the session to the same Telegram user and originating workflow. Launch tokens
and authenticated sessions have separate short expirations and bounded stores.

Pagination, filter toggles, and local checkmarks do not create Telegram
messages. API mutations for one session are serialized to prevent rapid taps
from racing state transitions. Commands still consume normal per-user and
per-chat inbound budgets. Catalog reads and final posts use the reference pool.
Searches use their own smaller pool and circuit so CPU-heavy corpus work cannot
consume every direct-reference permit.

The public HTML shell is not an authentication boundary. Protected data and
action routes fail closed unless both authorization layers validate. Browser
verse text and selections are treated as untrusted identifiers; final posting
retrieves and renders authoritative Scripture server-side.

## Concurrency and timeout model

Telegram updates may run concurrently. Commands and session-owned text replies
consume user/chat rate-limit tokens; callbacks are serialized by their
owner-scoped session. Direct Scripture/catalog work and searches additionally
require permits from their separate bounded pools.

Each synchronous Librarian call runs in a fixed `ThreadPoolExecutor`. The
asynchronous caller has an overall timeout, but Python cannot safely kill a
running thread. Consequently:

1. a timed-out caller receives a safe temporary-unavailable response;
2. the underlying thread is allowed to exit normally;
3. its semaphore permit is released only when the real concurrent future completes;
4. later requests reach the bounded queue timeout rather than entering the executor's internal unbounded queue.

This distinction prevents repeated upstream stalls from turning cancellation
into unlimited queued work. Reference and search failures also maintain
separate circuit state, so an expensive or invalid search outage does not make
ordinary `/bible` retrieval unavailable.

## Circuit breaker

Repository failures and lookup timeouts increment circuit failures. Caller validation, output limits, and translation-not-found results do not represent an upstream outage.

After the threshold:

- the circuit opens and calls fail quickly;
- readiness returns false;
- after the recovery interval, one half-open probe is allowed;
- success closes and resets the circuit;
- failure reopens it.

## Caches and rate-limit state

Librarian reference, chapter, book, translation, and search caches are bounded.
The negative translation cache has a TTL and size limit. Navigation-catalog
caches, interactive sessions, user/chat/client token buckets, abuse-window and
temporary-block state, rejection-notification cooldown state, and the durable
per-user translation table are all bounded.

Arbitrary reference strings, translation names, user IDs, chat IDs, and client
addresses therefore cannot create permanent process growth without limit.

The small-host profile retains one parsed translation, one search corpus, 256
chapters, 1000 parsed references, 200 Telegram interactions, 200 Mini App
sessions, and no more than two Mini App sessions per Telegram user. Stale
unreferenced corpus objects are pruned periodically; after a one-day race-safety
grace, the oldest complete cache entries are also evicted until the disk budget
is met. The optional JSONL file truncates in place at its byte ceiling;
container deployments write only to stdout.

## Startup and shutdown

Startup order:

1. validate all configuration;
2. construct bounded service objects;
3. initialize Telegram and start liveness with readiness still false;
4. when enabled, start the Mini App on its distinct listener and
   synchronize bot-owned launch controls;
5. synchronize the Telegram command menu and profile metadata;
6. optionally load/index the default search translation;
7. mark readiness true and start the runtime watchdog;
8. start exactly one configured transport:
   - polling, which removes any registered webhook; or
   - an authenticated webhook on loopback behind public HTTPS.

Shutdown order:

1. stop accepting Mini App launches and requests;
2. stop the Mini App and health listeners;
3. mark the Scripture service closed;
4. stop accepting work and wait for real worker threads;
5. close Librarian HTTP sessions;
6. close the per-instance preference database;
7. complete Telegram shutdown.

The Bot API does not offer a WebSocket update transport. Polling and webhooks
are mutually exclusive. A polling `Conflict` stops the instance with a
non-restarting exit status so duplicate processes do not continue fighting.

The supplied `systemd` template gives every named instance its own locked
`gb-<instance>` identity, root-owned application and secret configuration,
writable cache/state/JSONL paths, restart behavior, filesystem protection, no
capabilities, limited address families, task/file limits, event-loop watchdog,
`MemoryHigh`, `MemoryMax`, a swap ceiling, and a restart-storm limit.
Instances do not share a token, process, cache, preference database, health
port, Mini App port/session state, log file, interaction state, or virtual
environment.

The Docker image contains none of the host-native systemd/Caddy/TLS stack. Its
PID-1 supervisor reads one environment in the default single-bot mode or
multiple instance files in explicit multi-bot mode, launches one child process
per bot, checks liveness and RSS, applies restart backoff/circuit breaking, and
forwards termination gracefully. A small in-container setup utility controls
the supervisor without modifying the immutable image. Every child receives
isolated cache and preference paths and unique ports. Cluster deployments
should use one bot token per single-replica workload; current Mini App sessions
are process-local.

## Errors and observability

Expected failures map to fixed user-safe messages. Unexpected failures receive a random correlation ID; raw exception strings are never reflected to Telegram.

Every structured event is tagged with `INSTANCE_NAME` and is written to
journald plus the optional absolute `LOG_FILE`. Metadata audit mode records
event names, filter modes, translations, counts, exception class names, and
correlation IDs without Telegram message text. Content audit mode is an
explicit operator choice that additionally permits normalized search terms and
final references.

The independent identity audit mode is `disabled`, `pseudonymous`, or `raw`.
Pseudonymous mode uses stable keyed identifiers; raw mode records numeric
Telegram user/chat IDs and resolved Mini App client IP addresses for abuse
investigation. Telegram updates never expose user IPs. Mini App forwarding
headers are accepted only from configured trusted proxy networks, preventing a
public client from choosing the logged or rate-limited address. No mode records
tokens, names, usernames, verse bodies, repository payloads, browser
authorization data, or secret paths. Metrics remain aggregate counters.

## Privacy

In metadata audit mode the robot does not persist update text, references,
searches, names, usernames, profiles, or chat history. Identity logging is
configured separately: the default persists only keyed identifiers, while raw
mode deliberately persists Telegram IDs and Mini App client IPs. The
restricted per-instance preference database stores only Telegram user ID,
translation code, and update time. In explicitly enabled content audit mode,
final references and search terms are also persisted to the restricted
per-instance log for its configured retention period. Short-lived launch,
query, and selection state otherwise exists only in bounded process memory and
expires on inactivity or restart. Telegram `initData` is used only for request
authentication and is not written to application logs. Telegram and the
configured GetBible API remain independent external services.
