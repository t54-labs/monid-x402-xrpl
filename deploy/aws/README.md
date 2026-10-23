# GitHub builds and AWS runtime configuration

The AWS deployment uses one EC2 instance behind CloudFront. GitHub Actions builds
images; Parameter Store owns runtime environment variables. Production payment
credentials are not needed in GitHub. Keep the source service active until the
[cutover checks](../aws-migration.md#conditions-before-switching-production-traffic) pass.

## Build and deployment flow

[`.github/workflows/aws.yml`](../../.github/workflows/aws.yml) runs on pushes to
`main`, pull requests targeting `main`, and manual dispatch:

- Pull requests and main pushes run the application/configuration tests and build
  the Docker image without AWS credentials or deployment permissions. These
  validation images are not published; merging code does not update EC2.
- Only a manual dispatch on main can deploy. After tests pass, the `deploy`
  operation authenticates to AWS with OIDC, builds an amd64 image, pushes an
  immutable commit/run tag to ECR, and deploys its digest through SSM.
- Deployment jobs are serialized and never cancel an in-progress rollout. A run
  whose commit has been superseded on main skips the remote rollout.
- The host pulls before restarting, keeps one application worker, preserves the
  root-disk data mount, and verifies health/catalog and the standby gate. A failed
  health check restores the preceding image and rendered environment. Database
  contents are never rolled back automatically; schema changes need a separate
  compatibility and backup review.
- Rollout currently **requires standby mode**. Neither a code push nor a config
  refresh removes `/etc/monid/standby`. Production deployment/draining must be
  reviewed with the remaining payment-deadline and data-cutover work.

Before enabling deployment, protect main with at least one approving review,
dismiss stale approvals after new commits, and apply the protection to
administrators. Do not bypass these controls to install the workflow.

To release after merging the workflow, open **Actions → Build and manually deploy
AWS → Run workflow**, select **main**, and choose **deploy**. The selected main
revision is tested and built again for release. A dispatch on another branch
cannot run the deployment job. The CLI equivalent is:

```bash
gh workflow run aws.yml --ref main -f operation=deploy
```

## Where to manage settings

| Setting | Management location | Consumed by |
| --- | --- | --- |
| AWS account, region, role, repository, instance and parameter prefix | GitHub **Settings → Environments → aws → Environment variables** | Workflow only |
| Application settings such as public URL, polling and concurrency | AWS **Systems Manager → Parameter Store**, under `/monid-x402-xrpl/runtime/` | EC2 at boot or configuration refresh |
| `MONID_API_KEY` | The same Parameter Store path, type **SecureString** | EC2 only |
| Current image digest | `/etc/monid/release.env` | systemd/Docker |
| Rendered application environment | `/run/monid/gateway.env`, root-owned mode 0600 | Docker at startup |

Configure the `aws` GitHub environment to allow **only main**. Its variables are
`AWS_ACCOUNT_ID`, `AWS_REGION`, `AWS_ROLE_ARN`, `ECR_REGISTRY`, `ECR_REPOSITORY`,
`EC2_INSTANCE_ID`, `PARAMETER_PREFIX`, and `SERVICE_URL`. These are deployment
identifiers, not application secrets. No long-lived AWS key is stored in GitHub.

The base service role explicitly denies parameter value/history reads outside
`ParameterPrefix/*` and path reads anywhere except the exact `ParameterPrefix`.
This deny overrides the wildcard `GetParameter`/`GetParameters` allows in
`AmazonSSMManagedInstanceCore`, while retaining the agent's command and messaging
permissions. Ancestor paths (including `/`) are denied because a recursive read
of an ancestor can expose every descendant. The renderer's key allowlist is an
application check, not an IAM boundary. Parameter history reads within the prefix
are not granted by these templates.

The instance role pulls only this ECR repository. The GitHub role can push images
and run SSM commands on the designated instance; the workflow never retrieves
application parameter values. Repository
maintainers who can approve deployment code are consequently trusted operators.
Bind the OIDC subject to the intended repository and `aws` environment. Obtain
the repository's actual subject format, including immutable IDs when enabled,
before configuring the trust policy.

## Edit and apply environment variables

1. Open Parameter Store in the destination account and region. Filter by
   `/monid-x402-xrpl/runtime/` and edit the required parameter.
2. Use `SecureString` for `MONID_API_KEY`; never change it to `String`. The initial
   `__NOT_CONFIGURED__` placeholders render as empty values, keeping payment
   readiness closed. Add real production credentials only as part of the cutover.
3. After the workflow is merged, open **Actions → Build and manually deploy AWS → Run
   workflow**, select **main**, and choose **refresh-config**. This reloads the
   runtime configuration and restarts the existing image without a Docker build.
   Equivalently:

   ```bash
   gh workflow run aws.yml --ref main -f operation=refresh-config
   ```

4. Inspect the Actions/SSM outcome and `/health`. During standby, paid/job routes
   must continue returning 503, even if credentials eventually make the
   application's `paymentsReady` field true.

Manual image deployments also reload parameters. An ordinary container restart
uses the current rendered environment; a host boot fetches fresh parameters.
Editing Parameter Store alone does not change a running process.

The renderer allowlists application keys and rejects multiline values, plaintext
API-key parameters, disabled payment enforcement, and changed persistent data
paths. It writes atomically and does not print values on failure. The legacy
`/etc/monid/gateway.env` is retained only as the original-bootstrap guard; it is
**not** the active environment source after installing the managed runtime.

## Infrastructure and initial setup

- See [operations preparation](operations/README.md) for private backups,
  restore/schema rehearsals, monitoring, and production cutover checks.
- `artifacts.json` and `service.json` define the EC2/CloudFront base.
- `automation.json` adds the ECR repository, GitHub OIDC deployment role, and the
  existing instance role's runtime permissions. It does not create an OIDC
  provider or overwrite application parameters.
- `automation.guard` checks immutable/encrypted image storage and OIDC conditions.
- `runtime/` contains the host renderer, systemd units and rollout scripts.
- `deploy_via_ssm.py` sends only these allowlisted deployment files to the instance;
  application secrets are resolved locally on EC2, never transmitted through CI.

Create and validate a change set for `automation.json`, supplying the existing
instance ID/role, GitHub provider ARN, and verified environment subject. Configure
the GitHub environment branch policy **before** allowing OIDC deployment. Initialize
required runtime parameters without overwriting existing values. `DATABASE_PATH`
must be `/app/data/gateway.sqlite3`, `CATALOG_SNAPSHOT_PATH` must be
`/app/data/catalog.json`, and `X402_PAYMENT_ENABLED` must be `true`.

For an existing installation, apply the base `service.json` role restriction
before enabling runtime access. Use the same `ParameterPrefix` in both stacks,
the GitHub environment, and the host configuration. Inspect the change set to
ensure the permission update does not replace the instance or alter the origin.
Changing the prefix requires a coordinated configuration update; mismatches fail
closed. The restriction also blocks SSM documents that depend on parameters
outside this path; review such documents before enabling them.

Run the effective-role check with an administrative identity in the destination
account after applying the templates:

```bash
python scripts/check_parameter_access.py \
  --role-arn "$INSTANCE_ROLE_ARN" --region "$AWS_REGION" \
  --prefix "$PARAMETER_PREFIX"
```

This uses IAM `SimulatePrincipalPolicy` against all attached and inline policies,
including the SSM managed policy. It requires expected allows inside the runtime
path and explicit denies for other services, ancestors, lookalike prefixes,
regions, and accounts. It never retrieves parameter values. Run an SSM command
and the host environment loader afterward to verify agent/runtime compatibility;
policy simulation alone does not validate credentials or service behavior.

The initial managed-runtime installation can use the same SSM transport with
`--operation refresh-config`; it keeps the locally built image until the first
approved main deployment publishes and selects an ECR image. ECR image storage
adds usage charges beyond the compute/root-volume/IPv4 baseline. Retain required
rollback images when managing repository storage.
