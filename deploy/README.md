# Candidate GKE testnet deployment

This is a **candidate** GKE Autopilot design pending Frank's confirmation of the
hosting and secure configuration approach. It follows XRPL AI Hub's
`deploy/gke/production.yaml` conventions (Artifact Registry, NEG, BackendConfig,
ManagedCertificate and FrontendConfig), in the separate `monid-gateway` namespace.
Nothing here has been built, uploaded, applied or verified against a live cluster.
The build recipe publishes an image only; it contains no infrastructure
provisioning or deployment step.

## Files and required operator inputs

- `../cloudbuild.yaml`: build and publish only; requires `_IMAGE`.
- `../.gcloudignore`: upload allowlist; `../.dockerignore`: Docker context allowlist.
  Neither includes `.env`, `data/`, wallet files, Git metadata or a local venv.
- `gke/kustomization.yaml`: Namespace, ConfigMap, PVC, Deployment, Service,
  BackendConfig, Ingress, ManagedCertificate and FrontendConfig. The Secret
  example is deliberately excluded.
- `gke/secret.example.yaml`: empty placeholder only. Never enter a key in it.

Before any deployment, obtain approval of the hosting design and an authorized
cluster/context; an operator-owned public testnet domain and reserved global
static IP; a reviewed image digest; an out-of-band Monid key with an agreed USD
budget; and the testnet receiver's ownership, RLUSD trust line and XRP reserve.
The handoff testnet receiver is public configuration, not a production address.
The project/repository path is the handoff reference, not confirmation of IAM or
cluster access. No production domain, receiver, key or branding decision is made.

In an isolated release copy, replace **all** of the following, then review it:

- `monid-testnet.example.invalid` in ConfigMap `PUBLIC_BASE_URL`, Ingress host
  and ManagedCertificate domain, using the same approved host and `https://` URL.
- `REPLACE_WITH_RESERVED_TESTNET_IP_NAME` with the approved global static-IP name.
- `REPLACE_WITH_REVIEWED_IMAGE` (the whole image reference) with
  `us-west1-docker.pkg.dev/tworld-473700/x402-xrpl/monid-gateway@sha256:<verified-digest>`.

The reserved `.invalid` domain cannot get a certificate. The absent Secret and
placeholder image deliberately prevent an untouched deployment from serving tools.
Do not apply the directory recursively: use the Kustomization so the Secret
example is never applied. `PUBLIC_BASE_URL` must match the reachable HTTPS origin;
otherwise directory URLs and invoice resource URLs will be wrong.

## Build from reviewed source (future authorized operator step)

Use a clean commit, not the existing developer directory containing secrets. The
following commands are documentation, not an instruction to run them as part of
this configuration PR. Cloud Build, disks, IPs and load balancers incur charges.
The Docker builder digest follows the Hub recipe; base-image versions are in the
existing Dockerfile and still require normal dependency/image review.

```bash
git status --short                         # must be clean
git rev-parse HEAD
# Check for export-ignore/export-subst attributes before archiving; do not use
# an archive whose source has been omitted or substituted by such attributes.
git ls-files | git check-attr --stdin export-ignore export-subst
release_sha=$(git rev-parse HEAD)
release_dir=$(mktemp -d)
git archive --format=tar HEAD | tar -xf - -C "$release_dir"
# Inspect names only. Never upload .env, data/, wallets or local venv files.
(cd "$release_dir" && gcloud meta list-files-for-upload)
gcloud builds submit "$release_dir" \
  --project=tworld-473700 \
  --config="$release_dir/cloudbuild.yaml" \
  --substitutions="_IMAGE=us-west1-docker.pkg.dev/tworld-473700/x402-xrpl/monid-gateway:git-$release_sha"
```

Keep the build ID, source commit and published digest as release evidence. Verify
provenance and the registry digest before promotion; a commit tag is not an
immutable deployment reference. Build credentials need only source/log access and
Artifact Registry write, not runtime secrets or deployment privileges. The GKE
node identity needs Artifact Registry read. Confirm those IAM grants separately.

## Secret provisioning (future authorized operator step)

Have the operator obtain the key through the team's approved secret manager or
secure channel. Store only the raw key bytes (no trailing newline) in a
permission-0600 file outside any Git checkout and build context. Never paste it in
Slack, PRs, YAML, shell arguments, terminal output or CI substitutions. Do not use
`--from-literal`, `cat`, `kubectl get secret -o yaml`, or shell tracing.

