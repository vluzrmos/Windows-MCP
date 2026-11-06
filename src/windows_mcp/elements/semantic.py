"""Semantic UI Automation actions on a labelled element.

These call the control's own automation patterns (Invoke, Toggle, ExpandCollapse,
SelectionItem, Value, RangeValue, ScrollItem, LegacyIAccessible) instead of moving the
mouse or typing keys. They work on windows that are in the background or covered by
another window, do not steal focus or the user's cursor, and do not depend on DPI or
coordinates. They do not produce hover, mouse-down/up or key events, so a control that
only reacts to real pointer or keyboard events (some web/Electron widgets, games, custom
drawn controls) may ignore them; use the physical Click/Type path there.

Every action re-reads the element afterwards where it can and reports the delivery
disposition (see :mod:`windows_mcp.elements.report`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Literal

from windows_mcp.elements.identity import ElementHandle
from windows_mcp.elements.report import ActionReport, ActionStep, ElementError

logger = logging.getLogger(__name__)

SemanticAction = Literal[
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

SEMANTIC_ACTIONS: tuple[str, ...] = (
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
)

_SETTLE = 0.15


def _pattern(handle: ElementHandle, name: str) -> Any:
    from windows_mcp.uia import PatternId

    try:
        return handle.element.GetPattern(getattr(PatternId, name))
    except Exception:
        return None


def _toggle_name(state: Any) -> str | None:
    from windows_mcp.uia import ToggleState

    try:
        return {ToggleState.On: "on", ToggleState.Off: "off"}.get(state, "indeterminate")
    except Exception:
        return None


def _expand_name(state: Any) -> str | None:
    from windows_mcp.uia import ExpandCollapseState

    return {
        ExpandCollapseState.Expanded: "expanded",
        ExpandCollapseState.Collapsed: "collapsed",
        ExpandCollapseState.PartiallyExpanded: "partially_expanded",
        ExpandCollapseState.LeafNode: "leaf",
    }.get(state)


def read_state(handle: ElementHandle) -> dict[str, Any]:
    """Read the element's current semantic state (never the value of a secure field)."""
    element = handle.element
    state: dict[str, Any] = {}

    def put(key: str, fn: Callable[[], Any]) -> None:
        try:
            value = fn()
        except Exception:
            return
        if value is not None:
            state[key] = value

    put("name", lambda: (element.Name or "").strip())
    put("control_type", lambda: element.ControlTypeName)
    put("automation_id", lambda: element.AutomationId or None)
    put("enabled", lambda: bool(element.IsEnabled))
    put("offscreen", lambda: bool(element.IsOffscreen))
    put("focused", lambda: bool(element.HasKeyboardFocus))
    put("bounds", lambda: _rect_tuple(element.BoundingRectangle))

    toggle = _pattern(handle, "TogglePattern")
    if toggle is not None:
        put("toggle", lambda: _toggle_name(toggle.ToggleState))
    expand = _pattern(handle, "ExpandCollapsePattern")
    if expand is not None:
        put("expand_collapse", lambda: _expand_name(expand.ExpandCollapseState))
    selection_item = _pattern(handle, "SelectionItemPattern")
    if selection_item is not None:
        put("selected", lambda: bool(selection_item.IsSelected))
    range_value = _pattern(handle, "RangeValuePattern")
    if range_value is not None:
        put("range_value", lambda: range_value.Value)
        put("range_min", lambda: range_value.Minimum)
        put("range_max", lambda: range_value.Maximum)
    if handle.is_password:
        state["value"] = "<secure>"
        return state
    value = _pattern(handle, "ValuePattern")
    if value is not None:
        put("value", lambda: value.Value)
        put("read_only", lambda: bool(value.IsReadOnly))
    elif "range_value" not in state:
        legacy = _pattern(handle, "LegacyIAccessiblePattern")
        if legacy is not None:
            put("value", lambda: legacy.Value or None)
    return state


def _rect_tuple(rect: Any) -> tuple[int, int, int, int]:
    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def _unsupported(action: str, handle: ElementHandle, tried: list[str]) -> ElementError:
    patterns = ", ".join(tried) if tried else "the required pattern"
    return ElementError(
        "ACTION_NOT_SUPPORTED",
        f"{handle.describe()} does not support '{action}' (no {patterns}).",
        hint="Use a physical Click/Type on the element instead.",
    )


def _call(fn: Callable[[], Any]) -> tuple[bool, str | None]:
    try:
        result = fn()
    except Exception as error:  # COM failures surface here
        return False, str(error) or type(error).__name__
    if result is False:
        return False, "provider returned failure"
    return True, None


