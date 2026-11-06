"""Trace middleware (JSONL audit log) and Snapshot tree options."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from windows_mcp.infrastructure import trace
from windows_mcp.tools.snapshot import _tree_options


def test_trace_is_off_by_default() -> None:
    assert trace.resolve_trace_dir({}) is None
    assert trace.resolve_trace_dir({"WINDOWS_MCP_TRACE": "off"}) is None


def test_trace_dir_resolution(tmp_path) -> None:
    assert trace.resolve_trace_dir({"WINDOWS_MCP_TRACE": "1"}).name == "traces"
    assert trace.resolve_trace_dir({"WINDOWS_MCP_TRACE": str(tmp_path)}) == tmp_path


def test_redaction_is_recursive_and_covers_json_batch_steps() -> None:
    args = {
        "text": "secret",
        "label": 3,
        "steps": '[{"tool": "Type", "args": {"text": "pw", "label": 1}}]',
    }
    redacted = trace.redact(args)
    assert redacted["text"] == "<redacted 6 chars>"
    assert redacted["label"] == 3
    assert redacted["steps"][0]["args"]["text"] == "<redacted 2 chars>"
    assert trace.redact(args, enabled=False) == args


def _context(name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(name=name, arguments=arguments))


def test_middleware_writes_one_line_per_call(tmp_path) -> None:
    writer = trace.TraceWriter(tmp_path)
    middleware = trace.TraceMiddleware(writer)

    async def ok(context):
        return SimpleNamespace(content=[SimpleNamespace(text="Clicked.")], is_error=False)

    async def boom(context):
        raise ValueError("STALE_REF: gone")

    asyncio.run(middleware.on_call_tool(_context("Click", {"label": 1}), ok))
    with pytest.raises(ValueError):
        asyncio.run(middleware.on_call_tool(_context("Type", {"text": "pw"}), boom))

    lines = [json.loads(line) for line in writer.path.read_text(encoding="utf-8").splitlines()]
    assert [line["tool"] for line in lines] == ["Click", "Type"]
    assert lines[0]["ok"] is True and lines[0]["result"] == "Clicked."
    assert lines[1]["ok"] is False and "STALE_REF" in lines[1]["error"]
    assert lines[1]["args"]["text"] == "<redacted 2 chars>"


def test_tree_options_skeleton_clamps_depth() -> None:
    assert _tree_options(
        None, max_depth=None, skeleton=True, root_label=None, snapshot_id=None, surface=None
    ) == {"max_depth": 3}
    assert (
        _tree_options(
            None, max_depth=2, skeleton=True, root_label=None, snapshot_id=None, surface=None
        )["max_depth"]
        == 2
    )
    assert (
        _tree_options(
            None, max_depth=8, skeleton=False, root_label=None, snapshot_id=None, surface=None
        )["max_depth"]
        == 8
    )


@pytest.mark.parametrize("depth", [0, 51, True, "3"])
def test_tree_options_reject_bad_depth(depth) -> None:
    with pytest.raises(ValueError, match="max_depth"):
        _tree_options(
            None, max_depth=depth, skeleton=False, root_label=None, snapshot_id=None, surface=None
        )


def test_tree_options_reject_conflicting_arguments() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        _tree_options(
            None, max_depth=None, skeleton=False, root_label=1, snapshot_id=None, surface="menu"
        )
    with pytest.raises(ValueError, match="only used together with root_label"):
        _tree_options(
            None, max_depth=None, skeleton=False, root_label=None, snapshot_id="s1", surface=None
        )


def test_closed_surface_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from windows_mcp.desktop import surfaces
    from windows_mcp.elements.report import ElementError

    monkeypatch.setattr(surfaces, "surface_handles", lambda kind: [])
    with pytest.raises(ElementError, match="no menu surface is open"):
        _tree_options(
            None, max_depth=None, skeleton=False, root_label=None, snapshot_id=None, surface="menu"
        )
    monkeypatch.setattr(surfaces, "surface_handles", lambda kind: [11, 22])
    assert _tree_options(
        None, max_depth=None, skeleton=False, root_label=None, snapshot_id=None, surface="menu"
    )["only_handles"] == [11, 22]


def test_unknown_surface_kind() -> None:
    from windows_mcp.desktop.surfaces import surface_handles

    with pytest.raises(ValueError, match="surface must be one of"):
        surface_handles("dock")
