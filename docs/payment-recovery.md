# Durable payment retries and recovery

Previously, a settlement failure released the in-memory invoice claim. Replaying
the same signed payment executed Monid again, spending prepaid balance and possibly
creating another asynchronous job. The new `invoices` table in `DATABASE_PATH`
binds each invoice to its issued requirements, provider, endpoint and canonical JSON
input. An atomic SQLite transition commits the execution claim before any upstream
submission. The first verified payment's canonical envelope hash binds subsequent
delivery; knowing an invoice ID alone cannot retrieve a cached response.

## States and retry behavior

| State | Meaning | Same bound payment retry |
| --- | --- | --- |
| `quoted` | No execution claimed; persisted wall-clock TTL applies | Verify, atomically claim, execute |
| `executing` | Claim committed; work may have started or result persistence may be incomplete | `409 execution_status_unknown`; never resubmit |
| `unbilled` | Known provider failure, empty result or explicit 4xx rejection; saved public response | Return original response, no settlement or execution |
| `ready` | Public response and job credentials saved; no unresolved settlement attempt | Atomically claim settlement only |
| `settling` | Settlement claim committed; request may be in flight or interrupted | `409 settlement_status_unknown`; reconcile |
| `settlement_unknown` | Timeout, malformed response, HTTP error, or ambiguous outcome | `409 settlement_status_unknown`; reconcile |
| `settled` | Successful settlement and ledger update committed atomically | Return original public response and PAYMENT-RESPONSE |

Only an HTTP 200 response with `success: false`, the invoice's network,
`errorReason: insufficient_funds`, `settlementAttempted: false` and no transaction
is treated as an explicit pre-submission rejection. It returns the original 402
requirements and moves `settling` back to `ready`. All other failed settlement
responses require reconciliation, including legacy responses without proof of
non-submission and `submit_failed:*`, `submit_missing_tx_hash`, `validation_failed:*`.
Confirm the facilitator's no-submission contract before enabling unattended retries
in production; do not add ambiguous failures to this allowlist.

Keep and resend the original payment envelope and input. A newly signed transaction
for an already executed invoice is rejected; support must reconcile the original
payment. Used invoices, cached responses and tombstones do not expire. A changed
input on an unused quote produces a fresh quote even for flat prices. Executed
invoices reject changes. GET and unsigned empty/`{}` POST probes still only quote;
their empty-input quotes cannot authorize arbitrary nonempty input.

The async run ID is committed before polling. A transport failure, HTTP error or
malformed poll hands back the original job after settlement rather than creating new work. Settlement retries
reuse the saved job ID/token. Paid job polling requires the invoice to be settled;
no result or job token is returned while settlement is unresolved. Cached delivery
and settlement retries do not depend on current Monid balance or repricing.
They also remain available on the original route after a tool is delisted; new
unpaid requests to a delisted tool still return 404.

## Crash limitations

This is one durable **submission attempt per invoice**, not exactly-once upstream
execution. Monid offers no idempotency contract here. A POST timeout may mean work
was accepted. A crash after committing the claim but before sending the POST may
mean no work occurred. Both remain closed. A crash after Monid responds but before
the public response is saved can lose automatic delivery, even with a known run ID.
Persisted run IDs support investigation; automatic execution recovery is deliberately
disabled. Do not reset `executing`, mint a replacement payment automatically, or
retry a job-creating POST to discover whether it ran.

Likewise, a crash after `/settle` succeeds but before SQLite commits success leaves
`settling`. Neither a timeout nor an elapsed lease proves nonpayment. There is no
automatic settlement reconciler and no automatic expiry/unlock of active work.
An operator must verify facilitator records and validated XRPL outcomes. This can
temporarily withhold a paid result; resolve the incident instead of risking another
charge or upstream purchase.

## Offline settlement reconciliation

1. Stop **all** gateway processes using this database and back it up consistently.
   The CLI's `--gateway-stopped` is an operator assertion, not a process detector.
