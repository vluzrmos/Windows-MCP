"""Batch tool — run several tool calls in order inside one MCP request."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from typing import Any

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.infrastructure import with_analytics

MAX_STEPS = 32
MAX_TIMEOUT = 600.0
_RESULT_CHAR_LIMIT = 4000
_NOT_BATCHABLE = frozenset({"Batch", "ControlStatus"})


def _parse_steps(steps: list | str) -> list[dict[str, Any]]:
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError as error:
            raise ValueError(f"steps is not valid JSON: {error}") from None
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a non-empty list of {tool, args} objects")
    if len(steps) > MAX_STEPS:
        raise ValueError(f"steps may hold at most {MAX_STEPS} entries")
    parsed = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"steps[{index}] must be an object with 'tool' and optional 'args'")
        unknown = set(step) - {"tool", "args"}
        if unknown:
            raise ValueError(f"steps[{index}] has unknown keys: {', '.join(sorted(unknown))}")
        tool = step.get("tool")
        args = step.get("args", {}) or {}
        if not isinstance(tool, str) or not tool:
            raise ValueError(f"steps[{index}].tool must be a tool name")
        if not isinstance(args, dict):
            raise ValueError(f"steps[{index}].args must be an object")
        parsed.append({"tool": tool, "args": args})
    return parsed


def _validate_against(registry: dict[str, Callable], steps: list[dict[str, Any]]) -> None:
    """Reject the whole batch before anything runs when one step is malformed."""
    for index, step in enumerate(steps):
        tool = step["tool"]
        if tool in _NOT_BATCHABLE:
            raise ValueError(f"steps[{index}]: {tool} cannot be used inside Batch")
        func = registry.get(tool)
        if func is None:
            known = ", ".join(sorted(name for name in registry if name not in _NOT_BATCHABLE))
            raise ValueError(f"steps[{index}]: unknown tool {tool!r}; available: {known}")
        try:
            signature = inspect.signature(func)
        except TypeError, ValueError:
            continue
        params = signature.parameters
        accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        if not accepts_kwargs:
            unknown = set(step["args"]) - set(params) - {"ctx"}
            if unknown:
                raise ValueError(
                    f"steps[{index}] ({tool}): unknown argument(s) {', '.join(sorted(unknown))}"
                )
        missing = [
            name
            for name, param in params.items()
            if name != "ctx"
            and param.default is inspect.Parameter.empty
            and param.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and name not in step["args"]
        ]
        if missing:
            raise ValueError(
                f"steps[{index}] ({tool}): missing required argument(s) {', '.join(missing)}"
            )


def _render(result: Any) -> str:
    """Text of a tool result; images and other non-text parts are summarized."""
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (list, tuple)):
        parts = []
        for item in result:
            if isinstance(item, str):
                parts.append(item)
            else:
                parts.append(f"[{type(item).__name__} omitted]")
        text = "\n".join(parts)
    elif isinstance(result, dict):
        text = json.dumps(result, ensure_ascii=False, default=str)
    else:
        text = str(result)
    if len(text) > _RESULT_CHAR_LIMIT:
        text = text[:_RESULT_CHAR_LIMIT] + f"\n… [{len(text) - _RESULT_CHAR_LIMIT} chars truncated]"
    return text


def register(
    mcp: Any,
    *,
    get_desktop: Callable[[], Any],
    get_analytics: Callable[[], Any],
    registry: dict[str, Callable] | None = None,
) -> None:
    tools = registry if registry is not None else {}

    @mcp.tool(
        name="Batch",
        description=(
            "Runs up to 32 tool calls in order in one request, e.g. "
            '[{"tool":"Click","args":{"label":5}},{"tool":"WaitFor","args":{"condition":"dialog_open"}},'
            '{"tool":"Snapshot","args":{"surface":"dialog"}}]. Saves round trips for a known '
            "sequence. The whole list is validated first (tool names and argument names) and "
            "nothing runs if any step is malformed. Steps run sequentially and are not atomic: a "
            "failed step does not undo earlier ones. stop_on_error=True (default) stops at the "
            "first failure; later steps are reported as not_started. timeout bounds the whole "
            "batch in seconds. Labels in args must already exist when the batch starts (a "
            "Snapshot step replaces them for later steps). Batch and ControlStatus cannot be "
            "nested. Images in step results are omitted; call Screenshot separately to see one."
        ),
        annotations=ToolAnnotations(
            title="Batch",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Batch-Tool")
    async def batch_tool(
        steps: list[dict[str, Any]] | str,
        stop_on_error: bool | str = True,
        timeout: float = 60.0,
        ctx: Context = None,
    ) -> str:
        from windows_mcp.desktop.utils import as_bool

        stop_on_error = as_bool(stop_on_error, "stop_on_error")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= MAX_TIMEOUT
        ):
            raise ValueError(f"timeout must be greater than 0 and at most {MAX_TIMEOUT:g} seconds")
        parsed = _parse_steps(steps)
        _validate_against(tools, parsed)

        started = time.monotonic()
        deadline = started + float(timeout)
        lines: list[str] = []
        completed = failed = 0
        stopped_reason = None
        for index, step in enumerate(parsed):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped_reason = f"deadline reached before step {index}"
                break
            func = tools[step["tool"]]
            kwargs = dict(step["args"])
            if "ctx" in inspect.signature(func).parameters:
                kwargs["ctx"] = ctx
            step_started = time.monotonic()
            try:
                result = await asyncio.wait_for(func(**kwargs), timeout=remaining)
            except asyncio.TimeoutError:
                failed += 1
                lines.append(
                    f"[{index}] {step['tool']}: error — batch deadline expired mid-step "
                    "(the step may have partly run) [unknown, retry: unknown]"
                )
                stopped_reason = f"deadline reached during step {index}"
                break
            except Exception as error:
                failed += 1
                elapsed = time.monotonic() - step_started
                lines.append(f"[{index}] {step['tool']}: error after {elapsed:.2f}s — {error}")
                if stop_on_error:
                    stopped_reason = f"stop_on_error at step {index}"
                    break
                continue
            completed += 1
            elapsed = time.monotonic() - step_started
            body = _render(result)
            lines.append(
                f"[{index}] {step['tool']}: ok in {elapsed:.2f}s" + (f"\n{body}" if body else "")
            )

        not_started = len(parsed) - completed - failed
        summary = (
            f"Batch: {completed} ok, {failed} failed, {not_started} not_started of {len(parsed)} "
            f"in {time.monotonic() - started:.2f}s (sequential, not atomic)."
        )
        if stopped_reason:
            summary += f" Stopped: {stopped_reason}."
        return "\n\n".join([summary, *lines])
