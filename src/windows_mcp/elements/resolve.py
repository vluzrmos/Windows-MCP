"""Resolve a Snapshot label to a verified, actionable target.

This is the single path every label-based tool goes through:

1. the label must belong to the latest Snapshot (``snapshot_id`` when given),
2. its element must still exist and be the captured element (``STALE_REF`` otherwise),
3. the element must become actionable within the wait budget (auto-wait), and
4. input to its process must not be blocked by UIPI (``PERM_DENIED``).

Nodes without a live element (word runs, IA2/Firefox DOM nodes) fall back to their
captured coordinates, exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from windows_mcp.elements.actionability import (
    DEFAULT_WAIT_TIMEOUT,
    Actionability,
    wait_until_actionable,
)
from windows_mcp.elements.identity import ElementHandle
from windows_mcp.elements.report import ElementError
from windows_mcp.elements.safety import input_block_reason_for_pid


@dataclass
class ResolvedTarget:
    label: int
    node: Any
    handle: ElementHandle | None
    point: tuple[int, int]
    actionability: Actionability | None = None

    @property
    def verified(self) -> bool:
        return self.handle is not None

    def describe(self) -> str:
        if self.handle is not None:
            return f"#{self.label} {self.handle.describe()}"
        name = getattr(self.node, "name", "")
        kind = str(getattr(self.node, "control_type", "element")).lower()
        return f'#{self.label} {kind} "{name}"' if name else f"#{self.label} {kind}"

    def note(self) -> str:
        """One line describing how the target was verified."""
        if self.handle is None:
            return "target: captured coordinates (element identity not available for this node)"
        parts = ["target verified live"]
        if self.actionability is not None:
            parts.append(self.actionability.summary())
            if self.actionability.scrolled_into_view:
                parts.append("scrolled into view first")
        return "; ".join(parts)


def current_snapshot_id(desktop: Any) -> str | None:
    state = getattr(desktop, "desktop_state", None)
    tree_state = getattr(state, "tree_state", None)
    return getattr(tree_state, "snapshot_id", None)


def check_snapshot(desktop: Any, snapshot_id: str | None) -> None:
    if snapshot_id is None:
        return
    current = current_snapshot_id(desktop)
    if current is not None and snapshot_id.strip() != current:
        raise ElementError(
            "SNAPSHOT_MISMATCH",
            f"label belongs to snapshot {snapshot_id!r} but the latest capture is {current!r}; "
            "labels are only valid inside the snapshot that printed them.",
        )


def lookup_node(desktop: Any, label: int, snapshot_id: str | None = None) -> Any:
    if getattr(desktop, "desktop_state", None) is None:
        raise ElementError(
            "ELEMENT_NOT_FOUND",
            "Desktop state is empty.",
            hint="Call Snapshot (or Find) first.",
        )
    check_snapshot(desktop, snapshot_id)
    try:
        return desktop.get_node_from_label(label)
    except (IndexError, ValueError) as error:
        raise ElementError(
            "ELEMENT_NOT_FOUND", f"Failed to find element with label {label}: {error}"
        ) from error


def element_of(node: Any) -> ElementHandle | None:
    handle = getattr(node, "element", None)
    return handle if isinstance(handle, ElementHandle) else None


def resolve_label(
    desktop: Any,
    label: int,
    *,
    snapshot_id: str | None = None,
    pointer: bool = True,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    check_integrity: bool = True,
) -> ResolvedTarget:
    """Resolve *label* to a verified target point (see module docstring)."""
    node = lookup_node(desktop, label, snapshot_id)
    handle = element_of(node)
    fallback_point = _fallback_point(desktop, node, label)
    if handle is None:
        return ResolvedTarget(label=label, node=node, handle=None, point=fallback_point)

    preferred = fallback_point if _human_mode() else None
    actionability = wait_until_actionable(
        handle,
        pointer=pointer,
        timeout=wait_timeout,
        label=label,
        preferred_point=preferred,
    )
    if check_integrity and pointer:
        reason = input_block_reason_for_pid(handle.process_id)
        if reason:
            raise ElementError("PERM_DENIED", reason.removeprefix("PERM_DENIED: "))
    point = actionability.point or (
        actionability.live.center if actionability.live else fallback_point
    )
    return ResolvedTarget(
        label=label, node=node, handle=handle, point=point, actionability=actionability
    )


def require_element(
    desktop: Any, label: int, snapshot_id: str | None = None
) -> tuple[Any, ElementHandle]:
    """The node and live element for *label*, for semantic actions that need one."""
    node = lookup_node(desktop, label, snapshot_id)
    handle = element_of(node)
    if handle is None:
        raise ElementError(
            "ACTION_NOT_SUPPORTED",
            f"label {label} ({getattr(node, 'control_type', 'node')} "
            f"{getattr(node, 'name', '')!r}) has no UI Automation element (text-word run or "
            "IA2/Firefox DOM node), so it only supports coordinate input.",
            hint="Use Click/Type with this label instead.",
        )
    return node, handle


def _human_mode() -> bool:
    try:
        from windows_mcp.desktop import human_mouse

        return human_mouse.is_enabled()
    except Exception:
        return False


def _fallback_point(desktop: Any, node: Any, label: int) -> tuple[int, int]:
    """The captured target point (centre, or a random inner point in human mode)."""
    x, y = desktop.get_coordinates_from_label(label)
    return int(x), int(y)
