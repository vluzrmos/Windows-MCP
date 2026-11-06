"""Live identity verification and the actionability battery."""

import pytest

from tests.elements_fakes import FakeElement, FakeRect, make_handle
from windows_mcp.elements import actionability
from windows_mcp.elements.identity import read_live
from windows_mcp.elements.report import ElementError


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actionability, "_STABILITY_GAP", 0)
    monkeypatch.setattr(actionability, "_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(actionability, "_virtual_screen", lambda: (0, 0, 1920, 1080))


def test_read_live_returns_live_bounds() -> None:
    element = FakeElement(rect=FakeRect(100, 100, 200, 140))
    live = read_live(make_handle(element))
    assert live.center == (150, 120)
    assert live.is_enabled


def test_dead_element_is_stale() -> None:
    element = FakeElement(dead=True)
    with pytest.raises(ElementError) as excinfo:
        read_live(make_handle(element), label=3)
    assert excinfo.value.code == "STALE_REF"
    assert excinfo.value.retry == "safe"
    assert "label 3" in str(excinfo.value)


def test_recycled_runtime_id_is_stale() -> None:
    element = FakeElement(runtime_id=(42, 2))
    handle = make_handle(element, runtime_id=(42, 1))
    with pytest.raises(ElementError, match="different element"):
        read_live(handle)


def test_changed_control_type_is_stale() -> None:
    element = FakeElement(ControlTypeName="EditControl")
    handle = make_handle(element, control_type_name="ButtonControl")
    with pytest.raises(ElementError, match="changed from ButtonControl"):
        read_live(handle)


def test_name_change_is_tolerated_when_runtime_id_matches() -> None:
    element = FakeElement(Name="Pause")
    handle = make_handle(element, name="Play")
    assert read_live(handle).name == "Pause"


def _hit(monkeypatch: pytest.MonkeyPatch, hit_element) -> None:
    import windows_mcp.uia as uia

    monkeypatch.setattr(uia, "ControlFromPoint", lambda x, y: hit_element)


def test_actionable_element_passes_every_check(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement()
    _hit(monkeypatch, element)
    result = actionability.wait_until_actionable(make_handle(element), pointer=True, timeout=0.1)
    assert result.actionable
    assert result.point == (60, 25)
    assert {c.name: c.status for c in result.checks}["receives_events"] == "pass"


def test_hit_on_a_child_reaches_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement()
    child = FakeElement(Name="icon", runtime_id=(42, 9), parent=element)
    _hit(monkeypatch, child)
    result = actionability.evaluate(make_handle(element), pointer=True)
    assert result.actionable


def test_covered_element_fails_with_the_occluder(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement()
    overlay = FakeElement(Name="Cookie banner", ControlTypeName="PaneControl", runtime_id=(7, 7))
    _hit(monkeypatch, overlay)
    with pytest.raises(ElementError) as excinfo:
        actionability.wait_until_actionable(make_handle(element), pointer=True, timeout=0.01)
    assert excinfo.value.code == "NOT_ACTIONABLE"
    assert 'covered by pane "Cookie banner"' in str(excinfo.value)


def test_unknown_hit_test_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement()
    _hit(monkeypatch, None)
    result = actionability.evaluate(make_handle(element), pointer=True)
    assert result.actionable
    assert {c.name: c.status for c in result.checks}["receives_events"] == "unknown"


def test_disabled_element_times_out_naming_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement(IsEnabled=False)
    _hit(monkeypatch, element)
    with pytest.raises(ElementError, match="enabled=fail"):
        actionability.wait_until_actionable(make_handle(element), pointer=False, timeout=0.01)


def test_element_that_becomes_enabled_is_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement(IsEnabled=False)
    _hit(monkeypatch, element)
    calls = {"n": 0}
    original = actionability.evaluate

    def flip(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            element.IsEnabled = True
        return original(*args, **kwargs)

    monkeypatch.setattr(actionability, "evaluate", flip)
    result = actionability.wait_until_actionable(make_handle(element), pointer=True, timeout=1)
    assert result.actionable
    assert calls["n"] >= 3


def test_moving_element_is_not_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement(rects=[FakeRect(0, 0, 50, 20), FakeRect(5, 0, 55, 20)])
    _hit(monkeypatch, element)
    result = actionability.evaluate(make_handle(element), pointer=False)
    assert {c.name: c.status for c in result.checks}["stable"] == "fail"


def test_offscreen_element_is_scrolled_into_view(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.elements_fakes import PatternId

    element = FakeElement(IsOffscreen=True)

    class ScrollItem:
        def ScrollIntoView(self, waitTime: float = 0) -> bool:
            element.IsOffscreen = False
            return True

    element.patterns = {PatternId.ScrollItemPattern: ScrollItem()}
    _hit(monkeypatch, element)
    result = actionability.wait_until_actionable(make_handle(element), pointer=True, timeout=0.5)
    assert result.actionable
    assert result.scrolled_into_view