def _invoke(handle: ElementHandle, report: ActionReport) -> None:
    """Primary activation, walking a ladder of patterns like a click would."""
    ladder: list[tuple[str, str, Callable[[Any], Any]]] = [
        ("InvokePattern", "InvokePattern.Invoke", lambda p: p.Invoke(waitTime=0)),
        ("TogglePattern", "TogglePattern.Toggle", lambda p: p.Toggle(waitTime=0)),
        ("SelectionItemPattern", "SelectionItemPattern.Select", lambda p: p.Select(waitTime=0)),
        ("ExpandCollapsePattern", "ExpandCollapsePattern", None),
        (
            "LegacyIAccessiblePattern",
            "LegacyIAccessible.DoDefaultAction",
            lambda p: p.DoDefaultAction(waitTime=0),
        ),
    ]
    tried: list[str] = []
    for pattern_name, label, fn in ladder:
        pattern = _pattern(handle, pattern_name)
        if pattern is None:
            continue
        tried.append(pattern_name)
        if pattern_name == "ExpandCollapsePattern":
            state = _expand_name(pattern.ExpandCollapseState)
            if state == "leaf":
                continue
            if state == "expanded":
                label, fn = "ExpandCollapsePattern.Collapse", lambda p: p.Collapse(waitTime=0)
            else:
                label, fn = "ExpandCollapsePattern.Expand", lambda p: p.Expand(waitTime=0)
        if pattern_name == "LegacyIAccessiblePattern":
            try:
                if not (pattern.DefaultAction or "").strip():
                    report.steps.append(ActionStep(label, "skipped", detail="no default action"))
                    continue
            except Exception:
                pass
        ok, error = _call(lambda: fn(pattern))
        report.steps.append(ActionStep(label, "succeeded" if ok else "failed", detail=error))
        if ok:
            report.delivery = "delivered_unverified"
            report.verification = (
                "an activation is not verifiable by itself; check the UI (Snapshot) for the effect"
            )
            return
        # A failed COM call may still have reached the provider.
        report.delivery = "delivery_uncertain"
        raise ElementError(
            "ACTION_FAILED",
            f"{label} failed on {handle.describe()}: {error}.",
            delivery="delivery_uncertain",
            hint="Inspect the UI before retrying; the activation may have landed.",
        )
    raise _unsupported(
        "invoke", handle, tried or ["Invoke/Toggle/SelectionItem/ExpandCollapse pattern"]
    )


def _set_toggle(handle: ElementHandle, report: ActionReport, target: str | None) -> None:
    pattern = _pattern(handle, "TogglePattern")
    if pattern is None:
        raise _unsupported(report.action, handle, ["TogglePattern"])
    before = _toggle_name(pattern.ToggleState)
    if target is not None and before == target:
        report.delivery = "not_delivered"
        report.post_state["toggle"] = before
        report.notes.append(f"already {target}; nothing was sent")
        return
    # Tri-state controls may need more than one Toggle to reach the target.
    for _ in range(3 if target is not None else 1):
        ok, error = _call(lambda: pattern.Toggle(waitTime=0))
        report.steps.append(
            ActionStep("TogglePattern.Toggle", "succeeded" if ok else "failed", detail=error)
        )
        if not ok:
            raise ElementError(
                "ACTION_FAILED",
                f"TogglePattern.Toggle failed on {handle.describe()}: {error}.",
                delivery="delivery_uncertain",
            )
        time.sleep(_SETTLE)
        after = _toggle_name(pattern.ToggleState)
        if target is None or after == target:
            break
    report.post_state["toggle"] = after
    expected = target or ("on" if before == "off" else "off" if before == "on" else None)
    _verify(report, "toggle", after, expected, "element_state")


def _set_expand(handle: ElementHandle, report: ActionReport, expand: bool) -> None:
    pattern = _pattern(handle, "ExpandCollapsePattern")
    if pattern is None:
        raise _unsupported(report.action, handle, ["ExpandCollapsePattern"])
    target = "expanded" if expand else "collapsed"
    before = _expand_name(pattern.ExpandCollapseState)
    if before == target:
        report.delivery = "not_delivered"
        report.post_state["expand_collapse"] = before
        report.notes.append(f"already {target}; nothing was sent")
        return
    label = "ExpandCollapsePattern.Expand" if expand else "ExpandCollapsePattern.Collapse"
    ok, error = _call(
        lambda: pattern.Expand(waitTime=0) if expand else pattern.Collapse(waitTime=0)
    )
    report.steps.append(ActionStep(label, "succeeded" if ok else "failed", detail=error))
    if not ok:
        raise ElementError(
            "ACTION_FAILED",
            f"{label} failed on {handle.describe()}: {error}.",
            delivery="delivery_uncertain",
        )
    time.sleep(_SETTLE)
    after = _expand_name(pattern.ExpandCollapseState)
    report.post_state["expand_collapse"] = after
    if after == "partially_expanded" and expand:
        after = "expanded"
    _verify(report, "expand_collapse", after, target, "element_state")


