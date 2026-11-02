from types import SimpleNamespace

import pytest

from windows_mcp.desktop import service
from windows_mcp.desktop.service import Desktop
from windows_mcp.desktop.views import Status, Window
from windows_mcp.tree.views import BoundingBox

_PROCESS_NAMES = {1: "msedge.exe", 2: "Taskmgr.exe", 3: "Code.exe", 4: "WhatsApp.exe"}


def _window(name: str, pid: int) -> Window:
    return Window(
        name=name,
        is_browser=pid == 1,
        depth=0,
        status=Status.NORMAL,
        bounding_box=BoundingBox(left=0, top=0, right=10, bottom=10, width=10, height=10),
        handle=pid * 100,
        process_id=pid,
    )


@pytest.fixture
def desktop(monkeypatch: pytest.MonkeyPatch) -> Desktop:
    monkeypatch.setattr(
        service, "Process", lambda pid: SimpleNamespace(name=lambda: _PROCESS_NAMES[pid])
    )
    desktop = Desktop.__new__(Desktop)
    desktop.desktop_state = SimpleNamespace(
        active_window=None,
        windows=[
            _window("Gerenciador de Tarefas", 2),
            _window(
                "Métodos Ágeis com Scrum — Perguntas e respostas e mais 13 páginas"
                " - Pessoal — Microsoft​ Edge",
                1,
            ),
            _window("q_t5.txt - Visual Studio Code", 3),
            _window("WhatsApp", 4),
        ],
    )
    return desktop


@pytest.mark.parametrize(
    ("query", "pid"),
    [
        ("Edge", 1),
        ("edge", 1),
        ("Microsoft Edge", 1),
        ("msedge", 1),
        ("Visual Studio Code", 3),
        ("code", 3),
        ("Gerenciador de Tarefas", 2),
        ("WhatsApp", 4),
    ],
)
def test_find_window_by_app_name(desktop: Desktop, query: str, pid: int) -> None:
    window, error = desktop._find_window_by_name(query)
    assert error == ""
    assert window.process_id == pid


def test_find_window_unknown_app_reports_not_found(desktop: Desktop) -> None:
    window, error = desktop._find_window_by_name("Firefox")
    assert window is None
    assert error == "Application Firefox not found."


def test_aliases_survive_unreadable_process(
    desktop: Desktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(pid: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(service, "Process", boom)
    window, error = desktop._find_window_by_name("Edge")
    assert window.process_id == 1
