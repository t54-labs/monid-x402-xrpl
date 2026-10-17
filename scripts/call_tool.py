"""Pay for and call one tool on a running gateway, like an agent would.

    uv run python -m scripts.call_tool /t/exa/search '{"query": "XRP Ledger", "numResults": 3}'
    uv run python -m scripts.call_tool /t/exa/search '{"query": "x"}' --quote-only

Pays in RLUSD from the payer wallet in E2E_WALLETS_PATH (created by
scripts.e2e_testnet; testnet only). Prints the quote, the tool result, and the
settlement transaction with an explorer link.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

import httpx
from x402_xrpl.client import XRPLPresignedPaymentPayer, XRPLPresignedPaymentPayerOptions
from x402_xrpl.types import PaymentRequirements
from xrpl.wallet import Wallet

ROOT = Path(__file__).resolve().parent.parent
EXPLORERS = {
    "xrpl:0": "https://livenet.xrpl.org/transactions/",
    "xrpl:1": "https://testnet.xrpl.org/transactions/",
}
RPC_URLS = {"xrpl:0": "https://s1.ripple.com:51234/", "xrpl:1": "https://s.altnet.rippletest.net:51234/"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("tool", help="tool path, e.g. /t/exa/search")
    parser.add_argument("input", nargs="?", default="{}", help="tool input as JSON")
    parser.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://localhost:8000"))
    parser.add_argument(
        "--wallets", default=os.environ.get("E2E_WALLETS_PATH", str(ROOT / "data" / "e2e-wallets.json"))
    )
    parser.add_argument("--quote-only", action="store_true", help="show the price without paying")
    args = parser.parse_args()

    body = json.loads(args.input)
    url = f"{args.gateway.rstrip('/')}{args.tool}"
    quote = httpx.post(url, json=body, timeout=30)
    if quote.status_code != 402:
        print(f"expected a 402 quote, got HTTP {quote.status_code}: {quote.text[:300]}")
        return 1
    challenge = json.loads(base64.b64decode(quote.headers["PAYMENT-REQUIRED"]))
    req = PaymentRequirements.from_dict(challenge["accepts"][0])
    print(f"tool:   {challenge['resource']['description']}")
    print(f"price:  {req.amount} RLUSD on {req.network}  (pay to {req.pay_to})")
    print(f"detail: {json.dumps((req.extra or {}).get('pricing'))}")
    if args.quote_only:
        return 0
    if req.network != "xrpl:1":
        print("refusing to pay on a network other than testnet from this test script")
        return 1

    seeds = json.loads(Path(args.wallets).read_text())
    payer = XRPLPresignedPaymentPayer(
        XRPLPresignedPaymentPayerOptions(
            wallet=Wallet.from_seed(seeds["payer"]), network=req.network, rpc_url=RPC_URLS[req.network]
        )
    )
    header = payer.create_payment_header(req)
    paid = httpx.post(url, json=body, headers={"PAYMENT-SIGNATURE": header}, timeout=300)
    print(f"\nHTTP {paid.status_code}")
    print(json.dumps(paid.json(), ensure_ascii=False, indent=2)[:3000])
    if "PAYMENT-RESPONSE" in paid.headers:
        settlement = json.loads(base64.b64decode(paid.headers["PAYMENT-RESPONSE"]))
        tx = settlement.get("transaction")
        print(f"\nsettled: {tx}\n         {EXPLORERS.get(req.network, '')}{tx}")
    else:
        print("\nnot charged (no settlement)")
    return 0 if paid.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