After separately approving the cluster/context and creating the Namespace, an
operator can create the Secret using that protected file path:

```bash
kubectl config current-context
kubectl -n monid-gateway create secret generic monid-gateway-secrets \
  --from-file=MONID_API_KEY=/absolute/protected/path/monid-api-key
# Names/metadata only; do not export .data or .stringData.
kubectl -n monid-gateway get secret monid-gateway-secrets
```

This create command intentionally fails if the Secret already exists. Use the
team's controlled rotation procedure instead of overwriting it ad hoc. Restrict
Secret RBAC, remove the temporary file through the approved process, and never
copy developer `.env` or `data/e2e-wallets.json` into the workload. The gateway
requires no signing or receiving-wallet private key. Environment-based key
rotation requires a planned Pod restart and loses outstanding invoices.

## Runtime invariants and probes

- Keep exactly **one Pod AND one Uvicorn worker**. No HPA, RollingUpdate,
  `--reload`, or manual concurrent replacement Pod. `Recreate` prevents deployment
  upgrades from intentionally overlapping Pods; it is not distributed fencing
  against force-deletion/node partitions. Never force-delete an uncertain Pod.
- The 5 GiB `standard-rwo` PVC holds `/app/data/gateway.sqlite3` and
  `/app/data/catalog.json`. The non-root UID/GID 10001 and fsGroup 10001 make the
  mounted directory writable; the rest of the filesystem is read-only except
  `/tmp`. Confirm storage class existence, effective allocated capacity, Autopilot
  resource adjustments and volume permissions in the target cluster.
- Startup and liveness use HTTP `GET /health`. It returns **200 and `ok: true`
  even with missing configuration**. Do not parse `paymentsReady`, balance or
  catalog availability in liveness, and do not restart on upstream outages.
- Readiness parses the same local `/health` response, requiring
  `paymentsReady == true` and `tools > 0`. This is configuration/catalog
  readiness only. It does **not** call Monid or the facilitator. Failed readiness
  withdraws readiness without restarting or erasing invoices. An initially empty
  PVC waits for the background public catalog sync; after a failed sync the next
  attempt is normally one hour later. Investigate egress/logs; do not restart in a
  loop. Cached catalogs may be stale; monitor `catalogSyncedAt` separately.
- Actual code differs from the handoff: `/health.problems` contains configuration
  problems, **not low balance**. Paid non-probe calls check a cached balance and
  return 503 below the floor; GET/empty-POST price probes skip the balance check.
  Unreadable balance is currently allowed by `balance_ok()`; invalid keys and
  upstream failures may therefore only surface when starting a real run.
- GKE BackendConfig checks HTTP `/health` on port 8000 and cannot parse JSON.
  Its health status alone does not prove business readiness. Pod readiness and
  application's paid-route checks are separate; never open sales based only on
  a green load-balancer health check.
- Backend timeout is 600 seconds rather than GKE's default 30 seconds; async
  waits default to 240 seconds and individual upstream requests to 120 seconds.
  Concurrent-request queuing and slow dependencies can still exceed 600 seconds;
  there is no enforced overall request deadline. Do not blindly retry ambiguous
  paid requests. preStop allows 30 seconds for endpoint withdrawal, Uvicorn has
  600 seconds to drain, and the Pod grace period is 660 seconds. These reduce
  interruption risk, not a guarantee of settlement or invoice survival.

