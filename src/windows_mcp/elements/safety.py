"""Input and process safety guards.

Three small, independent guards ported from the ideas in vercel-labs/agent-desktop:

* **Protected processes** — session-critical images (``csrss.exe``, ``winlogon.exe``,
  ``explorer.exe`` …) that the Process tool refuses to kill, because terminating them
  breaks the user's desktop session. Matching is an exact, case-insensitive image-name
  comparison, so look-alikes (``iexplore.exe``, ``explorer++.exe``) stay killable.
* **Blocked shortcuts** — combos that take the foreground away from the run or change
  the session under it (``alt+f4``, ``win+l``, ``alt+tab`` …). Any modifier superset of a
  blocked combo is blocked too (``alt+shift+tab`` is as disruptive as ``alt+tab``). The
  caller can still send them with ``force=True``.
* **UIPI integrity check** — synthetic input sent to a process running at a higher
  integrity level (an elevated app) is silently dropped by Windows, while ``SendInput``
  still reports success. Comparing token integrity levels up front turns that silent
  no-op into a clear error. Any failure to read a token fails *open* (no verdict).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PROTECTED_PROCESSES: frozenset[str] = frozenset(
    {
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "smss.exe",
        "lsaiso.exe",
        "dwm.exe",
        "explorer.exe",
    }
)


def is_protected_process(identifier: str | None) -> bool:
    """Return True when *identifier* (an image name or full path) is session-critical.

    Args:
        identifier: Process image name (``explorer.exe``), bare name (``explorer``) or
            full path (``C:\\Windows\\explorer.exe``).
    """
    if not identifier:
        return False
    image = identifier.replace("/", "\\").rsplit("\\", 1)[-1].strip().casefold()
    if not image:
        return False
    if not image.endswith(".exe"):
        image = f"{image}.exe"
    return image in PROTECTED_PROCESSES


BLOCKED_SHORTCUTS: tuple[str, ...] = (
    "alt+f4",
    "win+l",
    "win+d",
    "alt+tab",
    "win+r",
    "win+x",
    "win+e",
    "ctrl+shift+esc",
)

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "lctrl": "ctrl",
    "rctrl": "ctrl",
    "alt": "alt",
    "menu": "alt",
    "lalt": "alt",
    "ralt": "alt",
    "shift": "shift",
    "lshift": "shift",
    "rshift": "shift",
    "win": "win",
    "windows": "win",
    "lwin": "win",
    "rwin": "win",
    "meta": "win",
    "cmd": "win",
    "super": "win",
}

_KEY_ALIASES = {"escape": "esc", "del": "delete"}


def _canonical_parts(shortcut: str) -> tuple[frozenset[str], str]:
    tokens = [token.strip().casefold() for token in shortcut.split("+") if token.strip()]
    modifiers: set[str] = set()
    keys: list[str] = []
    for token in tokens:
        if token in _MODIFIER_ALIASES:
            modifiers.add(_MODIFIER_ALIASES[token])
        else:
            keys.append(_KEY_ALIASES.get(token, token))
    # A combo made of modifiers only ("win") has its last modifier as the key.
    key = keys[-1] if keys else ""
    return frozenset(modifiers), key


def blocked_shortcut_match(shortcut: str) -> str | None:
    """Return the blocked combo *shortcut* matches, or None when it is allowed.

    A shortcut matches when its key equals a blocked combo's key and its modifiers are
    a superset of that combo's modifiers.
    """
    modifiers, key = _canonical_parts(shortcut)
    if not key:
        return None
    for entry in BLOCKED_SHORTCUTS:
        blocked_modifiers, blocked_key = _canonical_parts(entry)
        if blocked_key == key and blocked_modifiers <= modifiers:
            return entry
    return None


# Mandatory integrity level RIDs (SECURITY_MANDATORY_*_RID).
_INTEGRITY_NAMES = {
    0x0000: "untrusted",
    0x1000: "low",
    0x2000: "medium",
    0x2100: "medium-plus",
    0x3000: "high",
    0x4000: "system",
    0x5000: "protected",
}


def _token_integrity(token) -> int | None:
    import win32security

    sid, _attrs = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
    count = sid.GetSubAuthorityCount()
    if count <= 0:
        return None
    return int(sid.GetSubAuthority(count - 1))


def current_integrity_level() -> int | None:
    """Integrity RID of this server process, or None when it cannot be read."""
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            return _token_integrity(token)
        finally:
            token.Close()
    except Exception:
        logger.debug("Could not read own token integrity", exc_info=True)
        return None


def process_integrity_level(pid: int) -> int | None:
    """Integrity RID of process *pid*, or None when it cannot be read."""
    if not pid:
        return None
    try:
        import win32api
        import win32con
        import win32security

        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
            try:
                return _token_integrity(token)
            finally:
                token.Close()
        finally:
            handle.Close()
    except Exception:
        logger.debug("Could not read token integrity of pid %s", pid, exc_info=True)
        return None


def integrity_name(rid: int | None) -> str:
    if rid is None:
        return "unknown"
    return _INTEGRITY_NAMES.get(rid, hex(rid))


def input_block_reason_for_pid(pid: int | None) -> str | None:
    """Explain why input to *pid* would be dropped by UIPI, or None when it is allowed.

    Fails open: an unreadable token on either side yields None.
    """
    if not pid:
        return None
    target = process_integrity_level(pid)
    if target is None:
        return None
    own = current_integrity_level()
    if own is None or target <= own:
        return None
    return (
        f"PERM_DENIED: the target process (PID {pid}) runs at {integrity_name(target)} "
        f"integrity, above this server's {integrity_name(own)} integrity. Windows (UIPI) "
        "silently drops synthetic mouse and keyboard input sent to it, so the action would "
        "report success and do nothing. Run windows-mcp elevated to the same level, or act "
        "on a non-elevated window. [not_delivered, retry: safe]"
    )


def pid_at_point(x: int, y: int) -> int | None:
    """PID owning the top-level window under screen point (x, y), or None."""
    try:
        import win32gui
        import win32process

        hwnd = win32gui.WindowFromPoint((int(x), int(y)))
        if not hwnd:
            return None
        root = win32gui.GetAncestor(hwnd, 2)  # GA_ROOT
        _, pid = win32process.GetWindowThreadProcessId(root or hwnd)
        return pid or None
    except Exception:
        return None


def input_block_reason_at(x: int, y: int) -> str | None:
    """UIPI verdict for input aimed at screen point (x, y); None means allowed/unknown."""
    try:
        return input_block_reason_for_pid(pid_at_point(x, y))
    except Exception:
        return None
