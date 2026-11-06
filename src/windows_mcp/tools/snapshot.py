"""Snapshot and Screenshot tools — desktop state capture."""

import logging
from typing import Literal

from mcp.types import ToolAnnotations
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context

from windows_mcp.tools._snapshot_helpers import (
    _as_bool,
    _as_region,
    capture_desktop_state,
    build_snapshot_response,
    screenshot_png_bytes,
)
from windows_mcp.desktop.screenshot_file import save_screenshot

logger = logging.getLogger(__name__)

# Populated by register(); exposed for backward-compatible test imports.
state_tool = None
screenshot_tool = None


SKELETON_DEPTH = 3
MAX_DEPTH_LIMIT = 50


def _tree_options(
    desktop,
    *,
    max_depth: int | None,
    skeleton: bool,
    root_label: int | None,
    snapshot_id: str | None,
    surface: str | None,
) -> dict:
    """Translate the Snapshot tree arguments into Desktop.get_state options."""
    if max_depth is not None:
        if isinstance(max_depth, bool) or not isinstance(max_depth, int):
            raise ValueError("max_depth must be an integer")
        if not 1 <= max_depth <= MAX_DEPTH_LIMIT:
            raise ValueError(f"max_depth must be between 1 and {MAX_DEPTH_LIMIT}")
    if skeleton:
        max_depth = min(max_depth or SKELETON_DEPTH, SKELETON_DEPTH)
    options: dict = {"max_depth": max_depth}
    if root_label is not None and surface:
        raise ValueError("root_label and surface cannot be combined: a label already names its window")
    if root_label is not None:
        from windows_mcp.elements.resolve import require_element

        _node, handle = require_element(desktop, root_label, snapshot_id)
        options["root_element"] = handle
    elif snapshot_id is not None:
        raise ValueError("snapshot_id is only used together with root_label")
    if surface:
        from windows_mcp.desktop.surfaces import surface_handles
        from windows_mcp.elements.report import ElementError

        handles = surface_handles(surface)
        if not handles:
            raise ElementError(
                "ELEMENT_NOT_FOUND",
                f"no {surface} surface is open.",
                hint="Open it first (for a menu: click or right-click the element that opens it), "
                "or use WaitFor(condition='menu_open').",
            )
        options["only_handles"] = handles
    return options


