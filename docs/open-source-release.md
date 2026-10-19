# Open-source release checklist

T54 has approved open-sourcing the gateway, as confirmed on 2026-10-19. The assessed
base is `6221d9241b0433fa881afb296a2c3cf59319fb0f`; repository visibility was private
at inspection. Code publication and a production deployment are separate actions.

## Release packaging

- [x] Organization authorization to open-source is confirmed.
- [x] Record source references and third-party license evidence in
  [provenance](provenance.md) and the root third-party notices.
- [ ] Apply the final release license consistently. The complete
  [MIT text and exact packaging changes](license-plan.md) are prepared for selection;
  the source proposal is distinct from an installed package license declaration.
- [ ] Preserve supplier/third-party rights. Monid's proxy documentation supports
  user billing, but catalog/results, logos and co-branded artwork retain their own
  applicable terms; code publication does not relicense them.
- [ ] Record how the non-secret historical metadata below will be handled. Updating
  current files does not remove it from existing refs, commits or external copies.

## Historical disclosure choices — no history has been changed

The fetched `codex/testnet-gke-deployment` branch and PR #1 refs retain operational
cloud project/registry identifiers in `deploy/README.md` and
`deploy/gke/deployment.yaml` at commit
`74e4e52d5ec01f3c58015a9f26ed83508a2fd85a`. That branch's ConfigMap also contains an
operator testnet receiving address (public identifier, not a private key).
Earlier `.claude/launch.json` versions contain a personal absolute path/internal
checkout names; the assessed main includes them at
`6221d9241b0433fa881afb296a2c3cf59319fb0f`. Values are intentionally omitted here.
No real credential was identified in the assessed tracked history.

The release can use one of these historical-content approaches:

1. Retain the existing history, including these non-secret historical disclosures.
2. Publish a separately reviewed sanitized snapshot in a **new** repository, keeping
   the original private. Audit that new repository and its complete contents first.
3. Authorize a coordinated history rewrite and reference cleanup. This needs a
   separate plan for branches, tags, GitHub-managed PR refs, caches and existing
   clones; deleting a branch or editing main alone cannot retract all copies.
   Coordinate GitHub support where necessary and rerun the complete scan.

This PR does not rewrite commits, delete refs or claim that PR refs can be removed
by a normal branch push. If a credential is later found, revoke/rotate it before
history cleanup; rewriting alone does not make an exposed credential safe.

## Engineering release gates

- [ ] Review the [dated scan evidence](release/secret-scan-2026-10-19.json) and rerun
  a trusted scanner at the final release SHA against all advertised branch/tag/PR
  refs. Use full redaction and do not print raw findings or upload secrets as artifacts.
- [ ] Inspect GitHub PR/issue text, attachments, Actions logs/artifacts, releases,
  packages and any public documentation separately. These were not downloaded in
  the Git-history scan. Hidden/deleted refs, reflogs, unreachable objects, GitHub
  caches and external clones remain outside the demonstrated coverage.
- [ ] Review `.dockerignore`, `.gcloudignore`, package manifests and built archives.
  Run `python -m scripts.check_release_artifacts PATH_TO_SDIST PATH_TO_WHEEL`.
  Verify Docker's actual transmitted context using synthetic canaries in an isolated
  clone. Never copy a production `.env`, database, wallet or log into a build test.
- [ ] Preserve third-party license texts/NOTICE files in any dependency distribution
  and image. Review LGPL/MPL and binary bundled-library obligations for the exact
  shipped target; metadata inventory is not a complete legal or binary audit.
- [ ] Run the offline unit suite, lint, sdist/wheel build and packaged smoke checks.
  Do not run `scripts.e2e_testnet`, paid Monid calls or blockchain transactions as
  part of release preparation.
- [ ] Independently verify actual Railway image/config/storage and security posture
  before making production-readiness claims. Those environments are untouched by
  this PR. GKE examples require their own review and are not a deployment instruction.

GitHub secret scanning/push protection was disabled when inspected. Reading alerts
returned HTTP 404 (feature disabled), not a clean scanner result. This preparation
does not enable or change repository security settings. Gitleaks was instead run
locally from a checksum-verified official release; its scan remains a static
heuristic check, not a guarantee that no secret exists or a vulnerability audit.
The root `.gitleaks.toml` extends the scanner's built-in rules with XRPL seed
candidates; it does not treat public `r` addresses as keys. A reproducible command
after fetching the full advertised ref set into an isolated mirror is:

```bash
gitleaks git /path/to/isolated-mirror.git --config /path/to/checkout/.gitleaks.toml \
  --log-opts="--all --full-history" --ignore-gitleaks-allow --redact=100 \
  --report-format=json --report-path=/private/local/location/redacted-report.json
```

Use a trusted, checksum-verified scanner version and an empty/no ignore file. Never
scan a developer's ignored `.env`/wallet file as a way to include it in a report.
