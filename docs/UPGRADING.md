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
12. automatically restores the prior application if readiness fails.

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

Rollback does not silently replace the environment file. If a target release required a configuration migration, use `getbible-robot config` to restore compatible values from the restricted operational backup before retrying.

The Mini App migration is rollback-compatible: existing instances receive
`MINI_APP_ENABLED=false` plus bounded defaults. The upgrade does not publish a
new URL or require inbound access. After the upgraded bot is healthy, configure
the HTTPS route and enable it with:

```bash
sudo getbible-robot miniapp production
```

## Configuration migrations

1. Compare `.env.template` with `/etc/getbible-robot/<instance>.env` without printing secret values.
2. Use `sudo getbible-robot config <instance>`.
3. Add new keys deliberately.
4. Let the manager validate the complete file.
5. Restart and verify readiness.
6. Retain the prior secret file only according to the approved secret-retention policy.

`INSTANCE_NAME`, `LOG_FILE`, `HEALTH_PORT`, and `MINI_APP_LISTEN` remain
manager-owned. Mini App and webhook ports must be unique and must not match.

## Dependency updates

Maintainers edit `requirements.in` or `requirements-dev.in`, regenerate both locks with `scripts/refresh-locks.sh`, review the complete generated diff, and run the full Python 3.10–3.12 matrix. Production installs only the exact reviewed `requirements.txt` with hashes.

The Librarian policy remains:

```text
getbible>=1.2,<2
```

The generated lock selects the exact tested artifact. Production does not resolve a newer package during service start.

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
