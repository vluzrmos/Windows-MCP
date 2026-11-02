"""Human-like mouse trajectories, opt-in via ``WINDOWS_MCP_HUMAN_MOUSE``.

When enabled, the cursor glides to its target along a slightly curved cubic
Bezier path with ease-in/ease-out timing, small per-step jitter and an
occasional overshoot-and-correct on long moves, instead of teleporting or
moving along a straight line at constant speed. Clicks on labelled elements
land at a random point inside the element instead of its exact center.

Everything here is pure (no Win32 calls) so it can be unit tested; the
Desktop service replays the planned points with ``SetCursorPos``.
"""

import math
import os
import random

from windows_mcp.tree.views import BoundingBox

Point = tuple[int, int]
PathStep = tuple[int, int, float]

_rng = random.Random()

# Seconds between cursor updates; ~125 Hz, close to a typical mouse report rate.
_STEP_INTERVAL = 0.008
_MIN_DURATION = 0.15
_MAX_DURATION = 0.7
_OVERSHOOT_MIN_DISTANCE = 300
_OVERSHOOT_PROBABILITY = 0.3
# Fraction of a target box (per axis) a random click point may stray from center.
_BOX_SPREAD = 0.35


def is_enabled() -> bool:
    """Return True when ``WINDOWS_MCP_HUMAN_MOUSE`` asks for human-like movement."""
    value = os.getenv("WINDOWS_MCP_HUMAN_MOUSE", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def movement_duration(distance: float, rng: random.Random | None = None) -> float:
    """Estimate a natural movement time for ``distance`` pixels (Fitts-like law).

    Args:
        distance: Straight-line distance in pixels.
        rng: Random source; defaults to the module generator.

    Returns:
        Duration in seconds, clamped to a short, agent-friendly range.
    """
    rng = rng or _rng
    base = 0.12 + 0.1 * math.log2(1 + distance / 20)
    return min(_MAX_DURATION, max(_MIN_DURATION, base * rng.uniform(0.85, 1.15)))


def _ease(t: float, exponent: float) -> float:
    """Sigmoid ease-in/ease-out: slow start, fast middle, slow arrival."""
    a = t**exponent
    return a / (a + (1 - t) ** exponent)


def _bezier(p0: Point, p1: tuple, p2: tuple, p3: Point, t: float) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return x, y


def _curve(start: Point, end: Point, duration: float, rng: random.Random) -> list[PathStep]:
    """Plan one curved stroke from ``start`` to ``end`` lasting ``duration`` seconds."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    distance = math.hypot(dx, dy)
    if distance < 1:
        return [(end[0], end[1], 0.0)]
    # Unit normal to the straight line; control points bow the path to one side.
    nx, ny = -dy / distance, dx / distance
    side = rng.choice((-1, 1))
    bow1 = side * distance * rng.uniform(0.05, 0.2)
    bow2 = side * distance * rng.uniform(0.0, 0.15)
    p1 = (start[0] + dx * 0.3 + nx * bow1, start[1] + dy * 0.3 + ny * bow1)
    p2 = (start[0] + dx * 0.7 + nx * bow2, start[1] + dy * 0.7 + ny * bow2)
    exponent = rng.uniform(1.6, 2.4)
    steps = max(2, min(120, round(duration / _STEP_INTERVAL)))
    weights = [rng.uniform(0.8, 1.2) for _ in range(steps)]
    scale = duration / sum(weights)

    path: list[PathStep] = []
    for i in range(1, steps + 1):
        if i == steps:
            x, y = end
        else:
            fx, fy = _bezier(start, p1, p2, end, _ease(i / steps, exponent))
            # Hand tremor: one-pixel noise on every point except the arrival.
            x = round(fx) + rng.randint(-1, 1)
            y = round(fy) + rng.randint(-1, 1)
        path.append((x, y, weights[i - 1] * scale))
    return path


def plan_path(
    start: Point,
    end: Point,
    duration: float | None = None,
    rng: random.Random | None = None,
) -> list[PathStep]:
    """Plan a human-like cursor path from ``start`` to ``end``.

    Args:
        start: Current cursor position.
        end: Target position; always the final point of the path.
        duration: Total time in seconds. ``None`` picks a natural duration
            from the distance.
        rng: Random source; defaults to the module generator.

    Returns:
        ``(x, y, delay)`` steps excluding ``start``. Delays sum to the duration.
    """
    rng = rng or _rng
    start = (int(start[0]), int(start[1]))
    end = (int(end[0]), int(end[1]))
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    if distance < 1:
        return [(end[0], end[1], 0.0)]
    total = movement_duration(distance, rng) if duration is None else max(0.0, duration)

    if distance >= _OVERSHOOT_MIN_DISTANCE and rng.random() < _OVERSHOOT_PROBABILITY:
        ux = (end[0] - start[0]) / distance
        uy = (end[1] - start[1]) / distance
        past = rng.uniform(3, 8)
        overshoot = (
            end[0] + round(ux * past) + rng.randint(-2, 2),
            end[1] + round(uy * past) + rng.randint(-2, 2),
        )
        correction = min(total * 0.25, rng.uniform(0.06, 0.12))
        return _curve(start, overshoot, total - correction, rng) + _curve(
            overshoot, end, correction, rng
        )
    return _curve(start, end, total, rng)


def pick_point_in_box(box: BoundingBox, rng: random.Random | None = None) -> Point:
    """Pick a random click point inside ``box``, biased toward its center.

    The offset is Gaussian and truncated to the inner part of the box so the
    point never lands on an edge where a neighbouring element may begin.

    Args:
        box: Target element bounds.
        rng: Random source; defaults to the module generator.

    Returns:
        Screen coordinates inside the box.
    """
    rng = rng or _rng
    center = box.get_center()

    def offset(size: int) -> int:
        limit = size * _BOX_SPREAD
        if limit < 1:
            return 0
        return round(max(-limit, min(limit, rng.gauss(0, size * 0.15))))

    return center.x + offset(box.width), center.y + offset(box.height)


def jitter_point(x: int, y: int, radius: int = 2, rng: random.Random | None = None) -> Point:
    """Nudge a raw coordinate by at most ``radius`` pixels on each axis."""
    rng = rng or _rng
    return x + rng.randint(-radius, radius), y + rng.randint(-radius, radius)


def pre_click_pause(rng: random.Random | None = None) -> float:
    """Seconds to settle on the target before pressing a button."""
    return (rng or _rng).uniform(0.04, 0.12)


def press_hold(rng: random.Random | None = None) -> float:
    """Seconds a button stays down during a click."""
    return (rng or _rng).uniform(0.05, 0.11)
