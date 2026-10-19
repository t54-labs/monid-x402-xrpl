# monid-x402-xrpl

Release preparation is in progress. T54 has not yet approved an open-source
license for this gateway. See the [release decisions and checklist](docs/open-source-release.md),
[source provenance](docs/provenance.md) and [third-party notices](THIRD_PARTY_NOTICES.md).
Third-party license notices do not grant a license to the gateway itself.

An x402 gateway that sells the upstream tool catalog (Monid: about 2,600 agent tools
from about 90 providers) for **RLUSD on the XRP Ledger**. Agents pay us per call in
RLUSD; we run the tool with our own prepaid upstream key and return the result.

```
agent ──RLUSD (x402 exact, t54 facilitator)──▶ gateway ──API key, USD balance──▶ upstream tool
```

It is listed on XRPL AI Hub as a single merchant, **Monid Tool Router** ("Tool Router —
Powered by Monid", after the Heurist Inference Router convention): one receiving
address, every tool as a service under it.

## How a call works

1. `POST /t/{provider}{endpoint}` with the tool's input as JSON and no payment
   → `402` with a quote in RLUSD computed from that input.
2. The agent signs an RLUSD payment for that quote and repeats the request with
   `PAYMENT-SIGNATURE`.
3. The gateway verifies the payment with the facilitator (nothing moves yet), runs the
   tool upstream, and **settles only if the run succeeded**. Upstream errors and
   empty results are not charged, just as the upstream does not charge us for them.
4. A run still going after `RUN_WAIT_SECONDS` is settled and handed back as a job:
   `202 {jobId, jobToken, poll}`. `GET /jobs/{jobId}` with `X-Job-Token` is free.

A request whose input changes after quoting gets a fresh quote instead of running.
Invoices bind the provider, endpoint and canonical JSON input, including when the
price stays the same. Retrying an executed invoice requires the original verified
payment envelope and returns its saved result or retries settlement only. Unknown
execution/settlement returns `409` and requires reconciliation; it never authorizes
another upstream execution. See [payment recovery](docs/payment-recovery.md).

## Pricing

Upstream USD cost → RLUSD 1:1, times `1 + PRICE_MARKUP` (0 by default), rounded up to
`PRICE_QUANTUM_RLUSD`, never below `MIN_PRICE_RLUSD`.

| Upstream price type | Quote |
|---|---|
| `PER_CALL` | the price |
| `PER_RESULT` | flat fee + per-result × the count the input asks for (`limit`, `maxItems`, …) |
| `PER_UNIT` | per-unit × the units the input states (`duration`, text length, …) |
| `PER_UNIT_MATRIX` | the variant the input selects; the dearest one if it selects none |
| `TIERED` | default + matching tiers + the dearest output-measured tier per unit |

Sizes the input does not state fall back to `DEFAULT_RESULT_UNITS` /
`DEFAULT_METERED_UNITS`, so variable prices are estimates. Every run records the
upstream's real cost; `uv run python -m scripts.underquoted` lists runs that cost us
more than the buyer paid.

## Directory endpoints

- `GET /.well-known/x402?page=N`: paginated tool list for XRPL AI Hub. Each entry
  carries `url`, `name`, `description`, `price`, plus fields the Hub keeps for later
  (`externalId`, `category`, `subcategory`, `upstreamProvider`).
- `GET` / empty `POST` / `POST {}` on a tool URL: a priced `402` and never a run. This
  is how the Hub probes.
- `GET /catalog?q=&provider=&category=`: free search.
- `GET /health`: lists missing configuration; paid routes return `503` until it is empty.

## Excluded tools

Everything runs in **one** upstream workspace, so tools that create state (inboxes,
phone numbers, machines, files, browser sessions) would be shared by every buyer.
They stay off, along with wall-clock-billed tools, via `EXCLUDED_PROVIDERS` and
`EXCLUDED_ENDPOINTS` (see `.env.example`).

## Run

```bash
uv sync
cp .env.example .env   # set MONID_API_KEY and X402_XRPL_PAYTO_ADDRESS
uv run uvicorn gateway.main:app --reload
uv run pytest
uv run python -m scripts.catalog_report   # sync the live catalog and summarise prices
```

The catalog syncs from the upstream's public listing (no key) every
`CATALOG_REFRESH_SECONDS` and is cached at `CATALOG_SNAPSHOT_PATH`.

## Try one paid call

With the gateway running (`uv run uvicorn gateway.main:app`, testnet settings) and a
funded testnet payer wallet in `data/e2e-wallets.json` (created by the E2E run):

```bash
uv run python -m scripts.call_tool /t/exa/search '{"query": "XRP Ledger", "numResults": 2}'
uv run python -m scripts.call_tool /t/dataforseo/serp/google/organic/live/advanced '{}' --quote-only
```

It prints the RLUSD quote, the tool output, and the testnet settlement transaction.

## Preview on a local XRPL AI Hub

1. Start a disposable Postgres:
   `docker run -d --name monid-hub-pg -e POSTGRES_PASSWORD=password -e POSTGRES_DB=x402_xrpl -p 127.0.0.1:5440:5432 postgres:15-alpine`
2. Apply the Hub's non-Rewards migrations. Its Rewards migrations need the staging role setup, and the
   directory pages don't use them.
3. With the gateway running:
   `HUB_DATABASE_URL=postgresql://postgres:password@127.0.0.1:5440/x402_xrpl uv run python -m scripts.seed_local_hub`
4. Start the Hub API on :4011 and the website on :3000 (`hub-api` / `hub-web` in
   `.claude/launch.json`). Set `XRPL_AI_HUB_DIR` to your own absolute Hub checkout
   path in the launcher's environment first. The gateway launcher assumes this
   repository is the current working directory. The local Postgres password above
   is only for the disposable localhost database; use your own production secret.
5. Open http://localhost:3000/directory.

## End-to-end test (XRPL testnet)

```bash
uv run python -m scripts.catalog_report            # writes the catalog snapshot
E2E_CATALOG=data/catalog.json uv run python -m scripts.e2e_testnet
```

Runs the gateway on localhost and pays it with real **testnet** RLUSD through t54's
testnet facilitator: it creates faucet wallets on first run (kept in
`data/e2e-wallets.json`), opens RLUSD trust lines, buys a little testnet RLUSD
through the testnet AMM, then checks:
- Hub-style probes get a quote.
- A paid call settles on-chain to the merchant, carrying SourceTag 804681468.
- A failed upstream run is not charged.
- An async run settles.
- The ledger records each outcome.

The upstream is a local stand-in (`scripts/fake_monid.py`) unless
`E2E_REAL_UPSTREAM_KEY` is set.

## Open items

- `X402_XRPL_PAYTO_ADDRESS`: not created yet. It needs an RLUSD trust line.
- Invoices, results and execution tombstones now live in the same durable SQLite
  database as the run ledger. Keep one gateway deployment with persistent storage;
  do not use independent database copies or discard execution history on restart.
- Showing all tools on XRPL AI Hub needs the Hub's large-catalog changes: pagination,
  async import, and per-merchant limits.

## Deployment examples

The [sanitized GKE and Cloud Build examples](deploy/README.md) use operator-supplied
project, image, receiving-address and domain placeholders. They are not an approved
deployment plan. Review payment recovery, persistent storage and the separate
release gates before operating a paid service. Never commit `.env`, wallet seeds,
catalog/result snapshots, logs or databases; the build upload allowlists exclude them.
