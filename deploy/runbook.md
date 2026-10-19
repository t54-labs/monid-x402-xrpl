# GKE example operator runbook

This consolidates the operational guidance from PR #1 with the current durable
invoice implementation. It documents future operator work; no command below was
run against a cloud account or cluster during release preparation. Existing
Railway hosting is independent of these GKE examples.

## Inputs and a clean build

Use an operator-owned GCP project, Artifact Registry repository and region, a
reviewed immutable image digest, an HTTPS domain, a reserved global IP, a funded
testnet receiver with an RLUSD trust line, and an appropriate runtime secret.
Replace the domain in all three ConfigMap/Ingress/certificate locations and the
IP resource/image/receiver placeholders. `.invalid` domains cannot receive public
certificates; placeholders intentionally make the unedited example unusable.

Build from a reviewed clean checkout. Inspect `export-ignore`/`export-subst` before
using an archive, so source is not accidentally omitted or rewritten. The following
is a command template with placeholders, not a tested cloud build:

```bash
git status --short
git ls-files | git check-attr --stdin export-ignore export-subst
release_sha=$(git rev-parse HEAD)
release_dir=$(mktemp -d)
git archive --format=tar HEAD | tar -xf - -C "$release_dir"
(cd "$release_dir" && gcloud meta list-files-for-upload)
gcloud builds submit "$release_dir" \
  --project=PROJECT_ID \
  --config="$release_dir/deploy/examples/cloudbuild.yaml" \
  --substitutions="_IMAGE=REGION-docker.pkg.dev/PROJECT_ID/REPOSITORY/monid-gateway:git-$release_sha"
```

Do not upload a developer directory containing ignored `.env`, wallets, SQLite or
logs. Keep source SHA, build ID and published digest; verify registry provenance
and deploy by immutable digest, not just a tag. Build credentials need source/log
access and registry write, not runtime secrets or deployment privileges. The node
identity needs registry read. Check IAM separately; the example proves no access.

## Secret handling

Obtain the key through the operator's secret-management process. If using a
temporary file, keep raw key bytes in a permission-0600 file outside any checkout
and upload context. Never paste key values into PRs, YAML, terminal output, build
substitutions or shell arguments; avoid shell tracing and `--from-literal`.

With the intended namespace/context already established, an operator could create
the Secret from that protected file:

```bash
kubectl config current-context
kubectl -n monid-gateway create secret generic monid-gateway-secrets \
  --from-file=MONID_API_KEY=/absolute/protected/path/monid-api-key
kubectl -n monid-gateway get secret monid-gateway-secrets
```

Inspect names/metadata only, not `.data`/`.stringData`. The create command fails if
the Secret exists; use controlled rotation rather than overwriting it ad hoc.
Restrict Secret RBAC and remove temporary files through the operator's process.
The gateway needs no receiving/signing-wallet private key. Environment-based
rotation requires a planned restart, but current invoices/results/tombstones
survive on the retained SQLite database. Drain and reconcile ambiguous work.

## Runtime, health and storage

- Use one deployment, one Pod and one Uvicorn worker, with `Recreate`; no HPA or
  independent SQLite copies. Recreate does not fence a partitioned/force-deleted
  process. Do not force-delete an uncertain writer or force-detach its disk.
- The example 5 GiB `standard-rwo` PVC holds database and catalog files under
  `/app/data`. UID/GID/fsGroup 10001 and a writable `/tmp` accompany a read-only
  root filesystem. Verify actual storage class, capacity, resource adjustments
  and mount permissions in the target cluster; local rendering cannot prove them.
- Startup/liveness use `/health`, which returns HTTP 200 even with missing
  payment configuration. Low balance, stale catalogs or upstream outages must not
  trigger destructive restart loops. Readiness checks `paymentsReady` and `tools`.
  It is configuration/catalog readiness, not a paid upstream or settlement check.
- The load balancer also checks HTTP `/health` and cannot parse its JSON. A green
  load-balancer check alone does not establish business readiness. An initially
  empty catalog depends on background sync; the default retry interval is one hour.
  Investigate egress/errors and monitor `catalogSyncedAt` separately.
- Paid non-probe calls use a cached balance floor; price probes skip it.
  `balance_ok()` currently allows an unreadable balance, so invalid credentials
  or upstream failures can surface only at execution. Concurrency limiting is
  not a per-customer spending cap or a general ingress rate limiter.
- The example backend timeout is 600 seconds; default wait/poll and request
  timing can still exceed it under load. There is no enforced overall request
  deadline. preStop waits 30 seconds, Uvicorn drains for 600 seconds, and Pod grace
  is 660 seconds; those settings reduce interruption risk, not payment ambiguity.

Current invoices, public results, job tokens, submission claims and tombstones are
durable in SQLite. Do not replace them with old in-memory behavior or discard
history on restart. A crash during upstream submission/settlement can still require
manual investigation; neither a timeout nor a missing short-window transaction
proves that work/payment did not happen. Keep
`X402_ALLOW_UNSUBMITTED_RETRY=false` unless the deployed facilitator's explicit
non-submission contract is independently established. See
[payment recovery](../docs/payment-recovery.md) for exact states/reconciliation.

RWO storage is not a backup. Use consistent SQLite backup procedures, protect
results and bearer job tokens, monitor disk capacity and test restores. Rolling
back to the old payment implementation or restoring old execution history can
remove replay protections. Retain the PVC and reconcile work since any snapshot;
never delete PVC/Namespace as a rollback mechanism.

## Network and rollout checks

Check the target cluster's GKE HTTP load balancing, certificate/frontend/backend
CRDs, VPC-native NEG support, DNS, policies/NAT and outbound HTTPS to the configured
Monid API, facilitator and logo host. HTTPS terminates at Google's load balancer;
the example backend hop uses HTTP inside the VPC. No access to private Hub services
or shared Secrets/IAM is assumed. Agents must start on HTTPS; the example HTTP 308
redirect preserves methods but is not permission to send payment data over HTTP.
Wait for DNS/IP agreement and an Active managed certificate before exposing URLs.

Cloud Armor is only a commented optional attachment. A policy/rate limit must
actually be provisioned and tuned for agent bursts/polling before being relied on.

Render `kubectl kustomize deploy/examples/gke` locally, resolve all placeholders,
then have the operator perform target-context server-side validation and review
before applying only the rendered Kustomization. Do not apply the directory
recursively: the empty Secret example is intentionally excluded. Verify one
Pod/worker, PVC Bound, secret metadata, filesystem access, configuration/catalog
readiness, certificate state, HTTPS and discovery-origin agreement.

Begin with free health/catalog and GET/empty-POST probes. The E2E script uses a fake
upstream by default; `E2E_REAL_UPSTREAM_KEY` can enable paid upstream work, and chain
transactions remain real testnet transactions. No E2E, call-tool or blockchain
test is part of configuration validation here. Live billing, chain finality,
Hub registration/indexing and current Railway configuration remain unverified.

For mainnet, create a separately reviewed configuration changing network, issuer,
facilitator, receiver and URL together. Assess data/privacy policy, operational
spending caps, automated reconciliation and recovery responsibilities. Code
publication and offline tests do not establish a production operational service.
