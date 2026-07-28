# GetBible Robot documentation

Use this index as the starting point for development and production operation.

- [Installation](INSTALLATION.md) — interactive multi-instance setup, isolated accounts, exact deployment, logging, and first validation.
- [Docker deployment](DOCKER.md) — published GHCR images, release tags, attestations, non-root Compose operation, cluster probes, and resource budgets.
- [Configuration](CONFIGURATION.md) — every environment variable, default, range, and trust boundary.
- [Interactive workflows](INTERACTIONS.md) — direct commands, guided Bible selection, search filters, result confirmation, and rollout roadmap.
- [Telegram Mini App](MINI_APP.md) — authentication boundary, same-instance listener, HTTPS reverse proxy, BotFather settings, and verification.
- [Telegram delivery](WEBHOOKS.md) — polling, HTTPS webhooks, reverse-proxy setup, mode switching, and editable bot content.
- [Testing](TESTING.md) — deterministic checks, security checks, failure injection, and live smoke testing.
- [Upgrading and rollback](UPGRADING.md) — prebuilt atomic application swaps with automatic and manual rollback.
- [Uninstalling](UNINSTALL.md) — isolated instance removal, token handling, retained logs, and verification.
- [Dependencies](DEPENDENCIES.md) — input files, hashed locks, Dependabot, and the Librarian release policy.
- [Troubleshooting](TROUBLESHOOTING.md) — startup, Telegram, API, readiness, and lockfile diagnosis.
- [Architecture](ARCHITECTURE.md) — request flow, trust boundaries, concurrency, and privacy.
- [Operations](OPERATIONS.md) — instance selection, start/stop/status, runtime, JSON logs, diagnostics, monitoring, and incident response.
- [Release gate](RELEASE_GATE.md) — the complete deployability checklist.

The two external URL roles are intentionally different throughout the project:

```text
Scripture data:      https://api.getbible.net
Telegram web links: https://getbible.life
```

Security reports must follow [`SECURITY.md`](../SECURITY.md).
