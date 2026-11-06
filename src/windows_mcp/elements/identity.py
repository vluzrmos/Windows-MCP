"""Element identity captured at Snapshot time and re-verified before acting.

A Snapshot label used to be a bare index into the last capture: if the UI changed in
between, the click landed on whatever now occupied the old coordinates. Each labelled
node now keeps an :class:`ElementHandle` — the live UIA element plus the identity
evidence read during the walk — so an action can confirm, immediately before it runs,
that the element still exists and is still the same element (RuntimeId, control type,
AutomationId). A mismatch fails closed with ``STALE_REF`` instead of guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from windows_mcp.elements.report import ElementError

logger = logging.getLogger(__name__)

# Control types whose Name is often derived from mutable content (a field's text, a
# document title), so a changed name does not mean a different element.
_VOLATILE_NAME_TYPES = frozenset(
    {"EditControl", "DocumentControl", "ComboBoxControl", "TextControl", "SpinnerControl"}
)


@dataclass
class ElementHandle:
    """A live UIA element plus the identity evidence captured with it."""

    element: Any = field(repr=False, compare=False)
    runtime_id: tuple[int, ...] | None = None
    control_type_name: str = ""
    automation_id: str = ""
    class_name: str = ""
    name: str = ""
    framework_id: str = ""
    process_id: int | None = None
    window_handle: int | None = None
    is_password: bool = False

    @classmethod
    def from_cached(
        cls,
        node: Any,
        *,
        window_handle: int | None = None,
        process_id: int | None = None,
    ) -> "ElementHandle":
        """Build a handle from a node that carries the tree-traversal cache."""

        def read(attr: str, default: Any = "") -> Any:
            try:
                value = getattr(node, attr)
            except Exception:
                return default
            return default if value is None else value

        runtime_id: tuple[int, ...] | None
        try:
            from windows_mcp.uia import PropertyId

            raw = node.GetCachedPropertyValue(PropertyId.RuntimeIdProperty)
            runtime_id = tuple(int(part) for part in raw) or None
        except Exception:
            runtime_id = None
        return cls(
            element=node,
            runtime_id=runtime_id,
            control_type_name=str(read("CachedControlTypeName")),
            automation_id=str(read("CachedAutomationId")),
            class_name=str(read("CachedClassName")),
            name=str(read("CachedName")).strip(),
            framework_id=str(read("CachedFrameworkId")),
            process_id=process_id,
            window_handle=window_handle,
            is_password=bool(read("CachedIsPassword", False)),
        )

    @classmethod
    def from_live(
        cls,
        control: Any,
        *,
        window_handle: int | None = None,
        process_id: int | None = None,
    ) -> "ElementHandle":
        """Build a handle from an uncached control, reading its properties live."""

        def read(attr: str, default: Any = "") -> Any:
            try:
                value = getattr(control, attr)
            except Exception:
                return default
            return default if value is None else value

        return cls(
            element=control,
            runtime_id=runtime_id_of(control),
            control_type_name=str(read("ControlTypeName")),
            automation_id=str(read("AutomationId")),
            class_name=str(read("ClassName")),
            name=str(read("Name")).strip(),
            framework_id=str(read("FrameworkId")),
            process_id=process_id,
            window_handle=window_handle,
            is_password=bool(read("IsPassword", False)),
        )

    def describe(self) -> str:
        kind = self.control_type_name.removesuffix("Control").lower() or "element"
        name = "<secure>" if self.is_password else self.name
        return f'{kind} "{name}"' if name else kind


@dataclass
class LiveState:
    """Properties read live from the element right before an action."""

    left: int
    top: int
    right: int
    bottom: int
    is_offscreen: bool
    is_enabled: bool
    name: str
    has_focus: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def _is_dead(error: Exception) -> bool:
    try:
        from windows_mcp.tree.cache_utils import is_uia_dead_element_error

        return is_uia_dead_element_error(error)
    except Exception:
        return False


def read_live(handle: ElementHandle, *, label: int | None = None) -> LiveState:
    """Read the element's live state, verifying it is still the captured element.

    Raises:
        ElementError: ``STALE_REF`` when the element is gone or is no longer the same
            element (RuntimeId or control type changed).
    """
    where = f"label {label}" if label is not None else handle.describe()
    element = handle.element
    try:
        rect = element.BoundingRectangle
        control_type_name = element.ControlTypeName
        runtime_id = None
        if handle.runtime_id is not None:
            try:
                runtime_id = tuple(int(part) for part in element.GetRuntimeId())
            except Exception:
                runtime_id = None
        is_offscreen = bool(element.IsOffscreen)
        is_enabled = bool(element.IsEnabled)
        name = (element.Name or "").strip()
        try:
            has_focus = bool(element.HasKeyboardFocus)
        except Exception:
            has_focus = False
    except Exception as error:
        try:
            from windows_mcp.uia import is_timeout_exception

            timed_out = is_timeout_exception(error)
        except Exception:
            timed_out = False
        if timed_out:
            raise ElementError(
                "TIMEOUT",
                f"the application owning {where} is not responding to UI Automation.",
                hint="Wait for the app to respond, then take a new Snapshot.",
            ) from error
        if _is_dead(error) or "COMError" in type(error).__name__:
            raise ElementError(
                "STALE_REF",
                f"{where} ({handle.describe()}) no longer exists in the UI.",
            ) from error
        raise ElementError(
            "STALE_REF", f"{where} ({handle.describe()}) could not be read: {error}"
        ) from error

    if handle.runtime_id is not None and runtime_id is not None and runtime_id != handle.runtime_id:
        raise ElementError(
            "STALE_REF",
            f"{where} now points at a different element than the one captured "
            f"({handle.describe()}).",
        )
    if handle.control_type_name and control_type_name != handle.control_type_name:
        raise ElementError(
            "STALE_REF",
            f"{where} changed from {handle.control_type_name} to {control_type_name}.",
        )
    if (
        handle.runtime_id is None
        and handle.name
        and handle.control_type_name not in _VOLATILE_NAME_TYPES
        and name != handle.name
    ):
        # Without a RuntimeId the stable name is the strongest remaining evidence.
        raise ElementError(
            "STALE_REF",
            f"{where} was {handle.describe()} but now reads {name!r}.",
        )
    return LiveState(
        left=int(rect.left),
        top=int(rect.top),
        right=int(rect.right),
        bottom=int(rect.bottom),
        is_offscreen=is_offscreen,
        is_enabled=is_enabled,
        name=name,
        has_focus=has_focus,
    )


def runtime_id_of(control: Any) -> tuple[int, ...] | None:
    """Live RuntimeId of a UIA control, or None."""
    try:
        return tuple(int(part) for part in control.GetRuntimeId()) or None
    except Exception:
        return None
