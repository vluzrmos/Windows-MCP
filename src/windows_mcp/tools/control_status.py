"""Read-only MCP control ownership status and release tool."""

from typing import Literal
from mcp.types import ToolAnnotations

from windows_mcp.desktop.control import get_controller


def register(mcp, *, get_desktop, get_analytics):
    @mcp.tool(
        name="ControlStatus",
        description="Get desktop control owner, enabled state, gate policy, configured idle timeout and remaining user idle wait in seconds, or pass action='release' to immediately release AI desktop control and hide the HUD overlay before finishing a response.",
        annotations=ToolAnnotations(
            title="ControlStatus",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def control_status_tool(action: Literal["status", "release"] = "status") -> dict:
        if action == "release":
            return get_controller().release()
        return get_controller().status()
