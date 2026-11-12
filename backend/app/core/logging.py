"""Structured JSON logging with request/operation context and secret redaction (Phase 8).

Every record is one JSON object: ts, level, logger, message, plus the context bound for the current request or
operation (request_id, user_id, agent_id, cycle_id, ...) and any `extra={"fields": {...}}` of the call. Context lives in
a contextvar, so it follows the request or the worker cycle without threads or globals.

Redaction is applied to the rendered message, the fields and tracebacks: bearer tokens, JWTs, `password=`/`secret=`/
`api_key=`/`token=` values and provider keys are replaced with "<redacted>". Journal note text is never logged by
application code (only ids, counts and status codes).
"""

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

_context: ContextVar[dict] = ContextVar("agentx_log_context", default={})

_REDACTIONS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "<redacted-jwt>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "<redacted-key>"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|api_key|apikey|access_token|token|jwt_secret_key)(\s*[=:]\s*)"
                r"(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"), r"\1\2<redacted>"),
    (re.compile(r"(?i)(postgresql(?:\+\w+)?://[^:/@\s]+:)[^@\s]+@"), r"\1<redacted>@"),
]
SENSITIVE_KEYS = {"password", "password_hash", "token", "access_token", "authorization", "jwt_secret_key", "api_key",
                  "llm_api_key", "db_password", "secret", "notes", "prompt"}


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _clean(value):
    if isinstance(value, dict):
        return {k: ("<redacted>" if str(k).lower() in SENSITIVE_KEYS else _clean(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


def bind_log_context(**fields) -> None:
    """Add fields to the current context (e.g. user_id once authenticated). Scoped by log_context/the request."""
    _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})


@contextmanager
def log_context(**fields) -> Iterator[None]:
    token = _context.set({**_context.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict:
    return dict(_context.get())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        payload.update(_clean(_context.get()))
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(_clean(extra))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
            payload.setdefault("error_category", record.exc_info[0].__name__ if record.exc_info[0] else None)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn's own access log duplicates the structured request log (app.core.http) and is not JSON
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
