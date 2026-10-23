"""Bound the complete request without reopening uncertain payment attempts."""

import asyncio

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class RequestDeadlineMiddleware:
    def __init__(self, app: ASGIApp, *, seconds: float, concurrency: int):
        self.app = app
        self.seconds = seconds
        self.slots = asyncio.Semaphore(concurrency)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http" or not scope["path"].startswith(("/t/", "/jobs/")):
            return await self.app(scope, receive, send)
        signed = scope["method"] == "POST" and any(
            name.lower() == b"payment-signature" for name, _ in scope["headers"]
        )
        # Admission precedes invoice claiming and verification. Never queue a
        # signed request behind long-running paid work after claiming its invoice.
        if signed:
            if self.slots.locked():
                return await JSONResponse(
                    {"error": {"code": "gateway_busy", "message": "Retry the same signed request later."}},
                    status_code=503,
                    headers={"Retry-After": "5"},
                )(scope, receive, send)
            await self.slots.acquire()
        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        deadline = asyncio.timeout(self.seconds)
        try:
            try:
                async with deadline:
                    await self.app(scope, receive, tracked_send)
            except TimeoutError:
                if not deadline.expired() or started:
                    raise
                # Cancellation leaves executing/settling records durably closed.
                # A timeout is not evidence that execution or settlement failed.
                await JSONResponse(
                    {
                        "error": {
                            "code": "request_deadline_exceeded",
                            "message": (
                                "Outcome may be unknown. Retry only the original signed request; "
                                "do not obtain a new invoice for the same work."
                                if signed
                                else "The request deadline was exceeded."
                            ),
                        }
                    },
                    status_code=409 if signed else 504,
                )(scope, receive, send)
        finally:
            if signed:
                self.slots.release()
