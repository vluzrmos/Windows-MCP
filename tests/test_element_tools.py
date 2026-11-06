"""Tool-level tests: labels in the tree, Find, ElementAction, Click modes, WaitFor, Batch."""

import asyncio
from collections.abc import Callable

import pytest

from tests.elements_fakes import (
    FakeElement,
    FakeInvoke,
    FakeRect,
    FakeToggle,
    PatternId,
    ToggleState,
    make_handle,
)
from windows_mcp.desktop.views import DesktopState, Status, Window
from windows_mcp.elements import actionability, safety, semantic
from windows_mcp.elements.report import ElementError
from windows_mcp.tree.views import (
    BoundingBox,
    Center,
    SemanticNode,
    TreeElementNode,
    TreeState,
    _prune_structural,
)


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.tools[name] = func
            return func

        return decorator


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actionability, "_STABILITY_GAP", 0)
    monkeypatch.setattr(actionability, "_POLL_INTERVAL", 0.001)
    monkeypatch.setattr(actionability, "_virtual_screen", lambda: (0, 0, 1920, 1080))
    monkeypatch.setattr(semantic, "_SETTLE", 0)
    monkeypatch.setattr(safety, "input_block_reason_for_pid", lambda pid: None)
    monkeypatch.setattr(safety, "input_block_reason_at", lambda x, y: None)
    from windows_mcp.desktop import surfaces

    monkeypatch.setattr(surfaces, "list_surfaces", lambda kinds=None: [])


def _box(left=10, top=10, right=110, bottom=40) -> BoundingBox:
    return BoundingBox(
        left=left, top=top, right=right, bottom=bottom, width=right - left, height=bottom - top
    )


def _node(
    name: str, element: FakeElement | None, control_type: str = "Button", **metadata
) -> TreeElementNode:
    box = _box()
    return TreeElementNode(
        name=name,
        control_type=control_type,
        window_name="App",
        bounding_box=box,
        center=box.get_center(),
        metadata=dict(metadata),
        element=make_handle(element) if element is not None else None,
    )


def _state(nodes: list[TreeElementNode], snapshot_id: str = "sabc123") -> DesktopState:
    tree_state = TreeState(interactive_nodes=nodes, snapshot_id=snapshot_id)
    tree_state.assign_labels()
    window = Window(
        name="App",
        is_browser=False,
        depth=0,
        status=Status.NORMAL,
        bounding_box=_box(),
        handle=1,
        process_id=2,
    )
    return DesktopState(
        active_desktop={"name": "Desktop 1"},
        all_desktops=[{"name": "Desktop 1"}],
        active_window=window,
        windows=[],
        tree_state=tree_state,
    )


class FakeDesktop:
    def __init__(self, nodes: list[TreeElementNode]) -> None:
        self.desktop_state = _state(nodes)
        self.clicks: list[dict] = []
        self.get_state_calls: list[dict] = []

    def get_node_from_label(self, label: int):
        nodes = self.desktop_state.tree_state.labelled_nodes()
        if label < 0 or label >= len(nodes):
            raise IndexError(f"Label {label} out of range")
        return nodes[label]

    def get_coordinates_from_label(self, label: int):
        center = self.get_node_from_label(label).center
        return center.x, center.y

    def click(self, loc, button="left", clicks=1):
        self.clicks.append({"loc": list(loc), "button": button, "clicks": clicks})

    def get_state(self, **kwargs):
        self.get_state_calls.append(kwargs)
        return self.desktop_state