def _selection(handle: ElementHandle, report: ActionReport, mode: str) -> None:
    pattern = _pattern(handle, "SelectionItemPattern")
    if pattern is None:
        raise _unsupported(report.action, handle, ["SelectionItemPattern"])
    want_selected = mode != "remove_from_selection"
    try:
        before = bool(pattern.IsSelected)
    except Exception:
        before = None
    if before is not None and before == want_selected and mode != "select":
        report.delivery = "not_delivered"
        report.post_state["selected"] = before
        report.notes.append("selection already in the requested state; nothing was sent")
        return
    calls = {
        "select": ("SelectionItemPattern.Select", lambda: pattern.Select(waitTime=0)),
        "add_to_selection": (
            "SelectionItemPattern.AddToSelection",
            lambda: pattern.AddToSelection(waitTime=0),
        ),
        "remove_from_selection": (
            "SelectionItemPattern.RemoveFromSelection",
            lambda: pattern.RemoveFromSelection(waitTime=0),
        ),
    }
    label, fn = calls[mode]
    ok, error = _call(fn)
    report.steps.append(ActionStep(label, "succeeded" if ok else "failed", detail=error))
    if not ok:
        raise ElementError(
            "ACTION_FAILED",
            f"{label} failed on {handle.describe()}: {error}.",
            delivery="delivery_uncertain",
        )
    time.sleep(_SETTLE)
    try:
        after = bool(pattern.IsSelected)
    except Exception:
        after = None
    report.post_state["selected"] = after
    _verify(report, "selected", after, want_selected, "element_state")


def _set_value(handle: ElementHandle, report: ActionReport, value: str) -> None:
    value_pattern = _pattern(handle, "ValuePattern")
    if value_pattern is not None:
        try:
            if value_pattern.IsReadOnly:
                raise ElementError(
                    "ACTION_NOT_SUPPORTED",
                    f"{handle.describe()} is read-only.",
                    hint="Pick an editable field.",
                )
        except ElementError:
            raise
        except Exception:
            pass
        ok, error = _call(lambda: value_pattern.SetValue(value, waitTime=0))
        report.steps.append(
            ActionStep("ValuePattern.SetValue", "succeeded" if ok else "failed", detail=error)
        )
        if ok:
            _verify_value(handle, report, lambda: value_pattern.Value, value)
            return
        raise ElementError(
            "ACTION_FAILED",
            f"ValuePattern.SetValue failed on {handle.describe()}: {error}.",
            delivery="delivery_uncertain",
        )
    range_pattern = _pattern(handle, "RangeValuePattern")
    if range_pattern is not None:
        try:
            number = float(value)
        except ValueError:
            raise ElementError(
                "INVALID_ARGS",
                f"{handle.describe()} is a range control; value must be a number, got {value!r}.",
                hint="Pass a number within the control's range.",
            ) from None
        ok, error = _call(lambda: range_pattern.SetValue(number, waitTime=0))
        report.steps.append(
            ActionStep("RangeValuePattern.SetValue", "succeeded" if ok else "failed", detail=error)
        )
        if not ok:
            raise ElementError(
                "ACTION_FAILED",
                f"RangeValuePattern.SetValue failed on {handle.describe()}: {error}.",
                delivery="delivery_uncertain",
            )
        time.sleep(_SETTLE)
        try:
            after = range_pattern.Value
        except Exception:
            after = None
        report.post_state["range_value"] = after
        matched = after is not None and abs(float(after) - number) < 1e-6
        _verify(report, "range_value", after, after if matched else number, "element_value")
        return
    legacy = _pattern(handle, "LegacyIAccessiblePattern")
    if legacy is not None:
        ok, error = _call(lambda: legacy.SetValue(value, waitTime=0))
        report.steps.append(
            ActionStep("LegacyIAccessible.SetValue", "succeeded" if ok else "failed", detail=error)
        )
        if ok:
            _verify_value(handle, report, lambda: legacy.Value, value)
            return
    raise _unsupported(report.action, handle, ["ValuePattern", "RangeValuePattern"])


def _verify_value(
    handle: ElementHandle, report: ActionReport, read: Callable[[], Any], expected: str
) -> None:
    if handle.is_password:
        report.delivery = "delivered_unverified"
        report.verification = "secure field: the value is never read back"
        return
    time.sleep(_SETTLE)
    try:
        observed = read()
    except Exception:
        report.delivery = "delivered_unverified"
        report.verification = "the value could not be read back"
        return
    report.post_state["value"] = observed
    _verify(report, "value", observed, expected, "element_value")
    if report.delivery == "delivered_verified":
        report.verification = (
            "element_value: the element holds the value; the application has not "
            "necessarily committed it (some apps commit on Enter or focus loss)"
        )


