"""Structured logging and timing helpers.

Provides:
- ``get_logger(name)``: standard logger factory
- ``request_id_var``: ContextVar that carries a per-message correlation id
  through every async hop. Set once at the message boundary (listener),
  auto-inherited by spawned tasks.
- ``bind_request(request_id)``: context manager that binds / restores the id
- ``span(name, **extra)``: async/sync context manager that logs
  ``span.start`` / ``span.end`` with millisecond duration. Use to instrument
  the message path so "why was this 30s" becomes a grep-able trace.
- ``configure_structured_logging()``: install a JSON-line formatter on the
  root logger's stderr handler. Idempotent; safe to call multiple times.

Structured records look like::

    {"ts": "2026-04-24T01:23:45.678Z", "level": "INFO", "logger": "xiaobai.mcp",
     "request_id": "r-abc123", "msg": "span.end", "span": "media_download",
     "elapsed_ms": 842, "size_bytes": 104857}

The JSON goes to stderr; existing ``logger.info("%s", ...)`` calls are picked
up automatically — the formatter serializes the pre-rendered message into
``msg`` and merges any dict passed via ``extra={}``.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Iterator


# ── Correlation id ────────────────────────────────────────────────────

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "xiaobai_request_id", default=""
)


@contextlib.contextmanager
def bind_request(request_id: str) -> Iterator[None]:
    """Bind ``request_id`` to the current context for the duration of the block.

    Async tasks spawned inside inherit the value automatically via the
    ``contextvars`` machinery. Restores the previous id on exit.
    """
    token = request_id_var.set(request_id)
    try:
        yield
    finally:
        request_id_var.reset(token)


# ── Spans / timing ────────────────────────────────────────────────────

_span_logger = logging.getLogger("xiaobai.span")


class _Span:
    """Light timing context manager; works as both sync and async."""

    __slots__ = ("name", "extra", "_t0")

    def __init__(self, name: str, extra: dict[str, Any]) -> None:
        self.name = name
        self.extra = extra
        self._t0 = 0.0

    def __enter__(self) -> "_Span":
        self._t0 = time.perf_counter()
        _span_logger.debug("span.start", extra={"span": self.name, **self.extra})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed_ms = int((time.perf_counter() - self._t0) * 1000)
        level = logging.WARNING if exc else logging.INFO
        payload = {"span": self.name, "elapsed_ms": elapsed_ms, **self.extra}
        if exc:
            payload["error"] = f"{type(exc).__name__}: {exc}"
        _span_logger.log(level, "span.end", extra=payload)

    async def __aenter__(self) -> "_Span":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.__exit__(exc_type, exc, tb)


def span(name: str, **extra: Any) -> _Span:
    """Context manager that logs ``span.end`` with ``elapsed_ms``.

    Usage (async)::

        async with span("media_download", message_id=msg_id):
            ...

    Usage (sync)::

        with span("parse"):
            ...
    """
    return _Span(name, extra)


# ── Logger factory ────────────────────────────────────────────────────


def get_logger(name: str) -> logging.Logger:
    """Return a logger named ``name``; callers typically pass ``__name__``."""
    return logging.getLogger(name)


# ── Structured JSON formatter ─────────────────────────────────────────


_STD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "asctime", "taskName",
}


class _JsonFormatter(logging.Formatter):
    """Emit each record as a JSON line; merges ``extra={}`` dict into output."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        # Merge extra-injected attributes (span, elapsed_ms, etc.)
        for key, value in record.__dict__.items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_jsonl_installed = False


def install_jsonl_handler(path: str = "/tmp/xiaobai.jsonl") -> None:
    """Attach a JSONL file handler alongside existing log handlers.

    Writes one JSON record per line. Does NOT touch stderr or existing
    human-readable handlers, so ``tail -f`` on the old log still works
    and you get structured records via ``jq`` in parallel.

    Idempotent — subsequent calls are no-ops.
    """
    global _jsonl_installed
    if _jsonl_installed:
        return
    handler = logging.FileHandler(path)
    handler.setFormatter(_JsonFormatter())
    logging.getLogger().addHandler(handler)
    _jsonl_installed = True
