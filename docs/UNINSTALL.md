# Uninstalling

Choose whether to disable the service temporarily, remove only the application, or retire the Telegram bot completely. Preserve data only when there is a documented operational reason.

## Temporary disablement

Stop polling while keeping the installation and startup setting:

```bash
sudo systemctl stop getbible-robot.service
sudo systemctl status getbible-robot.service --no-pager
```

Prevent automatic startup as well:

```bash
sudo systemctl disable getbible-robot.service
```

The Telegram token remains valid until revoked through `@BotFather`.

## Before permanent removal

Record what is being removed:

```bash
cd /opt/getbible-robot
git rev-parse HEAD
sha256sum requirements.txt
sudo systemctl status getbible-robot.service --no-pager
```

Do not print `/etc/getbible-robot.env`. Decide explicitly whether the token and configuration must be retained for rollback or destroyed.

## Remove the service

```bash
sudo systemctl disable --now getbible-robot.service
sudo rm -f /etc/systemd/system/getbible-robot.service
sudo systemctl daemon-reload
sudo systemctl reset-failed getbible-robot.service || true
```

Confirm that no process remains:

```bash
systemctl status getbible-robot.service --no-pager || true
pgrep -af '/opt/getbible-robot/.*/python|/opt/getbible-robot/bot.py' || true
```

## Remove application code and environments

```bash
sudo rm -rf /opt/getbible-robot
```

This removes the checkout and virtual environments. It does not remove the external configuration, cache, service account, Telegram bot, or historical journal entries.

## Remove cached Scripture data

The cache contains repository data and metadata, not Telegram messages:

```bash
sudo rm -rf /var/cache/getbible-robot
```

Remove it for a complete uninstall. Preserve it only for a planned rollback on the same trusted host.

## Remove or preserve configuration

For a planned short-term rollback, move the configuration to a restricted backup:

```bash
sudo install -d -o root -g root -m 0700 /root/getbible-robot-backup
sudo mv /etc/getbible-robot.env /root/getbible-robot-backup/
sudo chmod 0600 /root/getbible-robot-backup/getbible-robot.env
```

For permanent removal:

```bash
sudo rm -f /etc/getbible-robot.env /etc/getbible-robot.env.backup
sudo rm -rf /var/lib/getbible-robot
```

Deleting a file does not guarantee forensic erasure on every filesystem or backup system. Follow the host's secret-destruction and backup-retention policy.

## Remove the service account

After the service and cache are gone:

```bash
if id getbible-robot >/dev/null 2>&1; then
  sudo userdel getbible-robot
fi
```

The system-created primary group is normally removed with the account. Check and remove it only if unused:

```bash
getent group getbible-robot || true
sudo groupdel getbible-robot 2>/dev/null || true
```

## Retire or rotate the Telegram token

A local uninstall does not invalidate the token.

- For permanent retirement, revoke/delete the bot or token through `@BotFather`.
- For migration to another host, rotate the token when practical and install the replacement only on the new host.
- If compromise is suspected, revoke first, then investigate; do not wait for uninstall completion.

## Journal retention

`systemd` journal entries may remain after uninstall. They should contain structured operational events and correlation IDs, not message text or tokens. Inspect before changing retention:

```bash
sudo journalctl -u getbible-robot.service --no-pager
```

Use the organization's logging-retention policy. Do not indiscriminately delete shared system journals.

## Verify complete removal

```bash
test ! -e /opt/getbible-robot
test ! -e /etc/getbible-robot.env
test ! -e /etc/systemd/system/getbible-robot.service
test ! -e /var/cache/getbible-robot
! id getbible-robot >/dev/null 2>&1
! ss -ltn | grep -q ':8081 '
```

Also send a Telegram command after token revocation or bot retirement and confirm that no response occurs.

## Reinstallation

A later reinstall should follow [Installation](INSTALLATION.md) from a clean reviewed commit and a newly created or deliberately restored token. Do not reuse an old virtual environment or combine an old lock with new source code.
