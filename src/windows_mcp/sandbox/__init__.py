"""Run windows-mcp inside Windows Sandbox so the agent never touches the host desktop.

The host writes an auth key into a shared folder, starts a sandbox whose logon command runs
``bootstrap.ps1``, and waits for the sandbox to write ``ready.json`` with its address. The
agent then talks to the server over streamable HTTP while the person keeps using the host.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

BOOTSTRAP_DIR = Path(__file__).resolve().parent

# Fixed paths inside the sandbox; bootstrap.ps1 defaults match them.
GUEST_ROOT = r"C:\mcp"
GUEST_BOOTSTRAP = GUEST_ROOT + r"\bootstrap"
GUEST_SOURCE = GUEST_ROOT + r"\source"
GUEST_SHARED = GUEST_ROOT + r"\shared"
GUEST_CACHE = GUEST_ROOT + r"\cache"
GUEST_PROFILE = GUEST_ROOT + r"\profile"

_GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


@dataclass(frozen=True)
class SandboxLayout:
    """Host folders shared with the sandbox, all under one root."""

    root: Path

    @property
    def shared(self) -> Path:
        return self.root / "shared"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def profile(self) -> Path:
        return self.root / "profile"

    @property
    def ready_file(self) -> Path:
        return self.shared / "ready.json"

    @property
    def key_file(self) -> Path:
        return self.shared / "auth.key"

    @property
    def id_file(self) -> Path:
        return self.root / "sandbox.id"


def find_source_checkout() -> Path | None:
    """Return the windows-mcp source checkout this package runs from, if any.

    A checkout is mapped into the sandbox so local changes run there; an installed wheel
    has none, and the sandbox falls back to the PyPI release.
    """
    candidate = BOOTSTRAP_DIR.parents[2]
    pyproject = candidate / "pyproject.toml"
    if pyproject.is_file() and 'name = "windows-mcp"' in pyproject.read_text(encoding="utf-8"):
        return candidate
    return None


def _mapped_folder(host: Path, guest: str, read_only: bool) -> str:
    return (
        "    <MappedFolder>\n"
        f"      <HostFolder>{escape(str(host))}</HostFolder>\n"
        f"      <SandboxFolder>{escape(guest)}</SandboxFolder>\n"
        f"      <ReadOnly>{'true' if read_only else 'false'}</ReadOnly>\n"
        "    </MappedFolder>\n"
    )


def build_wsb_config(
    layout: SandboxLayout,
    *,
    source: Path | None,
    port: int,
    memory_mb: int = 4096,
    keep_profile: bool = False,
    bootstrap_dir: Path = BOOTSTRAP_DIR,
) -> str:
    """Return the Windows Sandbox XML configuration that boots windows-mcp.

    Args:
        layout: Host folders to share.
        source: windows-mcp checkout to map read-only, or None to use the PyPI release.
        port: Port the server listens on inside the sandbox.
        memory_mb: Memory given to the sandbox.
        keep_profile: Also map a writable browser profile folder that survives restarts.
        bootstrap_dir: Folder holding bootstrap.ps1.
    """
    folders = [
        _mapped_folder(bootstrap_dir, GUEST_BOOTSTRAP, True),
        _mapped_folder(layout.shared, GUEST_SHARED, False),
        _mapped_folder(layout.cache, GUEST_CACHE, False),
    ]
    if source is not None:
        folders.append(_mapped_folder(source, GUEST_SOURCE, True))
    if keep_profile:
        folders.append(_mapped_folder(layout.profile, GUEST_PROFILE, False))

    command = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File "
        f"{GUEST_BOOTSTRAP}\\bootstrap.ps1 -Port {int(port)}"
    )
    return (
        "<Configuration>\n"
        "  <Networking>Enable</Networking>\n"
        "  <vGPU>Enable</vGPU>\n"
        "  <ClipboardRedirection>Enable</ClipboardRedirection>\n"
        f"  <MemoryInMB>{int(memory_mb)}</MemoryInMB>\n"
        "  <MappedFolders>\n"
        f"{''.join(folders)}"
        "  </MappedFolders>\n"
        "  <LogonCommand>\n"
        f"    <Command>{escape(command)}</Command>\n"
        "  </LogonCommand>\n"
        "</Configuration>\n"
    )


def prepare_layout(layout: SandboxLayout, *, keep_profile: bool) -> str:
    """Create the shared folders, write a fresh auth key and return it."""
    for folder in (layout.shared, layout.cache):
        folder.mkdir(parents=True, exist_ok=True)
    if keep_profile:
        layout.profile.mkdir(parents=True, exist_ok=True)
    layout.ready_file.unlink(missing_ok=True)
    key = secrets.token_urlsafe(32)
    layout.key_file.write_text(key, encoding="utf-8")
    return key


def read_ready(path: Path) -> dict | None:
    """Return the parsed ready.json, or None while it is missing or half-written."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_sandbox_id(output: str) -> str | None:
    """Pull the sandbox id from ``wsb start`` output (JSON with --raw, or plain text)."""
    try:
        data = json.loads(output)
    except ValueError:
        data = None
    if isinstance(data, dict):
        for key in ("Id", "id", "SandboxId"):
            if isinstance(data.get(key), str):
                return data[key]
    match = _GUID.search(output)
    return match.group(0) if match else None


def client_config(ip: str, port: int, key: str) -> str:
    """Return the MCP client JSON snippet for the sandbox server."""
    return json.dumps(
        {
            "mcpServers": {
                "windows-mcp-sandbox": {
                    "type": "http",
                    "url": f"http://{ip}:{port}/mcp/",
                    "headers": {"Authorization": f"Bearer {key}"},
                }
            }
        },
        indent=2,
    )


def wsb_path() -> str | None:
    """Return the Windows Sandbox CLI path, or None when it is not installed (pre-24H2)."""
    return shutil.which("wsb")


def run_wsb(*args: str) -> subprocess.CompletedProcess:
    """Run the Windows Sandbox CLI and capture its output."""
    exe = wsb_path()
    if exe is None:
        raise RuntimeError(
            "wsb.exe not found. Windows Sandbox CLI needs Windows 11 24H2+ with the "
            "'Windows Sandbox' optional feature enabled."
        )
    return subprocess.run([exe, *args], capture_output=True, text=True, errors="replace")


def open_window(sandbox_id: str) -> None:
    """Open the sandbox window without waiting: ``wsb connect`` blocks until it is closed."""
    exe = wsb_path()
    if exe is None:
        raise RuntimeError("wsb.exe not found.")
    subprocess.Popen(
        [exe, "connect", "--id", sandbox_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS,
    )


def wait_ready(layout: SandboxLayout, timeout: float, poll: float = 2.0) -> dict | None:
    """Wait for the sandbox to report ready (or an error); None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read_ready(layout.ready_file)
        if data is not None:
            return data
        time.sleep(poll)
    return None
