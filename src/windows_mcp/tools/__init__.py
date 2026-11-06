"""tools subpackage — registers all MCP tool definitions on a FastMCP instance."""

from collections.abc import Callable
from typing import Any

from windows_mcp.tools import (
    app,
    batch,
    clipboard,
    control_status,
    display,
    elements,
    filesystem,
    input,
    multi,
    notification,
    process,
    registry,
    scrape,
    shell,
    snapshot,
)

_MODULES = [
    control_status,
    app,
    display,
    shell,
    filesystem,
    snapshot,
    input,
    elements,
    scrape,
    multi,
    clipboard,
    process,
    notification,
    registry,
]


class _RecordingMCP:
    """Forward tool registration to *mcp* while keeping each tool's callable.

    Batch runs other tools in-process through this registry, so a step goes through
    exactly the same wrapper (analytics, UIA-thread dispatch) as a direct call.
    """

    def __init__(self, mcp: Any, registry: dict[str, Callable]) -> None:
        self._mcp = mcp
        self._registry = registry

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        decorator = self._mcp.tool(*args, **kwargs)
        name = kwargs.get("name")

        def record(func: Callable) -> Any:
            registered = decorator(func)
            if name:
                self._registry[name] = func
            return registered

        return record

    def __getattr__(self, item: str) -> Any:
        return getattr(self._mcp, item)


def register_all(mcp, *, get_desktop, get_analytics):
    """Register every tool module on *mcp*.

    *get_desktop* and *get_analytics* are zero-arg callables that return the
    current ``Desktop`` and ``PostHogAnalytics`` instances (resolved lazily so
    that tools can be registered before ``lifespan`` initializes the singletons).
    """
    tool_registry: dict[str, Callable] = {}
    recorder = _RecordingMCP(mcp, tool_registry)
    for mod in _MODULES:
        mod.register(recorder, get_desktop=get_desktop, get_analytics=get_analytics)
    batch.register(
        mcp, get_desktop=get_desktop, get_analytics=get_analytics, registry=tool_registry
    )
    return tool_registry
