# Architecture

## Request path

```text
Telegram message, reply, or callback update
  → registered command or interactive handler
  → per-user and per-chat token buckets
  → strict reference, search, session, and callback validation
  → bounded ScriptureService capacity wait
  → upstream circuit breaker
  → fixed-size worker pool
  → hardened GetBible Librarian client
  → https://api.getbible.net
  → validated result shape
  → escaped HTML and percent-encoded getbible.life link
  → UTF-16-aware, message-count-bounded Telegram chunks
```

Handlers never call synchronous repository code on the event-loop thread. `ScriptureService` owns the Librarian client, fixed executor, semaphore, timeout behavior, circuit state, and aggregate metrics.

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

An ordinary reference is parsed locally with the configured default translation first. Therefore `John 3:16` never causes a speculative network lookup for a translation named `3:16`.

Only when the complete input is not a valid reference does the robot consider the final whitespace-delimited token as a translation abbreviation. The candidate must match the complete abbreviation grammar. The preceding reference is validated locally before the candidate is repository-checked. Explicit `kjv` remains supported.

An empty `/bible` does not enter the reference parser and never falls back to a configured verse. It creates an owner-scoped interactive session and opens the guided translation, testament, book, chapter, first-verse, and last-verse flow.

## Search and confirmation flow

`/search <words>` constructs Librarian's default `SearchBible` criteria. An empty `/search` creates the same criteria in a filter dashboard and allows the user to change every exposed Librarian filter before replying with query text.

Librarian returns exact totals, grouped verse data, and ordered match metadata. The robot validates that every match points to a verse in the grouped results, retains only the configured bounded result set, and renders short previews as inline buttons. No verse is posted from search until the owner presses **Post selected**.

Selected match metadata is converted back into compressed canonical references. The final post performs a normal Librarian `select()` call and passes through the existing renderer. Search preview text is therefore never the authoritative post payload.

## Interactive session model

Callbacks contain an opaque random token rather than references, queries, or verse text. The in-memory session store validates token, chat, and user together, refreshes an inactivity TTL, and enforces an LRU size limit. Replies are accepted only when they target the exact bot prompt recorded for that session.

Local pagination, filter toggles, and checkmarks do not consume worker capacity. Every accepted callback still consumes the normal per-user and per-chat inbound rate budget. Catalog reads, searches, and final posts additionally use the service semaphore, executor, timeout, and circuit breaker.

## Concurrency and timeout model

Telegram updates may run concurrently, but all commands and session-owned callbacks or replies first consume user and chat rate-limit tokens. Scripture repository work additionally requires a `ScriptureService` semaphore permit.

The synchronous Librarian call runs in a fixed `ThreadPoolExecutor`. The asynchronous caller has an overall timeout, but Python cannot safely kill a running thread. Consequently:

1. a timed-out caller receives a safe temporary-unavailable response;
2. the underlying thread is allowed to exit normally;
3. its semaphore permit is released only when the real concurrent future completes;
4. later requests reach the bounded queue timeout rather than entering the executor's internal unbounded queue.

This distinction prevents repeated upstream stalls from turning cancellation into unlimited queued work.

## Circuit breaker

Repository failures and lookup timeouts increment circuit failures. Caller validation, output limits, and translation-not-found results do not represent an upstream outage.

After the threshold:

- the circuit opens and calls fail quickly;
- readiness returns false;
- after the recovery interval, one half-open probe is allowed;
- success closes and resets the circuit;
- failure reopens it.

## Caches and rate-limit state

Librarian reference, chapter, book, translation, and search caches are bounded. The negative translation cache has a TTL and size limit. Navigation-catalog caches, interactive sessions, user/chat token buckets, and rejection-notification cooldown state all have bounded least-recently-used retention.

Arbitrary reference strings, translation names, user IDs, and chat IDs therefore cannot create permanent process growth without limit.

## Startup and shutdown

Startup order:

1. validate all configuration;
2. construct bounded service objects;
3. initialize Telegram and register bot commands;
4. start the loopback health listener;
5. begin polling only message and callback-query updates required by the robot.

Shutdown order:

1. stop the health listener;
2. mark the Scripture service closed;
3. stop accepting work and wait for real worker threads;
4. close Librarian HTTP sessions;
5. complete Telegram shutdown.

The supplied `systemd` unit adds restart behavior, a dedicated identity, filesystem protection, no capabilities, limited address families, task/file limits, and `MemoryMax`.

## Errors and observability

Expected failures map to fixed user-safe messages. Unexpected failures receive a random correlation ID; raw exception strings are never reflected to Telegram.

Structured logs contain event names, exception class names, and correlation IDs, not message text. Metrics are aggregate counters. The loopback health server exposes no token, reference, verse text, repository payload, or filesystem path.

## Privacy

The robot does not persist update text, references, searches, favorites, profiles, or chat history. Short-lived query and selection state exists only in bounded process memory and expires on inactivity or restart. Telegram and the configured GetBible API are independent external services with their own data practices.

Any future persistence feature requires a separate design for consent, access control, retention, export, deletion, encryption, and incident response before implementation.
