#!/usr/bin/env bash
# First-boot installation only. Never rerun this script on a live merchant.
set -euo pipefail
test "$(id -u)" = 0
test ! -e /etc/monid/gateway.env
dnf install -y docker nginx
systemctl enable --now docker
install -d -m 0755 /srv/monid/data /etc/monid
chown 10001:10001 /srv/monid/data
chmod 0700 /srv/monid/data
touch /etc/monid/standby
cat > /etc/monid/gateway.env <<'ENV'
PUBLIC_BASE_URL=https://standby.invalid
DATABASE_PATH=/app/data/gateway.sqlite3
CATALOG_SNAPSHOT_PATH=/app/data/catalog.json
X402_PAYMENT_ENABLED=true
X402_XRPL_NETWORK=xrpl:0
X402_XRPL_ISSUER=rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De
X402_ALLOW_UNSUBMITTED_RETRY=false
RUN_WAIT_SECONDS=0
MAX_CONCURRENT_RUNS=8
PYTHONDONTWRITEBYTECODE=1
ENV
chmod 0600 /etc/monid/gateway.env
cp deploy/aws/nginx.conf /etc/nginx/nginx.conf
cp deploy/aws/monid-proxy.conf /etc/nginx/monid-proxy.conf
cp deploy/aws/monid-gateway.service /etc/systemd/system/monid-gateway.service
docker build --tag monid-gateway:release .
docker image inspect --format '{{.Id}}' monid-gateway:release > /etc/monid/image-id
systemctl daemon-reload
systemctl enable --now monid-gateway
nginx -t
systemctl enable --now nginx
