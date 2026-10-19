# PR #1 and PR #3 overlap

Compared PR #1 head `74e4e52d5ec01f3c58015a9f26ed83508a2fd85a` against PR #3's
preparation tree based on main `6221d9241b0433fa881afb296a2c3cf59319fb0f`.
The comparison does not merge, close or deploy either PR.

## Coverage

| PR #1 content | PR #3 replacement | Difference |
| --- | --- | --- |
| Root `.dockerignore` / `.gcloudignore` | Same root paths | Corrected directory-negation behavior; extra credential/runtime denies; third-party notices included; cloud build path updated. Native Docker and GCloud canary checks cover the replacement. |
| Root `cloudbuild.yaml` | `deploy/examples/cloudbuild.yaml` | Same pinned builder/build/publish steps; relocated as an inactive example. No root build entry or trigger is activated. |
| Seven `deploy/gke/*.yaml` files | Seven `deploy/examples/gke/*.yaml` files | Same resource topology, storage, ingress, probes and security settings. Project/image and operator receiving address become placeholders; single-deployment comment matches durable SQLite; explicit unsubmitted-retry default is false. |
| README deployment link | README deployment-examples section | Links to the new paths without implying deployed/production readiness. |
| Detailed `deploy/README.md` operations guidance | `deploy/README.md` plus `deploy/runbook.md` | Useful clean-source build, IAM/provenance, secure Secret handling, probes, storage, drain/rollback, network and future rollout procedures retained and updated. |

Before this revision, PR #3 preserved manifests/build steps but only summarized
PR #1's longer runbook. The added `deploy/runbook.md` closes that useful-content
gap. All useful deployment content now has a replacement. This is a functional
supersession, not byte-for-byte/path-for-path equivalence: root `cloudbuild.yaml`
and `deploy/gke/` are intentionally not reintroduced as active deployment entries.

## Descriptions in #1 that are obsolete or misleading

- The runbook says invoices/replay guards are process-local and restarting loses
  outstanding invoices (lines 98 and 138–143). Main now persists invoices,
  submission claims, results and tombstones after PR #2. Restart/ambiguous-execution
  risks remain, but discarding history or simply minting a new quote is unsafe.
- A Deployment comment likewise describes process-local guards. The replacement
  describes the current durable SQLite/single-deployment constraint.
- Its original 33-test / 23-upload-file / no-native-Docker-build record is historical,
  not the current validation result. PR #3 has a separate dated validation record.
- The real project/registry identifier and operator testnet receiver are not
  portable defaults. Root build config and `deploy/gke` commands no longer match
  the example paths used in PR #3.
- Earlier hosting/person-specific approval language is not an open-source approval
  requirement. T54 has confirmed code publication; operating a GKE/mainnet service
  still requires actual technical configuration and validation.
- The statement that testnet E2E necessarily consumes Monid USD is too broad:
  `scripts.e2e_testnet` defaults to a fake upstream. Real-upstream mode and
  call-tool against a paid service can spend credits; neither was run here.

## Minimal handling

Use PR #3's updated examples and runbook. Do not also merge PR #1 unchanged: it
would restore stale descriptions and original identifiers, duplicate old/new
deployment paths and overlap the safer ignore files. No unique functional change
now needs a separate cherry-pick from #1. The coordinator can mark #1 superseded
after accepting the replacement; this work does not close or delete it.

An offline `git merge-tree --write-tree --messages` comparison of the two branch
tips returned conflicts in `.dockerignore`, `.gcloudignore`, `README.md` and
`deploy/README.md`, without modifying the working tree. GitHub's per-PR mergeability
against main does not mean the two PRs can be combined without conflicts or semantic
regressions. No real merge was performed.

## Historical information that remains visible on publication

Making the existing repository public with its existing history leaves the old
cloud project/registry identifiers (`deploy/README.md`, `deploy/gke/deployment.yaml`),
operator testnet receiving address (`deploy/gke/configmap.yaml`), personal absolute
paths/internal checkout names (`.claude/launch.json`) and internal runbook/person/
workflow references in reachable old commits/branch/PR refs. Public issuer and
receiving addresses, source tags and facilitator URLs are identifiers, not secret
keys. Values are omitted from this report. No secret key was identified in the
scanned reachable history. Sanitizing current files or superseding PR #1 does not
remove these old objects; hidden/deleted refs and external copies remain outside
the demonstrated scan coverage.
