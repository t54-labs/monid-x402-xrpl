# Backups, monitoring, and cutover preparation

These are reusable procedures. Keep real resource identifiers, source schema,
ledger counts, reconciliation notes, and verification results in private records.
Do not attach database snapshots, manifests, environment files, or output from
production commands to GitHub issues, PRs, or Actions artifacts.

## Infrastructure and host installation

Validate `../operations.json` with cfn-lint and `../security.guard`, then create
and inspect a CloudFormation change set. Supply the existing instance ID and role.
The stack adds a private, encrypted, versioned backup bucket, an instance policy
limited to its backup prefix and the `Monid/Gateway` metric namespace, and four
CloudWatch alarms. Backups expire after 90 days; noncurrent versions after 30 days.
The bucket is retained on stack deletion. Set `NotificationTopicArn` to an
operator-managed SNS topic if notifications are required; otherwise alarms are
visible only in CloudWatch. Storage, custom metrics, alarms and API usage incur
normal AWS usage charges.

Copy these files and `scripts/ledger_snapshot.py` into a temporary directory on
the host through SSM. Run `bash install.sh REGION BACKUP_BUCKET INSTANCE_ID` as
root. Identifiers are operator inputs, not checked-in defaults. This installs
backup/metrics services and timers without deploying or restarting the gateway.
Keep host helper versions aligned with the reviewed application release.

- A daily timer takes a SQLite online backup, including committed WAL contents,
  verifies its integrity, and uploads the ledger followed by its checksum manifest.
  The manifest contains private schema/count metadata and is uploaded last.
- Local temporary copies are root-only and removed after upload. The private
  `/srv/monid/backups/latest-uri` records the last successful object prefix.
- Metrics run every five minutes: local nginx/application health, disk usage, and
  time since the last successful upload. Health requires payment configuration
  outside standby; in standby it requires a healthy catalog. This local check is
  not an independent CloudFront availability probe.
- Alarms cover unhealthy application, disk usage above 85%, no successful backup
  for 30 hours, and EC2 status failure. Two of three five-minute periods trigger
  the alarm; missing metrics are treated as failures.
- A successful upload is not proof of a usable restore. Complete the drill below.
  Daily snapshots can lose up to a day of updates after catastrophic disk loss.
  For a payment ledger, reconcile missing upstream/chain activity before reopening
  a restored backup; never treat an older backup as the latest execution history.

## Restore and schema rehearsal

Use the following commands on a private host/directory, never a public CI runner.
The destination must not already exist; the tool refuses to replace a live ledger.

```bash
umask 077
python -m scripts.ledger_snapshot backup --source SOURCE_DB --destination NEW_SNAPSHOT_DIR
python -m scripts.ledger_snapshot verify --snapshot NEW_SNAPSHOT_DIR
python -m scripts.ledger_snapshot rehearse --snapshot NEW_SNAPSHOT_DIR
```

The backup command uses SQLite's backup API instead of copying a live `.sqlite3`
file. It never opens a missing source in create mode. The rehearsal initializes
the new schema on a disposable second copy, then checks all existing columns and
row contents, including payment hashes, job tokens, saved responses and uncertain
states. Extra source columns are preserved. A legacy ledger with runs but no
invoice table is rejected for separate review. Schema compatibility is not proof
that a running old release has identical payment behavior.

To prove off-host recovery, download both `gateway.sqlite3` and `manifest.json`
from the same private S3 object prefix into a new mode-0700 directory. Verify the
manifest and rehearse against the intended release. Do not load environment
secrets, start a server, or perform upstream/settlement calls during this drill.
Catalog data can be regenerated from the upstream public listing; reproduce the
same provider exclusions and pricing configuration before validating its contents.

## Before the first production cutover

1. Review and merge the deployment PR. Explicitly run the manual deployment on
   main and verify the release digest, health, configuration and standby gate.
2. Compare source and target configuration privately. Required fields include
   upstream workspace key/API URL, payment network/asset/issuer/receiver,
   facilitator/source tag, pricing, exclusions, branding and public URL. Supply
   real values through Parameter Store, with the API key as SecureString.
3. Inspect the source ledger read-only. Reconcile executing, settling and unknown
   states against upstream and chain evidence; do not reset them to quoted/ready
   to make migration checks pass. See `scripts/reconcile_invoice.py` and the
   payment recovery documentation for the supported settlement-only workflow.
4. Test the entire request deadline with local fake services, then perform a
   separately controlled payment/async acceptance test after credential setup.
   `REQUEST_DEADLINE_SECONDS` defaults to 100 seconds, below nginx/CloudFront
   timeouts. Keep `RUN_WAIT_SECONDS=0` for prompt job handoff. Deadline expiration
   does not prove an upstream action was cancelled; retry only the same signed
   invoice. Uncertain outcomes remain closed for reconciliation.
5. Freeze new admissions and all writes on the source, including job polling;
   drain in-flight work. Take the final SQLite snapshot, verify and transfer it
   privately, and rehearse the upgrade on a copy. Stop the target gateway before
   installing the final database, preserve ownership and record checksums. Do not
   overwrite a live database or forget WAL/shm sidecars. Keep the source closed
   if the target begins accepting paid writes.
6. Test the intended hostname, discovery, payment success/failure, async polling,
   same-invoice retries and lost-response recovery. Account for previously issued
   quote resource URLs and old job URLs; do not blindly forward signed requests
   to a new hostname. Change client/Hub URLs only after this acceptance passes.
7. Verify off-host restore and alarm delivery (if configured). Keep Railway
   available for recovery, but never enable two independent payment writers.

## Subsequent releases and rollback

The image/configuration rollout scripts intentionally require the standby marker.
Manual dispatch is not a production draining mechanism. Before a production
release, close admission at nginx, let active requests finish, inspect unfinished
ledger states, and stop the gateway before replacing data or applying incompatible
schema changes. Make a verified off-host backup first. Reopen only after explicit
operator verification; a workflow must never silently remove the standby marker.

Automatic rollback restores the previous image/environment only. It does not
restore the ledger or guarantee backward schema compatibility. After any new paid
writes, preserve the newest execution history and reconcile unknown outcomes
before returning traffic to the former service. Do not overwrite a newer ledger
with a pre-release backup merely to make an old image start.
