"""Input tools — Click, Type, Scroll, Move, Shortcut, Wait, WaitFor."""

import json
import math
import re
import time
from collections.abc import Callable, Iterator
from typing import Any, Literal

from mcp.types import ToolAnnotations
from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.elements.report import ElementError, disposition_text
from windows_mcp.infrastructure import with_analytics
from fastmcp import Context


WaitForCondition = Literal[
    "text_exists",
    "text_gone",
    "active_window",
    "element_exists",
    "element_gone",
    "element_enabled",
    "focused_element",
    "element_value",
    "element_actionable",
    "element_stale",
    "window_opened",
    "window_closed",
    "menu_open",
    "menu_closed",
    "dialog_open",
    "dialog_closed",
    "process_started",
    "process_exited",
]

_TREE_CONDITIONS = frozenset(
    {
        "text_exists",
        "text_gone",
        "active_window",
        "element_exists",
        "element_gone",
        "element_enabled",
        "focused_element",
    }
)
_LABEL_CONDITIONS = frozenset({"element_value", "element_actionable", "element_stale"})
_TEXT_REQUIRED = frozenset(
    {"text_exists", "text_gone", "window_opened", "window_closed", "process_started", "process_exited"}
)

DEFAULT_WAIT_TIMEOUT = 3.0


def _resolve_label(desktop: Any, label: int) -> list[int]:
    """Resolve a UI element label to its captured screen coordinates (no verification)."""
    if desktop.desktop_state is None:
        raise ValueError("Desktop state is empty. Please call Snapshot first.")
    try:
        return list(desktop.get_coordinates_from_label(label))
    except Exception as e:
        raise ValueError(f"Failed to find element with label {label}: {e}")


def _validate_wait_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("wait_timeout must be a finite number of seconds")
    if value < 0 or value > 30:
        raise ValueError("wait_timeout must be between 0 and 30 seconds")
    return float(value)


def _resolve_target(
    desktop: Any,
    label: int,
    *,
    snapshot_id: str | None,
    wait_timeout: float,
    pointer: bool = True,
):
    """Verified, actionable target for *label* (see windows_mcp.elements.resolve)."""
    from windows_mcp.elements.resolve import resolve_label

    if getattr(desktop, "desktop_state", None) is None:
        raise ValueError("Desktop state is empty. Please call Snapshot first.")
    return resolve_label(
        desktop,
        label,
        snapshot_id=snapshot_id,
        pointer=pointer,
        wait_timeout=wait_timeout,
    )


def _check_point_integrity(x: int, y: int) -> None:
    from windows_mcp.elements.safety import input_block_reason_at

    reason = input_block_reason_at(x, y)
    if reason:
        raise PermissionError(reason)


def _validate_finite_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")


def _as_loc(value: list | str | None) -> list | None:
    """Coerce a JSON-stringified list back to a list.

    Claude Desktop strips anyOf schemas and the model serializes lists as
    strings (e.g. '[100, 200]'). Parsing here keeps the tools working.
    """
    if value is None or isinstance(value, list):
        return value
    return json.loads(value)


# One optional leading sign followed by decimal digits — exactly what int()
# accepts. `.lstrip("+-").isdigit()` used to strip *any* run of signs, so
# "+-5"/"--5" passed the check and then int() raised a confusing low-level
# error instead of the intended message; `.isdigit()` also accepts digits
# int() rejects (e.g. the superscript "²").
_SIGNED_INT_RE = re.compile(r"^[+-]?\d+$")