def _tools(desktop) -> dict[str, Callable]:
    from windows_mcp.tools import register_all

    mcp = FakeMCP()
    register_all(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return mcp.tools


def _hit(monkeypatch: pytest.MonkeyPatch, element) -> None:
    import windows_mcp.uia as uia

    monkeypatch.setattr(uia, "ControlFromPoint", lambda x, y: element)


# --- semantic tree rendering -------------------------------------------------


def test_semantic_tree_shows_labels_and_cut_containers() -> None:
    button = _node("OK", None)
    button.label = 4
    container = _node("Sidebar", None, control_type="Pane", children_cut=12)
    container.label = 7
    root = SemanticNode(control_type="Desktop", element_type="desktop")
    window = SemanticNode(control_type="Window", element_type="window", name="App")
    root.add_child(window)
    window.add_child(
        SemanticNode(
            control_type="Button",
            element_type="interactive",
            name="OK",
            center=Center(60, 25),
            source=button,
        )
    )
    window.add_child(
        SemanticNode(
            control_type="Pane",
            element_type="structural",
            name="Sidebar",
            metadata={"children_cut": 12},
            source=container,
        )
    )
    _prune_structural(root)  # a cut container has no children but must survive pruning
    text = TreeState(semantic_tree_root=root).semantic_tree_to_string()
    assert '#4 (60,25) button "OK"' in text
    assert '#7 pane "Sidebar"  [+12 children not expanded' in text


def test_labels_run_across_interactive_scrollable_and_containers() -> None:
    state = TreeState(
        interactive_nodes=[_node("a", None), _node("b", None)],
        container_nodes=[_node("c", None)],
    )
    state.assign_labels()
    assert [n.label for n in state.labelled_nodes()] == [0, 1, 2]


# --- Click ---------------------------------------------------------------------


def test_click_label_aims_at_the_live_position(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement(rect=FakeRect(500, 500, 600, 540))  # moved since the snapshot
    _hit(monkeypatch, element)
    desktop = FakeDesktop([_node("OK", element)])
    result = asyncio.run(_tools(desktop)["Click"](label=0))
    assert desktop.clicks == [{"loc": [550, 520], "button": "left", "clicks": 1}]
    assert "target verified live" in result
    assert "delivered_unverified, retry: unsafe" in result


def test_click_stale_label_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop = FakeDesktop([_node("OK", FakeElement(dead=True))])
    with pytest.raises(ElementError, match="STALE_REF"):
        asyncio.run(_tools(desktop)["Click"](label=0))
    assert desktop.clicks == []


def test_click_rejects_label_from_older_snapshot() -> None:
    desktop = FakeDesktop([_node("OK", FakeElement())])
    with pytest.raises(ElementError, match="SNAPSHOT_MISMATCH"):
        asyncio.run(_tools(desktop)["Click"](label=0, snapshot_id="sold000"))
    assert desktop.clicks == []


def test_click_label_without_element_uses_captured_coordinates() -> None:
    desktop = FakeDesktop([_node("word", None, control_type="Word")])
    result = asyncio.run(_tools(desktop)["Click"](label=0))
    assert desktop.clicks[0]["loc"] == [60, 25]
    assert "captured coordinates" in result


def test_semantic_click_invokes_without_mouse_input() -> None:
    element = FakeElement()
    invoke = FakeInvoke()
    element.patterns = {PatternId.InvokePattern: invoke}
    desktop = FakeDesktop([_node("OK", element)])
    result = asyncio.run(_tools(desktop)["Click"](label=0, mode="semantic"))
    assert invoke.calls == 1
    assert desktop.clicks == []
    assert "InvokePattern.Invoke=succeeded" in result


def test_auto_click_falls_back_to_physical_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    element = FakeElement()  # no patterns
    _hit(monkeypatch, element)
    desktop = FakeDesktop([_node("OK", element)])
    asyncio.run(_tools(desktop)["Click"](label=0, mode="auto"))
    assert len(desktop.clicks) == 1


def test_semantic_click_rejects_right_button() -> None:
    desktop = FakeDesktop([_node("OK", FakeElement())])
    with pytest.raises(ElementError, match="ACTION_NOT_SUPPORTED"):
        asyncio.run(_tools(desktop)["Click"](label=0, mode="semantic", button="right"))


# --- ElementAction ---------------------------------------------------------------


def test_element_action_check_and_read() -> None:
    element = FakeElement(ControlTypeName="CheckBoxControl", Name="Wrap")
    element.patterns = {PatternId.TogglePattern: FakeToggle(element, ToggleState.Off)}
    desktop = FakeDesktop([_node("Wrap", element, control_type="Check Box")])
    tools = _tools(desktop)
    out = asyncio.run(tools["ElementAction"](label=0, action="check"))
    assert "delivered_verified" in out and "toggle='on'" in out
    out = asyncio.run(tools["ElementAction"](label=0, action="read"))
    assert "not_delivered (retry: safe)" in out


def test_element_action_on_node_without_element() -> None:
    desktop = FakeDesktop([_node("word", None, control_type="Word")])
    with pytest.raises(ElementError, match="ACTION_NOT_SUPPORTED"):
        asyncio.run(_tools(desktop)["ElementAction"](label=0, action="invoke"))


def test_element_action_refuses_higher_integrity_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "input_block_reason_for_pid", lambda pid: "PERM_DENIED: elevated")
    element = FakeElement()
    element.patterns = {PatternId.InvokePattern: FakeInvoke()}
    desktop = FakeDesktop([_node("OK", element)])
    with pytest.raises(ElementError, match="PERM_DENIED"):
        asyncio.run(_tools(desktop)["ElementAction"](label=0, action="invoke"))


# --- Find ----------------------------------------------------------------------


def test_find_filters_and_reports_labels() -> None:
    desktop = FakeDesktop(
        [
            _node("Save", FakeElement(Name="Save", AutomationId="save")),
            _node("Save As", FakeElement(Name="Save As", AutomationId="saveas")),
            _node(
                "Wrap",
                FakeElement(Name="Wrap", ControlTypeName="CheckBoxControl"),
                control_type="Check Box",
                toggle_state="off",
            ),
        ]
    )
    tools = _tools(desktop)
    out = asyncio.run(tools["Find"](name="save", exact=True))
    assert (
        "1 match(es)" in out and '#0 (60,25) button "Save"' in out and "automation_id:save" in out
    )
    out = asyncio.run(tools["Find"](automation_id="saveas"))
    assert '#1 (60,25) button "Save As"' in out
    out = asyncio.run(tools["Find"](toggle="off"))
    assert '"Wrap"' in out
    out = asyncio.run(tools["Find"](control_type="slider"))
    assert "No element matched" in out and "control types present: button, check box" in out


def test_find_requires_a_filter() -> None:
    with pytest.raises(ValueError, match="at least one filter"):
        asyncio.run(_tools(FakeDesktop([]))["Find"]())


# --- WaitFor -------------------------------------------------------------------


def test_wait_for_window_opened_uses_window_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    from windows_mcp.elements import waits

    titles = iter([["Explorer"], ["Explorer", "Save As"]])
    monkeypatch.setattr(waits, "window_titles", lambda: next(titles))
    out = asyncio.run(
        _tools(FakeDesktop([]))["WaitFor"](
            condition="window_opened", text="save as", timeout=1, interval=0.001
        )
    )
    assert "window 'Save As' is open" in out


def test_wait_for_window_timeout_reports_open_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from windows_mcp.elements import waits

    monkeypatch.setattr(waits, "window_titles", lambda: ["Untitled - Notepad"])
    with pytest.raises(TimeoutError, match="open windows: 'Untitled - Notepad'"):
        asyncio.run(
            _tools(FakeDesktop([]))["WaitFor"](
                condition="window_opened", text="Save As", timeout=0.01, interval=0.001
            )
        )


def test_wait_for_element_gone() -> None:
    desktop = FakeDesktop([_node("Loading", None)])
    tools = _tools(desktop)
    with pytest.raises(TimeoutError, match="still present"):
        asyncio.run(
            tools["WaitFor"](condition="element_gone", text="loading", timeout=0.01, interval=0.001)
        )
    desktop.desktop_state = _state([_node("Done", None)])
    out = asyncio.run(
        tools["WaitFor"](condition="element_gone", text="loading", timeout=1, interval=0.001)
    )
    assert "matching element is gone" in out


def test_wait_for_element_value_polls_one_element(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.elements_fakes import FakeValue

    element = FakeElement(ControlTypeName="EditControl")
    value = FakeValue("1")
    element.patterns = {PatternId.ValuePattern: value}
    desktop = FakeDesktop([_node("Display", element, control_type="Edit")])
    reads = {"n": 0}
    original = semantic.read_state

    def read_state(handle):
        reads["n"] += 1
        if reads["n"] == 2:
            value.Value = "7"
        return original(handle)

    monkeypatch.setattr(semantic, "read_state", read_state)
    out = asyncio.run(
        _tools(desktop)["WaitFor"](
            condition="element_value", label=0, text="7", timeout=1, interval=0.001
        )
    )
    assert "reads '7'" in out
    assert desktop.get_state_calls == []  # never re-captured the tree


def test_wait_for_label_condition_requires_label() -> None:
    with pytest.raises(ValueError, match="label is required"):
        asyncio.run(_tools(FakeDesktop([]))["WaitFor"](condition="element_actionable"))


# --- Batch ---------------------------------------------------------------------


def test_batch_runs_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    element = FakeElement()
    element.patterns = {PatternId.InvokePattern: FakeInvoke()}
    desktop = FakeDesktop([_node("OK", element)])
    out = asyncio.run(
        _tools(desktop)["Batch"](
            steps=[
                {"tool": "ElementAction", "args": {"label": 0, "action": "invoke"}},
                {"tool": "Find", "args": {"name": "OK"}},
            ]
        )
    )
    assert out.startswith("Batch: 2 ok, 0 failed, 0 not_started of 2")
    assert "[0] ElementAction: ok" in out and "[1] Find: ok" in out


def test_batch_validates_everything_before_running() -> None:
    desktop = FakeDesktop([_node("OK", FakeElement())])
    tools = _tools(desktop)
    with pytest.raises(ValueError, match="unknown argument"):
        asyncio.run(
            tools["Batch"](
                steps=[
                    {"tool": "Click", "args": {"label": 0}},
                    {"tool": "Click", "args": {"nope": 1}},
                ]
            )
        )
    with pytest.raises(ValueError, match="missing required argument"):
        asyncio.run(tools["Batch"](steps=[{"tool": "ElementAction", "args": {"label": 0}}]))
    with pytest.raises(ValueError, match="cannot be used inside Batch"):
        asyncio.run(tools["Batch"](steps=[{"tool": "Batch", "args": {"steps": []}}]))
    assert desktop.clicks == []


def test_batch_stops_on_error_and_reports_not_started() -> None:
    desktop = FakeDesktop([_node("OK", FakeElement(dead=True))])
    # steps may also arrive JSON-encoded (Claude Desktop flattens list schemas).
    out = asyncio.run(
        _tools(desktop)["Batch"](
            steps='[{"tool": "Click", "args": {"label": 0}}, {"tool": "Find", "args": {"name": "OK"}}]'
        )
    )
    assert "0 ok, 1 failed, 1 not_started" in out
    assert "STALE_REF" in out
    assert "Stopped: stop_on_error at step 0" in out