2. Identify pending invoices with a read-only query, without exporting response
   bodies or job tokens:

   ```sql
   SELECT i.id, i.state, i.run_id, r.upstream_run_id, r.upstream_status,
          r.settlement_tx, i.updated_at
   FROM invoices i LEFT JOIN runs r ON r.id = i.run_id
   WHERE i.state IN ('executing', 'settling', 'settlement_unknown');
   ```

3. For unknown settlement, correlate the **original** signed payment with the
   facilitator and validated chain evidence. Check network, issuer/currency,
   amount, destination, invoice and transaction identity. A missing transaction in
   a short query window, an unvalidated response, or the gateway's local ledger
   alone is insufficient proof of non-settlement. Record evidence in an incident
   reference; keep private credentials and signed blobs out of that reference.
4. Only after proving settlement, record the confirmed transaction and payer:

   ```bash
   python -m scripts.reconcile_invoice --database /persistent/gateway.sqlite3 \
     --invoice INVOICE_ID --outcome settled --transaction CONFIRMED_TX_HASH \
     --payer CONFIRMED_PAYER --evidence INCIDENT_REFERENCE --gateway-stopped
   ```

   Or, only after proving the payment was neither submitted nor settled and cannot
   still complete, explicitly permit **settlement-only** retry:

   ```bash
   python -m scripts.reconcile_invoice --database /persistent/gateway.sqlite3 \
     --invoice INVOICE_ID --outcome not-settled \
     --evidence INCIDENT_REFERENCE --gateway-stopped
   ```

5. Restart and have the buyer resend the original envelope/input. Confirmed
   settlement returns cached output; confirmed non-settlement permits one new
   settlement attempt. If the signed payment is no longer usable, resolve with
   support; do not clear the execution claim. The command changes only unknown
   settlement states with a saved response, updates the run ledger atomically, and
   records the evidence reference. It cannot reopen execution, contact any service,
   load `.env`, or verify the operator's external evidence itself.

For unknown execution, use a persisted Monid run ID for read-only investigation
when present. If absent, consult upstream records and resolve delivery/refund
manually. Leave the execution tombstone intact; the reconciliation CLI intentionally
does not unlock it. Do not claim work was unbilled simply because the response was lost.

## Upgrade, storage and production gates

The schema is additive and created when the service opens its configured database.
No existing run rows are rewritten. Old in-memory invoices are not importable and
return `409 unknown_invoice` after upgrade. Drain requests and reconcile outstanding
legacy runs before issuing replacement quotes. The fix PR is based on main and does
not include the independent deployment PR. Existing legacy jobs with a recorded
`settled` ledger state and a valid job token remain pollable after upgrade; pending
legacy jobs remain closed until operational resolution.

Keep one deployment on persistent local SQLite storage. Independent app connections
sharing a SQLite file use atomic claims, but this does not establish a supported
multi-host SQLite deployment. Do not run old and new gateway binaries together:
the old binary does not enforce these claims. Rollback to the vulnerable binary or
restoring an old database snapshot can remove the protection; retain all execution
history and reconcile work since the snapshot before reopening paid routes.

The database now retains public results and bearer job tokens, in addition to payment
metadata; protect filesystem access and backups accordingly. It stores only a payment
hash, not the signed payment blob or upstream API key. Outstanding unused quotes are
capped and expired; execution history is intentionally retained without automatic
pruning. Monitor storage capacity and choose a reviewed retention/archive policy
before production. Database failure before the claim prevents execution; failure
after submission requires incident recovery, never blind retry.

Offline unit tests use temporary databases, synthetic credentials, mocked upstream
and facilitator calls, and reject outbound socket connections and DNS. They cover
failure, timeout, crash windows, restart, expiry, forged/mismatched replay, parallel
SQLite connections, async recovery and explicit reconciliation. They do not validate
live Monid billing, facilitator response semantics, chain finality, cloud volumes,
wallet configuration or production readiness. Those remain separately approved
deployment/audit gates. Do not run paid or blockchain tests as part of this suite.
