"""Tests for ta yazi command and dependency handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch
from rich.console import Console

from taskagent.cli import cmd_yazi


def test_cmd_yazi_when_yazi_is_installed(tmp_path: Path):
    console = Console(record=True)
    manager = MagicMock()
    manager.issues_root = tmp_path / "store"
    manager.issues_root.mkdir()

    args = MagicMock()

    with (
        patch("shutil.which", return_value="/usr/bin/yazi"),
        patch("subprocess.run") as mock_run,
    ):
        cmd_yazi(console, manager, args)
        mock_run.assert_called_once_with(["/usr/bin/yazi", str(manager.issues_root)])

    output = console.export_text()
    assert "Opening Yazi in:" in output


def test_cmd_yazi_when_yazi_not_installed_declined(tmp_path: Path):
    console = Console(record=True)
    manager = MagicMock()
    manager.issues_root = tmp_path / "store"
    manager.issues_root.mkdir()

    args = MagicMock()

    with (
        patch("shutil.which", return_value=None),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="n"),
        patch("subprocess.run") as mock_run,
    ):
        cmd_yazi(console, manager, args)
        mock_run.assert_not_called()

    output = console.export_text()
    assert "Yazi file manager ('yazi') is not installed" in output
    assert "uv tool install yazi-cli" in output
