"""End-to-end run on XRPL testnet with real RLUSD settlement.

    uv run python -m scripts.e2e_testnet

What is real: the gateway (uvicorn on localhost), the x402-xrpl payer, XRPL testnet,
testnet RLUSD, and t54's testnet facilitator (verify + settle on-chain).
What is a stand-in: the upstream API (scripts/fake_monid.py), unless real mode is on:
E2E_REAL_UPSTREAM=1 uses MONID_API_KEY from .env (or E2E_REAL_UPSTREAM_KEY) and calls
the real upstream, which spends a few cents of the prepaid balance. Real mode skips
the async case and uses an invalid input for the not-charged case.

Test wallets are created from the testnet faucet on first run and kept in
E2E_WALLETS_PATH (default data/e2e-wallets.json, git-ignored). The payer buys a
little testnet RLUSD with faucet XRP through the testnet AMM.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from x402_xrpl.client import XRPLPresignedPaymentPayer, XRPLPresignedPaymentPayerOptions
from x402_xrpl.types import PaymentRequirements
from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountLines, Tx
from xrpl.models.transactions import Payment, PaymentFlag, TrustSet
from xrpl.transaction import submit_and_wait
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet, generate_faucet_wallet

from gateway.config import RLUSD_CURRENCY_HEX, RLUSD_TESTNET_ISSUER, T54_FACILITATOR_SOURCE_TAG, Settings

ROOT = Path(__file__).resolve().parent.parent
RPC_URL = os.environ.get("E2E_RPC_URL", "https://s.altnet.rippletest.net:51234/")
FACILITATOR_URL = os.environ.get("E2E_FACILITATOR_URL", "https://xrpl-facilitator-testnet.t54.ai")
WORK = Path(os.environ.get("E2E_WORK_DIR", ROOT / "data" / "e2e"))
WALLETS_PATH = Path(os.environ.get("E2E_WALLETS_PATH", ROOT / "data" / "e2e-wallets.json"))
CATALOG = os.environ.get("E2E_CATALOG", "")  # a catalog snapshot (scripts.catalog_report writes one)
GATEWAY_PORT, UPSTREAM_PORT = 8910, 8911
GATEWAY = f"http://127.0.0.1:{GATEWAY_PORT}"
TOOL = "/t/exa/search"
REAL_INPUT = os.environ.get("E2E_REAL_INPUT", '{"query": "XRP Ledger agent payments", "numResults": 3}')
PAYER_RLUSD_TARGET = Decimal("2")

rpc = JsonRpcClient(RPC_URL)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}", flush=True)


def rlusd(value: Decimal | str) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=RLUSD_CURRENCY_HEX, issuer=RLUSD_TESTNET_ISSUER, value=str(value))


def rlusd_balance(address: str) -> Decimal:
    lines = rpc.request(AccountLines(account=address, peer=RLUSD_TESTNET_ISSUER)).result.get("lines", [])
    for line in lines:
        if line.get("currency") == RLUSD_CURRENCY_HEX:
            return Decimal(line["balance"])
    return Decimal(0)


def has_trust_line(address: str) -> bool:
    lines = rpc.request(AccountLines(account=address, peer=RLUSD_TESTNET_ISSUER)).result.get("lines", [])
    return any(line.get("currency") == RLUSD_CURRENCY_HEX for line in lines)


def load_wallets() -> tuple[Wallet, Wallet]:
    if WALLETS_PATH.exists():
        seeds = json.loads(WALLETS_PATH.read_text())
        return Wallet.from_seed(seeds["payer"]), Wallet.from_seed(seeds["merchant"])
    print("creating testnet wallets from the faucet…", flush=True)
    payer, merchant = generate_faucet_wallet(rpc), generate_faucet_wallet(rpc)
    WALLETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    WALLETS_PATH.write_text(json.dumps({"payer": payer.seed, "merchant": merchant.seed}))
    WALLETS_PATH.chmod(0o600)
    return payer, merchant


def ensure_rlusd(payer: Wallet, merchant: Wallet) -> None:
    for wallet in (payer, merchant):
        if not has_trust_line(wallet.classic_address):
            submit_and_wait(
                TrustSet(account=wallet.classic_address, limit_amount=rlusd("1000000")), rpc, wallet
            )
    balance = rlusd_balance(payer.classic_address)
    if balance < PAYER_RLUSD_TARGET:
        # Buy testnet RLUSD with faucet XRP: a payment to self, routed through the AMM.
        need = PAYER_RLUSD_TARGET - balance
        submit_and_wait(
            Payment(
                account=payer.classic_address,
                destination=payer.classic_address,
                amount=rlusd(need),
                send_max=xrp_to_drops(30),
                flags=PaymentFlag.TF_PARTIAL_PAYMENT,
            ),
            rpc,
            payer,
        )


def wait_healthy(url: str, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up")


def start(module: str, port: int, env: dict[str, str], log: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env={**os.environ, **env},
        stdout=log.open("wb"),
        stderr=subprocess.STDOUT,
    )


def challenge(response: httpx.Response) -> PaymentRequirements:
    body = json.loads(base64.b64decode(response.headers["PAYMENT-REQUIRED"]))
    return PaymentRequirements.from_dict(body["accepts"][0])


def pay_and_call(
    payer: XRPLPresignedPaymentPayer, path: str, body: dict[str, Any]
) -> tuple[PaymentRequirements, httpx.Response]:
    quote = httpx.post(f"{GATEWAY}{path}", json=body, timeout=30)
    assert quote.status_code == 402, quote.text
    req = challenge(quote)
    header = payer.create_payment_header(req)
    paid = httpx.post(f"{GATEWAY}{path}", json=body, headers={"PAYMENT-SIGNATURE": header}, timeout=300)
    return req, paid


def main() -> int:
    if not CATALOG or not Path(CATALOG).exists():
        print("set E2E_CATALOG to a catalog snapshot (uv run python -m scripts.catalog_report writes one)")
        return 2
    real_key = os.environ.get("E2E_REAL_UPSTREAM_KEY", "")
    if not real_key and os.environ.get("E2E_REAL_UPSTREAM") == "1":
        real_key = Settings(_env_file=ROOT / ".env").MONID_API_KEY.strip()
        if not real_key:
            print("E2E_REAL_UPSTREAM=1 but MONID_API_KEY is not set in .env")
            return 2
    print(f"upstream: {'REAL (api.monid.ai)' if real_key else 'local stand-in'}", flush=True)
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    shutil.copy(CATALOG, WORK / "catalog.json")

    payer_wallet, merchant_wallet = load_wallets()
    print(f"payer {payer_wallet.classic_address}  merchant {merchant_wallet.classic_address}", flush=True)
    ensure_rlusd(payer_wallet, merchant_wallet)
    check(
        "payer holds testnet RLUSD",
        rlusd_balance(payer_wallet.classic_address) >= Decimal("0.1"),
        f"{rlusd_balance(payer_wallet.classic_address)} RLUSD",
    )

    procs: list[subprocess.Popen[bytes]] = []
    upstream_url = "https://api.monid.ai"
    if not real_key:
        procs.append(
            start(
                "scripts.fake_monid:app",
                UPSTREAM_PORT,
                {"CATALOG": str(WORK / "catalog.json")},
                WORK / "upstream.log",
            )
        )
        upstream_url = f"http://127.0.0.1:{UPSTREAM_PORT}"
        wait_healthy(f"{upstream_url}/public/v1/categories")
    gateway_env = {
        "PUBLIC_BASE_URL": GATEWAY,
        "DATABASE_PATH": str(WORK / "gateway.sqlite3"),
        "CATALOG_SNAPSHOT_PATH": str(WORK / "catalog.json"),
        "MONID_API_BASE_URL": upstream_url,
        "MONID_API_KEY": real_key or "e2e-key",
        "X402_XRPL_NETWORK": "xrpl:1",
        "X402_XRPL_ISSUER": RLUSD_TESTNET_ISSUER,
        "X402_XRPL_FACILITATOR_URL": FACILITATOR_URL,
        "X402_XRPL_PAYTO_ADDRESS": merchant_wallet.classic_address,
        "RUN_POLL_INTERVAL_SECONDS": "0.5",
        # A test account may hold only the free credit; production keeps its own floor.
        "MONID_MIN_BALANCE_USD": os.environ.get("E2E_MIN_BALANCE_USD", "0.05"),
    }
    procs.append(start("gateway.main:app", GATEWAY_PORT, gateway_env, WORK / "gateway.log"))
    try:
        health = wait_healthy(f"{GATEWAY}/health")
        check(
            "gateway healthy with payments ready",
            health["paymentsReady"] and health["tools"] > 0,
            f"{health['tools']} tools",
        )
        run_checks(payer_wallet, merchant_wallet, real=bool(real_key))
    finally:
        for proc in procs:
            proc.send_signal(signal.SIGINT)
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed; logs in {WORK}")
    return 1 if failed else 0


def run_checks(payer_wallet: Wallet, merchant_wallet: Wallet, *, real: bool) -> None:
    payer = XRPLPresignedPaymentPayer(
        XRPLPresignedPaymentPayerOptions(wallet=payer_wallet, network="xrpl:1", rpc_url=RPC_URL)
    )
    merchant = merchant_wallet.classic_address

    # 1. Discovery and probes, as XRPL AI Hub sees them.
    discovery = httpx.get(f"{GATEWAY}/.well-known/x402", timeout=30).json()
    check(
        "discovery lists the catalog",
        discovery["total"] > 2000 and len(discovery["resources"]) > 0,
        f"total {discovery['total']}, {discovery['pages']} pages",
    )
    probe = httpx.post(f"{GATEWAY}{TOOL}", json={}, timeout=30)
    req = challenge(probe) if probe.status_code == 402 else None
    check(
        "Hub-style probe gets an RLUSD challenge",
        req is not None
        and req.asset == RLUSD_CURRENCY_HEX
        and req.pay_to == merchant
        and (req.extra or {}).get("sourceTag") == T54_FACILITATOR_SOURCE_TAG,
        f"amount {req.amount if req else '-'}",
    )

    # 2. Paid call that succeeds: settled on-chain.
    payer_before, merchant_before = rlusd_balance(payer_wallet.classic_address), rlusd_balance(merchant)
    req, paid = pay_and_call(payer, TOOL, json.loads(REAL_INPUT) if real else {"query": "xrpl"})
    check(
        "paid call returns the tool output",
        paid.status_code == 200 and "output" in paid.json(),
        f"HTTP {paid.status_code}",
    )
    if real:
        print(f"  upstream output (first 300 chars): {json.dumps(paid.json())[:300]}", flush=True)
    check("upstream billing details are not exposed", "billing" not in paid.text and "hints" not in paid.text)
    settlement = (
        json.loads(base64.b64decode(paid.headers["PAYMENT-RESPONSE"]))
        if "PAYMENT-RESPONSE" in paid.headers
        else {}
    )
    tx_hash = settlement.get("transaction")
    check("settlement returned a transaction hash", bool(tx_hash), str(tx_hash))
    if tx_hash:
        tx = rpc.request(Tx(transaction=tx_hash)).result
        meta = tx.get("meta") or {}
        delivered = meta.get("delivered_amount") or {}
        tx_json = tx.get("tx_json") or tx
        check(
            "on-chain payment validated",
            tx.get("validated") is True and meta.get("TransactionResult") == "tesSUCCESS",
            meta.get("TransactionResult", "?"),
        )
        check(
            "delivered RLUSD to the merchant",
            tx_json.get("Destination") == merchant
            and isinstance(delivered, dict)
            and delivered.get("currency") == RLUSD_CURRENCY_HEX
            and Decimal(delivered.get("value", "0")) == Decimal(req.amount),
            f"{delivered.get('value') if isinstance(delivered, dict) else delivered} RLUSD",
        )
        check(
            "payment carries the facilitator SourceTag (Hub indexing)",
            tx_json.get("SourceTag") == T54_FACILITATOR_SOURCE_TAG,
            str(tx_json.get("SourceTag")),
        )
    merchant_after = rlusd_balance(merchant)
    check(
        "merchant RLUSD balance went up by the quote",
        merchant_after - merchant_before == Decimal(req.amount),
        f"+{merchant_after - merchant_before}",
    )

    # 3. Paid call whose upstream run fails: verified but never settled.
    payer_mid = rlusd_balance(payer_wallet.classic_address)
    bad_input = {"definitelyNotAField": True} if real else {"query": "fail"}
    _, failed = pay_and_call(payer, TOOL, bad_input)
    time.sleep(5)  # a settlement would have been validated by now
    check(
        "failed upstream run is not charged",
        400 <= failed.status_code < 500
        and "PAYMENT-RESPONSE" not in failed.headers
        and rlusd_balance(payer_wallet.classic_address) == payer_mid,
        f"HTTP {failed.status_code}",
    )

    # 4. Async upstream run: polled, then settled.
    if real:
        expected = ["settled", "rejected_unbilled"]
    else:
        expected = ["settled", "failed_unbilled", "settled"]
        _, slow = pay_and_call(
            payer, "/t/apify/apify/instagram-search-scraper", {"query": "slow", "limit": 2}
        )
        check(
            "async run completes and settles",
            slow.status_code == 200 and "PAYMENT-RESPONSE" in slow.headers,
            f"HTTP {slow.status_code}",
        )

    # 5. The ledger recorded what happened.
    rows = (
        sqlite3.connect(WORK / "gateway.sqlite3")
        .execute("SELECT state FROM runs ORDER BY created_at")
        .fetchall()
    )
    states = [row[0] for row in rows]
    check("run ledger states", states == expected, str(states))
    print(
        f"payer spent {payer_before - rlusd_balance(payer_wallet.classic_address)} RLUSD in total", flush=True
    )


if __name__ == "__main__":
    sys.exit(main())
