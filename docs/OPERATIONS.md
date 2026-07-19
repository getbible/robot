# Operations

## Configuration

Copy `.env.template` outside the checkout and keep it readable only by the service account. `TELEGRAM_API_TOKEN` is canonical; the deprecated `TELEGRAM_TOKEN` fallback exists only for migration. Startup fails if both are set differently.

Keep these boundaries separate:

```text
GETBIBLE_API_BASE_URL=https://api.getbible.net
GETBIBLE_WEB_BASE_URL=https://getbible.life
```

Only loopback HTTP is accepted; all nonlocal configured URLs must use HTTPS and may not contain credentials, paths, queries, or fragments.

## Deployment

Install from an exact robot commit and use `pip --require-hashes`. Validate and start:

```bash
sudo systemd-analyze verify /etc/systemd/system/getbible-robot.service
sudo systemctl restart getbible-robot
sudo systemctl status getbible-robot --no-pager
curl --fail http://127.0.0.1:8081/readyz
```

The bot calls `setMyCommands` during post-initialization. A failed initialization does not enter polling.

## Monitoring

Alert on:

- repeated service restarts;
- `getbible_robot_ready == 0`;
- `getbible_robot_circuit_open == 1`;
- growth in `lookup_timeouts`, `repository_failures`, or `queue_rejections`;
- sustained inbound rate-limit rejection;
- memory approaching `MemoryMax`.

The health server exposes no messages, tokens, paths, or verse text. Keep it loopback-only.

## Incident response

1. Disable or stop the service if it is producing unsafe or incorrect responses.
2. Revoke the Telegram token through `@BotFather` if disclosure is possible.
3. Preserve journal entries and the deployed commit/lockfile checksums.
4. Roll back to the last release that passed the complete release gate.
5. Confirm `/readyz`, a private test lookup, rate limiting, and link domains.
6. Document the root cause and add a deterministic regression test before redeploying.

## Rollback

```bash
cd /opt/getbible-robot
git fetch --tags origin
git checkout --detach <last-known-good-commit>
rm -rf venv
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install --require-hashes -r requirements.txt
sudo systemctl restart getbible-robot
curl --fail http://127.0.0.1:8081/readyz
```

Do not reuse a lockfile from a different commit.

## Capacity changes

Increase limits only after load testing. Hard complexity limits should never be disabled for administrators. Scaling the process count multiplies per-process cache and thread-pool memory; size `MemoryMax` accordingly.
