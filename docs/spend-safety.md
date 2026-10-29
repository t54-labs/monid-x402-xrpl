# Durable spend admission

This change is preparation for reviewed execution. It does not approve Monid billing bounds, resale, catalog publication, payment configuration or activation. Keep the existing standby admission gate and `X402_PAYMENT_ENABLED=true`. The payment-disabled compatibility setting cannot authorize execution in this version.

## Closed defaults and policy ownership

New settings are `MONID_SPEND_BUDGET_MICROS=0`, `MONID_SPEND_BUDGET_ID=`, `MONID_SPEND_ACCOUNT_ID=` and `MONID_TOOL_POLICIES_PATH=`. An absent, zero, malformed or expired policy keeps new work closed. Keep policy files operator-controlled, regular files, immutable to the runtime user, with no symlinks. Policy documents are limited to 1 MiB. Do not put credentials in them.

The budget is a **lifetime allocation in integer micro-USD for one dedicated upstream account/workspace**, not a daily reset, account balance or sale price. Use stable nonsecret IDs; they appear in signed quote metadata. The first valid claim pins the account ID, budget ID and ceiling in SQLite. Changing any of those after a claim refuses new work; it cannot reset spent capacity. Key rotation changes request binding but preserves the same lifetime allocation. Multiple independent ledgers or external users of the same upstream account are not covered by one ledger's cap. Production must retain a single writer and prohibit unaccounted external spending or use a separately reviewed account-wide control.

A policy document has exactly `version: 1` and `tools`. An empty tool list is intentionally denied. Each reviewed entry requires all of:

- `provider` and `endpoint`: exact native routing identity from the selected catalog tool.
- `catalog_digest`: SHA-256 returned by `gateway.spend.catalog_digest(tool)` for the complete selected tool representation.
- `max_cost_micros`: reviewed conservative maximum upstream charge for every admitted input, including possible retries, asynchronous work, metering and provider fees. The gateway never retries a run submission automatically.
- `expires_at`: finite UTC Unix timestamp; checked before quoting and dispatch.
- `inputs` and `required`: exact allowlisted native fields and required names. Unknown fields are rejected recursively.
- `review_reference`: nonsecret evidence identifying the reviewed native API/schema revision, billing contract and cost-bound review. Catalog display prices are not this evidence.

Supported input rules are bounded `string` (`max_length`), nonnegative bounded `integer` (`minimum`, `maximum`), `boolean`, bounded `array` (`max_items`, `items`), and `object` (`inputs`, `required`). Rule dictionaries accept only those named fields. Maximum nesting is four, object fields 64, array items 100, and a serialized request is at most 16 KiB. Unsupported schemas remain closed. This is a conservative admission language, not a general JSON Schema interpreter or a replacement for native schema review.

The pinned upstream catalog does not supply a trustworthy native schema version or hard billing cap. A catalog hash detects catalog changes; it cannot prove the provider's actual billing behavior. Do not populate a real policy until the maximum and native contract are established. **An incorrect reviewed maximum can understate actual external cost. Unknown provider billing is not bounded by this patch.** No real maximum, price, recipient, account ID or API key is supplied here.

## Transaction and failure behavior

After the existing payment verification succeeds, one `BEGIN IMMEDIATE` transaction checks the invoice and its request/policy binding, verifies the stable budget, sums all reservations, inserts a unique reservation and claims the invoice. Two processes or connections cannot both spend the same remaining capacity. Both invoice ID and signed-proof digest are unique. Failed claims roll back the budget insert, reservation and invoice transition together.

Immediately before dispatch, the current catalog, policy, account, key binding, base URL and input must still match. The reservation is durably changed to `dispatch_unknown` before the upstream request. Process death between this intent and I/O conservatively consumes capacity. Success, rejection, empty output, timeout, cancellation, asynchronous acceptance and unknown settlement all retain the full maximum. Retrieving an existing response or polling a known job does not reserve again or submit new work.

There is deliberately **no automatic release**, even for a definitive pre-dispatch rejection. This conservative first version requires a separate reviewed reconciliation mechanism before any allocation could be reduced. There is no budget-reset endpoint or scheduled refill. Existing invoice settlement reconciliation cannot release reservations or reopen execution. An exhausted budget reports payment readiness false; a previously issued quote may still reach payment verification but cannot claim capacity or dispatch a provider request.

Liveness `/health` remains available in standby. `paymentsReady` is false without a usable reviewed policy, available budget, matching budget identity and payment enforcement. A successful health response alone is not paid readiness or commercial approval. Existing public catalog display behavior is not proof that a tool has a reviewed spend policy.

## Migration, backup and rollback

Schema version 0 upgrades additively to SQLite `PRAGMA user_version=1`, introducing `spend_budget`, `spend_reservations` and protective triggers. Existing `runs` and `invoices` rows are preserved byte-for-byte. Previously submitted work lacking a reservation blocks new spend, including legacy runs without invoices; no historical cost is silently treated as zero. Unsupported schema versions and changed financial definitions refuse startup. Do not hand-edit, drop or recreate financial tables/triggers to clear a failure.

`scripts/ledger_snapshot.py` writes format-2 manifests with native schema version and inventories of all four financial tables. SHA-256 and integrity verification cover the entire consistent SQLite backup. Scratch rehearsal checks preservation of every original row/column in those tables, including reservations. Historical format-1/schema-0 snapshots can be rehearsed on disposable copies. Rehearsal never restores over a live database and does not establish that historical spend has been reconciled.

`docs/financial-schema-v1.json` records the exact nonsecret native SQL contract, verified by a regression test. Before installation, update the separately managed host backup/restore descriptor to accept **this exact four-table Monid schema and its triggers as a new reviewed native version**. The current deployed two-table Monid descriptor must remain fail-closed; do not broaden it to ignore extra tables. The application image, backup schema descriptor, migration evidence and image provenance need coordinated review before any runtime change. This local patch does not modify or install that descriptor.

Rollback must preserve the current ledger. Never restore a pre-migration or older backup over newer financial state. An old payment-enabled image cannot use its former unreserved invoice-claim path because the retained trigger rejects it; nevertheless, old images are supported only behind standby, since they do not implement the new guard or readiness semantics. Do not remove financial tables or disable payment enforcement to make an old image execute. A data-loss recovery requires an explicit one-writer freeze and external reconciliation for any interval newer than the snapshot, not an automatic restore-and-enable operation.

## Offline evidence and remaining gates

Tests use synthetic policies, signatures, providers and account labels. Outbound sockets are blocked, including in crash and competing-process tests. These tests prove local admission and durable-state invariants; they are not live provider billing, payment or deployment acceptance.

Before paid activation, review per-tool native bounds and commercial permission; allocate the dedicated account lifetime ceiling; reconcile any historical work; validate the exact new schema through host backups/scratch restore; review the publication and deployment patch; then separately approve credential setup, payment configuration and bounded live tests. Existing standby, Railway and public metadata remain unchanged by local preparation.
