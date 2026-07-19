# Architecture

## Request path

```text
Telegram update
  → command handler
  → per-user and per-chat rate limiter
  → strict local reference/translation parser
  → bounded ScriptureService queue
  → circuit breaker
  → bounded worker pool
  → hardened GetBible Librarian client
  → https://api.getbible.net
  → safe HTML renderer and verse-boundary chunker
  → Telegram response linking to https://getbible.life
```

The handler layer does not call synchronous HTTP code. `ScriptureService` owns the Librarian client, thread pool, concurrency semaphore, timeouts, circuit state, and aggregate metrics.

## Trust boundaries

Telegram update text is untrusted. It is reconstructed only from `context.args`, length checked, split into a bounded number of references, and fully parsed before repository access.

Repository JSON is also treated as untrusted. Librarian caps response bytes and validates JSON/checksums; the robot validates the minimal chapter/verse shape again before formatting.

Telegram HTML is generated only by `modules.renderer`. Every text field is escaped, every URL segment is percent encoded, and chunks are built below Telegram's limit without cutting tags or entities.

## Translation resolution

An ordinary reference is first parsed locally using the configured default translation. Therefore `John 3:16` never causes a speculative network lookup for `3:16`.

Only when the complete input is not a valid reference does the robot consider the final whitespace-delimited token as a translation abbreviation. The candidate must match the complete abbreviation grammar, be repository-validated through bounded I/O, and leave a valid preceding reference. Explicit `kjv` remains supported.

## Availability

- The inbound limiter state is bounded LRU-like state.
- Repository calls use a fixed-size executor and semaphore.
- Queue waiting and overall operation time are bounded.
- Librarian applies connect/read timeouts, retries only idempotent GETs, and caps response bodies.
- Repeated upstream failures open the circuit; one half-open probe tests recovery.
- systemd applies process restart, task, file-descriptor, and memory limits.
- `/healthz`, `/readyz`, and `/metrics` bind to loopback.

## Privacy

The bot does not persist update text. Logs contain event types, exception classes, and random correlation IDs. Metrics are aggregate counters. New persistence features require a separate retention, export, deletion, and access-control design.
