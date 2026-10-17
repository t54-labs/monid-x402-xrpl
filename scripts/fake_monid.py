"""A stand-in for the upstream API, for end-to-end runs without a real key.

Serves a saved catalog snapshot on the public listing routes and answers runs with
canned results. A run whose input has {"query": "fail"} completes with a provider 404
(the upstream bills that at zero); {"query": "slow"} runs asynchronously.

    CATALOG=path/to/catalog.json uvicorn scripts.fake_monid:app --port 8911
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
_runs: dict[str, dict[str, Any]] = {}
_polls: dict[str, int] = {}
_snapshot: dict[str, Any] = {}


def _catalog() -> dict[str, Any]:
    if not _snapshot:
        with open(os.environ["CATALOG"], encoding="utf-8") as fh:
            _snapshot.update(json.load(fh))
    return _snapshot


def _authorized(request: Request) -> bool:
    return request.headers.get("Authorization") == f"Bearer {os.environ.get('FAKE_MONID_KEY', 'e2e-key')}"


@app.get("/public/v1/endpoints")
async def endpoints(limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
    items = _catalog()["endpoints"]
    start = int(cursor or 0)
    page = items[start : start + limit]
    nxt = start + limit
    return {"items": page, "cursor": str(nxt) if nxt < len(items) else None, "total": len(items)}


@app.get("/public/v1/categories")
async def categories() -> dict[str, Any]:
    return {"items": _catalog().get("taxonomy", [])}


def _completed(run_id: str, provider: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    failed = payload.get("query") == "fail"
    return {
        "runId": run_id,
        "provider": provider,
        "endpoint": endpoint,
        "status": "COMPLETED",
        "output": None if failed else {"echo": payload, "results": ["r1", "r2"]},
        "providerResponse": {"httpStatus": 404} if failed else {"httpStatus": 200},
        "billing": {
            "calculatedCost": {"value": 0 if failed else 7000, "unit": "MICRO_DOLLAR", "currency": "USD"}
        },
        "billedUnits": 0 if failed else 1,
        "hints": {"next": "monid runs get ..."},
    }


@app.post("/v1/run")
async def run(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"code": 401, "message": "Unauthorized"})
    body = await request.json()
    run_id = uuid.uuid4().hex.upper()
    payload = body.get("input") or {}
    result = _completed(run_id, body["provider"], body["endpoint"], payload)
    if payload.get("query") == "slow":
        _runs[run_id] = result
        _polls[run_id] = 0
        return JSONResponse(status_code=202, content={"runId": run_id, "status": "READY"})
    status = result["providerResponse"]["httpStatus"]
    return JSONResponse(status_code=status, content=result)


@app.get("/v1/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"code": 401, "message": "Unauthorized"})
    if run_id not in _runs:
        return JSONResponse(status_code=404, content={"code": 404, "message": "not found"})
    _polls[run_id] += 1
    if _polls[run_id] < 2:
        return JSONResponse(content={"runId": run_id, "status": "RUNNING"})
    return JSONResponse(content=_runs[run_id])


@app.get("/v1/wallet/balance")
async def balance(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"code": 401, "message": "Unauthorized"})
    return JSONResponse(
        content={"balance": {"value": 50, "currency": "USD"}, "held": {"value": 0, "currency": "USD"}}
    )
