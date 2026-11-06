"""Live wait predicates that do not need a full UI-tree capture.

WaitFor's original conditions re-capture the accessibility tree on every poll. The
predicates here read only what they need — Win32 window titles, process lists, one
element's live state — so they are cheap to poll and never replace the labels of the
latest Snapshot.
"""

from __future__ import annotations

from typing import Any

from windows_mcp.elements.identity import ElementHandle

WINDOW_CONDITIONS = frozenset({"window_opened", "window_closed"})
SURFACE_CONDITIONS = frozenset({"menu_open", "menu_closed", "dialog_open", "dialog_closed"})
PROCESS_CONDITIONS = frozenset({"process_started", "process_exited"})
ELEMENT_CONDITIONS = frozenset({"element_value", "element_actionable", "element_stale"})
LIVE_CONDITIONS = WINDOW_CONDITIONS | SURFACE_CONDITIONS | PROCESS_CONDITIONS | ELEMENT_CONDITIONS


def _casefold_contains(haystack: str | None, needle: str | None) -> bool:
    if needle is None:
        return True
    return haystack is not None and needle.casefold() in str(haystack).casefold()


def window_titles() -> list[str]:
    from windows_mcp.desktop.surfaces import _visible_top_level_windows

    return [title for _, _, title in _visible_top_level_windows() if title]


def check_window(condition: str, text: str) -> tuple[bool, str]:
    titles = window_titles()
    matches = [title for title in titles if _casefold_contains(title, text)]
    if condition == "window_opened":
        if matches:
            return True, f"window {matches[0]!r} is open"
    elif not matches:
        return True, f"no window title contains {text!r}"
    shown = ", ".join(repr(t) for t in (matches or titles)[:8])
    label = "matching windows" if matches else "open windows"
    return False, f"{label}: {shown or 'none'}"


def check_surface(condition: str, text: str | None) -> tuple[bool, str]:
    from windows_mcp.desktop.surfaces import list_surfaces

    kind = "menu" if condition.startswith("menu") else "dialog"
    surfaces = [s for s in list_surfaces((kind,)) if _casefold_contains(s.title, text)]
    want_open = condition.endswith("_open")
    if want_open and surfaces:
        return True, f"{surfaces[0].describe()} is open"
    if not want_open and not surfaces:
        return True, f"no {kind} is open"
    if surfaces:
        return False, "open: " + ", ".join(s.describe() for s in surfaces[:5])
    return False, f"no {kind} is open"


def check_process(condition: str, text: str) -> tuple[bool, str]:
    import psutil

    wanted_pid = int(text) if text.strip().isdigit() else None
    wanted_name = None if wanted_pid is not None else text.strip().casefold()
    if wanted_name and not wanted_name.endswith(".exe"):
        wanted_name_exe = f"{wanted_name}.exe"
    else:
        wanted_name_exe = wanted_name
    running = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").casefold()
        if wanted_pid is not None and proc.info["pid"] == wanted_pid:
            running.append(f"{name} (PID {proc.info['pid']})")
        elif wanted_name and name in (wanted_name, wanted_name_exe):
            running.append(f"{name} (PID {proc.info['pid']})")
    if condition == "process_started":
        if running:
            return True, f"running: {', '.join(running[:5])}"
        return False, f"no process matches {text!r}"
    if not running:
        return True, f"no process matches {text!r}"
    return False, f"still running: {', '.join(running[:5])}"


def check_element(
    condition: str, handle: ElementHandle, label: int, text: str | None
) -> tuple[bool, str]:
    from windows_mcp.elements.actionability import evaluate
    from windows_mcp.elements.report import ElementError
    from windows_mcp.elements.semantic import read_state

    if condition == "element_stale":
        try:
            evaluate(handle, pointer=False, label=label)
        except ElementError as error:
            if error.code == "STALE_REF":
                return True, f"label {label} is gone"
            raise
        return False, f"label {label} still exists"
    if condition == "element_actionable":
        result = evaluate(handle, pointer=True, label=label)
        if result.actionable:
            return True, f"label {label} is actionable ({result.summary()})"
        return False, result.summary()
    from windows_mcp.elements.identity import read_live

    read_live(handle, label=label)  # STALE_REF when the element is gone
    state = read_state(handle)
    observed: Any = state.get(
        "value", state.get("range_value", state.get("toggle", state.get("name")))
    )
    if _casefold_contains(observed, text):
        return True, f"label {label} reads {observed!r}"
    return False, f"label {label} reads {observed!r}"
