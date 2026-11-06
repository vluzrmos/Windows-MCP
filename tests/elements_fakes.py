"""Fake UI Automation controls and patterns for the elements tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from windows_mcp.uia import ExpandCollapseState, PatternId, ToggleState


@dataclass
class FakeRect:
    left: int
    top: int
    right: int
    bottom: int

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class FakeToggle:
    def __init__(
        self, element: "FakeElement", state: ToggleState = ToggleState.Off, sticky: bool = False
    ):
        self.element = element
        self.ToggleState = state
        self.sticky = sticky
        self.calls = 0

    def Toggle(self, waitTime: float = 0) -> bool:
        self.calls += 1
        if not self.sticky:
            self.ToggleState = (
                ToggleState.On if self.ToggleState == ToggleState.Off else ToggleState.Off
            )
        return True


class FakeExpand:
    def __init__(self, state: ExpandCollapseState = ExpandCollapseState.Collapsed):
        self.ExpandCollapseState = state
        self.calls: list[str] = []

    def Expand(self, waitTime: float = 0) -> bool:
        self.calls.append("expand")
        self.ExpandCollapseState = ExpandCollapseState.Expanded
        return True

    def Collapse(self, waitTime: float = 0) -> bool:
        self.calls.append("collapse")
        self.ExpandCollapseState = ExpandCollapseState.Collapsed
        return True


class FakeValue:
    def __init__(self, value: str = "", read_only: bool = False, ignore_writes: bool = False):
        self.Value = value
        self.IsReadOnly = read_only
        self.ignore_writes = ignore_writes
        self.writes: list[str] = []

    def SetValue(self, value: str, waitTime: float = 0) -> bool:
        self.writes.append(value)
        if not self.ignore_writes:
            self.Value = value
        return True


class FakeInvoke:
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def Invoke(self, waitTime: float = 0) -> bool:
        self.calls += 1
        if self.fail:
            raise OSError("COM failure")
        return True


class FakeSelectionItem:
    def __init__(self, selected: bool = False):
        self.IsSelected = selected
        self.calls: list[str] = []

    def Select(self, waitTime: float = 0) -> bool:
        self.calls.append("select")
        self.IsSelected = True
        return True

    def AddToSelection(self, waitTime: float = 0) -> bool:
        self.calls.append("add")
        self.IsSelected = True
        return True

    def RemoveFromSelection(self, waitTime: float = 0) -> bool:
        self.calls.append("remove")
        self.IsSelected = False
        return True


@dataclass
class FakeElement:
    Name: str = "OK"
    ControlTypeName: str = "ButtonControl"
    AutomationId: str = "ok"
    IsEnabled: bool = True
    IsOffscreen: bool = False
    HasKeyboardFocus: bool = False
    IsPassword: bool = False
    rect: FakeRect = field(default_factory=lambda: FakeRect(10, 10, 110, 40))
    runtime_id: tuple[int, ...] = (42, 1)
    patterns: dict[int, Any] = field(default_factory=dict)
    dead: bool = False
    parent: "FakeElement | None" = None
    rects: list[FakeRect] | None = None  # successive BoundingRectangle reads

    @property
    def BoundingRectangle(self) -> FakeRect:
        if self.dead:
            raise _dead_error()
        if self.rects:
            return self.rects.pop(0) if len(self.rects) > 1 else self.rects[0]
        return self.rect

    def GetRuntimeId(self) -> list[int]:
        if self.dead:
            raise _dead_error()
        return list(self.runtime_id)

    def GetPattern(self, pattern_id: int) -> Any:
        return self.patterns.get(pattern_id)

    def GetParentControl(self) -> "FakeElement | None":
        return self.parent

    def SetFocus(self) -> bool:
        self.HasKeyboardFocus = True
        return True


def _dead_error() -> Exception:
    from windows_mcp.uia import UIADeadElementError

    return UIADeadElementError("element not available")


def make_handle(element: FakeElement, **overrides: Any):
    from windows_mcp.elements.identity import ElementHandle

    values = dict(
        element=element,
        runtime_id=tuple(element.runtime_id),
        control_type_name=element.ControlTypeName,
        automation_id=element.AutomationId,
        name=element.Name,
        process_id=None,
        window_handle=None,
        is_password=element.IsPassword,
    )
    values.update(overrides)
    return ElementHandle(**values)


__all__ = [
    "ExpandCollapseState",
    "FakeElement",
    "FakeExpand",
    "FakeInvoke",
    "FakeRect",
    "FakeSelectionItem",
    "FakeToggle",
    "FakeValue",
    "PatternId",
    "ToggleState",
    "make_handle",
]
