import json
import xml.etree.ElementTree as ET
from pathlib import Path

from click.testing import CliRunner

from windows_mcp import sandbox as sb


def _folders(xml: str) -> dict[str, tuple[str, str]]:
    root = ET.fromstring(xml)
    return {
        f.findtext("SandboxFolder"): (f.findtext("HostFolder"), f.findtext("ReadOnly"))
        for f in root.iter("MappedFolder")
    }


def test_wsb_config_maps_source_read_only_and_shared_writable(tmp_path: Path) -> None:
    layout = sb.SandboxLayout(tmp_path / "sb & co")
    xml = sb.build_wsb_config(layout, source=tmp_path / "src", port=9001, memory_mb=2048)

    folders = _folders(xml)
    assert folders[sb.GUEST_SOURCE] == (str(tmp_path / "src"), "true")
    assert folders[sb.GUEST_BOOTSTRAP][1] == "true"
    assert folders[sb.GUEST_SHARED] == (str(layout.shared), "false")
    assert folders[sb.GUEST_CACHE] == (str(layout.cache), "false")
    assert sb.GUEST_PROFILE not in folders

    root = ET.fromstring(xml)
    assert root.findtext("Networking") == "Enable"
    assert root.findtext("MemoryInMB") == "2048"
    command = root.findtext("LogonCommand/Command")
    assert command.endswith(r"C:\mcp\bootstrap\bootstrap.ps1 -Port 9001")


def test_wsb_config_without_source_and_with_profile(tmp_path: Path) -> None:
    layout = sb.SandboxLayout(tmp_path)
    folders = _folders(sb.build_wsb_config(layout, source=None, port=1, keep_profile=True))
    assert sb.GUEST_SOURCE not in folders
    assert folders[sb.GUEST_PROFILE] == (str(layout.profile), "false")


def test_bootstrap_script_ships_with_package() -> None:
    assert (sb.BOOTSTRAP_DIR / "bootstrap.ps1").is_file()


def test_prepare_layout_writes_fresh_key_and_clears_ready(tmp_path: Path) -> None:
    layout = sb.SandboxLayout(tmp_path)
    layout.shared.mkdir(parents=True)
    layout.ready_file.write_text("{}", encoding="utf-8")

    key = sb.prepare_layout(layout, keep_profile=True)

    assert layout.key_file.read_text(encoding="utf-8") == key
    assert len(key) >= 32
    assert not layout.ready_file.exists()
    assert layout.cache.is_dir() and layout.profile.is_dir()
    assert sb.prepare_layout(layout, keep_profile=False) != key


def test_read_ready_handles_missing_partial_and_bom(tmp_path: Path) -> None:
    path = tmp_path / "ready.json"
    assert sb.read_ready(path) is None
    path.write_text('{"status": "rea', encoding="utf-8")
    assert sb.read_ready(path) is None
    path.write_text('{"status": "ready", "ip": "1.2.3.4", "port": 1}', encoding="utf-8-sig")
    assert sb.read_ready(path) == {"status": "ready", "ip": "1.2.3.4", "port": 1}


def test_parse_sandbox_id() -> None:
    guid = "0f3b1c2d-1111-2222-3333-444455556666"
    assert sb.parse_sandbox_id(json.dumps({"Id": guid})) == guid
    assert sb.parse_sandbox_id(f"Windows Sandbox environment started: {guid}\n") == guid
    assert sb.parse_sandbox_id("error") is None


def test_client_config() -> None:
    data = json.loads(sb.client_config("172.20.1.5", 8765, "k"))
    server = data["mcpServers"]["windows-mcp-sandbox"]
    assert server["url"] == "http://172.20.1.5:8765/mcp/"
    assert server["headers"]["Authorization"] == "Bearer k"


def test_sandbox_start_prints_config(tmp_path: Path, monkeypatch) -> None:
    from windows_mcp import __main__ as cli

    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path)
    calls = []

    def fake_wsb(*args):
        calls.append(args)

        class R:
            returncode = 0
            stdout = '{"Id": "0f3b1c2d-1111-2222-3333-444455556666"}'
            stderr = ""

        return R()

    monkeypatch.setattr(sb, "run_wsb", fake_wsb)
    monkeypatch.setattr(
        sb,
        "wait_ready",
        lambda layout, timeout: {"status": "ready", "ip": "10.0.0.2", "port": 8765},
    )

    opened = []
    monkeypatch.setattr(sb, "open_window", opened.append)

    result = CliRunner().invoke(cli.main, ["sandbox", "start", "--headless"])

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "start" and "<Configuration>" in calls[0][2]
    assert opened == []
    assert "http://10.0.0.2:8765/mcp/" in result.output
    assert (tmp_path / "sandbox" / "sandbox.id").exists()

    result = CliRunner().invoke(cli.main, ["sandbox", "start"])
    assert result.exit_code == 0, result.output
    assert opened == ["0f3b1c2d-1111-2222-3333-444455556666"]
