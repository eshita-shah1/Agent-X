"""HTTP hardening as one plain ASGI middleware (Phase 8): request ids, structured request logs, security headers, a
request-body size cap, and safe 500 responses. No threads and no shared mutable state.

  * Request id: a client-supplied `X-Request-ID` is kept only if it is short and harmless ([A-Za-z0-9._-], at most 64);
    otherwise a new one is generated. It is bound to the log context for the whole request and echoed in the response.
  * Request log: one JSON line per request (method, path without the query string, status, duration, user id when
    authenticated). Query strings are not logged: they can carry search text from private journal notes.
  * Security headers on every response; API responses are also `Cache-Control: no-store` with a deny-all CSP.
    `Strict-Transport-Security` only in production (TLS terminates at the reverse proxy).
  * Body cap: a declared `Content-Length` above MAX_REQUEST_BODY_BYTES is refused with 413 before the body is read; a
    body without a length (chunked) is counted as it streams and refused at the same limit.
  * Unhandled errors: logged with the request context and a traceback (redacted); the client receives only
    `{"detail": "Internal server error", "request_id": ...}`, never exception text. A transaction the endpoint did not
    commit is rolled back by the session scope, so no partial trading record is persisted.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from app.core.logging import log_context

log = logging.getLogger("agentx.http")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

BASE_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
API_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]
HSTS = (b"strict-transport-security", b"max-age=31536000; includeSubDomains")


class BodyTooLarge(Exception):
    pass


class HardeningMiddleware:
    def __init__(self, app, max_body_bytes: int, hsts: bool = False):
        self.app, self.max_body, self.hsts = app, max_body_bytes, hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        path = scope.get("path", "")
        started = time.perf_counter()
        state = {"status": None, "started": False, "too_large": False}
        extra = list(BASE_HEADERS) + (API_HEADERS if path.startswith("/api/") else []) + ([HSTS] if self.hsts else [])

        async def send_wrapper(message):
            if state["too_large"]:
                # the framework turned the aborted body read into its own error response: replace it with the 413
                if message["type"] == "http.response.start" and not state["started"]:
                    await json_response(413, {"detail": f"Request body too large (limit {self.max_body} bytes)",
                                              "request_id": request_id})
                return
            if message["type"] == "http.response.start":
                state["status"], state["started"] = message["status"], True
                existing = {k.lower() for k, _ in message.get("headers", [])}
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode()),
                                                  *[(k, v) for k, v in extra if k not in existing]]}
            await send(message)

        async def json_response(status: int, body: dict):
            state["status"], state["started"] = status, True
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json"), (b"x-request-id", request_id.encode()), *extra]})
            await send({"type": "http.response.body", "body": json.dumps(body).encode()})

        declared = headers.get(b"content-length")
        received = 0

        async def receive_wrapper():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    state["too_large"] = True
                    raise BodyTooLarge()
            return message

        with log_context(request_id=request_id):
            try:
                if declared is not None and declared.isdigit() and int(declared) > self.max_body:
                    await json_response(413, {"detail": f"Request body too large (limit {self.max_body} bytes)",
                                              "request_id": request_id})
                else:
                    await self.app(scope, receive_wrapper, send_wrapper)
            except BodyTooLarge:
                if not state["started"]:
                    await json_response(413, {"detail": f"Request body too large (limit {self.max_body} bytes)",
                                              "request_id": request_id})
            except Exception:
                log.exception("unhandled error", extra={"fields": {"method": scope.get("method"), "path": path}})
                if state["started"]:
                    raise
                await json_response(500, {"detail": "Internal server error", "request_id": request_id})
            finally:
                user_id = (scope.get("state") or {}).get("user_id")
                log.info("request", extra={"fields": {
                    "method": scope.get("method"), "path": path, "status": state["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1), "user_id": user_id,
                }})
