#!/usr/bin/env bash
# One writer, serialized releases, and no automatic production cutover.
set -Eeuo pipefail
umask 077
exec 9>/run/lock/monid-deploy.lock
flock -n 9 || { echo 'Another deployment is running.' >&2; exit 1; }
test -f /etc/monid/standby || {
  echo 'Automatic rollout currently requires standby mode; review production cutover first.' >&2
  exit 1
}
runuser -u nginx -- test -f /etc/monid/standby || {
  echo 'nginx cannot access the standby marker.' >&2
  exit 1
}
source /etc/monid/runtime.conf
export AWS_REGION PARAMETER_PREFIX
operation=${1:?An image digest or refresh-config is required}
install -d -m 0700 /run/monid
rollback_dir=$(mktemp -d /run/monid/rollback.XXXXXX)
cp /etc/monid/release.env "$rollback_dir/release.env"
cp /run/monid/gateway.env "$rollback_dir/gateway.env"
switched=false
success=false
finish() {
  result=$?
  trap - EXIT
  if [[ "$switched" == true && "$success" != true ]]; then
    cp "$rollback_dir/release.env" /etc/monid/release.env
    cp "$rollback_dir/gateway.env" /run/monid/gateway.env
    if systemctl restart monid-gateway; then
      echo 'Previous image and runtime configuration restored; inspect health before retrying.' >&2
    else
      echo 'Rollback restart failed; service remains closed and needs attention.' >&2
    fi
  fi
  rm -rf "$rollback_dir"
  exit "$result"
}
trap finish EXIT
if [[ "$operation" != refresh-config ]]; then
  expected="$ECR_REGISTRY/$ECR_REPOSITORY@sha256:"
  [[ "$operation" == "$expected"* ]] || { echo 'Image repository is not allowed.' >&2; exit 1; }
  digest=${operation#"$expected"}
  [[ "$digest" =~ ^[a-f0-9]{64}$ ]] || { echo 'An immutable image digest is required.' >&2; exit 1; }
  export DOCKER_CONFIG="$rollback_dir/docker"
  mkdir -m 0700 "$DOCKER_CONFIG"
  aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY" >/dev/null 2>&1
  docker pull "$operation"
  docker logout "$ECR_REGISTRY" >/dev/null 2>&1 || true
fi
python3 /opt/monid/bin/load_environment.py
switched=true
if [[ "$operation" != refresh-config ]]; then
  printf 'MONID_IMAGE=%s\n' "$operation" > /etc/monid/release.env.new
  mv /etc/monid/release.env.new /etc/monid/release.env
fi
systemctl restart monid-gateway
for attempt in $(seq 1 60); do
  if python3 - <<'PY'
import json, urllib.request, urllib.error
try:
    with urllib.request.urlopen('http://127.0.0.1/health', timeout=3) as response:
        health = json.load(response)
    assert health['ok'] and health['tools'] > 0
    try:
        response = urllib.request.urlopen('http://127.0.0.1/jobs/__deployment_probe__', timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    assert response.status == 503
except Exception:
    raise SystemExit(1)
PY
  then
    success=true
    echo 'Deployment verified: health and catalog ready, standby gate preserved.'
    exit 0
  fi
  sleep 1
done
echo 'Deployment health check failed.' >&2
exit 1
