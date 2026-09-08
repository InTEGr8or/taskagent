"""Module for discovering and extracting last-active agent session info."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from multi_agent_registry import (
    AgentCLIInfo,
    DiscoveredChat,
    discover_agent_chats,
    get_agent_cli_registry,
)
from multi_agent_registry.discovery import get_chat_workspace
from taskagent.store_registry import project_host_root


@dataclass
class AgentLastUsedInfo:
    """Information on the last active session for an agent CLI in a repository."""

    agent_id: str
    agent_name: str
    description: str
    last_active: datetime
    last_user_comment: str
    log_path: Path


def _extract_text(node: Any) -> str:
    """Recursively extract string content from dicts, lists, or primitive strings."""
    if isinstance(node, str):
        return node
    elif isinstance(node, dict):
        if "text" in node and isinstance(node["text"], str):
            return node["text"]
        if "content" in node:
            return _extract_text(node["content"])
        if "message" in node:
            return _extract_text(node["message"])
    elif isinstance(node, list):
        parts = [_extract_text(item) for item in node]
        return " ".join(p for p in parts if p)
    return ""


def _is_user_role(role_val: Any) -> bool:
    if isinstance(role_val, str):
        r = role_val.lower()
        return r in (
            "user",
            "say",
            "user_input",
            "user_explicit",
            "human",
            "user_feedback",
        )
    return False


def _parse_last_user_comment_and_timestamp(
    discovered: DiscoveredChat,
) -> Tuple[datetime, str]:
    """Extract timestamp and first 200 chars of last user comment from chat log file."""
    path = discovered.path
    file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    user_comment = ""

    if not path.is_file():
        return file_mtime, user_comment

    try:
        if discovered.parser_type == "jsonl":
            text_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in reversed(text_lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        msg = data.get("message")
                        role = (
                            data.get("type")
                            or data.get("role")
                            or data.get("source")
                            or (msg.get("role") if isinstance(msg, dict) else None)
                        )
                        if _is_user_role(role):
                            raw_comment = _extract_text(msg if msg else data).strip()
                            if (
                                raw_comment
                                and not raw_comment.startswith("<local-command-")
                                and not raw_comment.startswith("<command-name>")
                            ):
                                user_comment = raw_comment
                                created_at = data.get("created_at") or data.get(
                                    "timestamp"
                                )
                                if created_at:
                                    try:
                                        file_mtime = datetime.fromisoformat(
                                            str(created_at).replace("Z", "+00:00")
                                        )
                                    except ValueError:
                                        pass
                                break
                except Exception:
                    continue

        elif discovered.parser_type == "json":
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw_text)
            messages = []
            if isinstance(data, list):
                messages = data
            elif isinstance(data, dict):
                messages = (
                    data.get("messages")
                    or data.get("steps")
                    or data.get("history")
                    or []
                )

            if isinstance(messages, list):
                for msg in reversed(messages):
                    if isinstance(msg, dict):
                        role = (
                            msg.get("say")
                            or msg.get("role")
                            or msg.get("type")
                            or msg.get("source")
                        )
                        if _is_user_role(role):
                            raw_comment = _extract_text(msg).strip()
                            if raw_comment:
                                user_comment = raw_comment
                                break

        elif discovered.parser_type == "markdown":
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            text_lines = raw_text.splitlines()
            user_blocks: List[str] = []
            current_block: List[str] = []
            in_user = False
            for line in text_lines:
                if line.startswith("#") or line.startswith("> "):
                    if in_user and current_block:
                        user_blocks.append("\n".join(current_block).strip())
                        current_block = []
                    in_user = "user" in line.lower() or "human" in line.lower()
                elif in_user:
                    current_block.append(line)
            if in_user and current_block:
                user_blocks.append("\n".join(current_block).strip())
            if user_blocks:
                user_comment = user_blocks[-1]

    except Exception:
        pass

    import re

    # Unwrap <USER_REQUEST> tag, keeping human prompt text
    user_comment = re.sub(r"</?USER_REQUEST>", "", user_comment)

    # Strip system context wrappers (e.g. <CONTEXT_SUMMARY>, <PLAN>, <ADDITIONAL_METADATA>)
    user_comment = re.sub(
        r"<(CONTEXT_SUMMARY|PLAN|ADDITIONAL_METADATA)>.*?</\1>",
        "",
        user_comment,
        flags=re.DOTALL,
    )
    user_comment = re.sub(
        r"</?(CONTEXT_SUMMARY|PLAN|ADDITIONAL_METADATA)>",
        "",
        user_comment,
    )
    user_comment = " ".join(user_comment.split())

    if len(user_comment) > 200:
        user_comment = user_comment[:197] + "..."

    return file_mtime, user_comment


def get_last_active_agents(
    project_dir: Optional[Path] = None,
    limit: int = 5,
) -> List[AgentLastUsedInfo]:
    """Find and rank most recently active AI agent CLIs in a project repository."""
    if project_dir is None:
        project_dir = Path.cwd()

    host_root = project_host_root(project_dir)
    discovered_chats = discover_agent_chats(project_dir=host_root)
    registry = get_agent_cli_registry()

    agent_chats_map: dict[str, List[DiscoveredChat]] = {}
    for chat in discovered_chats:
        is_global_log = str(chat.path).startswith(str(Path.home())) and not str(
            chat.path
        ).startswith(str(host_root))
        ws = get_chat_workspace(chat)
        if is_global_log:
            if ws is None:
                continue
            try:
                chat_host = project_host_root(ws)
                if chat_host != host_root and str(chat_host) != str(host_root):
                    continue
            except Exception:
                continue
        elif ws is not None:
            try:
                chat_host = project_host_root(ws)
                if chat_host != host_root and str(chat_host) != str(host_root):
                    continue
            except Exception:
                pass
        agent_chats_map.setdefault(chat.agent_id, []).append(chat)

    results: List[AgentLastUsedInfo] = []

    for agent_id, chats in agent_chats_map.items():
        agent_info: Optional[AgentCLIInfo] = registry.get(agent_id)
        name = agent_info.name if agent_info else agent_id.capitalize()
        description = agent_info.description if agent_info else ""

        newest_mtime: Optional[datetime] = None
        newest_comment: str = ""
        newest_path: Optional[Path] = None

        for chat in chats:
            mtime, comment = _parse_last_user_comment_and_timestamp(chat)
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime = mtime
                newest_comment = comment
                newest_path = chat.path

        if newest_mtime is not None and newest_path is not None:
            results.append(
                AgentLastUsedInfo(
                    agent_id=agent_id,
                    agent_name=name,
                    description=description,
                    last_active=newest_mtime,
                    last_user_comment=newest_comment,
                    log_path=newest_path,
                )
            )

    results.sort(key=lambda item: item.last_active, reverse=True)
    return results[:limit]
