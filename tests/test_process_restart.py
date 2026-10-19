"""Actual child-process termination around durable claims, with all network blocked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CHILD = r"""
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError("Network is disabled in process crash tests")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked

from gateway.config import Settings
Settings.model_config["env_file"] = None
from tests.test_app import FakeFacilitator, FakeUpstream, build, decode_header, sign

root, mode, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
class RecordedUpstream(FakeUpstream):
    def handler(self, request):
        if request.url.path == "/v1/run":
            with (root / "upstream-attempts").open("a") as stream:
                stream.write("attempt\n")
        return super().handler(request)
class RecordedFacilitator(FakeFacilitator):
    async def post(self, path, json):
        if path == "/settle":
            with (root / "settlement-attempts").open("a") as stream:
                stream.write("attempt\n")
        return await super().post(path, json)

async def main():
    facilitator = RecordedFacilitator(settle_ok=not (mode == "first" and phase == "rejected"))
    app, client = build(root, RecordedUpstream(), facilitator)
    if mode == "first":
        challenge = await client.post("/t/exa/search", json={"query": "xrpl"})
        headers = {"PAYMENT-SIGNATURE": sign(decode_header(challenge.headers["PAYMENT-REQUIRED"]))}
        (root / "synthetic-payment.json").write_text(json.dumps(headers))
        async def crash(*args, **kwargs):
            os._exit(77)
        if phase == "before_post":
            app.state.ledger.create = crash
        elif phase == "after_post":
            app.state.x402_invoices.save_response = crash
        elif phase == "after_result":
            app.state.x402_invoices.start_settlement = crash
        elif phase == "after_settlement":
            app.state.x402_invoices.settlement_result = crash
    else:
        headers = json.loads((root / "synthetic-payment.json").read_text())
    response = await client.post("/t/exa/search", json={"query": "xrpl"}, headers=headers)
    print(json.dumps({"status": response.status_code, "has_output": "output" in response.json()}))
    await client.aclose()
    await app.state.upstream._client.aclose()
    app.state.ledger.close()

asyncio.run(main())
"""


@pytest.mark.parametrize(
    "phase, expected_status, upstream_count, settlement_count",
    [
        ("before_post", 409, 0, 0),
        ("after_post", 409, 1, 0),
        ("after_result", 200, 1, 1),
        ("after_settlement", 409, 1, 1),
        ("settled", 200, 1, 1),
        ("rejected", 200, 1, 2),
    ],
)
def test_process_death_preserves_execution_and_settlement_claims(
    tmp_path, phase, expected_status, upstream_count, settlement_count
):
    repo = Path(__file__).resolve().parents[1]
    # Do not inherit API keys, wallet configuration, proxies or dotenv paths.
    env = {"PATH": os.defpath, "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    first = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), "first", phase],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    expected_first = 0 if phase in ("settled", "rejected") else 77
    assert first.returncode == expected_first, first.stderr
    second = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path), "second", phase],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert result == {"status": expected_status, "has_output": expected_status == 200}
    for name, count in [("upstream-attempts", upstream_count), ("settlement-attempts", settlement_count)]:
        marker = tmp_path / name
        actual = len(marker.read_text().splitlines()) if marker.exists() else 0
        assert actual == count
