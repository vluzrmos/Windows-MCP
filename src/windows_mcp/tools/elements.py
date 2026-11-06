"""Element tools — ElementAction (semantic UIA actions) and Find (targeted search)."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from windows_mcp.desktop.utils import as_bool as _as_bool
from windows_mcp.elements.report import ElementError
from windows_mcp.infrastructure import with_analytics

ElementActionName = Literal[
    "invoke",
    "toggle",
    "check",
    "uncheck",
    "expand",
    "collapse",
    "select",
    "add_to_selection",
    "remove_from_selection",
    "set_value",
    "clear",
    "focus",
    "scroll_into_view",
    "read",
]

_OVERLAY_ACTIONS = frozenset({"invoke", "expand", "select", "toggle", "check", "uncheck"})
_MAX_FIND_LIMIT = 200


def _validate_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("wait_timeout must be a finite number of seconds")
    if value < 0 or value > 30:
        raise ValueError("wait_timeout must be between 0 and 30 seconds")
    return float(value)


def _matches(value: object, expected: str | None, exact: bool) -> bool:
    if expected is None:
        return True
    if value is None:
        return False
    text = str(value).strip().casefold()
    wanted = expected.strip().casefold()
    if exact:
        return text == wanted
    if wanted in text:
        return True
    try:
        from thefuzz import fuzz

        return bool(text) and fuzz.partial_ratio(wanted, text) >= 85
    except Exception:
        return False


def _control_type_matches(node: Any, expected: str | None) -> bool:
    if expected is None:
        return True
    wanted = expected.strip().casefold().removesuffix("control").replace(" ", "")
    aliases = {"textbox": "edit", "textfield": "edit", "textarea": "edit", "searchfield": "edit"}
    wanted = aliases.get(wanted, wanted)
    candidates = {str(getattr(node, "control_type", "")).casefold().replace(" ", "")}
    handle = getattr(node, "element", None)
    if handle is not None:
        candidates.add(
            str(getattr(handle, "control_type_name", "")).casefold().removesuffix("control")
        )
    return wanted in candidates


def _node_state_matches(node: Any, state: dict[str, Any]) -> bool:
    metadata = getattr(node, "metadata", {}) or {}
    for key, expected in state.items():
        if expected is None:
            continue
        if key == "focused" and bool(metadata.get("has_focused")) != expected:
            return False
        if key == "toggle" and metadata.get("toggle_state") != expected:
            return False
        if key == "expanded":
            observed = metadata.get("expand_collapse_state")
            if (observed == "expanded") != expected:
                return False
        if key == "selected" and bool(metadata.get("is_selected")) != expected:
            return False
    return True


def _format_match(node: Any) -> str:
    from windows_mcp.tree.views import _node_meta_str

    kind = str(getattr(node, "control_type", "")).lower()
    name = getattr(node, "name", "")
    center = getattr(node, "center", None)
    coords = f"({center.x},{center.y})" if center is not None else "(?)"
    handle = getattr(node, "element", None)
    extra = ""
    automation_id = getattr(handle, "automation_id", "") if handle is not None else ""
    if automation_id:
        extra += f"  [automation_id:{automation_id}]"
    cut = (getattr(node, "metadata", {}) or {}).get("children_cut")
    if cut:
        extra += f"  [+{cut} children not expanded]"
    meta = _node_meta_str(getattr(node, "metadata", {}) or {})
    window = getattr(node, "window_name", "")
    return f'#{node.label} {coords} {kind} "{name}"{meta}{extra}  — window "{window}"'


def register(
    mcp: Any,
    *,
    get_desktop: Callable[[], Any],
    get_analytics: Callable[[], Any],
) -> None:
    @mcp.tool(
        name="ElementAction",
        description=(
            "Acts on a UI element by label (#N from Snapshot or Find) through UI Automation "
            "patterns instead of the mouse/keyboard: no cursor movement, no focus stealing, works "
            "on background or covered windows. Actions: invoke (primary activation — Invoke, then "
            "Toggle/Select/ExpandCollapse/default action), toggle, check, uncheck, expand, "
            "collapse, select, add_to_selection, remove_from_selection, set_value (value=text or "
            "number), clear, focus, scroll_into_view, read (returns the element's live state: "
            "name, value, toggle, expanded, selected, enabled, bounds). The label is verified live "
            "first (STALE_REF when the element is gone or changed — take a new Snapshot). "
            "check/uncheck/expand/collapse are idempotent (already in state → not_delivered). State "
            "changes are read back: delivered_verified when the effect is confirmed. The result "
            "always reports delivery and retry: repeat an action only when retry is 'safe'. "
            "Controls that only react to real pointer/keyboard events may ignore these actions; "
            "use Click/Type for them."
        ),
        annotations=ToolAnnotations(
            title="ElementAction",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "ElementAction-Tool", run_sync_in_thread=False)
    def element_action_tool(
        label: int,
        action: ElementActionName,
        value: str | None = None,
        snapshot_id: str | None = None,
        wait_timeout: float = 3.0,
        ctx: Context = None,
    ) -> str:
        from windows_mcp.desktop.surfaces import describe_new_overlays, overlay_snapshot
        from windows_mcp.elements.actionability import wait_until_actionable
        from windows_mcp.elements.resolve import require_element
        from windows_mcp.elements.safety import input_block_reason_for_pid
        from windows_mcp.elements.semantic import SEMANTIC_ACTIONS, perform

        desktop = get_desktop()
        wait_timeout = _validate_timeout(wait_timeout)
        normalized = str(action).strip().lower().replace("-", "_")
        if normalized not in SEMANTIC_ACTIONS:
            raise ValueError(f"action must be one of: {', '.join(SEMANTIC_ACTIONS)}")
        if value is not None and not isinstance(value, str):
            value = str(value)
        _node, handle = require_element(desktop, label, snapshot_id)
        target = f"#{label} {handle.describe()}"
        if normalized != "read":
            # Semantic writes need a live, stable, enabled element; visibility and hit
            # testing do not apply because no pointer is involved.
            wait_until_actionable(handle, pointer=False, timeout=wait_timeout, label=label)
            reason = input_block_reason_for_pid(handle.process_id)
            if reason:
                raise ElementError("PERM_DENIED", reason.removeprefix("PERM_DENIED: "))
        before = overlay_snapshot() if normalized in _OVERLAY_ACTIONS else None
        report = perform(handle, normalized, value=value, target=target)
        text = report.to_text()
        if before is not None and report.delivery != "not_delivered":
            opened = describe_new_overlays(before)
            if opened:
                text += "\n" + opened
        return text

    @mcp.tool(
        name="Find",
        description=(
            "Finds UI elements by name, control_type, automation_id, value or text and returns "
            "only the matches, each with its label (#N), coordinates, state and window — much "
            "cheaper than reading a whole Snapshot when you know what you are looking for. Takes "
            "a fresh capture (it becomes the latest snapshot, so its labels replace earlier ones). "
            "window_name restricts the search to windows whose title contains it (faster, and "
            "avoids the element budget on big desktops); root_label (+ snapshot_id) searches "
            "only under one element of the previous capture. Matching is case-insensitive and "
            "fuzzy unless exact=True. State filters: focused, toggle ('on'/'off'), expanded, "
            "selected. Use limit=2 before a destructive action to confirm the match is unique. "
            "When a control_type matches nothing, the control types that were present are listed."
        ),
        annotations=ToolAnnotations(
            title="Find",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @with_analytics(get_analytics(), "Find-Tool", run_sync_in_thread=False)
    def find_tool(
        name: str | None = None,
        control_type: str | None = None,
        automation_id: str | None = None,
        value: str | None = None,
        text: str | None = None,
        window_name: str | None = None,
        exact: bool | str = False,
        focused: bool | str | None = None,
        toggle: Literal["on", "off"] | None = None,
        expanded: bool | str | None = None,
        selected: bool | str | None = None,
        limit: int = 20,
        use_dom: bool | str = False,
        root_label: int | None = None,
        snapshot_id: str | None = None,
        ctx: Context = None,
    ) -> str:
        from windows_mcp.tools.input import _window_scope

        desktop = get_desktop()
        exact = _as_bool(exact, "exact")
        use_dom = _as_bool(use_dom, "use_dom")
        state = {
            "focused": None if focused is None else _as_bool(focused, "focused"),
            "toggle": toggle,
            "expanded": None if expanded is None else _as_bool(expanded, "expanded"),
            "selected": None if selected is None else _as_bool(selected, "selected"),
        }
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _MAX_FIND_LIMIT
        ):
            raise ValueError(f"limit must be an integer between 1 and {_MAX_FIND_LIMIT}")
        if not any(
            value_ is not None
            for value_ in (name, control_type, automation_id, value, text, *state.values())
        ):
            raise ValueError(
                "Provide at least one filter: name, control_type, automation_id, value, text, "
                "focused, toggle, expanded or selected."
            )

        options: dict[str, Any] = {}
        scope_note = ""
        if root_label is not None:
            from windows_mcp.elements.resolve import require_element

            _node, handle = require_element(desktop, root_label, snapshot_id)
            options["root_element"] = handle
            scope_note = f" under #{root_label} {handle.describe()}"
        elif window_name:
            handles = _window_scope(desktop, window_name)
            if handles:
                options["only_handles"] = handles
                scope_note = f" in windows matching {window_name!r}"
        desktop_state = desktop.get_state(
            use_vision=False, use_dom=use_dom, use_ui_tree=True, use_annotation=False, **options
        )
        tree_state = desktop_state.tree_state
        nodes = tree_state.labelled_nodes()
        matches = []
        for node in nodes:
            if window_name and not _matches(getattr(node, "window_name", ""), window_name, False):
                continue
            if not _control_type_matches(node, control_type):
                continue
            if not _matches(getattr(node, "name", ""), name, exact):
                continue
            handle = getattr(node, "element", None)
            if automation_id is not None and not _matches(
                getattr(handle, "automation_id", None), automation_id, True
            ):
                continue
            metadata = getattr(node, "metadata", {}) or {}
            if value is not None and not _matches(metadata.get("value"), value, exact):
                continue
            if text is not None and not any(
                _matches(candidate, text, False)
                for candidate in (
                    getattr(node, "name", ""),
                    metadata.get("value"),
                    metadata.get("help_text"),
                )
            ):
                continue
            if not _node_state_matches(node, state):
                continue
            matches.append(node)

        header = f"Snapshot ID: {tree_state.snapshot_id}"
        if tree_state.truncated:
            header += (
                f"\nNote: the capture hit the {tree_state.element_limit}-element limit; narrow it "
                "with window_name or root_label if an expected element is missing."
            )
        if not matches:
            lines = [header, f"No element matched{scope_note}."]
            if control_type is not None:
                present = sorted(
                    {str(getattr(n, "control_type", "")).lower() for n in nodes} - {""}
                )
                lines.append("control types present: " + (", ".join(present) or "none"))
            return "\n".join(lines)
        shown = matches[:limit]
        lines = [
            header,
            f"{len(matches)} match(es){scope_note}"
            + (f", showing {limit}" if len(matches) > limit else "")
            + ":",
        ]
        lines.extend(_format_match(node) for node in shown)
        return "\n".join(lines)