def _verify(report: ActionReport, key: str, observed: Any, expected: Any, scope: str) -> None:
    if observed is None:
        report.delivery = "delivered_unverified"
        report.verification = f"{scope}: {key} could not be read back"
        return
    if expected is None or observed == expected:
        report.delivery = "delivered_verified"
        report.verification = report.verification or f"{scope}: {key} read back as {observed!r}"
        return
    report.delivery = "delivered_unverified"
    raise ElementError(
        "ACTION_FAILED",
        f"{report.action} was delivered to {report.target} but {key} reads {observed!r} "
        f"instead of {expected!r}.",
        delivery="delivered_unverified",
        hint="The input landed; inspect the element (ElementAction read) before retrying.",
        details={"post_state": dict(report.post_state)},
    )


def _focus(handle: ElementHandle, report: ActionReport) -> None:
    ok, error = _call(lambda: handle.element.SetFocus())
    report.steps.append(ActionStep("SetFocus", "succeeded" if ok else "failed", detail=error))
    if not ok:
        raise ElementError(
            "ACTION_FAILED",
            f"SetFocus failed on {handle.describe()}: {error}.",
            delivery="delivery_uncertain",
        )
    time.sleep(_SETTLE)
    try:
        focused = bool(handle.element.HasKeyboardFocus)
    except Exception:
        focused = None
    report.post_state["focused"] = focused
    _verify(report, "focused", focused, True, "element_state")


def _scroll_into_view(handle: ElementHandle, report: ActionReport) -> None:
    pattern = _pattern(handle, "ScrollItemPattern")
    if pattern is None:
        raise _unsupported(report.action, handle, ["ScrollItemPattern"])
    ok, error = _call(lambda: pattern.ScrollIntoView(waitTime=0))
    report.steps.append(
        ActionStep(
            "ScrollItemPattern.ScrollIntoView", "succeeded" if ok else "failed", detail=error
        )
    )
    if not ok:
        raise ElementError(
            "ACTION_FAILED",
            f"ScrollIntoView failed on {handle.describe()}: {error}.",
            delivery="delivery_uncertain",
        )
    time.sleep(_SETTLE)
    try:
        offscreen = bool(handle.element.IsOffscreen)
        report.post_state["offscreen"] = offscreen
        report.post_state["bounds"] = _rect_tuple(handle.element.BoundingRectangle)
    except Exception:
        offscreen = None
    _verify(report, "offscreen", offscreen, False, "element_state")


def perform(
    handle: ElementHandle,
    action: str,
    *,
    value: str | None = None,
    target: str | None = None,
) -> ActionReport:
    """Perform *action* semantically on *handle* and return the report.

    Raises:
        ElementError: when the action is not supported or failed; its ``delivery`` says
            whether anything may have reached the application.
    """
    action = action.strip().lower().replace("-", "_")
    if action not in SEMANTIC_ACTIONS:
        raise ElementError(
            "INVALID_ARGS",
            f"unknown action {action!r}; expected one of {', '.join(SEMANTIC_ACTIONS)}.",
        )
    report = ActionReport(
        action=action, target=target or handle.describe(), delivery="not_delivered"
    )
    _checkpoint()
    if action == "read":
        report.post_state = read_state(handle)
        report.verification = "read-only; nothing was sent"
        return report
    if action == "invoke":
        _invoke(handle, report)
    elif action == "toggle":
        _set_toggle(handle, report, None)
    elif action in ("check", "uncheck"):
        _set_toggle(handle, report, "on" if action == "check" else "off")
    elif action in ("expand", "collapse"):
        _set_expand(handle, report, action == "expand")
    elif action in ("select", "add_to_selection", "remove_from_selection"):
        _selection(handle, report, action)
    elif action == "set_value":
        if value is None:
            raise ElementError("INVALID_ARGS", "set_value requires value.")
        _set_value(handle, report, value)
    elif action == "clear":
        _set_value(handle, report, "")
    elif action == "focus":
        _focus(handle, report)
    elif action == "scroll_into_view":
        _scroll_into_view(handle, report)
    _record_step()
    return report


def _checkpoint() -> None:
    """Honor the AI-control lease (user takeover) before sending anything."""
    try:
        from windows_mcp.desktop.control import get_controller

        get_controller().checkpoint_current()
    except ImportError:  # pragma: no cover - control module is always importable
        pass


def _record_step() -> None:
    try:
        from windows_mcp.desktop.control import get_controller

        get_controller().record_step_current()
    except ImportError:
        pass
