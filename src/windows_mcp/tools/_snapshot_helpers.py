"""Snapshot / Screenshot shared helpers.

These functions were originally defined in ``__main__.py`` and are now shared
by the Snapshot and Screenshot tool modules.
"""

import io
import json
import logging
import os
import time

from typing import Callable, Literal
from fastmcp.utilities.types import Image
from textwrap import dedent
from windows_mcp.desktop.service import Desktop, Size
from windows_mcp.desktop.utils import remove_private_use_chars, repair_surrogates
from windows_mcp.tools._local_vision import (
    _get_vision_settings,
    run_openai_vision_llm,
    run_winocr,
)


logger = logging.getLogger(__name__)

MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 1920, 1080
COMPACT_IMAGE_WIDTH, COMPACT_IMAGE_HEIGHT = 1280, 720
COMPACT_4K_IMAGE_WIDTH, COMPACT_4K_IMAGE_HEIGHT = 1920, 1080
NORMAL_4K_IMAGE_WIDTH, NORMAL_4K_IMAGE_HEIGHT = 2560, 1440
UHD_THRESHOLD_WIDTH, UHD_THRESHOLD_HEIGHT = 3840, 2160
COMPACT_JPEG_QUALITY = 95

ResolutionMode = Literal["compact", "normal"]
VisionMode = Literal["image", "winocr", "llm", "winocr+llm"]


def _normalize_vision_mode(value: str | None) -> VisionMode:
    if value is None or not str(value).strip():
        default_mode = _get_vision_settings().get("mode") or "image"
        value = default_mode
    normalized = str(value).strip().lower()
    if normalized in {"winocr", "ocr"}:
        return "winocr"
    if normalized in {"llm", "vlm", "openai"}:
        return "llm"
    if normalized in {"winocr+llm", "ocr+llm", "both", "hybrid"}:
        return "winocr+llm"
    return "image"


def _normalize_resolution(value: str | None) -> ResolutionMode:
    if value is None:
        return "compact"
    normalized = str(value).strip().lower()
    if normalized in {"normal", "full", "high", "1080p"}:
        return "normal"
    return "compact"


def _resolve_max_image_size(resolution_mode: ResolutionMode, width: int, height: int) -> Size:
    is_4k_or_above = width >= UHD_THRESHOLD_WIDTH or height >= UHD_THRESHOLD_HEIGHT
    if resolution_mode == "compact":
        if is_4k_or_above:
            return Size(width=COMPACT_4K_IMAGE_WIDTH, height=COMPACT_4K_IMAGE_HEIGHT)
        return Size(width=COMPACT_IMAGE_WIDTH, height=COMPACT_IMAGE_HEIGHT)
    if is_4k_or_above:
        return Size(width=NORMAL_4K_IMAGE_WIDTH, height=NORMAL_4K_IMAGE_HEIGHT)
    return Size(width=MAX_IMAGE_WIDTH, height=MAX_IMAGE_HEIGHT)


def _screenshot_scale() -> float:
    value = os.getenv("WINDOWS_MCP_SCREENSHOT_SCALE", "1.0")
    try:
        scale = float(value)
    except ValueError:
        logger.warning("Invalid WINDOWS_MCP_SCREENSHOT_SCALE value %r, using 1.0", value)
        scale = 1.0
    if not (0.1 <= scale <= 1.0):
        logger.warning("WINDOWS_MCP_SCREENSHOT_SCALE %r out of range [0.1, 1.0], clamping", scale)
        scale = max(0.1, min(1.0, scale))
    return scale


