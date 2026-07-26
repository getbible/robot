# Uninstalling

Use the manager to remove one isolated instance without affecting the others:

```bash
sudo getbible-robot uninstall production
```

The command displays the exact target, requires typing its instance name, asks whether to delete its retained JSON log, and requires final confirmation.

It then:

- disables and stops `getbible-robot@production.service`;
- removes that instance's application and virtual environments;
- removes its root-only environment and metadata;
- removes its cache and state/home;
- removes its locked Linux service account;
- optionally removes its JSON log;
- reloads systemd and clears the failed service state.

The shared manager, unit template, rotation configuration, and every other instance remain available.

## Temporary disablement

Keep the installation but stop polling:

```bash
sudo getbible-robot stop production
```

Prevent automatic startup while preserving all files:

```bash
sudo systemctl disable getbible-robot@production.service
```

Re-enable later:

```bash
sudo systemctl enable getbible-robot@production.service
sudo getbible-robot start production
```

## Token handling

Removing local files does not invalidate a Telegram token.

- Revoke the token through `@BotFather` for permanent retirement.
- Rotate it when moving a bot between hosts.
- If compromise is suspected, revoke first and investigate second.
- Never assign the same token to two active polling instances.

## Retained logs

If the uninstall questionnaire preserves the JSON log, it remains at:

```text
/var/log/getbible-robot/<instance>.jsonl
```

Journald may also retain historical events. Apply the host's approved retention policy. Content-audit logs require the same privacy handling after uninstall as they did while the service was active.

## Verify removal

```bash
sudo getbible-robot list
systemctl status getbible-robot@production.service --no-pager || true
test ! -e /opt/getbible-robot/production
test ! -e /etc/getbible-robot/production.env
test ! -e /etc/getbible-robot/instances/production.conf
test ! -e /var/cache/getbible-robot/production
test ! -e /var/lib/getbible-robot/production
! id gb-production >/dev/null 2>&1
```

Also confirm the health port is no longer listening and that the bot does not answer after token revocation.

## Remove the shared manager

Only after `sudo getbible-robot list` reports no instances:

```bash
sudo rm -f /usr/local/sbin/getbible-robot
sudo rm -f /etc/systemd/system/getbible-robot@.service
sudo rm -f /etc/logrotate.d/getbible-robot
sudo systemctl daemon-reload
```

Remove empty shared directories only after resolving whether setup logs or retained instance logs must be preserved.

## Reinstallation

Use a clean reviewed checkout and `sudo ./setup.sh install`. Do not reuse an old virtual environment or combine old code, a new lock, and an old unit.
