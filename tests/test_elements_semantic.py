"""Semantic element actions, read-back verification and delivery dispositions."""

import pytest

from tests.elements_fakes import (
    ExpandCollapseState,
    FakeElement,
    FakeExpand,
    FakeInvoke,
    FakeSelectionItem,
    FakeToggle,
    FakeValue,
    PatternId,
    ToggleState,
    make_handle,
)
from windows_mcp.elements import semantic
from windows_mcp.elements.report import ElementError


@pytest.fixture(autouse=True)
def no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semantic, "_SETTLE", 0)


def _element(**patterns) -> FakeElement:
    element = FakeElement()
    element.patterns = {getattr(PatternId, name): pattern for name, pattern in patterns.items()}
    return element


def test_check_is_verified_and_idempotent() -> None:
    element = FakeElement(ControlTypeName="CheckBoxControl")
    toggle = FakeToggle(element, ToggleState.Off)
    element.patterns = {PatternId.TogglePattern: toggle}
    handle = make_handle(element)

    report = semantic.perform(handle, "check")
    assert report.delivery == "delivered_verified"
    assert report.retry == "unsafe"
    assert report.post_state == {"toggle": "on"}

    again = semantic.perform(handle, "check")
    assert again.delivery == "not_delivered"
    assert again.retry == "safe"
    assert toggle.calls == 1


def test_toggle_that_does_not_change_reports_contradiction() -> None:
    element = FakeElement(ControlTypeName="CheckBoxControl")
    element.patterns = {PatternId.TogglePattern: FakeToggle(element, ToggleState.Off, sticky=True)}
    with pytest.raises(ElementError) as excinfo:
        semantic.perform(make_handle(element), "toggle")
    assert excinfo.value.code == "ACTION_FAILED"
    assert excinfo.value.delivery == "delivered_unverified"
    assert excinfo.value.retry == "unsafe"


def test_set_value_reads_back_the_value() -> None:
    value = FakeValue("old")
    report = semantic.perform(make_handle(_element(ValuePattern=value)), "set_value", value="new")
    assert value.writes == ["new"]
    assert report.delivery == "delivered_verified"
    assert report.post_state["value"] == "new"


def test_set_value_mismatch_is_an_unverified_delivery() -> None:
    value = FakeValue("old", ignore_writes=True)
    with pytest.raises(ElementError) as excinfo:
        semantic.perform(make_handle(_element(ValuePattern=value)), "set_value", value="new")
    assert excinfo.value.code == "ACTION_FAILED"
    assert excinfo.value.delivery == "delivered_unverified"


def test_read_only_field_refuses_before_writing() -> None:
    value = FakeValue("x", read_only=True)
    with pytest.raises(ElementError) as excinfo:
        semantic.perform(make_handle(_element(ValuePattern=value)), "set_value", value="y")
    assert excinfo.value.code == "ACTION_NOT_SUPPORTED"
    assert excinfo.value.retry == "safe"
    assert value.writes == []


def test_secure_field_value_is_never_read_back() -> None:
    element = _element(ValuePattern=FakeValue(""))
    element.IsPassword = True
    handle = make_handle(element, is_password=True)
    report = semantic.perform(handle, "set_value", value="hunter2")
    assert report.delivery == "delivered_unverified"
    assert "hunter2" not in report.to_text()
    assert semantic.read_state(handle)["value"] == "<secure>"


def test_invoke_prefers_invoke_pattern() -> None:
    invoke = FakeInvoke()
    report = semantic.perform(make_handle(_element(InvokePattern=invoke)), "invoke")
    assert invoke.calls == 1
    assert report.delivery == "delivered_unverified"
    assert report.steps[0].label == "InvokePattern.Invoke"


def test_invoke_falls_back_to_expand_collapse() -> None:
    expand = FakeExpand(ExpandCollapseState.Collapsed)
    report = semantic.perform(make_handle(_element(ExpandCollapsePattern=expand)), "invoke")
    assert expand.calls == ["expand"]
    assert report.steps[0].label == "ExpandCollapsePattern.Expand"


def test_failed_invoke_is_delivery_uncertain() -> None:
    with pytest.raises(ElementError) as excinfo:
        semantic.perform(make_handle(_element(InvokePattern=FakeInvoke(fail=True))), "invoke")
    assert excinfo.value.delivery == "delivery_uncertain"
    assert excinfo.value.retry == "unsafe"


def test_unsupported_action_is_not_delivered() -> None:
    with pytest.raises(ElementError) as excinfo:
        semantic.perform(make_handle(_element()), "expand")
    assert excinfo.value.code == "ACTION_NOT_SUPPORTED"
    assert excinfo.value.delivery == "not_delivered"


def test_expand_and_collapse_are_idempotent() -> None:
    expand = FakeExpand(ExpandCollapseState.Expanded)
    handle = make_handle(_element(ExpandCollapsePattern=expand))
    assert semantic.perform(handle, "expand").delivery == "not_delivered"
    report = semantic.perform(handle, "collapse")
    assert report.delivery == "delivered_verified"
    assert expand.calls == ["collapse"]


def test_selection_actions() -> None:
    item = FakeSelectionItem(selected=False)
    handle = make_handle(_element(SelectionItemPattern=item))
    assert semantic.perform(handle, "select").delivery == "delivered_verified"
    assert semantic.perform(handle, "add_to_selection").delivery == "not_delivered"
    assert semantic.perform(handle, "remove_from_selection").post_state == {"selected": False}


def test_focus_is_verified() -> None:
    element = _element()
    report = semantic.perform(make_handle(element), "focus")
    assert element.HasKeyboardFocus
    assert report.delivery == "delivered_verified"


def test_read_reports_state_without_delivering() -> None:
    element = FakeElement(ControlTypeName="CheckBoxControl")
    element.patterns = {PatternId.TogglePattern: FakeToggle(element, ToggleState.On)}
    report = semantic.perform(make_handle(element), "read")
    assert report.delivery == "not_delivered"
    assert report.post_state["toggle"] == "on"
    assert report.post_state["bounds"] == (10, 10, 110, 40)


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ElementError, match="INVALID_ARGS"):
        semantic.perform(make_handle(_element()), "explode")
