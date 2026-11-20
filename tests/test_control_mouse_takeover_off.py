"""With ``mouse_takeover`` off, a bumped mouse never takes control from the AI.

Only the keyboard (the takeover hotkey, or any key) hands control to the person; once
they hold it, the mouse keeps extending that hold like before.
"""

import ctypes

import pytest

from windows_mcp.desktop import control, control_hooks
from windows_mcp.infrastructure.config import WindowsMCPConfig, load_config, write_config


def controller(state: str) -> control.ControlCoordinator:
    owner = control.ControlCoordinator()
    owner.mouse_takeover = False
    owner._state = state
    return owner


def mouse_event(owner, x: int, y: int, message: int = 0x200, injected: bool = False) -> int:
    data = control._MouseHookData()
    data.flags = 1 if injected else 0
    data.pt.x, data.pt.y = x, y
    return owner._physical_mouse(0, message, ctypes.addressof(data))


def key_event(owner, vk: int, down: bool = True) -> int:
    data = control._KeyHookData()
    data.vkCode = vk
    return owner._physical_key(0, 0x100 if down else 0x101, ctypes.addressof(data))


@pytest.fixture(autouse=True)
def passthrough_hook(monkeypatch):
    monkeypatch.setattr(control._user32, "CallNextHookEx", lambda *args: 0)


@pytest.mark.parametrize("state", ["ready", "ai"])
def test_mouse_moves_and_clicks_do_not_count(state):
    owner = controller(state)
    mouse_event(owner, 400, 300, injected=True)
    mouse_event(owner, 900, 700)
    mouse_event(owner, 900, 700, 0x201)
    assert owner._last_physical_event == 0.0
    assert owner._events.empty()
    assert owner._mouse_down == {1}  # Button state is still tracked for release logic.


def test_raw_motion_does_not_count(monkeypatch):
    owner = controller("ai")
    monkeypatch.setattr(control_hooks, "_read_raw_mouse", lambda _: (1, 5000, 5000))
    owner._raw_input(0)
    assert owner._last_physical_event == 0.0
    assert owner._events.empty()


def test_ready_state_survives_mouse_motion():
    owner = controller("ready")
    mouse_event(owner, 400, 300, injected=True)
    for x in range(401, 900, 7):
        mouse_event(owner, x, 300)
    assert owner.status()["state"] == "ready"
    owner.begin_call("Click")  # Must not raise USER_CONTROL.


def test_key_still_takes_control():
    owner = controller("ready")
    key_event(owner, 0x41)
    assert owner._last_physical_event > 0
    assert owner._events.get_nowait() == ("key",)
    assert owner.status()["state"] == "user"


def test_mouse_extends_a_hold_the_person_already_has(monkeypatch):
    owner = controller("user")
    monkeypatch.setattr(control_hooks, "_read_raw_mouse", lambda _: (1, 3, 0))
    mouse_event(owner, 400, 300, injected=True)
    mouse_event(owner, 410, 300)
    owner._raw_input(0)
    assert owner._last_physical_event > 0
    assert owner._events.get_nowait() == ("point", 410, 300, 0x200)


def test_default_keeps_mouse_takeover():
    owner = control.ControlCoordinator()
    owner._state = "ready"
    mouse_event(owner, 400, 300, injected=True)
    mouse_event(owner, 401, 300)
    assert owner.status()["state"] == "user"


def test_config_round_trip(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[control]\nmouse_takeover = false\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.control.mouse_takeover is False

    write_config(cfg, path)
    assert load_config(path).control.mouse_takeover is False
    assert WindowsMCPConfig().control.mouse_takeover is True


def test_config_rejects_non_bool(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[control]\nmouse_takeover = "no"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)
