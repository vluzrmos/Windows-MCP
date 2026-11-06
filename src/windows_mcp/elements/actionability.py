"""Pre-dispatch actionability checks with a bounded auto-wait.

Before an action is delivered to a labelled element, a battery of live checks runs:

* ``alive``           — the element still exists and is the captured element
* ``visible``         — non-empty bounds and not offscreen (pointer input only)
* ``stable``          — bounds did not move between two samples (no animation)
* ``enabled``         — the control accepts input
* ``receives_events`` — a hit test at the target point lands on the element and not on
  something covering it (pointer input only)

Transient checks are polled until they pass or the wait budget runs out; the result
names every failed check so the caller knows *why* the element was not actionable. An
offscreen element that supports ScrollItemPattern is scrolled into view once.

Every probe fails open: evidence that cannot be read never blocks an action.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from windows_mcp.elements.identity import ElementHandle, LiveState, read_live, runtime_id_of
from windows_mcp.elements.report import ElementError

logger = logging.getLogger(__name__)

CheckStatus = Literal["pass", "fail", "unknown"]
DEFAULT_WAIT_TIMEOUT = 3.0
_POLL_INTERVAL = 0.1
_STABILITY_GAP = 0.04
_MAX_ANCESTOR_WALK = 40


@dataclass
class Check:
    name: str
    status: CheckStatus
    reason: str | None = None

    def to_text(self) -> str:
        return f"{self.name}={self.status}" + (f" ({self.reason})" if self.reason else "")


@dataclass
class Actionability:
    checks: list[Check] = field(default_factory=list)
    live: LiveState | None = None
    point: tuple[int, int] | None = None
    scrolled_into_view: bool = False

    @property
    def actionable(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    def summary(self) -> str:
        return ", ".join(check.to_text() for check in self.checks)


def _virtual_screen() -> tuple[int, int, int, int]:
    try:
        from windows_mcp import uia

        left, top, width, height = uia.GetVirtualScreenRect()
        return left, top, left + width, top + height
    except Exception:
        return (-(10**6), -(10**6), 10**6, 10**6)


def _describe_control(control: Any) -> str:
    try:
        kind = str(control.ControlTypeName).removesuffix("Control").lower()
    except Exception:
        kind = "element"
    try:
        name = (control.Name or "").strip()
    except Exception:
        name = ""
    return f'{kind} "{name}"' if name else kind


def _ancestors(control: Any, limit: int = _MAX_ANCESTOR_WALK):
    current = control
    for _ in range(limit):
        if current is None:
            return
        yield current
        try:
            current = current.GetParentControl()
        except Exception:
            return


def hit_test(handle: ElementHandle, x: int, y: int) -> tuple[CheckStatus, str | None]:
    """Does input at (x, y) reach the element?

    Returns ``pass`` when the element under the point is the target or one of its
    descendants, ``fail`` with the occluder's description when something else covers
    it, and ``unknown`` when the probe could not decide.
    """
    try:
        from windows_mcp.uia import ControlFromPoint

        hit = ControlFromPoint(int(x), int(y))
    except Exception:
        return "unknown", None
    if hit is None:
        return "unknown", None
    target_id = handle.runtime_id or runtime_id_of(handle.element)
    if target_id is None:
        return "unknown", None
    hit_id = runtime_id_of(hit)
    if hit_id is None:
        return "unknown", None
    for ancestor in _ancestors(hit):
        if runtime_id_of(ancestor) == target_id:
            return "pass", None
    # The hit element is an ancestor of the target: the target itself is not
    # hit-testable (common for text runs and some custom controls). No verdict.
    for ancestor in _ancestors(handle.element):
        if runtime_id_of(ancestor) == hit_id:
            return "unknown", "target is not hit-testable; its container answered"
    return "fail", f"covered by {_describe_control(hit)}"


def _candidate_points(live: LiveState) -> list[tuple[int, int]]:
    cx, cy = live.center
    qx, qy = max(1, live.width // 4), max(1, live.height // 4)
    return [
        (cx, cy),
        (live.left + qx, live.top + qy),
        (live.right - qx, live.top + qy),
        (live.left + qx, live.bottom - qy),
        (live.right - qx, live.bottom - qy),
    ]


def _try_scroll_into_view(handle: ElementHandle) -> bool:
    try:
        from windows_mcp.uia import PatternId

        pattern = handle.element.GetPattern(PatternId.ScrollItemPattern)
        if pattern is None:
            return False
        pattern.ScrollIntoView(waitTime=0.15)
        return True
    except Exception:
        logger.debug("ScrollIntoView failed", exc_info=True)
        return False


def evaluate(
    handle: ElementHandle,
    *,
    pointer: bool,
    label: int | None = None,
    preferred_point: tuple[int, int] | None = None,
) -> Actionability:
    """Run the check battery once. Raises ElementError(STALE_REF) for a dead element."""
    first = read_live(handle, label=label)
    time.sleep(_STABILITY_GAP)
    live = read_live(handle, label=label)
    result = Actionability(live=live)
    result.checks.append(Check("alive", "pass"))

    if pointer:
        sl, st, sr, sb = _virtual_screen()
        cx, cy = live.center
        if live.area <= 0:
            result.checks.append(Check("visible", "fail", "element has empty bounds"))
        elif live.is_offscreen:
            result.checks.append(Check("visible", "fail", "element is offscreen"))
        elif not (sl <= cx < sr and st <= cy < sb):
            result.checks.append(Check("visible", "fail", "element center is off the desktop"))
        else:
            result.checks.append(Check("visible", "pass"))

    if first.rect() == live.rect():
        result.checks.append(Check("stable", "pass"))
    else:
        result.checks.append(Check("stable", "fail", "element is moving or resizing"))

    result.checks.append(
        Check("enabled", "pass") if live.is_enabled else Check("enabled", "fail", "disabled")
    )

    if pointer and result.actionable:
        candidates = _candidate_points(live)
        if preferred_point is not None:
            px, py = preferred_point
            if live.left <= px < live.right and live.top <= py < live.bottom:
                candidates.insert(0, (px, py))
        probes: list[tuple[tuple[int, int], CheckStatus, str | None]] = []
        for point in candidates:
            status, reason = hit_test(handle, *point)
            if status == "pass":
                result.point = point
                result.checks.append(Check("receives_events", "pass"))
                break
            probes.append((point, status, reason))
        else:
            occluder = next((reason for _, status, reason in probes if status == "fail"), None)
            undecided = [point for point, status, _ in probes if status == "unknown"]
            if undecided:
                # Fail open: no probe proved the element is covered at this point.
                result.point = undecided[0]
                result.checks.append(Check("receives_events", "unknown", occluder))
            else:
                result.checks.append(Check("receives_events", "fail", occluder))
    elif pointer:
        result.point = live.center
    return result


def wait_until_actionable(
    handle: ElementHandle,
    *,
    pointer: bool,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    label: int | None = None,
    preferred_point: tuple[int, int] | None = None,
) -> Actionability:
    """Poll the check battery until it passes or *timeout* seconds elapse.

    Raises:
        ElementError: ``STALE_REF`` when the element disappears, ``NOT_ACTIONABLE`` when
            the budget runs out with a failing check (the message lists every check).
    """
    deadline = time.monotonic() + max(0.0, timeout)
    scrolled = False
    while True:
        result = evaluate(handle, pointer=pointer, label=label, preferred_point=preferred_point)
        result.scrolled_into_view = scrolled
        if result.actionable:
            return result
        visible = next((c for c in result.checks if c.name == "visible"), None)
        if pointer and not scrolled and visible is not None and visible.status == "fail":
            if _try_scroll_into_view(handle):
                scrolled = True
                continue
        if time.monotonic() >= deadline:
            where = f"label {label}" if label is not None else handle.describe()
            raise ElementError(
                "NOT_ACTIONABLE",
                f"{where} ({handle.describe()}) was not actionable after {timeout:.1f}s: "
                f"{result.summary()}.",
                details={"checks": [c.__dict__ for c in result.checks]},
            )
        time.sleep(_POLL_INTERVAL)
