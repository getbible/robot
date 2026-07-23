# GetBible Robot documentation

Use this index as the starting point for development and production operation.

- [Installation](INSTALLATION.md) — clean-host installation, permissions, systemd, and first validation.
- [Configuration](CONFIGURATION.md) — every environment variable, default, range, and trust boundary.
- [Interactive workflows](INTERACTIONS.md) — direct commands, guided Bible selection, search filters, result confirmation, and rollout roadmap.
- [Testing](TESTING.md) — deterministic checks, security checks, failure injection, and live smoke testing.
- [Upgrading and rollback](UPGRADING.md) — safe code and dependency upgrades with rollback steps.
- [Uninstalling](UNINSTALL.md) — partial or complete removal, token handling, and verification.
- [Dependencies](DEPENDENCIES.md) — input files, hashed locks, Dependabot, and the Librarian release policy.
- [Troubleshooting](TROUBLESHOOTING.md) — startup, Telegram, API, readiness, and lockfile diagnosis.
- [Architecture](ARCHITECTURE.md) — request flow, trust boundaries, concurrency, and privacy.
- [Operations](OPERATIONS.md) — monitoring, incident response, capacity, and production routines.
- [Release gate](RELEASE_GATE.md) — the complete deployability checklist.

The two external URL roles are intentionally different throughout the project:

```text
Scripture data:      https://api.getbible.net
Telegram web links: https://getbible.life
```

Security reports must follow [`SECURITY.md`](../SECURITY.md).
