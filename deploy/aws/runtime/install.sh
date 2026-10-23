#!/usr/bin/env bash
# Install deployment tooling without copying application secrets through CI.
set -euo pipefail
umask 077
test "$(id -u)" = 0
test -f /etc/monid/standby
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
region=${1:?region}
prefix=${2:?parameter prefix}
registry=${3:?ECR registry}
repository=${4:?ECR repository}
operation=${5:?image or refresh-config}
[[ "$region" =~ ^[a-z0-9-]+$ ]]
[[ "$prefix" =~ ^/[a-zA-Z0-9/_-]+$ ]]
[[ "$registry" =~ ^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com$ ]]
[[ "$repository" =~ ^[a-z0-9-]+$ ]]
install -d -m 0700 /opt/monid/bin /run/monid
# nginx must be able to stat the maintenance marker; files remain root-only.
install -d -m 0755 /etc/monid
runuser -u nginx -- test -f /etc/monid/standby
install -m 0700 "$script_dir/load_environment.py" "$script_dir/deploy.sh" /opt/monid/bin/
printf 'AWS_REGION=%s\nPARAMETER_PREFIX=%s\nECR_REGISTRY=%s\nECR_REPOSITORY=%s\n' "$region" "$prefix" "$registry" "$repository" > /etc/monid/runtime.conf
if [[ ! -f /etc/monid/release.env ]]; then
  printf 'MONID_IMAGE=monid-gateway:release\n' > /etc/monid/release.env
fi
install -m 0644 "$script_dir/monid-environment.service" "$script_dir/monid-gateway.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable monid-environment monid-gateway >/dev/null
systemctl start monid-environment
/opt/monid/bin/deploy.sh "$operation"
