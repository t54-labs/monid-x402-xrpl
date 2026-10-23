#!/usr/bin/env bash
set -euo pipefail
umask 077
source /etc/monid/operations.conf
exec 9>/run/lock/monid-backup.lock
flock -n 9 || exit 1
install -d -m 0700 /srv/monid/backups
work=$(mktemp -d /srv/monid/backups/work.XXXXXX)
trap 'rm -rf "$work"' EXIT
python3 /opt/monid/bin/ledger_snapshot.py backup --source /srv/monid/data/gateway.sqlite3 --destination "$work/snapshot"
release=$(date -u +%Y%m%dT%H%M%SZ)-$(cat /proc/sys/kernel/random/uuid)
prefix="s3://$BACKUP_BUCKET/ledger/$INSTANCE_ID/$release"
# Publish the manifest last; an incomplete upload must not be treated as a backup.
for name in gateway.sqlite3 manifest.json; do
  aws s3 cp "$work/snapshot/$name" "$prefix/$name" --region "$AWS_REGION" --sse AES256 --only-show-errors >/dev/null 2>&1
done
date +%s > /srv/monid/backups/last-success.new
mv /srv/monid/backups/last-success.new /srv/monid/backups/last-success
printf '%s\n' "$prefix" > /srv/monid/backups/latest-uri
printf 'Private ledger backup completed.\n'