PVC persistence does **not** persist invoices or in-process replay guards. Every
restart, rollout, eviction or node loss invalidates outstanding quotes; clients
must obtain a fresh quote. In-flight Monid work or settlement can be interrupted.
Keep downtime planned, let active requests finish, then reconcile ledger,
facilitator receipts, RLUSD transactions and Monid runs before retrying uncertain
calls. SQLite records and job tokens remain on disk: protect backups accordingly.
Never delete the PVC/Namespace as a rollback. RWO storage is not a backup or a
cross-process execution lock. Arrange consistent SQLite backups (stop the writer
or use SQLite's backup API), test restoration, and monitor disk usage before sales.

## Network and controlled rollout

Confirm GKE HTTP load balancing, ManagedCertificate/FrontendConfig/BackendConfig
CRDs, VPC-native NEG support, DNS and outbound HTTPS to `api.monid.ai` and
`xrpl-facilitator-testnet.t54.ai`. Catalog logo checks also require `monid.ai`.
Check inherited network policies/NAT; no namespace-wide policy in this PR assumes
access to the Hub's private services. HTTPS terminates at Google's load balancer;
the backend hop is HTTP inside the cluster/VPC. Do not reuse Hub Secrets or IAM.

The external Ingress has only the approved-host route. FrontendConfig redirects
HTTP to HTTPS with 308 (preserves method); HTTP stays enabled for redirect and
certificate provisioning. Agents must start on HTTPS and never send payment
headers/bodies over HTTP. DNS must resolve to the reserved IP and the managed
certificate must become Active before publishing the URL. Check HTTPS and redirect
from outside the cluster, plus the domain agreement in discovery entries.

Cloud Armor is an optional, commented BackendConfig attachment. Frank/operations
must approve and provision a policy before enabling it; no policy or rate limit
exists merely because this template is present. Tune for legitimate agent bursts
and polling, monitor 429s, and retest. Current concurrency limiting is not a
per-customer spending cap or general ingress rate limiter.

For a later authorized rollout: render and review locally with
`kubectl kustomize deploy/gke`; check there are no unresolved placeholders; perform
server-side dry-run against the **approved** context to validate GKE CRDs and
admission (that step is not offline); then apply only the reviewed rendered file.
Verify one Pod/one worker, PVC Bound, Secret metadata, mount writability,
`paymentsReady`, nonempty catalog, certificate Active and HTTPS. Use only free
health/catalog and GET/empty-POST discovery probes initially. The later signed
testnet end-to-end call requires explicit authorization because even testnet XRPL
payments use **real Monid USD credit**. Do not run the E2E/call-tool scripts during
configuration validation.

Rollback is a reviewed previous image digest with the same compatible ConfigMap
and retained PVC, using Recreate and the same downtime/reconciliation procedure.
Do not copy an older database over a newer ledger or force-detach a disk from a
possibly running writer. This release does not modify Hub PR #80, run migrations,
register a merchant or enable mainnet.

## Offline checks for this PR

The existing 33 unit tests passed using `.venv/bin/python -B` and pytest with its
cache disabled, under an audit hook that rejected network connections/DNS and
reads of developer `.env`, `.env.bak` and `data/e2e-wallets.json`.
`kubectl kustomize deploy/gke` rendered nine resources with no Secret. Local YAML
validation checked duplicate keys, cross-resource references, Settings types and
testnet identities, persistent paths, process/replica invariants, HTTPS host
agreement and readiness behavior (ready, missing configuration, empty catalog,
missing fields, failed local transport). `gcloud meta list-files-for-upload`
listed only 23 source/build files. Synthetic canaries exercised both ignore
allowlists using gcloud's local matcher; no Docker-native build was run.
Changed files were scanned for key/token patterns and `git diff --check` passed.

These are offline checks, not GKE schema/admission, image startup, mounted-disk
permission, certificate, network, balance or settlement verification. Those
checks remain part of the separately authorized deployment stage.

## Before mainnet

The gateway payment logic has **not received independent code review**. This PR
is deployment preparation, not a payment/security review or mainnet clearance.
Require gateway review and Hub PR #80 review/merge, owner-executed M102 migration
under the correct database identity, plus Frank's approved hosting/security plan.
Never run Prisma migrate deploy on the handoff local database: it has no ledger.

Obtain a team-controlled mainnet receiver with RLUSD trust line and XRP reserve,
a production Monid key/credit and balance floor, an HTTPS domain, pricing/brand
approval, and a decision on personal-information tools. The testnet exclusions
preserve existing shared-state restrictions; they do not settle that policy.
Daily automated reconciliation and single-call/daily spending caps are still
unimplemented. Agree ownership and operational limits before opening sales.
Prepare a separately reviewed mainnet configuration changing network, issuer,
facilitator, receiver and public URL together; do not edit testnet in place.
After authorized deployment, verify a small real call and Hub indexer volume
attribution with SourceTag 804681468. Neither live deployment nor indexer
attribution has been verified by this configuration work.

References: [GKE Ingress configuration](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/ingress-configuration),
[GKE TLS/managed certificates](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/secure-traffic-management),
[Persistent Disk CSI](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver).
