import math
import random
from unittest.mock import Mock

import pytest

from windows_mcp.desktop import human_mouse, service
from windows_mcp.desktop.control_ledger import InputLedger
from windows_mcp.desktop.service import Desktop
from windows_mcp.tree.views import BoundingBox


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_is_enabled_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WINDOWS_MCP_HUMAN_MOUSE", value)
    assert human_mouse.is_enabled()


@pytest.mark.parametrize("value", ["", "0", "false", "off", "nope"])
def test_is_enabled_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("WINDOWS_MCP_HUMAN_MOUSE", value)
    assert not human_mouse.is_enabled()


def test_is_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINDOWS_MCP_HUMAN_MOUSE", raising=False)
    assert not human_mouse.is_enabled()


@pytest.mark.parametrize("seed", range(50))
def test_plan_path_ends_on_target_and_stays_near_the_stroke(seed: int) -> None:
    rng = random.Random(seed)
    start, end = (100, 100), (900, 500)
    path = human_mouse.plan_path(start, end, rng=rng)

    assert path[-1][:2] == end
    distance = math.dist(start, end)
    # Bow (<=20% of distance) plus overshoot and tremor margins.
    margin = distance * 0.25 + 12
    for x, y, delay in path:
        assert delay >= 0
        assert min(start[0], end[0]) - margin <= x <= max(start[0], end[0]) + margin
        assert min(start[1], end[1]) - margin <= y <= max(start[1], end[1]) + margin


def test_plan_path_is_not_linear_and_has_varying_speed() -> None:
    rng = random.Random(7)
    path = human_mouse.plan_path((0, 0), (600, 0), rng=rng)

    assert any(y != 0 for _, y, _ in path[:-1])
    steps = [math.dist(a[:2], b[:2]) for a, b in zip(path, path[1:])]
    assert max(steps) > 2 * min(steps)
    assert len({round(delay, 6) for _, _, delay in path}) > 1


def test_plan_path_respects_explicit_duration() -> None:
    path = human_mouse.plan_path((0, 0), (500, 300), duration=1.25, rng=random.Random(3))
    assert sum(delay for _, _, delay in path) == pytest.approx(1.25)


def test_plan_path_natural_duration_is_bounded() -> None:
    for seed in range(20):
        path = human_mouse.plan_path((0, 0), (1900, 1000), rng=random.Random(seed))
        assert 0.15 <= sum(delay for _, _, delay in path) <= 0.7 + 1e-9


def test_plan_path_same_point_is_single_step() -> None:
    assert human_mouse.plan_path((5, 5), (5, 5)) == [(5, 5, 0.0)]


def test_pick_point_in_box_stays_in_inner_area() -> None:
    rng = random.Random(11)
    box = BoundingBox(left=100, top=200, right=200, bottom=240, width=100, height=40)
    points = {human_mouse.pick_point_in_box(box, rng) for _ in range(1000)}

    assert len(points) > 20
    for x, y in points:
        assert 115 <= x <= 185
        assert 206 <= y <= 234


def test_pick_point_in_tiny_box_returns_center() -> None:
    box = BoundingBox(left=10, top=10, right=11, bottom=11, width=1, height=1)
    assert human_mouse.pick_point_in_box(box, random.Random(0)) == (10, 10)


def test_jitter_point_is_bounded() -> None:
    rng = random.Random(5)
    for _ in range(200):
        x, y = human_mouse.jitter_point(50, 60, radius=2, rng=rng)
        assert abs(x - 50) <= 2 and abs(y - 60) <= 2


def _desktop() -> Desktop:
    desktop = Desktop.__new__(Desktop)
    desktop.desktop_state = None
    return desktop


def test_desktop_drag_human_mode_presses_moves_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[int, ...]]] = []
    owner = Mock()
    owner.input_ledger = InputLedger()
    owner.physical_mouse_down.return_value = False
    monkeypatch.setattr(service, "get_controller", lambda: owner)
    monkeypatch.setenv("WINDOWS_MCP_HUMAN_MOUSE", "1")
    monkeypatch.setattr(service, "sleep", lambda seconds: None)
    cursor = [(0, 0)]
    monkeypatch.setattr(service.uia, "GetCursorPos", lambda: cursor[-1])

    def set_cursor(x: int, y: int) -> None:
        cursor.append((x, y))
        calls.append(("move", (x, y)))

    monkeypatch.setattr(service.uia, "SetCursorPos", set_cursor)
    monkeypatch.setattr(
        service.uia, "PressMouse", lambda x, y, **kw: calls.append(("down", (x, y)))
    )
    monkeypatch.setattr(service.uia, "ReleaseMouse", lambda **kw: calls.append(("up", ())))

    result = _desktop().drag([400, 300], from_loc=[50, 60], duration=0.5)

    down = calls.index(("down", (50, 60)))
    assert calls[down - 1] == ("move", (50, 60))
    assert calls[-2:] == [("move", (400, 300)), ("up", ())]
    assert len([c for c in calls[down:] if c[0] == "move"]) > 10
    assert result == {"start": [50, 60], "end": [400, 300], "duration": 0.5}
