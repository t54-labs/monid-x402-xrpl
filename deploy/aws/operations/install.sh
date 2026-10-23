#!/usr/bin/env bash
set -euo pipefail
umask 077
test "$(id -u)" = 0
region=${1:?region}
bucket=${2:?backup bucket}
instance=${3:?instance}
[[ "$region" =~ ^[a-z0-9-]+$ ]]
[[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]]
[[ "$instance" =~ ^i-[a-f0-9]+$ ]]
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
install -d -m 0700 /opt/monid/bin /srv/monid/backups
printf 'AWS_REGION=%s\nBACKUP_BUCKET=%s\nINSTANCE_ID=%s\n' "$region" "$bucket" "$instance" > /etc/monid/operations.conf
install -m 0700 backup.sh /opt/monid/bin/backup.sh
install -m 0700 metrics.py /opt/monid/bin/metrics.py
install -m 0700 ledger_snapshot.py /opt/monid/bin/ledger_snapshot.py
install -m 0644 monid-backup.service monid-backup.timer monid-metrics.service monid-metrics.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start monid-backup.service
systemctl start monid-metrics.service
systemctl enable --now monid-backup.timer monid-metrics.timer
