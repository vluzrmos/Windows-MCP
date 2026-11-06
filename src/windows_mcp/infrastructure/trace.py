"""Opt-in local audit trace of tool calls (JSONL).

The server has full system access and no rollback, so a record of what an agent did
is the next best thing. Set ``WINDOWS_MCP_TRACE`` to turn it on:

* ``1`` / ``true`` / ``yes`` / ``on`` — write to ``~/.windows-mcp/traces``
* any other non-empty value — treated as the directory to write to

Each server process appends to its own ``<timestamp>-<pid>.jsonl`` file, one JSON object
per tool call: timestamp, tool, arguments, outcome, duration, error and the first part of
the text result. The trace stays on this machine; it is never sent anywhere.

Typed text can be sensitive, so the values of ``text``, ``value`` and ``content``
arguments (at any nesting depth, including Batch steps) are redacted to their length by
default. Set ``WINDOWS_MCP_TRACE_REDACT=false`` to record them verbatim. Results and
arguments can still contain private desktop content (window titles, file paths,
PowerShell commands): treat trace files like screenshots.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_REDACTED_KEYS = frozenset({"text", "value", "content"})
_RESULT_PREVIEW_CHARS = 500


def resolve_trace_dir(env: dict[str, str] | None = None) -> Path | None:
    """Directory traces go to, or None when tracing is off."""
    source = os.environ if env is None else env
    raw = (source.get("WINDOWS_MCP_TRACE") or "").strip()
    if not raw or raw.lower() in _FALSE:
        return None
    if raw.lower() in _TRUE:
        from windows_mcp.infrastructure.config import CONFIG_DIR

        return CONFIG_DIR / "traces"
    return Path(raw).expanduser()


def redaction_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get("WINDOWS_MCP_TRACE_REDACT") or "").strip().lower() not in _FALSE


def redact(value: Any, *, enabled: bool = True) -> Any:
    """Replace sensitive argument values with a length marker, recursively."""
    if not enabled:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _REDACTED_KEYS and isinstance(item, (str, int, float)):
                out[key] = f"<redacted {len(str(item))} chars>"
            else:
                out[key] = redact(item, enabled=enabled)
        return out
    if isinstance(value, list):
        return [redact(item, enabled=enabled) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(("[", "{")):
        # Batch steps may arrive as a JSON string.
        try:
            return redact(json.loads(value), enabled=enabled)
        except ValueError, TypeError:
            return value
    return value


def _result_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if content is None:
        return str(result)[:_RESULT_PREVIEW_CHARS] if result is not None else ""
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else f"[{type(block).__name__}]")
    return "\n".join(parts)[:_RESULT_PREVIEW_CHARS]


class TraceWriter:
    """Append-only JSONL writer, one file per server process."""

    def __init__(self, directory: Path, *, redact_args: bool = True) -> None:
        self.directory = directory
        self.redact_args = redact_args
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"{stamp}-{os.getpid()}.jsonl"
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                logger.warning("Could not write trace record to %s", self.path, exc_info=True)


class TraceMiddleware(Middleware):
    """Record every tool call to a :class:`TraceWriter`."""

    def __init__(self, writer: TraceWriter) -> None:
        self.writer = writer

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable) -> Any:
        name = getattr(context.message, "name", "?")
        arguments = getattr(context.message, "arguments", None) or {}
        started = time.monotonic()
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "tool": name,
            "args": redact(arguments, enabled=self.writer.redact_args),
        }
        try:
            result = await call_next(context)
        except Exception as error:
            record.update(
                ok=False,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"{type(error).__name__}: {error}"[:_RESULT_PREVIEW_CHARS],
            )
            self.writer.write(record)
            raise
        is_error = bool(getattr(result, "is_error", False))
        record.update(
            ok=not is_error,
            duration_ms=int((time.monotonic() - started) * 1000),
            result=_result_text(result),
        )
        self.writer.write(record)
        return result


def build_trace_middleware() -> TraceMiddleware | None:
    """The trace middleware when ``WINDOWS_MCP_TRACE`` enables it, else None."""
    directory = resolve_trace_dir()
    if directory is None:
        return None
    writer = TraceWriter(directory, redact_args=redaction_enabled())
    logger.info("Tool-call trace enabled: %s", writer.path)
    return TraceMiddleware(writer)
