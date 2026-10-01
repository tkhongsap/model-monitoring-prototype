"""Process logging for the live plane (spec D.2).

``configure(format)`` installs one root handler on stderr: JSON lines when
``LOG_FORMAT=json`` (one object per line, every ``extra=`` field of the record carried as
a top-level key), a plain ``time level logger: message`` line otherwise. It is idempotent:
calling it again replaces the handler it installed and never touches handlers owned by
uvicorn, pytest or the operator.

Secrets: the live plane logs hosts, ids, ticks and durations, never a URL path, a token
or a payload (`alert_delivery`, `http_retry`). This module adds nothing to that.
"""
from __future__ import annotations

import json
import logging
import sys
import time

JSON = "json"
PLAIN = "plain"

# attributes every LogRecord carries; anything else came from ``extra=``
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {
    "message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per record; ``extra`` fields become top-level keys."""

    def format(self, record: logging.LogRecord) -> str:
        body: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            body[key] = value if _json_safe(value) else repr(value)
        if record.exc_info:
            body["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(body, sort_keys=False, ensure_ascii=False,
                          separators=(",", ":"), default=str)


def _json_safe(value) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


class PlainFormatter(logging.Formatter):
    """Human-readable line; ``extra`` fields are appended as ``key=value``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s",
                         datefmt="%Y-%m-%dT%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{key}={value}" for key, value in record.__dict__.items()
                          if key not in _STANDARD_ATTRS and not key.startswith("_"))
        return f"{line} {extras}" if extras else line


def configure(fmt: str | None = None, level: int = logging.INFO) -> logging.Handler:
    """Install (or replace) the control tower's root log handler. Returns it."""
    fmt = (fmt or PLAIN).strip().lower()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_control_tower", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == JSON else PlainFormatter())
    handler._control_tower = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return handler
