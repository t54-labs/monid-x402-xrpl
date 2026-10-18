"""Offline wheel import/startup/CLI smoke: python -m scripts.check_package PATH.whl.

Extracts only into a temporary directory; never installs the wheel or loads .env.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SMOKE = r"""
import asyncio
import importlib
import json
import os
import pkgutil
import socket
import sys
from pathlib import Path

def blocked(*args, **kwargs):
    raise AssertionError("Packaged smoke tests must stay offline")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.getaddrinfo = blocked

import gateway
from gateway.config import Settings
Settings.model_config["env_file"] = None
from gateway.catalog import CatalogStore, save_snapshot

async def idle_catalog(self):
    await asyncio.Event().wait()
CatalogStore.run_forever = idle_catalog
save_snapshot(os.environ["CATALOG_SNAPSHOT_PATH"], [{
    "provider": "synthetic", "endpoint": "/echo", "displayName": "Synthetic Echo",
    "price": {"type": "PER_CALL", "amount": {"value": 0.001, "currency": "USD"}},
}], [], "synthetic-snapshot")
assert Path(gateway.__file__).is_relative_to(Path.cwd()), "Imports must come from the built wheel"
modules = [item.name for item in pkgutil.iter_modules(gateway.__path__, "gateway.")]
for name in modules:
    importlib.import_module(name)
from gateway.main import app
import httpx

async def smoke():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="https://gateway.example") as client:
            health = await client.get("/health")
            assert health.status_code == 200 and health.json()["paymentsReady"] is False
            paid = await client.post("/t/synthetic/echo", json={"query": "synthetic"})
            assert paid.status_code == 503
            directory = await client.get("/.well-known/x402")
            assert directory.status_code == 200 and directory.json()["total"] == 1
            catalog = await client.get("/catalog")
            assert catalog.status_code == 200 and catalog.json()["total"] == 1
            missing = await client.get("/jobs/missing", headers={"X-Job-Token": "synthetic"})
            assert missing.status_code == 404
asyncio.run(smoke())
print(json.dumps({"modules": modules, "route_checks": 5, "network": "disabled", "dotenv": "disabled"}))
"""


def check(wheel: Path) -> None:
    expected = {
        f"gateway/{path.name}"
        for path in Path(__file__).resolve().parents[1].joinpath("gateway").glob("*.py")
    }
    with tempfile.TemporaryDirectory(prefix="monid-package-smoke-") as directory:
        root = Path(directory)
        with zipfile.ZipFile(wheel) as archive:
            if not expected.issubset(archive.namelist()):
                raise AssertionError("Wheel is missing gateway modules")
            archive.extractall(root)
        env = {
            "PATH": os.defpath,
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "DATABASE_PATH": str(root / "synthetic.sqlite3"),
            "CATALOG_SNAPSHOT_PATH": str(root / "synthetic-catalog.json"),
            "MONID_API_KEY": "synthetic-key",
            "X402_XRPL_PAYTO_ADDRESS": "",
            "X402_XRPL_FACILITATOR_URL": "",
        }
        result = subprocess.run(
            [sys.executable, "-c", SMOKE], cwd=root, env=env, capture_output=True, text=True, timeout=20
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        print(result.stdout.strip())
        for args, expected_code in [
            (["--help"], 0),
            (
                [
                    "--database",
                    str(root / "missing.sqlite3"),
                    "--invoice",
                    "synthetic",
                    "--outcome",
                    "not-settled",
                    "--evidence",
                    "synthetic-proof",
                    "--gateway-stopped",
                ],
                2,
            ),
        ]:
            cli = subprocess.run(
                [sys.executable, "-m", "gateway.reconcile", *args],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if cli.returncode != expected_code:
                raise AssertionError(cli.stderr)
        assert not (root / "missing.sqlite3").exists()
        print(json.dumps({"packaged_cli_checks": 2, "result": "passed"}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    check(parser.parse_args().wheel)


if __name__ == "__main__":
    main()
