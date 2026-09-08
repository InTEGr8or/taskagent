"""Tests for agent chat log pattern registration and discovery."""

from pathlib import Path

import pytest

from taskagent.agent_registry import (
    AgentCLIInfo,
    get_agent_cli_registry,
    inspect_agent_cli,
)
from taskagent.chat import (
    DiscoveredChat,
    discover_agent_chats,
)


def test_agent_cli_registry_has_chat_log_patterns():
    registry = get_agent_cli_registry()

    expected_agents = ["agy", "roo", "cline", "aider", "claude", "opencode"]
    for agent_id in expected_agents:
        assert agent_id in registry
        info = registry[agent_id]
        assert isinstance(info.chat_log_patterns, list)
        assert len(info.chat_log_patterns) > 0
        assert isinstance(info.chat_parser_type, str)
        assert len(info.chat_parser_type) > 0

    assert registry["claude"].chat_parser_type == "jsonl"
    assert registry["aider"].chat_parser_type == "markdown"
    assert registry["agy"].chat_parser_type == "jsonl"


def test_agent_cli_info_defaults():
    info = AgentCLIInfo(
        id="test",
        name="Test Agent",
        binary="test",
        description="Test description",
    )
    assert info.chat_log_patterns == []
    assert info.chat_parser_type == "json"


def test_inspect_agent_cli_chat_info():
    info = inspect_agent_cli("claude")
    assert "chat_log_patterns" in info
    assert "chat_parser_type" in info
    assert info["chat_parser_type"] == "jsonl"
    assert len(info["chat_log_patterns"]) > 0


def test_discover_agent_chats_unknown_agent():
    with pytest.raises(ValueError, match="Unknown agent CLI"):
        discover_agent_chats("nonexistent_agent_xyz")


def test_discover_agent_chats_all_agents():
    chats = discover_agent_chats()
    assert isinstance(chats, list)
    for chat in chats:
        assert isinstance(chat, DiscoveredChat)
        assert isinstance(chat.agent_id, str)
        assert isinstance(chat.path, Path)
        assert isinstance(chat.parser_type, str)


def test_discover_agent_chats_specific_agent():
    chats = discover_agent_chats("claude")
    assert isinstance(chats, list)
    for chat in chats:
        assert chat.agent_id == "claude"
        assert chat.parser_type == "jsonl"


def test_discover_agent_chats_repo_scoping_with_gwt(tmp_path: Path):
    host_root = tmp_path / "my_project"
    host_root.mkdir()
    gwt_dir = host_root / ".gwt" / "feature-branch"
    gwt_dir.mkdir(parents=True)

    history_file = host_root / ".aider.chat.history.md"
    history_file.write_text("# Aider Chat History\n", encoding="utf-8")

    chats = discover_agent_chats("aider", project_dir=gwt_dir)
    found_paths = [chat.path.resolve() for chat in chats]
    assert history_file.resolve() in found_paths


def test_discover_agent_chats_home_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)

    claude_project_dir = fake_home / ".claude" / "projects" / "my-repo"
    claude_project_dir.mkdir(parents=True)
    chat_file = claude_project_dir / "session.jsonl"
    chat_file.write_text('{"role": "user", "content": "hello"}\n', encoding="utf-8")

    chats = discover_agent_chats("claude", project_dir=tmp_path)
    found_chats = [c for c in chats if c.agent_id == "claude"]
    assert len(found_chats) > 0
    assert any(c.path.resolve() == chat_file.resolve() for c in found_chats)
    matched = [c for c in found_chats if c.path.resolve() == chat_file.resolve()][0]
    assert matched.parser_type == "jsonl"
