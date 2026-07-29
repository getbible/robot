# Upgrading and rollback

Upgrade one instance from a clean checkout of the exact reviewed target commit. The manager builds and validates the replacement before stopping the running service.

## Preconditions

- Target commit has green CI/security and CodeQL gates.
- Host Python remains in the supported 3.10–3.12 range.
- Configuration and release notes were reviewed.
- A dedicated test instance passed the live Telegram smoke matrix.
- Current `status` and `runtime` output were recorded.
- The currently deployed application is healthy and available for rollback.

## Upgrade

For the normal server checkout, update `master` and deploy the complete
application in place:

```bash
cd ~/robot
git switch master
git pull --ff-only
git status --short
sudo ./setup.sh update production --source "$PWD"
```

`git status --short` must print nothing. The `update` command is an alias for
the transactional `upgrade` operation and is also available as **Update /
upgrade deployment** in the interactive maintenance menu. It replaces the
entire installed application tree, including `miniapp/`, its interface
catalogs, styles, and images. It then restarts the instance and verifies both
Robot readiness and the configured Mini App HTTPS route.

The repository owns `setup.sh` as an executable file. Run it directly; a fresh
checkout must not require `chmod`. Although the manager itself runs through
`sudo`, it inspects the operator-owned source checkout with Git's optional
locks disabled. The deployment must not refresh or change ownership of
`.git/index`.

The Mini App revalidates every packaged asset when Telegram opens it, so the
new deployment cannot reuse JavaScript, CSS, locale catalogs, or branding from
the previous commit. Close any Mini App view that was already open during the
upgrade and launch `/bible` or `/search` again.

For a separately reviewed checkout pinned to an exact commit:

```bash
git clone https://github.com/getbible/robot.git robot-upgrade
cd robot-upgrade
git checkout --detach <reviewed-target-sha>
git status --short
sudo ./setup.sh upgrade production --source .
```

The installed command is equivalent when given the source checkout:

```bash
sudo getbible-robot upgrade production --source /path/to/robot-upgrade
```

The manager:

1. confirms the current and target exact commits;
2. adds missing backwards-compatible configuration defaults, including disabled
   Mini App settings, without printing or replacing existing secrets;
3. clones the target into `app.next`;
4. builds a fresh virtual environment;
5. installs the target lock with `--require-hashes`;
6. runs `pip check`;
7. validates the existing instance environment with the target code;
8. installs and verifies the target manager/unit;
9. stops only the selected instance;
10. atomically replaces `app` and retains `app.previous`;
11. starts the service and waits for readiness;
12. verifies the configured Mini App listener and public HTTPS route;
13. automatically restores the prior application if readiness fails.

Source, virtual environment, and lock are always moved as one application tree. The service never combines code from one commit with a lock from another.

After success:

```bash
sudo getbible-robot doctor production
sudo getbible-robot logs production 200
```

Complete the private and group smoke tests in [Testing](TESTING.md).

## Manual rollback

The manager retains one immediately previous application:

```bash
sudo getbible-robot rollback production
```

It displays both commit SHAs, requires explicit confirmation, swaps the complete application trees, starts the selected service, and waits for readiness. If the rollback target itself fails readiness, the original application is restored.

When the Mini App is enabled, rollback verifies both its local listener and
public HTTPS path before accepting the target.

Rollback does not silently replace the environment file. If a future release
requires a configuration change, follow that release's documented upgrade
instructions before retrying.

## Configuration migrations

1. Compare `.env.template` with `/etc/getbible-robot/<instance>.env` without printing secret values.
2. Use `sudo getbible-robot config <instance>`.
3. Add new keys deliberately.
4. Let the manager validate the complete file.
5. Restart and verify readiness.
6. Retain the prior secret file only according to the approved secret-retention policy.

`INSTANCE_NAME`, `LOG_FILE`, `HEALTH_PORT`, `MINI_APP_ENABLED`,
`MINI_APP_PUBLIC_URL`, `MINI_APP_LISTEN`, and `MINI_APP_PORT` remain
manager-owned. Mini App and webhook ports must be unique and must not match.

## Dependency updates

Maintainers edit `requirements.in` or `requirements-dev.in`, regenerate both locks with `scripts/refresh-locks.sh`, review the complete generated diff, and run the full Python 3.10–3.12 matrix. Production installs only the exact reviewed `requirements.txt` with hashes.

The Librarian policy remains:

```text
getbible>=1.2.1,<2
```

The generated lock selects the exact tested artifact. Production does not resolve a newer package during service start.

## Docker upgrade and rollback

Docker deployments upgrade by changing the explicit `ROBOT_IMAGE` value in the
private Compose environment and letting the setup manager pull, validate, and
recreate the workload:

```bash
cd ~/robot
cp -- .env ".env.before-$(date +%Y%m%dT%H%M%S)"
${EDITOR:-vi} .env
./setup.sh docker-validate
./setup.sh docker-update
./setup.sh docker-doctor
```

For example:

```dotenv
ROBOT_IMAGE=ghcr.io/getbible/robot:2.1.0
```

Use an exact semantic version or `sha-<full-commit>` tag for production. Do not
use `edge`; it is the CI-approved `master` channel for pre-production testing.
The persistent named volume is retained when the container is recreated.

Rollback is the same controlled operation: restore the prior `ROBOT_IMAGE`
value, validate, and run `docker-update` again. Do not start the old and new
containers concurrently with the same Telegram token.

## Deployment record

Record:

- instance;
- date and operator;
- previous and target commit;
- target `requirements.txt` SHA-256;
- Python version;
- unit checksum;
- configuration schema changes without secret values;
- CI and CodeQL URLs;
- `doctor` result;
- live smoke-test result;
- rollback result when rehearsed.