def _snapshot_profile_enabled() -> bool:
    value = os.getenv("WINDOWS_MCP_PROFILE_SNAPSHOT", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(value: bool | str) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _as_region(value: list | str | None) -> list | None:
    if value is None or isinstance(value, list):
        return value
    return json.loads(value)


def capture_desktop_state(
    desktop: Desktop,
    *,
    use_vision: bool,
    use_dom: bool,
    use_annotation: bool,
    use_ui_tree: bool,
    width_reference_line: int | None,
    height_reference_line: int | None,
    display: list[int] | None,
    region: list[int] | None,
    tool_name: str,
    resolution: str | None = "compact",
    vision_mode: str | None = None,
    vision_prompt: str | None = None,
    llm_url: str | None = None,
    llm_token: str | None = None,
    llm_model: str | None = None,
):
    profile_enabled = _snapshot_profile_enabled()
    profile_started_at = time.perf_counter()
    stage_started_at = profile_started_at
    desktop_state_ms = 0.0
    metadata_render_ms = 0.0
    screenshot_encode_ms = 0.0

    if use_dom and not use_ui_tree:
        raise ValueError("use_dom=True requires use_ui_tree=True")

    resolved_vision_mode = _normalize_vision_mode(vision_mode)
    # If caller specified winocr/llm vision_mode explicitly, ensure screenshot is captured
    if vision_mode and resolved_vision_mode != "image":
        use_vision = True

    resolution_mode = _normalize_resolution(resolution)
    image_format = "jpeg" if resolution_mode == "compact" else "png"
    max_size_resolver: Callable[[int, int], Size] = (
        lambda w, h: _resolve_max_image_size(resolution_mode, w, h)
    )

    display_indices = Desktop.parse_display_selection(display)

    grid_lines = None
    if width_reference_line and height_reference_line:
        grid_lines = (int(width_reference_line), int(height_reference_line))

    desktop_state = desktop.get_state(
        use_vision=use_vision,
        use_dom=use_dom,
        use_annotation=use_annotation,
        use_ui_tree=use_ui_tree,
        as_bytes=False,
        scale=_screenshot_scale(),
        grid_lines=grid_lines,
        display_indices=display_indices,
        region=region,
        max_image_size=max_size_resolver,
    )
    if profile_enabled:
        desktop_state_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    interactive_elements = desktop_state.tree_state.interactive_elements_to_string()
    scrollable_elements = desktop_state.tree_state.scrollable_elements_to_string()
    semantic_tree = desktop_state.tree_state.semantic_tree_to_string()
    windows = desktop_state.windows_to_string()
    active_window = desktop_state.active_window_to_string()
    active_desktop = desktop_state.active_desktop_to_string()
    all_desktops = desktop_state.desktops_to_string()
    if profile_enabled:
        metadata_render_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    screenshot_bytes = None
    local_vision_data: dict[str, object] | None = None
    if use_vision and desktop_state.screenshot is not None:
        pil_img = desktop_state.screenshot
        scale = desktop_state.screenshot_scale or 1.0
        coord_scale = round(1.0 / scale, 6) if scale > 0 else 1.0
        region_offset = (0, 0)
        if desktop_state.screenshot_region is not None:
            region_offset = (
                int(desktop_state.screenshot_region.left),
                int(desktop_state.screenshot_region.top),
            )

        if resolved_vision_mode in ("image", "llm", "winocr+llm"):
            buffered = io.BytesIO()
            if image_format == "jpeg":
                rgb_img = pil_img.convert("RGB") if pil_img.mode not in ("RGB", "L") else pil_img
                rgb_img.save(buffered, format="JPEG", quality=COMPACT_JPEG_QUALITY, optimize=True)
            else:
                pil_img.save(buffered, format="PNG")
            screenshot_bytes = buffered.getvalue()
            buffered.close()

        if resolved_vision_mode == "winocr":
            local_vision_data = run_winocr(
                pil_img, coord_scale=coord_scale, region_offset=region_offset
            )
            screenshot_bytes = None
        elif resolved_vision_mode == "llm":
            local_vision_data = run_openai_vision_llm(
                screenshot_bytes or b"",
                image_format=image_format,
                prompt=vision_prompt,
                llm_url=llm_url,
                llm_token=llm_token,
                llm_model=llm_model,
                coord_scale=coord_scale,
                region_offset=region_offset,
            )
            screenshot_bytes = None
        elif resolved_vision_mode == "winocr+llm":
            ocr_res = run_winocr(
                pil_img, coord_scale=coord_scale, region_offset=region_offset
            )
            llm_res = run_openai_vision_llm(
                screenshot_bytes or b"",
                image_format=image_format,
                prompt=vision_prompt,
                llm_url=llm_url,
                llm_token=llm_token,
                llm_model=llm_model,
                coord_scale=coord_scale,
                region_offset=region_offset,
            )
            local_vision_data = {
                "backend": "winocr+llm",
                "winocr": ocr_res,
                "llm": llm_res,
            }
            screenshot_bytes = None

    if profile_enabled:
        screenshot_encode_ms = (time.perf_counter() - stage_started_at) * 1000
        logger.info(
            "%s profile: desktop_state_ms=%.1f metadata_render_ms=%.1f encode_ms=%.1f total_ms=%.1f resolution=%s vision_mode=%s use_vision=%s use_dom=%s use_ui_tree=%s use_annotation=%s display=%s",
            tool_name,
            desktop_state_ms,
            metadata_render_ms,
            screenshot_encode_ms,
            (time.perf_counter() - profile_started_at) * 1000,
            resolution_mode,
            resolved_vision_mode,
            use_vision,
            use_dom,
            use_ui_tree,
            use_annotation,
            display,
        )

    return {
        "desktop_state": desktop_state,
        "interactive_elements": interactive_elements,
        "scrollable_elements": scrollable_elements,
        "semantic_tree": semantic_tree,
        "windows": windows,
        "active_window": active_window,
        "active_desktop": active_desktop,
        "all_desktops": all_desktops,
        "screenshot_bytes": screenshot_bytes,
        "screenshot_format": image_format,
        "resolution_mode": resolution_mode,
        "vision_mode": resolved_vision_mode,
        "local_vision_data": local_vision_data,
        "use_ui_tree": use_ui_tree,
    }


def build_snapshot_response(
    capture_result: dict[str, object],
    *,
    include_ui_details: bool,
    ui_detail_note: str | None = None,
):
    desktop_state = capture_result["desktop_state"]
    interactive_elements = capture_result["interactive_elements"]
    scrollable_elements = capture_result["scrollable_elements"]
    semantic_tree = capture_result["semantic_tree"]
    windows = capture_result["windows"]
    active_window = capture_result["active_window"]
    active_desktop = capture_result["active_desktop"]
    all_desktops = capture_result["all_desktops"]
    screenshot_bytes = capture_result["screenshot_bytes"]

    # Some applications (e.g. VS Code) embed Unicode Private Use Area characters in the
    # Automation Element Name property of certain UI elements (e.g. navigation bar items in VS Code).
    # These characters can cause display issues, so we strip them out before rendering.
    interactive_elements = remove_private_use_chars(interactive_elements)
    scrollable_elements = remove_private_use_chars(scrollable_elements)
    semantic_tree = remove_private_use_chars(semantic_tree)

    # Emoji reach us from UIA as raw UTF-16 surrogate pairs. Left alone they make
    # the strict UTF-8 JSON encoder reject the entire response, so every string
    # heading into it is repaired -- window titles included, since an emoji in a
    # window title is enough to take the whole snapshot down.
    interactive_elements = repair_surrogates(interactive_elements)
    scrollable_elements = repair_surrogates(scrollable_elements)
    semantic_tree = repair_surrogates(semantic_tree)
    windows = repair_surrogates(windows)
    active_window = repair_surrogates(active_window)
    active_desktop = repair_surrogates(active_desktop)
    all_desktops = repair_surrogates(all_desktops)

    def display_to_string(display):
        primary = " primary" if display.primary else ""
        return (
            f"{display.index}:{display.device_name} "
            f"{display.bounding_box.xyxy_to_string()}{primary}"
        )

    metadata_text = f"Cursor Position: {desktop_state.cursor_position}\n"
    if desktop_state.screenshot_original_size:
        orig = desktop_state.screenshot_original_size
        scale = desktop_state.screenshot_scale or 1.0
        if scale < 1.0:
            coord_scale = round(1.0 / scale, 6)
            metadata_text += (
                f"Screenshot Original Size: {orig.to_string()}\n"
                f"Screenshot Coordinate Scale: {coord_scale} "
                f"— image pixels are downscaled; multiply every image pixel coordinate by "
                f"{coord_scale} before passing to Click, Move, Scroll, or any loc= argument "
                f"(e.g. image pixel (200, 150) → screen coordinate ({round(200 * coord_scale)}, {round(150 * coord_scale)}))\n"
            )
        else:
            metadata_text += f"Screenshot Size: {orig.to_string()}\n"
    if desktop_state.available_displays:
        metadata_text += "Visible Displays: "
        metadata_text += "; ".join(
            display_to_string(display) for display in desktop_state.available_displays
        )
        metadata_text += "\n"
    if desktop_state.screenshot_displays:
        metadata_text += f"Selected Displays: {','.join(str(index) for index in desktop_state.screenshot_displays)}\n"
    if desktop_state.screenshot_region:
        metadata_text += f"Screenshot Region: {desktop_state.screenshot_region.xyxy_to_string()}\n"
        metadata_text += "Coordinate Space: Virtual desktop coordinates\n"
        if include_ui_details and capture_result["use_ui_tree"]:
            metadata_text += (
                "UI Tree Scope: Region-filtered. Windows and UI elements outside this region "
                "are omitted. Omit region and display to inspect the full desktop.\n"
            )
    if desktop_state.screenshot_backend:
        metadata_text += f"Screenshot Backend: {desktop_state.screenshot_backend}\n"
    if ui_detail_note:
        metadata_text += f"{ui_detail_note}\n"

    response_text = dedent(f'''
    {metadata_text}
    Active Desktop:
    {active_desktop}

    All Desktops:
    {all_desktops}

    Focused Window:
    {active_window}

    Opened Windows:
    {windows}
    ''')
    if include_ui_details:
        response_text += dedent(f'''

    UI Tree:
    {semantic_tree or "No elements found."}''')

    local_vision_data = capture_result.get("local_vision_data")
    if local_vision_data is not None:
        vision_json = json.dumps(local_vision_data, ensure_ascii=False, indent=2)
        vision_json = repair_surrogates(remove_private_use_chars(vision_json))
        response_text += f"\n\nLocal Vision Output ({capture_result.get('vision_mode')}):\n{vision_json}"

    response = [response_text]
    if screenshot_bytes:
        img_format = capture_result.get("screenshot_format", "png")
        response.append(Image(data=screenshot_bytes, format=img_format))
    return response