def _as_point(value: object, name: str) -> list[int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{name} must be a list of exactly 2 integers [x, y]")
    parsed = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{name} must contain integers, not booleans")
        if isinstance(item, int):
            parsed.append(item)
            continue
        if isinstance(item, str):
            stripped = item.strip()
            if _SIGNED_INT_RE.match(stripped):
                parsed.append(int(stripped))
                continue
        raise ValueError(f"{name} must contain exactly 2 integers")
    return parsed


def _text_matches(value: object | None, expected: str | None) -> bool:
    if expected is None:
        return True
    if value is None:
        return False
    return expected.casefold() in str(value).casefold()


def _metadata_text_matches(metadata: dict[str, object], expected: str | None) -> bool:
    return any(_text_matches(value, expected) for value in metadata.values())


def _iter_nodes(desktop_state: Any) -> Iterator[Any]:
    tree_state = getattr(desktop_state, "tree_state", None)
    if tree_state is None:
        return
    yield from getattr(tree_state, "interactive_nodes", [])
    yield from getattr(tree_state, "scrollable_nodes", [])


def _iter_text_sources(desktop_state: Any) -> Iterator[object]:
    active_window = getattr(desktop_state, "active_window", None)
    if active_window is not None:
        yield active_window.name

    for window in getattr(desktop_state, "windows", []):
        yield window.name

    tree_state = getattr(desktop_state, "tree_state", None)
    if tree_state is None:
        return

    for node in _iter_nodes(desktop_state):
        yield node.name
        yield node.control_type
        yield node.window_name
        for value in getattr(node, "metadata", {}).values():
            yield value

    for node in getattr(tree_state, "dom_informative_nodes", []):
        yield getattr(node, "text", "")


def _node_matches(node: Any, text: str | None, window_name: str | None) -> bool:
    metadata: dict[str, object] = getattr(node, "metadata", {})
    return (
        _text_matches(getattr(node, "name", ""), text)
        or _text_matches(getattr(node, "control_type", ""), text)
        or _metadata_text_matches(metadata, text)
    ) and _text_matches(getattr(node, "window_name", ""), window_name)


def _matches_wait_condition(
    desktop_state: Any,
    condition: WaitForCondition,
    text: str | None,
    window_name: str | None,
) -> tuple[bool, str]:
    if condition in ("text_exists", "text_gone"):
        found = any(_text_matches(source, text) for source in _iter_text_sources(desktop_state))
        if condition == "text_exists":
            return (True, f"text {text!r} appeared") if found else (False, f"text {text!r} was absent")
        return (True, f"text {text!r} is gone") if not found else (False, f"text {text!r} is still shown")

    if condition == "active_window":
        expected = window_name or text
        active_window = getattr(desktop_state, "active_window", None)
        active_name = active_window.name if active_window else ""
        if _text_matches(active_name, expected):
            return True, f"active window matched {active_name!r}"
        return False, f"active window was {active_name!r}"

    if condition in {"element_exists", "element_enabled", "element_gone"}:
        for node in _iter_nodes(desktop_state):
            if _node_matches(node, text, window_name):
                if condition == "element_gone":
                    return False, f"element {getattr(node, 'name', '')!r} is still present"
                return True, f"element matched {getattr(node, 'name', '')!r}"
        if condition == "element_gone":
            return True, "matching element is gone"
        return False, "matching element was absent"

    if condition == "focused_element":
        for node in _iter_nodes(desktop_state):
            metadata = getattr(node, "metadata", {})
            if metadata.get("has_focused") and _node_matches(node, text, window_name):
                return True, f"focused element matched {getattr(node, 'name', '')!r}"
        return False, "matching focused element was absent"

    raise ValueError(f"Unsupported WaitFor condition: {condition}")


_CONDITION_ALIASES = {
    "text": "text_exists",
    "window": "active_window",
    "element": "element_exists",
    "enabled": "element_enabled",
    "focused": "focused_element",
    "gone": "element_gone",
    "value": "element_value",
    "actionable": "element_actionable",
    "stale": "element_stale",
    "menu": "menu_open",
    "dialog": "dialog_open",
}


def _validate_wait_for_args(
    condition: str,
    text: str | None,
    window_name: str | None,
    timeout: float,
    interval: float,
    label: int | None = None,
) -> WaitForCondition:
    _validate_finite_number(timeout, "timeout")
    _validate_finite_number(interval, "interval")

    normalized = condition.strip().lower().replace("-", "_")
    normalized = _CONDITION_ALIASES.get(normalized, normalized)
    valid_conditions = list(WaitForCondition.__args__)
    if normalized not in valid_conditions:
        raise ValueError("condition must be one of: " + ", ".join(valid_conditions))

    if timeout <= 0 or timeout > 120:
        raise ValueError("timeout must be greater than 0 and at most 120 seconds")
    if interval <= 0 or interval > 5:
        raise ValueError("interval must be greater than 0 and at most 5 seconds")

    if normalized in _TEXT_REQUIRED and not text:
        raise ValueError(f"text is required when condition is {normalized}")
    if normalized == "active_window" and not (text or window_name):
        raise ValueError("text or window_name is required when condition is active_window")
    if normalized in {"element_exists", "element_enabled", "element_gone"} and not (
        text or window_name
    ):
        raise ValueError(
            "text or window_name is required when condition is element_exists, "
            "element_enabled or element_gone"
        )
    if normalized in _LABEL_CONDITIONS and label is None:
        raise ValueError(f"label is required when condition is {normalized}")
    if normalized == "element_value" and text is None:
        raise ValueError("text is required when condition is element_value")

    return normalized


def _window_scope(desktop: Any, window_name: str | None) -> list[int] | None:
    """Top-level handles whose title contains *window_name*, to scope a tree capture."""
    if not window_name:
        return None
    try:
        from windows_mcp.desktop.surfaces import _visible_top_level_windows

        handles = [
            hwnd
            for hwnd, _cls, title in _visible_top_level_windows()
            if title and window_name.casefold() in title.casefold()
        ]
    except Exception:
        return None
    return handles or None


def _overlay_before() -> Any:
    from windows_mcp.desktop.surfaces import overlay_snapshot

    return overlay_snapshot()


def _overlay_note(before: Any) -> str | None:
    from windows_mcp.desktop.surfaces import describe_new_overlays

    return describe_new_overlays(before)


def register(
    mcp: Any,
    *,
    get_desktop: Callable[[], Any],
    get_analytics: Callable[[], Any],
) -> None:
    @mcp.tool(
        name="Click",
        description=(
            "Performs mouse clicks at specified coordinates [x, y] or passing a UI element's label "
            "(the #N shown in the Snapshot UI tree). Supports button types: 'left' for "
            "selection/activation, 'right' for context menus, 'middle'. Supports clicks: 0=hover "
            "only (no click), 1=single click (select/focus), 2=double click (open/activate). "
            "Provide either loc or label. A label is verified live before the click: the element "
            "must still exist and be the same element (else STALE_REF — take a new Snapshot), and "
            "the click waits up to wait_timeout seconds for it to become visible, stable, enabled "
            "and uncovered, scrolling it into view when needed. Pass snapshot_id to reject a label "
            "from an older capture. mode='physical' (default) moves the real mouse; "
            "mode='semantic' activates the element through UI Automation (Invoke/Toggle/Select/"
            "Expand) without moving the mouse or taking focus — works on background or covered "
            "windows, but some web/custom controls ignore it; mode='auto' uses semantic for a "
            "plain left click when the element supports it and falls back to physical. The result "
            "ends with a delivery disposition: repeat a click only when retry is 'safe'."
        ),
        annotations=ToolAnnotations(
            title="Click",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Click-Tool", run_sync_in_thread=False)
    def click_tool(
        loc: list[int] | str | None = None,
        label: int | None = None,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        mode: Literal["physical", "semantic", "auto"] = "physical",
        snapshot_id: str | None = None,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        wait_timeout = _validate_wait_timeout(wait_timeout)
        if mode not in ("physical", "semantic", "auto"):
            raise ValueError("mode must be one of: physical, semantic, auto")
        if loc is None and label is None:
            raise ValueError("Either loc or label must be provided.")

        if mode in ("semantic", "auto") and label is not None:
            semantic_result = _semantic_click(
                desktop, label, button, clicks, mode, snapshot_id
            )
            if semantic_result is not None:
                return semantic_result
        elif mode == "semantic":
            raise ValueError("mode='semantic' requires a label (coordinates have no element).")

        notes: list[str] = []
        if label is not None:
            target = _resolve_target(
                desktop, label, snapshot_id=snapshot_id, wait_timeout=wait_timeout
            )
            loc = list(target.point)
            notes.append(f"{target.describe()} — {target.note()}")
        if len(loc) != 2:
            raise ValueError("Location must be a list of exactly 2 integers [x, y]")
        x, y = loc[0], loc[1]
        if label is None:
            _check_point_integrity(x, y)
        before = _overlay_before() if clicks else None
        desktop.click(loc=loc, button=button, clicks=clicks)
        num_clicks = {0: "Hover", 1: "Single", 2: "Double"}
        result = f"{num_clicks.get(clicks)} {button} clicked at ({x},{y})."
        if clicks:
            opened = _overlay_note(before)
            if opened:
                notes.append(opened)
        notes.append(
            "disposition: "
            + disposition_text("delivered_unverified" if clicks else "not_delivered")
            + (" — check the UI for the effect" if clicks else "")
        )
        return "\n".join([result, *notes])

    def _semantic_click(
        desktop: Any,
        label: int,
        button: str,
        clicks: int,
        mode: str,
        snapshot_id: str | None,
    ) -> str | None:
        """Semantic activation; None means 'fall back to physical' (auto mode only)."""
        from windows_mcp.elements.resolve import element_of, lookup_node
        from windows_mcp.elements.semantic import perform

        supported = button == "left" and clicks in (1, 2)
        if not supported:
            if mode == "semantic":
                raise ElementError(
                    "ACTION_NOT_SUPPORTED",
                    f"mode='semantic' supports only left single/double clicks, not "
                    f"button={button!r} clicks={clicks}.",
                    hint="Use mode='physical' for hover, right and middle clicks.",
                )
            return None
        node = lookup_node(desktop, label, snapshot_id)
        handle = element_of(node)
        if handle is None:
            if mode == "semantic":
                raise ElementError(
                    "ACTION_NOT_SUPPORTED",
                    f"label {label} has no UI Automation element, so it cannot be clicked "
                    "semantically.",
                    hint="Use mode='physical'.",
                )
            return None
        from windows_mcp.elements.actionability import evaluate

        check = evaluate(handle, pointer=False, label=label)
        if not check.actionable:
            raise ElementError(
                "NOT_ACTIONABLE", f"label {label} ({handle.describe()}): {check.summary()}."
            )
        before = _overlay_before()
        try:
            report = perform(handle, "invoke", target=f"#{label} {handle.describe()}")
        except ElementError as error:
            if mode == "auto" and error.code == "ACTION_NOT_SUPPORTED":
                return None
            raise
        lines = [f"Semantic click: {report.to_text()}"]
        opened = _overlay_note(before)
        if opened:
            lines.append(opened)
        return "\n".join(lines)

    @mcp.tool(
        name="Type",
        description=(
            "Types text at specified coordinates [x, y] or passing a UI element's label (#N from "
            "Snapshot). Set clear=True to clear existing text first, False to append. Set "
            "press_enter=True to submit after typing. Set caret_position to 'start' (beginning), "
            "'end' (end), or 'idle' (default). Provide either loc or label. clear and press_enter "
            "accept only true or false. An empty text types nothing, so combine it with "
            "clear=True to just empty a field. A label is verified live and auto-waited like in "
            "Click (wait_timeout, snapshot_id). mode='semantic' writes the value through UI "
            "Automation (ValuePattern) without focusing the window or sending keys, then reads it "
            "back (delivered_verified when it matches); it cannot press Enter, and apps that "
            "validate on key events may not notice the change — use mode='physical' (default) "
            "there."
        ),
        annotations=ToolAnnotations(
            title="Type",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Type-Tool", run_sync_in_thread=False)
    def type_tool(
        text: str,
        loc: list[int] | str | None = None,
        label: int | None = None,
        clear: bool | str = False,
        caret_position: Literal["start", "idle", "end"] = "idle",
        press_enter: bool | str = False,
        mode: Literal["physical", "semantic"] = "physical",
        snapshot_id: str | None = None,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        clear = _as_bool(clear, "clear")
        press_enter = _as_bool(press_enter, "press_enter")
        wait_timeout = _validate_wait_timeout(wait_timeout)
        if mode not in ("physical", "semantic"):
            raise ValueError("mode must be one of: physical, semantic")
        if loc is None and label is None:
            raise ValueError("Either loc or label must be provided.")
        if mode == "semantic":
            return _semantic_type(desktop, text, label, clear, caret_position, press_enter, snapshot_id)

        notes: list[str] = []
        if label is not None:
            target = _resolve_target(
                desktop, label, snapshot_id=snapshot_id, wait_timeout=wait_timeout
            )
            loc = list(target.point)
            notes.append(f"{target.describe()} — {target.note()}")
        if len(loc) != 2:
            raise ValueError("Location must be a list of exactly 2 integers [x, y]")
        x, y = loc[0], loc[1]
        if label is None:
            _check_point_integrity(x, y)
        desktop.type(
            loc=loc,
            text=text,
            caret_position=caret_position,
            clear=clear,
            press_enter=press_enter,
        )
        notes.append("disposition: " + disposition_text("delivered_unverified"))
        return "\n".join([f"Typed {text} at ({x},{y}).", *notes])

    def _semantic_type(
        desktop: Any,
        text: str,
        label: int | None,
        clear: bool,
        caret_position: str,
        press_enter: bool,
        snapshot_id: str | None,
    ) -> str:
        from windows_mcp.elements.resolve import require_element
        from windows_mcp.elements.semantic import perform, read_state

        if label is None:
            raise ValueError("mode='semantic' requires a label (coordinates have no element).")
        if press_enter:
            raise ElementError(
                "ACTION_NOT_SUPPORTED",
                "mode='semantic' cannot press Enter (that is a key event).",
                hint="Use mode='physical', or follow up with Shortcut('enter').",
            )
        _node, handle = require_element(desktop, label, snapshot_id)
        value = text
        if not clear and text:
            current = read_state(handle).get("value")
            if isinstance(current, str) and current and current != "<secure>":
                value = text + current if caret_position == "start" else current + text
        report = perform(handle, "set_value", value=value, target=f"#{label} {handle.describe()}")
        return f"Semantic type: {report.to_text()}"

    @mcp.tool(
        name="Scroll",
        description="Scrolls at coordinates [x, y], a UI element's label (#N from Snapshot), or current mouse position if loc=None. Type: vertical (default) or horizontal. Direction: up/down for vertical, left/right for horizontal. wheel_times controls amount (1 wheel ≈ 3-5 lines). Use for navigating long content, lists, and web pages. A label is verified live before scrolling (STALE_REF when the element is gone; pass snapshot_id to pin the capture).",
        annotations=ToolAnnotations(
            title="Scroll",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Scroll-Tool", run_sync_in_thread=False)
    def scroll_tool(
        loc: list[int] | str | None = None,
        label: int | None = None,
        type: Literal["horizontal", "vertical"] = "vertical",
        direction: Literal["up", "down", "left", "right"] = "down",
        wheel_times: int = 1,
        snapshot_id: str | None = None,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        wait_timeout = _validate_wait_timeout(wait_timeout)
        if label is not None:
            loc = list(
                _resolve_target(
                    desktop, label, snapshot_id=snapshot_id, wait_timeout=wait_timeout
                ).point
            )
        if loc and len(loc) != 2:
            raise ValueError("Location must be a list of exactly 2 integers [x, y]")
        response = desktop.scroll(loc, type, direction, wheel_times)
        if response:
            return response
        return (
            f"Scrolled {type} {direction} by {wheel_times} wheel times"
            + f" at ({loc[0]},{loc[1]})."
            if loc
            else ""
        )

    @mcp.tool(
        name="Move",
        description=(
            "Moves mouse cursor to coordinates [x, y] or passing a UI element's label (#N from "
            "Snapshot, verified live like in Click). "
            "Set drag=True to perform a drag-and-drop operation from the current mouse position "
            "to the target coordinates, or provide from_loc=[x, y] to make the drag explicit-start "
            "and atomic in one tool call. Optional duration controls bounded intermediate movement. "
            "Default (drag=False) is a simple cursor move (hover). "
            "Drag uses synthesized mouse input, which XAML shell surfaces such as Start menu "
            "tiles and pinned taskbar buttons ignore: there the drag does nothing even though "
            "the call succeeds, so use their context-menu commands (for example Move to front) "
            "instead and check the result with a Screenshot. "
            "Provide either loc or label."
        ),
        annotations=ToolAnnotations(
            title="Move",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Move-Tool", run_sync_in_thread=False)
    def move_tool(
        loc: list[int] | str | None = None,
        label: int | None = None,
        drag: bool | str = False,
        from_loc: list[int] | str | None = None,
        duration: float | int | str | None = None,
        snapshot_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        desktop = get_desktop()
        loc = _as_loc(loc)
        from_loc = _as_loc(from_loc)
        drag = _as_bool(drag, "drag")
        if loc is None and label is None:
            raise ValueError("Either loc or label must be provided.")
        if label is not None:
            loc = list(
                _resolve_target(
                    desktop, label, snapshot_id=snapshot_id, wait_timeout=DEFAULT_WAIT_TIMEOUT
                ).point
            )
        if not isinstance(loc, list) or len(loc) != 2:
            raise ValueError("loc must be a list of exactly 2 integers [x, y]")
        if from_loc is not None and (not isinstance(from_loc, list) or len(from_loc) != 2):
            raise ValueError("from_loc must be a list of exactly 2 integers [x, y]")
        has_drag_only_options = any(
            value is not None
            for value in (
                from_loc,
                duration,
            )
        )
        if has_drag_only_options and not drag:
            raise ValueError("from_loc and duration require drag=True")
        if drag:
            loc = _as_point(loc, "loc")
            if from_loc is not None:
                from_loc = _as_point(from_loc, "from_loc")
        x, y = loc[0], loc[1]
        if drag:
            result = desktop.drag(
                loc,
                from_loc=from_loc,
                duration=duration,
            )
            start_x, start_y = result["start"]
            effective_duration = result["duration"]
            if effective_duration is None:
                return f"Dragged from ({start_x},{start_y}) to ({x},{y})."
            return (
                f"Dragged from ({start_x},{start_y}) to ({x},{y}) "
                f"over {effective_duration:.3f} seconds."
            )
        else:
            desktop.move(loc)
            return f"Moved the mouse pointer to ({x},{y})."

    @mcp.tool(
        name="Shortcut",
        description=(
            'Executes keyboard shortcuts using key combinations separated by +. Examples: "ctrl+c" '
            '(copy), "ctrl+v" (paste), "ctrl+s" (save), "win" (Start menu), "enter", "esc". Use '
            "for quick actions and system commands. Shortcuts that take the foreground away or "
            "change the session — alt+f4, win+l, win+d, alt+tab, win+r, win+x, win+e, "
            "ctrl+shift+esc and any combo that adds modifiers to them — are refused unless "
            "force=True; prefer App(mode='switch') over alt+tab."
        ),
        annotations=ToolAnnotations(
            title="Shortcut",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Shortcut-Tool")
    def shortcut_tool(shortcut: str, force: bool | str = False, ctx: Context = None):
        from windows_mcp.elements.safety import blocked_shortcut_match

        force = _as_bool(force, "force")
        blocked = blocked_shortcut_match(shortcut)
        if blocked and not force:
            raise ElementError(
                "POLICY_DENIED",
                f"{shortcut!r} matches the blocked shortcut {blocked!r}: it takes the foreground "
                "away from the run or changes the session (closing, locking, switching or "
                "raising a shell surface).",
                hint="Pass force=True only when this is really intended; use App(mode='switch') "
                "to change windows.",
            )
        get_desktop().shortcut(shortcut)
        return f"Pressed {shortcut}."

    @mcp.tool(
        name="Wait",
        description="Pauses execution for specified duration in seconds. Use when waiting for: applications to launch/load, UI animations to complete, page content to render, dialogs to appear, or between rapid actions. Prefer WaitFor, which returns as soon as a condition holds instead of guessing a duration.",
        annotations=ToolAnnotations(
            title="Wait",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Wait-Tool")
    def wait_tool(duration: int, ctx: Context = None) -> str:
        time.sleep(duration)
        return f"Waited for {duration} seconds."

    @mcp.tool(
        name="WaitFor",
        description=(
            "Waits until a UI condition is satisfied, polling inside the tool to avoid repeated "
            "Snapshot calls. Tree conditions (re-read the accessibility tree; window_name scopes "
            "the read to matching windows): text_exists, text_gone, active_window, "
            "element_exists, element_gone (e.g. a spinner disappearing), element_enabled, "
            "focused_element. Live conditions (cheap; do not replace the latest Snapshot's "
            "labels): window_opened / window_closed (text = title substring), menu_open / "
            "menu_closed and dialog_open / dialog_closed (optional text = title), process_started "
            "/ process_exited (text = image name or PID), and for a Snapshot label: element_value "
            "(label's value contains text), element_actionable (visible, stable, enabled, "
            "uncovered), element_stale (the element is gone). Set use_dom=True for browser DOM "
            "text. On timeout the error reports what was last observed."
        ),
        annotations=ToolAnnotations(
            title="WaitFor",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "WaitFor-Tool", run_sync_in_thread=False)
    def wait_for_tool(
        condition: str,
        text: str | None = None,
        window_name: str | None = None,
        timeout: float = 10.0,
        interval: float = 0.25,
        use_dom: bool | str = False,
        label: int | None = None,
        snapshot_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        normalized = _validate_wait_for_args(
            condition=condition,
            text=text,
            window_name=window_name,
            timeout=timeout,
            interval=interval,
            label=label,
        )
        desktop = get_desktop()
        use_dom_bool = _as_bool(use_dom, "use_dom")
        check = _wait_checker(desktop, normalized, text, window_name, use_dom_bool, label, snapshot_id)
        started_at = time.monotonic()
        deadline = started_at + timeout
        attempts = 0
        last_detail = "condition was not evaluated"

        while True:
            attempts += 1
            matched, last_detail = check()
            if matched:
                elapsed = time.monotonic() - started_at
                return (
                    f"WaitFor condition '{normalized}' satisfied after "
                    f"{elapsed:.2f}s and {attempts} attempt(s): {last_detail}."
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Timed out after {timeout:.2f}s waiting for '{normalized}': {last_detail}."
                )
            time.sleep(min(interval, remaining))

    def _wait_checker(
        desktop: Any,
        condition: str,
        text: str | None,
        window_name: str | None,
        use_dom: bool,
        label: int | None,
        snapshot_id: str | None,
    ) -> Callable[[], tuple[bool, str]]:
        from windows_mcp.elements import waits

        if condition in _TREE_CONDITIONS:
            scope = (
                _window_scope(desktop, window_name)
                if condition in {"element_exists", "element_enabled", "element_gone", "focused_element"}
                else None
            )

            def check_tree() -> tuple[bool, str]:
                kwargs: dict[str, Any] = dict(
                    use_vision=False, use_dom=use_dom, use_ui_tree=True, use_annotation=False
                )
                if scope is not None:
                    kwargs["only_handles"] = scope
                desktop_state = desktop.get_state(**kwargs)
                return _matches_wait_condition(
                    desktop_state=desktop_state,
                    condition=condition,
                    text=text,
                    window_name=window_name,
                )

            return check_tree
        if condition in waits.WINDOW_CONDITIONS:
            return lambda: waits.check_window(condition, text or "")
        if condition in waits.SURFACE_CONDITIONS:
            return lambda: waits.check_surface(condition, text)
        if condition in waits.PROCESS_CONDITIONS:
            return lambda: waits.check_process(condition, text or "")
        from windows_mcp.elements.resolve import require_element

        _node, handle = require_element(desktop, label, snapshot_id)

        def check_element() -> tuple[bool, str]:
            try:
                return waits.check_element(condition, handle, label, text)
            except ElementError as error:
                if error.code == "TIMEOUT":
                    return False, str(error)
                raise

        return check_element
