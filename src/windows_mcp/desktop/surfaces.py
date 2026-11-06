"""UI surfaces: overlays and shell areas observed separately from a whole window.

While a menu, dialog or shell flyout is open, the elements of the window behind it are
the wrong target. ``Snapshot(surface=...)`` reads just that layer, and Click /
ElementAction report the surfaces an action opened (``opened: menu``), so the agent knows
to read the overlay next instead of polling.

Detection is pure Win32 (EnumWindows + class names + owning process), so it is cheap
enough to run before and after every click.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

SurfaceKind = Literal[
    "focused",
    "menu",
    "dialog",
    "taskbar",
    "system_tray",
    "start_menu",
    "action_center",
]

SURFACE_KINDS: tuple[str, ...] = (
    "focused",
    "menu",
    "dialog",
    "taskbar",
    "system_tray",
    "start_menu",
    "action_center",
)

# Popup menu window classes: classic Win32, WinUI/XAML islands, Office, Chromium/Electron.
_MENU_CLASSES = frozenset(
    {
        "#32768",
        "Xaml_WindowedPopupClass",
        "Microsoft.UI.Content.PopupWindowSiteBridge",
        "Net UI Tool Window",
        "Chrome_WidgetWin_2",
    }
)
_DIALOG_CLASSES = frozenset({"#32770", "NUIDialog", "bosa_sdm_msword"})
_TASKBAR_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
_TRAY_OVERFLOW_CLASSES = frozenset(
    {"TopLevelWindowForOverflowXamlIsland", "NotifyIconOverflowWindow"}
)
_START_PROCESSES = frozenset({"startmenuexperiencehost.exe", "searchhost.exe", "searchapp.exe"})
_ACTION_CENTER_PROCESSES = frozenset({"shellexperiencehost.exe"})

_GW_OWNER = 4
_WS_EX_TOOLWINDOW = 0x00000080
_WS_CAPTION = 0x00C00000
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_DWMWA_CLOAKED = 14


@dataclass(frozen=True)
class Surface:
    kind: str
    handle: int
    title: str
    class_name: str

    def describe(self) -> str:
        return f'{self.kind} "{self.title}"' if self.title else self.kind


def _is_cloaked(hwnd: int) -> bool:
    try:
        cloaked = ctypes.c_int(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        return result == 0 and cloaked.value != 0
    except Exception:
        return False


def _process_name(hwnd: int) -> str:
    try:
        import psutil
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().casefold() if pid else ""
    except Exception:
        return ""


def _visible_top_level_windows() -> list[tuple[int, str, str]]:
    """(hwnd, class, title) of visible, uncloaked, non-empty top-level windows."""
    import win32gui

    found: list[tuple[int, str, str]] = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or _is_cloaked(hwnd):
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right - left <= 0 or bottom - top <= 0:
                return True
            found.append((hwnd, win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(callback, None)
    except Exception:
        logger.debug("EnumWindows failed", exc_info=True)
    return found


def _is_dialog(hwnd: int, class_name: str) -> bool:
    import win32gui

    if class_name in _DIALOG_CLASSES:
        return True
    try:
        owner = win32gui.GetWindow(hwnd, _GW_OWNER)
        if not owner:
            return False
        style = win32gui.GetWindowLong(hwnd, _GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, _GWL_EXSTYLE)
        return bool(style & _WS_CAPTION) and not ex_style & _WS_EX_TOOLWINDOW
    except Exception:
        return False


def list_surfaces(kinds: tuple[str, ...] | None = None) -> list[Surface]:
    """Every open surface of the requested *kinds* (default: all but ``focused``)."""
    wanted = set(kinds or [k for k in SURFACE_KINDS if k != "focused"])
    surfaces: list[Surface] = []
    if "focused" in wanted:
        try:
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                surfaces.append(
                    Surface(
                        "focused", hwnd, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd)
                    )
                )
        except Exception:
            pass
    windows = _visible_top_level_windows()
    for hwnd, class_name, title in windows:
        if "menu" in wanted and class_name in _MENU_CLASSES:
            surfaces.append(Surface("menu", hwnd, title, class_name))
        elif "taskbar" in wanted and class_name in _TASKBAR_CLASSES:
            surfaces.append(Surface("taskbar", hwnd, title, class_name))
        elif "system_tray" in wanted and class_name in _TRAY_OVERFLOW_CLASSES:
            surfaces.append(Surface("system_tray", hwnd, title, class_name))
        elif "dialog" in wanted and _is_dialog(hwnd, class_name):
            surfaces.append(Surface("dialog", hwnd, title, class_name))
        elif class_name == "Windows.UI.Core.CoreWindow" and wanted & {
            "start_menu",
            "action_center",
        }:
            process = _process_name(hwnd)
            if "start_menu" in wanted and process in _START_PROCESSES:
                surfaces.append(Surface("start_menu", hwnd, title, class_name))
            elif "action_center" in wanted and process in _ACTION_CENTER_PROCESSES:
                surfaces.append(Surface("action_center", hwnd, title, class_name))
    if "system_tray" in wanted and not any(s.kind == "system_tray" for s in surfaces):
        # The notification area lives inside the primary taskbar on Windows 11.
        surfaces.extend(
            Surface("system_tray", hwnd, title, class_name)
            for hwnd, class_name, title in windows
            if class_name == "Shell_TrayWnd"
        )
    return surfaces


def surface_handles(kind: str) -> list[int]:
    """Window handles presenting *kind*; empty when that surface is not open."""
    kind = kind.strip().lower().replace("-", "_")
    if kind not in SURFACE_KINDS:
        raise ValueError(f"surface must be one of: {', '.join(SURFACE_KINDS)}")
    return [surface.handle for surface in list_surfaces((kind,))]


# Overlays worth announcing after an action: they mean the app is waiting on them.
_ANNOUNCED = ("menu", "dialog", "start_menu", "action_center", "system_tray")


def overlay_snapshot() -> set[Surface]:
    try:
        return set(list_surfaces(_ANNOUNCED))
    except Exception:
        return set()


def describe_new_overlays(before: set[Surface]) -> str | None:
    """A note naming the overlays opened since *before*, or None."""
    try:
        after = set(list_surfaces(_ANNOUNCED))
    except Exception:
        return None
    opened = sorted(after - before, key=lambda s: (s.kind, s.title))
    if not opened:
        return None
    names = ", ".join(surface.describe() for surface in opened)
    return (
        f"opened: {names} — the app is waiting on it; read it with "
        "Snapshot(surface=...) before acting on the window behind it."
    )
