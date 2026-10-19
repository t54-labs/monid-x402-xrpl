# Deployment examples

`examples/gke/` is a sanitized adaptation of the candidate configuration from
commit `74e4e52d5ec01f3c58015a9f26ed83508a2fd85a`. It is provided for review, not
for direct deployment. No existing Railway or GCP environment is described here.
The [operator runbook](runbook.md) preserves the useful procedures from PR #1,
updated for durable invoices. The [overlap analysis](../docs/pr-1-overlap.md)
explains the relocated paths and why PR #1 should not also be merged unchanged.

Before considering GKE, an operator must supply and review:

- An operator-owned `PROJECT_ID`, `REGION` and Artifact Registry `REPOSITORY`, plus
  an immutable reviewed image digest in `examples/gke/deployment.yaml`.
- An HTTPS domain in `configmap.yaml` and both locations in `networking.yaml`,
  a reserved IP resource, DNS, certificate and ingress policy.
- An operator-owned **testnet** receiving address with an RLUSD trust line.
  Public receiving/issuer addresses are not secret keys. No wallet seed belongs
  in the gateway workload or these manifests.
- A `monid-gateway-secrets` Secret provisioned out of band. Its example is empty
  and deliberately excluded from `kustomization.yaml`. Do not populate tracked files.
- A persistent volume, one deployment, and reviewed backup, upgrade and recovery
  procedures. Independent SQLite copies or restoring old execution history can
  permit replay; see [payment recovery](../docs/payment-recovery.md).

`/health` returns HTTP 200 even with incomplete payment configuration. The example
uses an exec readiness probe for `paymentsReady` and catalog readiness. The ingress
health check alone does not establish that paid traffic is ready. Keep
`X402_ALLOW_UNSUBMITTED_RETRY=false` until deployment-specific facilitator semantics
are independently established. Review drain times, filesystem permissions, GKE
CRDs, quotas and timeout settings before any use.

`examples/cloudbuild.yaml` only illustrates building/publishing an image. It is not
an active root build configuration or a deployment trigger. If separately approved,
use an operator-owned project and an explicit image substitution, for example
`REGION-docker.pkg.dev/PROJECT_ID/REPOSITORY/monid-gateway:RELEASE_ID`.
Cloud uploads use the root `.gcloudignore`; Docker uses `.dockerignore` separately.
Supply real runtime secrets through the platform's secret mechanism, never build
arguments, checked-in YAML or upload archives.

These examples have not been applied to GCP. They do not establish current Railway
deployment state, mainnet readiness, live billing behavior or chain finality.
Older branch and PR refs still contain the original operational identifiers; the
[release checklist](../docs/open-source-release.md) records the separate decision
needed before making the existing repository public.