def register(mcp, *, get_desktop, get_analytics):
    global state_tool, screenshot_tool
    @mcp.tool(
        name='Snapshot',
        description="Take a screenshot and inspect the screen. Keywords: screenshot, screen capture, see screen, observe, look, inspect, UI elements, what's on screen. Captures complete desktop state including: system language, focused/opened windows, interactive elements (buttons, text fields, links, menus with coordinates), and scrollable areas. Set use_vision=True to include screenshot/vision analysis. Set vision_mode='winocr' (ultra-fast native Windows OCR returning JSON text + screen coordinates), vision_mode='llm' (local OpenAI-compatible Vision LLM returning JSON/text), vision_mode='winocr+llm' (both), or vision_mode='image' (returns the image directly). When vision_mode is omitted, the server's configured default is used (WINDOWS_MCP_VISION_MODE or [vision] mode in config.toml; 'winocr' when unset), so pass vision_mode='image' explicitly whenever you need to see the actual image. Set resolution='compact' (default: 720p JPEG 95% quality, or 1080p on 4K+) for fast, low-token vision or resolution='normal' (1080p/1440p lossless PNG) when you need fine visual details. Set use_annotation=False to get a clean screenshot without bounding box overlays on UI elements (default: True, draws colored rectangles around detected elements). Set use_ui_tree=False for a faster screenshot-only snapshot when you do not need interactive or scrollable element extraction. Set width_reference_lines/height_reference_lines to overlay a grid for better spatial reasoning (make sure vision is enabled to use it). Set use_dom=True for browser content to get web page elements instead of browser UI. Set display=[0] or display=[0,1] using zero-based active Windows display indices; omit it to keep the default full-desktop behavior. Set region=[left, top, right, bottom] in virtual-desktop pixel coordinates to capture and inspect only that rectangle instead of the whole screen/display — useful when you already know which area matters and want to save tokens; region takes precedence over display when both are given, and an invalid or out-of-bounds region raises an error rather than silently capturing something else. Set save_path to an absolute file path (or an existing directory for a timestamped name) to also write the captured screenshot as a lossless PNG file, whatever vision_mode and resolution are used; this turns on use_vision and the saved path is reported in the response. Existing files are never overwritten. Every labelled element is shown as #N in the UI tree together with a Snapshot ID; pass the label to Click/Type/Scroll/Move/ElementAction. Dense apps: set skeleton=True (or max_depth=N) for a shallow map where cut containers report [+N children not expanded], then drill into one with root_label=#N (plus snapshot_id) to read only that region. Set surface to read just one UI layer: 'menu' (an open popup/context menu), 'dialog', 'focused' (foreground window), 'taskbar', 'system_tray', 'start_menu' or 'action_center'. Always call this first to understand the current desktop state before taking actions.",
        annotations=ToolAnnotations(
            title="Snapshot",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "State-Tool", run_sync_in_thread=False)
    def _state_tool(
        use_vision: bool | str = False,
        use_dom: bool | str = False,
        use_annotation: bool | str = True,
        use_ui_tree: bool | str = True,
        resolution: Literal["compact", "normal"] = "compact",
        vision_mode: Literal["image", "winocr", "llm", "winocr+llm"] | None = None,
        vision_prompt: str | None = None,
        llm_model: str | None = None,
        width_reference_line: int | None = None,
        height_reference_line: int | None = None,
        display: list[int] | None = None,
        region: list[int] | str | None = None,
        save_path: str | None = None,
        max_depth: int | None = None,
        skeleton: bool | str = False,
        root_label: int | None = None,
        snapshot_id: str | None = None,
        surface: str | None = None,
        ctx: Context = None,
    ):
        tree_options = _tree_options(
            get_desktop(),
            max_depth=max_depth,
            skeleton=_as_bool(skeleton),
            root_label=root_label,
            snapshot_id=snapshot_id,
            surface=surface,
        )
        try:
            capture_result = capture_desktop_state(
                get_desktop(),
                use_vision=_as_bool(use_vision) or bool(save_path),
                use_dom=_as_bool(use_dom),
                use_annotation=_as_bool(use_annotation),
                use_ui_tree=_as_bool(use_ui_tree),
                width_reference_line=width_reference_line,
                height_reference_line=height_reference_line,
                display=display,
                region=_as_region(region),
                tool_name="Snapshot tool",
                resolution=resolution,
                vision_mode=vision_mode,
                vision_prompt=vision_prompt,
                llm_model=llm_model,
                tree_options=tree_options,
            )
        except Exception as e:
            from windows_mcp.elements.report import ElementError

            if isinstance(e, ElementError):
                raise
            logger.warning(
                "Snapshot failed with display=%s region=%s use_vision=%s use_dom=%s resolution=%s vision_mode=%s",
                display,
                region,
                use_vision if 'use_vision' in locals() else None,
                use_dom if 'use_dom' in locals() else None,
                resolution if 'resolution' in locals() else None,
                vision_mode if 'vision_mode' in locals() else None,
                exc_info=True,
            )
            return [f'Error capturing desktop state: {str(e)}. Please try again.']

        saved_path = None
        if save_path:
            try:
                saved_path = save_screenshot(screenshot_png_bytes(capture_result), save_path)
            except (OSError, ValueError) as e:
                return [f"Error saving screenshot: {e}"]

        return build_snapshot_response(
            capture_result, include_ui_details=True, saved_path=saved_path
        )

    @mcp.tool(
        name='Screenshot',
        description="Captures a fast screenshot-first desktop snapshot with cursor position, desktop/window summaries, and an image or local vision JSON output depending on vision_mode. This path skips UI tree extraction for speed. Use Snapshot when you need interactive element ids, scrollable regions, or browser DOM extraction. Set vision_mode='winocr' (ultra-fast native Windows OCR returning JSON text + screen coordinates in ~100ms), vision_mode='llm' (local OpenAI-compatible Vision LLM returning JSON/text), vision_mode='winocr+llm' (both), or vision_mode='image' (returns the image directly). When vision_mode is omitted, the server's configured default is used (WINDOWS_MCP_VISION_MODE or [vision] mode in config.toml; 'winocr' when unset), so pass vision_mode='image' explicitly whenever you need to see the actual image. Set resolution='compact' (default: 720p JPEG 95% quality, or 1080p on 4K+) for fast, low-token vision or resolution='normal' (1080p/1440p lossless PNG) when you need fine visual details. Set display=[0] or display=[0,1] using zero-based active Windows display indices to capture only those monitors. Set region=[left, top, right, bottom] in virtual-desktop pixel coordinates to capture only that rectangle instead of the whole screen/display — useful when you already know which area matters and want to save tokens; region takes precedence over display when both are given, and an invalid or out-of-bounds region raises an error rather than silently capturing something else. Set save_path to an absolute file path (or an existing directory for a timestamped name) to also write the captured screenshot as a lossless PNG file, whatever vision_mode and resolution are used; the saved path is reported in the response and existing files are never overwritten. Note: the returned image may be downscaled for efficiency; when it is, multiply image coordinates by the ratio of original size to displayed size to get the actual screen coordinates for mouse actions (Click, Move, etc.).",
        annotations=ToolAnnotations(
            title="Screenshot",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Screenshot-Tool")
    def _screenshot_tool(
        use_annotation: bool | str = False,
        resolution: Literal["compact", "normal"] = "compact",
        vision_mode: Literal["image", "winocr", "llm", "winocr+llm"] | None = None,
        vision_prompt: str | None = None,
        llm_model: str | None = None,
        width_reference_line: int | None = None,
        height_reference_line: int | None = None,
        display: list[int] | None = None,
        region: list[int] | str | None = None,
        save_path: str | None = None,
        ctx: Context = None,
    ):
        try:
            capture_result = capture_desktop_state(
                get_desktop(),
                use_vision=True,
                use_dom=False,
                use_annotation=_as_bool(use_annotation),
                use_ui_tree=False,
                width_reference_line=width_reference_line,
                height_reference_line=height_reference_line,
                display=display,
                region=_as_region(region),
                tool_name="Screenshot tool",
                resolution=resolution,
                vision_mode=vision_mode,
                vision_prompt=vision_prompt,
                llm_model=llm_model,
            )
        except Exception as e:
            logger.warning(
                "Screenshot failed with display=%s region=%s resolution=%s vision_mode=%s",
                display,
                region,
                resolution if 'resolution' in locals() else None,
                vision_mode if 'vision_mode' in locals() else None,
                exc_info=True,
            )
            return [f'Error capturing screenshot: {str(e)}. Please try again.']

        saved_path = None
        if save_path:
            try:
                saved_path = save_screenshot(screenshot_png_bytes(capture_result), save_path)
            except (OSError, ValueError) as e:
                return [f"Error saving screenshot: {e}"]

        return build_snapshot_response(
            capture_result,
            include_ui_details=False,
            saved_path=saved_path,
            ui_detail_note="UI Tree: Skipped for fast screenshot-only capture. Call Snapshot when you need interactive or scrollable elements.",
        )

    state_tool = _state_tool
    screenshot_tool = _screenshot_tool
