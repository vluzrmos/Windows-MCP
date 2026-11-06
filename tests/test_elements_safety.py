"""Protected processes, blocked shortcuts and the UIPI integrity verdict."""

import asyncio
from collections.abc import Callable

import pytest

from windows_mcp.elements import safety
from windows_mcp.elements.report import ElementError
from windows_mcp.process import service as process_service
from windows_mcp.tools.input import register


@pytest.mark.parametrize(
    "identifier",
    [
        "explorer.exe",
        "EXPLORER.EXE",
        "explorer",
        r"C:\Windows\explorer.exe",
        "C:/Windows/System32/lsass.exe",
        "dwm",
    ],
)
def test_protected_processes_match_exact_image_names(identifier: str) -> None:
    assert safety.is_protected_process(identifier)


@pytest.mark.parametrize("identifier", ["iexplore.exe", "explorer++.exe", "notlsass.exe", "", None])
def test_lookalikes_are_not_protected(identifier) -> None:
    assert not safety.is_protected_process(identifier)


@pytest.mark.parametrize(
    ("shortcut", "blocked"),
    [
        ("alt+f4", "alt+f4"),
        ("ALT+F4", "alt+f4"),
        ("alt+shift+tab", "alt+tab"),
        ("shift+alt+tab", "alt+tab"),
        ("win+l", "win+l"),
        ("windows+d", "win+d"),
        ("ctrl+shift+escape", "ctrl+shift+esc"),
        ("ctrl+alt+f4", "alt+f4"),
    ],
)
def test_blocked_shortcuts_include_supersets_and_aliases(shortcut: str, blocked: str) -> None:
    assert safety.blocked_shortcut_match(shortcut) == blocked


@pytest.mark.parametrize(
    "shortcut", ["ctrl+c", "ctrl+v", "win", "tab", "alt", "f4", "ctrl+shift+s", "enter"]
)
def test_ordinary_shortcuts_are_allowed(shortcut: str) -> None:
    assert safety.blocked_shortcut_match(shortcut) is None


def test_integrity_verdict_blocks_only_a_higher_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "current_integrity_level", lambda: 0x2000)
    monkeypatch.setattr(safety, "process_integrity_level", lambda pid: 0x3000)
    reason = safety.input_block_reason_for_pid(1234)
    assert reason is not None and "high integrity" in reason and "medium" in reason

    monkeypatch.setattr(safety, "process_integrity_level", lambda pid: 0x2000)
    assert safety.input_block_reason_for_pid(1234) is None


def test_integrity_verdict_fails_open_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "process_integrity_level", lambda pid: None)
    assert safety.input_block_reason_for_pid(1234) is None
    monkeypatch.setattr(safety, "process_integrity_level", lambda pid: 0x4000)
    monkeypatch.setattr(safety, "current_integrity_level", lambda: None)
    assert safety.input_block_reason_for_pid(1234) is None
    assert safety.input_block_reason_for_pid(None) is None


def test_own_process_integrity_is_readable() -> None:
    assert safety.current_integrity_level() is not None


def test_process_kill_refuses_protected_names_even_with_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    def fail(*args, **kwargs):
        raise AssertionError("no process may be enumerated or killed")

    monkeypatch.setattr(psutil, "process_iter", fail)
    for name in ("explorer", "explorer.exe", "winlogon.exe"):
        result = process_service.kill_process(name=name, force=True)
        assert result.startswith("Refused:")


def test_process_kill_refuses_protected_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return "csrss.exe"

        def kill(self):
            raise AssertionError("must not kill")

        terminate = kill

    monkeypatch.setattr(psutil, "Process", FakeProcess)
    assert process_service.kill_process(pid=4321, force=True).startswith("Refused:")


class FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *, name: str, **kwargs: object) -> Callable:
        def decorator(func: Callable) -> Callable:
            self.tools[name] = func
            return func

        return decorator


class ShortcutDesktop:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def shortcut(self, shortcut: str) -> None:
        self.pressed.append(shortcut)


def _shortcut_tool(desktop: ShortcutDesktop) -> Callable:
    mcp = FakeMCP()
    register(mcp, get_desktop=lambda: desktop, get_analytics=lambda: None)
    return mcp.tools["Shortcut"]


def test_shortcut_tool_refuses_blocked_combo_without_force() -> None:
    desktop = ShortcutDesktop()
    tool = _shortcut_tool(desktop)
    with pytest.raises(ElementError, match="POLICY_DENIED"):
        asyncio.run(tool(shortcut="alt+tab"))
    assert desktop.pressed == []


def test_shortcut_tool_sends_blocked_combo_with_force() -> None:
    desktop = ShortcutDesktop()
    tool = _shortcut_tool(desktop)
    assert asyncio.run(tool(shortcut="alt+tab", force=True)) == "Pressed alt+tab."
    assert asyncio.run(tool(shortcut="ctrl+c")) == "Pressed ctrl+c."
    assert desktop.pressed == ["alt+tab", "ctrl+c"]
