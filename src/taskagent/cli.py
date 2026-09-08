from typing import Any, List, Optional, Tuple, Dict, Set, Union
from pathlib import Path
from datetime import datetime
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
import sys

from taskagent.theme import DEFAULT as theme

# Removed invalid NO_BORDER definition
import argparse
import os
import json
import re
import questionary
import subprocess
import shlex
import shutil
import pyperclip  # type: ignore

try:
    import tty
    import termios

    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

try:
    import msvcrt

    HAS_MSVCRT = True
except ImportError:
    HAS_MSVCRT = False

from rich.live import Live

from taskagent.models.issue import Issue
from taskagent.models.metric import SubtaskMetric
from taskagent.manager import TaskAgent
from taskagent.discovery import discover, get_task_agent_project_root
from taskagent import agent
from taskagent.perf import notify_perf_logging_if_enabled


import verkit  # type: ignore[import-untyped]


def get_tool_version() -> str:
    """Read the task-agent tool version."""
    return verkit.get_installed_version("task-agent")


def get_latest_pypi_version(timeout: int = 4) -> Optional[str]:
    """Fetch the latest version of task-agent from PyPI."""
    return verkit.get_latest_pypi_version("task-agent", timeout=timeout)


def display_version_info(console: Console):
    """Display running and PyPI version information."""
    verkit.display_version_info(console, "task-agent", upgrade_cmd="ta self-up")


def get_committed_version(
    root: Optional[Path] = None,
) -> Tuple[str, Optional[str]]:
    """Read the version from HEAD (committed code), not working tree."""
    info = verkit.inspect_committed(root)
    return info.version, info.source


def promote_project_version(
    console: Console,
    part: str,
    *,
    project_root: Optional[Path] = None,
    allow_amend: bool = True,
) -> str:
    """Bump project version and commit it. Returns the new version string."""
    return verkit.promote_version(
        part, project_root=project_root, console=console, allow_amend=allow_amend
    )


def tag_project_version(
    console: Console,
    *,
    project_root: Optional[Path] = None,
    push: bool = True,
    push_branch: bool = True,
) -> str:
    """Create ``vX.Y.Z`` on HEAD from the *committed* version and optionally push."""
    return verkit.tag_version(
        project_root=project_root,
        console=console,
        push=push,
        push_branch=push_branch,
    )


def get_project_version(root: Optional[Path] = None) -> Tuple[str, Optional[str]]:
    """Read the current project version from various project files (working tree)."""
    info = verkit.inspect_project(root)
    return info.version, info.source


def get_key() -> str:
    """Read a single key or escape sequence from stdin."""
    if HAS_TERMIOS and sys.stdin.isatty():
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                # Start of an escape sequence
                # We want to read more if it's an arrow key
                import select

                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch += sys.stdin.read(2)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch
    elif HAS_MSVCRT:
        # Windows handling
        ch = msvcrt.getch()  # type: ignore
        if ch in (b"\x00", b"\xe0"):
            # Special key (like arrow keys)
            ch2 = msvcrt.getch()  # type: ignore
            # Map common Windows keys to Unix escape sequences for consistency
            if ch2 == b"H":
                return "\x1b[A"  # Up
            if ch2 == b"P":
                return "\x1b[B"  # Down
            return f"\x1b[{ch2.decode('ascii')}"

        # Handle Ctrl keys on Windows (mapped to control characters)
        # Ctrl+K is \x0b, Ctrl+J is \x0a (newline)
        return ch.decode("ascii", errors="ignore")

    return sys.stdin.read(1)


def get_editor() -> str:
    """Get the default editor, checking in order: EDITOR env, nvim, vim."""
    editor = os.environ.get("EDITOR")
    if editor:
        return editor
    if shutil.which("nvim"):
        return "nvim"
    return "vim"


def select_issue(
    console: Console,
    issues: List[Issue],
    slug_part: Optional[str],
    status_filter: Optional[List[str]] = None,
) -> Optional[Issue]:
    """Helper to select an issue based on partial slug/title and status filter."""
    if not issues:
        return None

    # Apply status filter if provided
    filtered = issues
    if status_filter:
        filtered = [i for i in issues if i.status in status_filter]

    if not filtered:
        return None

    # If no slug_part provided, return top one
    if slug_part is None:
        return filtered[0]

    # Find matches by slug prefix, exact title, or title substring (retitled tasks)
    q = slug_part.lower()
    q_slug = TaskAgent.slugify(slug_part)
    matches = [
        i
        for i in filtered
        if i.slug.startswith(slug_part)
        or i.slug.startswith(q_slug)
        or i.name.lower() == q
        or q in i.name.lower()
        or TaskAgent.slugify(i.name) == q_slug
        or TaskAgent.slugify(i.name).startswith(q_slug)
    ]
    # De-dupe while preserving order
    seen = set()
    unique_matches = []
    for i in matches:
        if i.slug not in seen:
            seen.add(i.slug)
            unique_matches.append(i)
    matches = unique_matches

    if not matches:
        return None

    if len(matches) == 1:
        return matches[0]

    # Interactive selection
    choices = [f"{i.slug} ({i.status})" for i in matches]
    selection = questionary.select(
        "Multiple issues match. Select one:", choices=choices, use_jk_keys=True
    ).ask()

    if selection is None:
        return None

    selected_slug = selection.split(" (")[0]
    return next(i for i in matches if i.slug == selected_slug)


def render_issue(
    console: Console,
    issue: Issue,
    issue_file: Path,
    issues: Optional[List[Issue]] = None,
    manager: Optional[TaskAgent] = None,
    use_pager: bool = True,
):
    """Render an issue's details to the console, using a pager if necessary.

    When ``manager`` is provided, secondary Markdown documents in the task
    directory are included after the primary README.

    Set ``use_pager=False`` for batch/multi-task output so the full stream
    is printed without interactive paging between items.
    """
    secondary_paths: List[Path] = []
    if manager is not None:
        try:
            content = manager.format_task_details(issue.slug, include_completed=True)
            secondary_paths = manager.list_secondary_documents(
                issue.slug, include_completed=True
            )
        except FileNotFoundError:
            with issue_file.open("r", encoding="utf-8") as f:
                content = f.read()
    else:
        with issue_file.open("r", encoding="utf-8") as f:
            content = f.read()

    deps_info = ""
    if issue.subtask_of:
        deps_info += (
            f"[bold blue]SUBTASK OF:[/bold blue] [yellow]{issue.subtask_of}[/yellow]\n"
        )

    # Collect blockers: both explicit blocked_by and derived open subtasks
    blockers = list(issue.blocked_by)
    if issues:
        open_subtasks = [
            i.slug
            for i in issues
            if i.subtask_of == issue.slug and i.status != "completed"
        ]
        blockers.extend(open_subtasks)

    if blockers:
        deps_info += f"[bold blue]BLOCKED BY:[/bold blue] [yellow]{', '.join(blockers)}[/yellow]\n"

    docs_info = ""
    if secondary_paths:
        names = ", ".join(p.name for p in secondary_paths)
        docs_info = (
            f"[bold blue]DOCUMENTS:[/bold blue] [cyan]{len(secondary_paths)}[/cyan] "
            f"([dim]{names}[/dim])\n"
        )

    panel = Panel(
        f"[bold blue]ISSUE:[/bold blue] [cyan]{issue.name}[/cyan]\n"
        f"[bold blue]SLUG:[/bold blue] {issue.slug} | "
        f"[bold blue]PRIORITY:[/bold blue] {issue.priority} | "
        f"[bold blue]STATUS:[/bold blue] {issue.status}\n"
        f"[bold blue]FILE:[/bold blue]\n{issue_file}\n"
        f"{docs_info}"
        f"{deps_info}",
        box=theme.panel_box,
    )

    md = Markdown(content)

    if use_pager:
        with console.pager(styles=True):
            console.print(panel)
            console.print(md)
    else:
        console.print(panel)
        console.print(md)


def get_created_date(manager: TaskAgent, slug: str) -> str:
    """Get the creation/modification date of a task file."""
    try:
        issue_file = manager.find_issue_file(slug, include_completed=True)
        if issue_file and issue_file.exists():
            try:
                content = issue_file.read_text(encoding="utf-8")
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        frontmatter = parts[1]
                        for line in frontmatter.splitlines():
                            if line.strip().startswith("created_at:"):
                                raw_val = line.split(":", 1)[1].strip()
                                try:
                                    dt = datetime.fromisoformat(raw_val)
                                    return dt.strftime("%Y-%m-%d %H:%M")
                                except ValueError:
                                    for fmt in (
                                        "%Y-%m-%d %H:%M",
                                        "%Y-%m-%d %H:%M:%S",
                                        "%Y-%m-%d",
                                    ):
                                        try:
                                            dt = datetime.strptime(raw_val, fmt)
                                            return dt.strftime("%Y-%m-%d %H:%M")
                                        except ValueError:
                                            pass
                                    return raw_val
            except Exception:
                pass
            stat = issue_file.stat()
            birthtime = getattr(stat, "st_birthtime", None)
            t = birthtime if birthtime is not None else stat.st_mtime
            return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return "unknown"


def show_store_remote_status(console: Console, manager: TaskAgent) -> None:
    """Always-on one-liner: does this mission store have a push remote?"""
    from taskagent.store_registry import (
        format_remote_status_line,
        mission_remote_status,
    )

    status = mission_remote_status(
        manager.mission_root, issues_root=manager.issues_root
    )
    console.print(format_remote_status_line(status))


def maybe_show_strategy(console: Console, manager: TaskAgent) -> bool:
    """Show the project strategy panel if the cooldown has elapsed.

    Returns True if the strategy was displayed.
    """
    if not manager.should_show_strategy():
        return False

    content = manager.get_strategy()
    if not content:
        return False

    # Strip the H1 header if present — we use it as the panel title instead
    lines = content.split("\n")
    title = "Strategy"
    body_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and title == "Strategy":
            title = stripped.lstrip("# ").strip()
            continue
        # Skip HTML comments (the hint comment)
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip()
    if not body or body == "_Define the current strategic direction for this project._":
        return False

    meta = manager.get_strategy_meta()
    last_shown = meta.get("last_shown_at", "never")
    if last_shown != "never":
        try:
            from datetime import datetime as dt

            last_dt = dt.fromisoformat(last_shown)
            elapsed = (dt.now() - last_dt).total_seconds()
            if elapsed < 3600:
                age = f"{int(elapsed / 60)}m ago"
            elif elapsed < 86400:
                age = f"{int(elapsed / 3600)}h ago"
            else:
                age = f"{int(elapsed / 86400)}d ago"
            subtitle = f"last shown {age} · ta strategy"
        except Exception:
            subtitle = "ta strategy"
    else:
        subtitle = "ta strategy"

    # Print the title, body (with custom theme), and subtitle
    console.print(f"[bold blue]📐 {title}[/bold blue]")

    from rich.theme import Theme

    strategy_theme = Theme(
        {
            "markdown.paragraph": "green",
            "markdown.item": "green",
            "markdown.h1": "bold blue",
            "markdown.h2": "bold blue",
            "markdown.h3": "bold blue",
            "markdown.h4": "bold blue",
            "markdown.h5": "bold blue",
            "markdown.h6": "bold blue",
        }
    )

    with console.use_theme(strategy_theme):
        console.print(Markdown(body))

    console.print(f"[dim]{subtitle}[/dim]")
    console.print()
    manager.update_strategy_last_shown()
    return True


def cmd_strategy(
    console: Console,
    manager: TaskAgent,
    action: Optional[str] = None,
    value: Optional[str] = None,
):
    """View, edit, or initialize the project strategy."""
    if action == "cooldown":
        if value is None:
            hours = manager.strategy_cooldown_hours
            if os.environ.get("TA_STRATEGY_COOLDOWN_HOURS") is not None:
                source = "TA_STRATEGY_COOLDOWN_HOURS"
            elif manager.get_strategy_meta().get("cooldown_hours") is not None:
                source = "strategy/.meta.json"
            else:
                source = "default"
            console.print(
                f"Strategy cooldown: [bold]{hours:g}h[/bold] [dim]({source})[/dim]"
            )
            console.print(
                "[dim]Set with: ta strategy cooldown <hours> (0 shows it every time)[/dim]"
            )
            return
        try:
            hours = float(value)
        except ValueError:
            console.print(
                f"[red]Invalid cooldown value: {value!r} (expected hours)[/red]"
            )
            return
        if hours < 0:
            console.print("[red]Cooldown must be 0 or greater.[/red]")
            return
        manager.set_strategy_cooldown_hours(hours)
        console.print(f"[bold green]Strategy cooldown set to {hours:g}h.[/bold green]")
        return

    if action == "init":
        path = manager.init_strategy()
        console.print(f"[bold green]Strategy initialized:[/bold green] {path}")
        console.print("[dim]Edit it with: ta strategy edit[/dim]")
        return

    if action == "edit":
        path = manager.init_strategy()
        editor = get_editor()
        subprocess.run([editor, str(path)])
        console.print("[bold green]Strategy updated.[/bold green]")
        return

    # Default: view
    content = manager.get_strategy()
    if not content:
        console.print(
            "[yellow]No strategy defined yet.[/yellow]\n"
            "[dim]Run [bold]ta strategy init[/bold] to create one.[/dim]"
        )
        return

    lines = content.split("\n")
    title = "Strategy"
    body_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and title == "Strategy":
            title = stripped.lstrip("# ").strip()
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        body_lines.append(line)

    body = "\n".join(body_lines).strip()

    meta = manager.get_strategy_meta()
    last_shown = meta.get("last_shown_at", "never")

    # Print the title, body (with custom theme), and subtitle
    console.print(f"[bold blue]📐 {title}[/bold blue]")

    from rich.theme import Theme

    strategy_theme = Theme(
        {
            "markdown.paragraph": "green",
            "markdown.item": "green",
            "markdown.h1": "bold blue",
            "markdown.h2": "bold blue",
            "markdown.h3": "bold blue",
            "markdown.h4": "bold blue",
            "markdown.h5": "bold blue",
            "markdown.h6": "bold blue",
        }
    )

    if body:
        with console.use_theme(strategy_theme):
            console.print(Markdown(body))
    else:
        console.print("[dim]Empty strategy — edit it with: ta strategy edit[/dim]")

    console.print(f"[dim]last shown: {last_shown} · ta strategy edit[/dim]")


def cmd_next(console: Console, manager: TaskAgent, text_mode: bool = False):
    """Show the top issue."""
    if text_mode:
        show_store_remote_status(console, manager)
        next_issue = manager.get_next_issue()
        if not next_issue:
            console.print(f"[yellow]No issues found in {manager.mission_path}[/yellow]")
            return

        issue_file = manager.find_issue_file(next_issue.slug)

        if not issue_file:
            console.print(
                f"[red]Issue file not found for slug: {next_issue.slug}[/red]"
            )
            sys.exit(1)

        issues = manager.load_mission()
        render_issue(
            console,
            next_issue,
            issue_file,
            issues,
            manager=manager,
            use_pager=False,
        )
    else:
        with console.pager(styles=True):
            show_store_remote_status(console, manager)
            maybe_show_strategy(console, manager)
            next_issue = manager.get_next_issue()
            if not next_issue:
                console.print(
                    f"[yellow]No issues found in {manager.mission_path}[/yellow]"
                )
                return

            issue_file = manager.find_issue_file(next_issue.slug)

            if not issue_file:
                console.print(
                    f"[red]Issue file not found for slug: {next_issue.slug}[/red]"
                )
                sys.exit(1)

            issues = manager.load_mission()
            render_issue(
                console,
                next_issue,
                issue_file,
                issues,
                manager=manager,
                use_pager=False,
            )


def normalize(s: str) -> str:
    """Normalize pattern: remove dashes, punctuation, lowercase"""
    import re

    return re.sub(r"[^a-zA-Z0-9]", "", s).lower()


def fuzzy_match(slug: str, pattern: str) -> bool:
    slug_clean = normalize(slug)
    pat_clean = normalize(pattern)
    return pat_clean in slug_clean or slug_clean.startswith(pat_clean)


def cmd_search(console: Console, manager: TaskAgent, pattern: str):
    """Search for issues by slug pattern (case-insensitive fuzzy match)."""
    pat_norm = normalize(pattern)
    if not pat_norm:
        console.print("[yellow]No pattern provided.[/yellow]")
        return

    matches: List[Issue] = []

    # Search mission issues
    issues = manager.load_mission()
    for i in issues:
        if fuzzy_match(i.slug, pat_norm):
            matches.append(i)

    # Always search completed tasks too
    for f, slug in manager.walk_completed():
        if fuzzy_match(slug, pat_norm):
            name = manager.extract_title(f)
            matches.append(Issue(name=name, slug=slug, status="completed", priority=0))

    if not matches:
        console.print(f"[yellow]No issues match pattern '{pattern}'.[/yellow]")
        return

    if len(matches) == 1:
        issue = matches[0]
        issue_file = manager.find_issue_file(
            issue.slug, include_completed=(issue.status == "completed")
        )
        if not issue_file:
            console.print(f"[red]Issue file not found for {issue.slug}[/red]")
            return

        render_issue(console, issue, issue_file, issues, manager=manager)
        console.print("[dim]Press 'e' to edit, 'q' to exit.[/dim]")
        try:
            key = get_key()
        except Exception:
            key = "q"
        if key == "e" and issue_file:
            editor = get_editor()
            subprocess.run([editor, str(issue_file)])
            manager.init_project()
        return

    cursor = 0

    with Live(auto_refresh=False, console=console, screen=True) as live:
        while True:
            table = Table(
                title=f"[bold blue]Search Results: '{pattern}'[/bold blue]",
                box=theme.table_box,
                show_header=True,
                header_style=theme.header_style,
                padding=theme.table_padding,
            )
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("Status", width=10)
            table.add_column("Slug", style="cyan")

            for idx, issue in enumerate(matches):
                style = "bold cyan" if idx == cursor else "white"
                prefix = "> " if idx == cursor else "  "
                status_style = (
                    "bold green"
                    if issue.status == "active"
                    else ("bold yellow" if issue.status == "pending" else "dim")
                )
                table.add_row(
                    str(idx + 1),
                    f"[{status_style}]{issue.status.upper()}[/{status_style}]",
                    f"{prefix}{issue.slug}",
                    style=style,
                )

            help_text = "[dim]l: view | e: edit | q: exit[/dim]"

            live.update(
                Panel(table, subtitle=help_text, box=theme.panel_box), refresh=True
            )

            try:
                key = get_key()
            except Exception:
                key = "q"

            if key in ["q", "\x1b"]:
                break
            elif key in ["k", "\x1b[A"]:
                cursor = max(0, cursor - 1)
            elif key in ["j", "\x1b[B"]:
                cursor = min(len(matches) - 1, cursor + 1)
            elif key == "l":
                live.stop()
                issue = matches[cursor]
                issue_file = manager.find_issue_file(
                    issue.slug, include_completed=(issue.status == "completed")
                )
                if issue_file:
                    render_issue(console, issue, issue_file, issues, manager=manager)
                    console.print(
                        "[dim]Press 'e' to edit, 'q' to return to list.[/dim]"
                    )
                    try:
                        inner_key = get_key()
                    except Exception:
                        inner_key = "q"
                    if inner_key == "e":
                        editor = get_editor()
                        subprocess.run([editor, str(issue_file)])
                        manager.init_project()
                        issues = manager.load_mission()
                        matches = [i for i in issues if fuzzy_match(i.slug, pat_norm)]
                        # Also re-search completed
                        new_matches: List[Issue] = list(matches)
                        for f, slug in manager.walk_completed():
                            if fuzzy_match(slug, pat_norm) and not any(
                                m.slug == slug for m in new_matches
                            ):
                                name = manager.extract_title(f)
                                new_matches.append(
                                    Issue(
                                        name=name,
                                        slug=slug,
                                        status="completed",
                                        priority=0,
                                    )
                                )
                        matches = new_matches
                        if cursor >= len(matches):
                            cursor = max(0, len(matches) - 1)
                else:
                    console.print(f"[red]Issue file not found for {issue.slug}[/red]")
                live.start()
            elif key == "e":
                live.stop()
                issue = matches[cursor]
                issue_file = manager.find_issue_file(issue.slug)
                if issue_file:
                    editor = get_editor()
                    subprocess.run([editor, str(issue_file)])
                    manager.init_project()
                    issues = manager.load_mission()
                    matches = [i for i in issues if i.slug.startswith(pattern)]
                    if cursor >= len(matches):
                        cursor = max(0, len(matches) - 1)
                live.start()


def cmd_history(console: Console, manager: TaskAgent, limit: int = 20):
    """Show completed tasks in reverse chronological order."""
    all_completed = manager.walk_completed()

    if not all_completed:
        console.print("[yellow]No completed tasks found.[/yellow]")
        return

    def get_mtime_iso(path: Path) -> str:
        try:
            content = path.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = parts[1]
                    for line in frontmatter.splitlines():
                        if line.strip().startswith("created_at:"):
                            raw_val = line.split(":", 1)[1].strip()
                            try:
                                dt = datetime.fromisoformat(raw_val)
                                return dt.strftime("%Y-%m-%d %H:%M")
                            except ValueError:
                                for fmt in (
                                    "%Y-%m-%d %H:%M",
                                    "%Y-%m-%d %H:%M:%S",
                                    "%Y-%m-%d",
                                ):
                                    try:
                                        dt = datetime.strptime(raw_val, fmt)
                                        return dt.strftime("%Y-%m-%d %H:%M")
                                    except ValueError:
                                        pass
                                return raw_val
        except Exception:
            pass
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            )
        except Exception:
            return ""

    all_completed.sort(key=lambda x: get_mtime_iso(x[0]), reverse=True)

    cursor = 0
    window_size = console.height - 8

    with Live(auto_refresh=False, console=console, screen=True) as live:
        while True:
            start_idx = max(
                0, min(cursor - window_size // 2, len(all_completed) - window_size)
            )
            if len(all_completed) <= window_size:
                start_idx = 0
                display_items = all_completed
            else:
                display_items = all_completed[start_idx : start_idx + window_size]

            table = Table(
                title="[bold blue]History[/bold blue]",
                box=theme.table_box,
                show_header=True,
                header_style=theme.header_style,
                padding=theme.table_padding,
            )
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("Date", style="dim", width=16)
            table.add_column("Slug", style="cyan")

            for idx, (file, slug) in enumerate(display_items):
                absolute_idx = start_idx + idx
                style = "bold cyan" if absolute_idx == cursor else "white"
                prefix = "> " if absolute_idx == cursor else "  "
                date_str = get_mtime_iso(file)
                table.add_row(
                    str(absolute_idx + 1), date_str, f"{prefix}{slug}", style=style
                )

            help_text = "[dim]v/l: view | c: copy slug | q: exit[/dim]"

            live.update(
                Panel(table, subtitle=help_text, box=theme.panel_box), refresh=True
            )

            try:
                key = get_key()
            except Exception:
                key = "q"

            if key in ["q", "\x1b"]:
                break
            elif key in ["k", "\x1b[A"]:
                cursor = max(0, cursor - 1)
            elif key in ["j", "\x1b[B"]:
                cursor = min(len(all_completed), cursor + 1)
            elif key in ["v", "l"]:
                live.stop()
                file, slug = all_completed[cursor]
                issue = Issue(name=slug, slug=slug, status="completed", priority=0)
                render_issue(console, issue, file, manager=manager)
                try:
                    get_key()
                except Exception:
                    pass
                live.start()
            elif key in ["c"]:
                # Copy slug to clipboard
                _, slug = all_completed[cursor]
                try:
                    pyperclip.copy(slug)
                    console.print(f"[green]Copied slug to clipboard: {slug}[/green]")
                except Exception as e:
                    console.print(f"[yellow]Failed to copy to clipboard: {e}[/yellow]")


def cmd_recover_history(console: Console, manager: TaskAgent):
    """Recover deleted task files from Git history and populate/restore task creation dates into YAML frontmatter."""
    console.print("[blue]Checking Git history for deleted task files...[/blue]")
    try:
        out = subprocess.check_output(
            [
                "git",
                "log",
                "--all",
                "--pretty=format:",
                "--name-only",
                "--diff-filter=D",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        deleted_paths = sorted(
            list(set(line.strip() for line in out.splitlines() if line.strip()))
        )
    except Exception as e:
        console.print(f"[red]Failed to query git history: {e}[/red]")
        sys.exit(1)

    existing_slugs = set()
    for root, dirs, files in os.walk(manager.issues_root):
        for f in files:
            if f.endswith(".md"):
                file_path = Path(root) / f
                if f == "README.md":
                    slug = file_path.parent.name
                else:
                    slug = file_path.stem
                if slug != "tasks":
                    existing_slugs.add(slug)

    try:
        for issue in manager.load_mission():
            existing_slugs.add(issue.slug)
    except Exception:
        pass

    def get_slug_from_path(path_str: str) -> str:
        p = Path(path_str)
        if p.name == "README.md":
            return p.parent.name
        return p.stem

    def get_deleted_file_content(path: str) -> Optional[str]:
        try:
            commits = (
                subprocess.check_output(
                    ["git", "log", "--all", "--format=%H", "--", path],
                    stderr=subprocess.DEVNULL,
                    text=True,
                )
                .strip()
                .splitlines()
            )
            for commit in commits:
                try:
                    res = subprocess.check_output(
                        ["git", "show", f"{commit}:{path}"],
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    if res:
                        return res
                except Exception:
                    pass
        except Exception:
            pass
        return None

    restored_count = 0
    for path_str in deleted_paths:
        parts = Path(path_str).parts
        if not (("tasks" in parts or "issues" in parts) and path_str.endswith(".md")):
            continue
        if parts[-1] in ["plan.md", "README.md"] and (
            len(parts) <= 2 or parts[-2] in ["tasks", "issues"]
        ):
            continue

        slug = get_slug_from_path(path_str)
        if slug in existing_slugs or slug == "tasks":
            continue

        content = get_deleted_file_content(path_str)
        if not content:
            continue

        status = "pending"
        year = None
        for part in ["completed", "pending", "draft", "active"]:
            if part in parts:
                status = part
                idx = parts.index(part)
                if part == "completed" and idx + 1 < len(parts):
                    next_part = parts[idx + 1]
                    if next_part.isdigit() and len(next_part) == 4:
                        year = next_part
                break

        if status == "completed":
            year_str = year or str(datetime.now().year)
            target_file = (
                manager.issues_root / "completed" / year_str / slug / "README.md"
            )
        else:
            target_file = manager.issues_root / status / slug / "README.md"

        try:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            target_file.write_text(content, encoding="utf-8")
            console.print(f"[green]Restored: {slug} (status: {status})[/green]")
            existing_slugs.add(slug)
            restored_count += 1
        except Exception as e:
            console.print(f"[red]Failed to restore {slug}: {e}[/red]")

    console.print(
        f"[bold green]Restored {restored_count} task(s) from git history.[/bold green]"
    )

    # 2. Run folder migration for any loose files in workspace
    migrated_count = manager.migrate_all_to_folders()
    if migrated_count > 0:
        console.print(
            f"[green]Migrated {migrated_count} file-based task(s) to folder format.[/green]"
        )

    # 3. Recover dates and write/update frontmatter
    console.print("[blue]Recovering task creation dates into frontmatter...[/blue]")
    updated_dates_count = 0
    for root, dirs, files in os.walk(manager.issues_root):
        root_path = Path(root)
        try:
            rel_parts = root_path.relative_to(manager.issues_root).parts
            if ".task-agent" in rel_parts:
                continue
        except ValueError:
            pass

        for file in files:
            if file != "README.md":
                continue

            readme_path = root_path / "README.md"
            slug = root_path.name
            if slug == "tasks":
                continue

            # Query git log with wildcard pathspecs matching the slug in issues or tasks directories
            earliest_date = None
            pathspecs = [
                f"*tasks*/{slug}/README.md",
                f"*tasks*/{slug}.md",
                f"*issues*/{slug}/README.md",
                f"*issues*/{slug}.md",
            ]
            try:
                out = subprocess.check_output(
                    ["git", "log", "--all", "--format=%aI", "--reverse", "--"]
                    + pathspecs,
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                if out:
                    earliest_date = out.splitlines()[0]
            except Exception:
                pass

            if not earliest_date:
                # Fallback to file creation/modification date
                try:
                    stat = readme_path.stat()
                    birthtime = getattr(stat, "st_birthtime", None)
                    t = birthtime if birthtime is not None else stat.st_mtime
                    earliest_date = datetime.fromtimestamp(t).astimezone().isoformat()
                except Exception:
                    earliest_date = datetime.now().astimezone().isoformat()

            content = readme_path.read_text(encoding="utf-8")
            if content.startswith("---"):
                content_parts = content.split("---", 2)
                if len(content_parts) >= 3:
                    frontmatter = content_parts[1]
                    lines = frontmatter.splitlines()
                    has_created_at = False
                    for i, line in enumerate(lines):
                        if line.strip().startswith("created_at:"):
                            lines[i] = f"created_at: {earliest_date}"
                            has_created_at = True
                            break
                    if not has_created_at:
                        lines.append(f"created_at: {earliest_date}")
                    new_frontmatter = "\n".join(lines) + "\n"
                    new_content = f"---{new_frontmatter}---{content_parts[2]}"
                else:
                    new_content = f"---\ncreated_at: {earliest_date}\n---\n\n" + content
            else:
                new_content = f"---\ncreated_at: {earliest_date}\n---\n\n" + content

            readme_path.write_text(new_content, encoding="utf-8")
            updated_dates_count += 1

    console.print(
        f"[bold green]Updated {updated_dates_count} task(s) with creation dates.[/bold green]"
    )

    # Run project initialization to sync the restored files with mission.usv
    manager.init_project()


def cmd_show(
    console: Console,
    manager: TaskAgent,
    slug_part: Union[str, List[str]],
    children: bool = False,
    include_completed: bool = False,
):
    """Show one or more tasks' README and secondary Markdown documents.

    *slug_part* may be a single slug/title or a list of them.
    With *children*, expand each root to include all transitive dependents
    (``subtask_of`` children and tasks blocked by the root). Completed
    dependents are omitted unless *include_completed* is True.
    """
    if isinstance(slug_part, str):
        queries: List[str] = [slug_part]
    else:
        queries = list(slug_part)

    if not queries:
        console.print("[yellow]No task slugs provided.[/yellow]")
        return

    slugs, missing = manager.expand_show_slugs(
        queries, children=children, include_completed=include_completed
    )
    for q in missing:
        console.print(f"[red]Issue not found: {q}[/red]")
    if not slugs:
        return

    # Relations pool for blocker display (open mission + optional completed)
    issues = manager.load_issues_for_relations(include_completed=include_completed)
    # Always include mission issues for panel metadata even if not in pool flags
    mission = manager.load_mission()
    slug_to_issue = {i.slug: i for i in mission}
    for i in issues:
        slug_to_issue.setdefault(i.slug, i)

    multi = len(slugs) > 1
    for idx, slug in enumerate(slugs):
        issue_file = manager.find_issue_file(slug, include_completed=True)
        if not issue_file:
            console.print(f"[red]Issue not found: {slug}[/red]")
            continue
        issue = slug_to_issue.get(slug)
        if issue is None:
            name = manager.extract_title(issue_file)
            issue = Issue(name=name, slug=slug, status="completed", priority=0)
            blocked_by, subtask_of = manager.extract_relations(issue_file)
            issue.blocked_by = blocked_by
            issue.subtask_of = subtask_of

        if multi and idx > 0:
            console.print()
        if multi or children:
            console.print(f"[bold dim]—— {slug} ——[/bold dim]")

        render_issue(
            console,
            issue,
            issue_file,
            list(slug_to_issue.values()),
            manager=manager,
            use_pager=not multi and not children,
        )


def cmd_document(console: Console, manager: TaskAgent, args) -> None:
    """Manage secondary documents on a task (add / list)."""
    action = getattr(args, "document_command", None) or getattr(args, "action", None)
    if action == "list":
        resolved = manager.resolve_issue_slug(args.slug, include_completed=True)
        slug = resolved or manager.slugify(args.slug)
        try:
            docs = manager.list_secondary_documents(slug, include_completed=True)
        except FileNotFoundError:
            console.print(f"[red]Issue not found: {args.slug}[/red]")
            return
        if not docs:
            console.print(f"[yellow]No secondary documents on '{slug}'.[/yellow]")
            return
        console.print(
            f"[bold blue]Secondary documents on [cyan]{slug}[/cyan] "
            f"({len(docs)}):[/bold blue]"
        )
        for d in docs:
            console.print(f"  • [cyan]{d.name}[/cyan]  [dim]{d}[/dim]")
        return

    if action == "add":
        resolved = manager.resolve_issue_slug(args.slug, include_completed=True)
        slug = resolved or manager.slugify(args.slug)
        content = args.body or ""
        if args.file:
            content = Path(args.file).read_text(encoding="utf-8")
        elif not content and not sys.stdin.isatty():
            content = sys.stdin.read()
        if not content and not args.file:
            console.print(
                "[yellow]No content provided. Use -b/--body, -f/--file, or stdin.[/yellow]"
            )
            return
        try:
            path = manager.add_task_document(
                slug,
                args.filename,
                content,
                overwrite=bool(args.overwrite),
            )
        except FileNotFoundError:
            console.print(f"[red]Issue not found: {args.slug}[/red]")
            return
        except (ValueError, FileExistsError, RuntimeError) as e:
            console.print(f"[red]{e}[/red]")
            return
        console.print(
            f"[bold green]Added document [cyan]{path.name}[/cyan] to "
            f"[cyan]{slug}[/cyan][/bold green]\n[dim]{path}[/dim]"
        )
        return

    console.print(
        "[yellow]Usage: ta document {add|list} …  (see ta document --help)[/yellow]"
    )


def maybe_show_inbox_banner(console: Console, manager: TaskAgent) -> None:
    """Print an unread inbox banner if present (never mutates inbox state)."""
    try:
        from taskagent.inbox import format_unread_banner, moniker_for_store

        store = manager.issues_root
        if not store:
            return
        moniker = moniker_for_store(store)
        banner = format_unread_banner(store, moniker=moniker)
        if banner:
            console.print(f"[bold magenta]{banner}[/bold magenta]")
    except Exception:
        pass


def cmd_inbox(console: Console, manager: TaskAgent, args) -> None:
    """Inbox messaging: list / show / send / ack / gc."""
    from taskagent.inbox import (
        DEFAULT_RETENTION_DAYS,
        ack_message,
        find_unread_message,
        format_unread_banner,
        gc_inbox,
        list_unread,
        moniker_for_store,
        parse_message_file,
        resolve_sender_moniker,
        send_to_repo,
        snapshot_from_issue,
    )
    from taskagent.store_registry import (
        AmbiguousRepoMatchError,
        RepoNotFoundError,
    )

    action = getattr(args, "inbox_command", None)
    store = manager.issues_root
    moniker = moniker_for_store(store) if store else None

    if action == "watch":
        from taskagent.inbox import watch_inbox

        thread = getattr(args, "thread", None) or None
        timeout = getattr(args, "timeout", None)
        msgs = watch_inbox(store, thread=thread, timeout_seconds=timeout)
        if not msgs:
            console.print("[dim]Timed out waiting for inbox messages.[/dim]")
            return
        for m in msgs:
            console.print(f"  {m.summary_line()}")
        return

    if action == "list":
        thread = getattr(args, "thread", None)
        msgs = list_unread(store, thread=thread)
        if not msgs:
            scope = f" (thread={thread})" if thread else ""
            console.print(f"[dim]No unread inbox messages{scope}.[/dim]")
            return
        title = "[bold blue]Unread inbox[/bold blue]"
        if moniker:
            title += f" [dim]({moniker})[/dim]"
        console.print(f"{title} — {len(msgs)} message(s)")
        for m in msgs:
            console.print(f"  {m.summary_line()}")
        return

    if action == "show":
        path = find_unread_message(store, args.id)
        if path is None:
            # Also allow showing already-read by scanning? v1: unread only + path
            console.print(f"[red]Unread message not found: {args.id}[/red]")
            return
        msg = parse_message_file(path, status="unread")
        console.print(
            Panel(
                f"[bold]id[/bold]: {msg.id}\n"
                f"[bold]from[/bold]: {msg.from_moniker}\n"
                f"[bold]kind[/bold]: {msg.kind}\n"
                f"[bold]thread[/bold]: {msg.thread or '—'}\n"
                f"[bold]created_at[/bold]: {msg.created_at}\n"
                f"[bold]path[/bold]: {msg.path}",
                title="Inbox message",
                box=theme.panel_box,
            )
        )
        console.print(Markdown(msg.body or "_(empty body)_"))
        return

    if action == "send":
        to_repo = args.to
        kind = args.kind or "info"
        body = args.body or ""
        if args.file:
            body = Path(args.file).read_text(encoding="utf-8")
        elif not body and not sys.stdin.isatty():
            body = sys.stdin.read()
        thread = args.thread
        task_slug = None
        snapshot = None
        if args.task:
            slug = manager.resolve_issue_slug(args.task) or manager.slugify(args.task)
            task_slug = slug
            issues = manager.load_mission()
            issue = next((i for i in issues if i.slug == slug), None)
            if issue is None:
                # completed fallback
                issue_file = manager.find_issue_file(slug, include_completed=True)
                if issue_file:
                    from taskagent.models.issue import Issue

                    issue = Issue(
                        name=manager.extract_title(issue_file),
                        slug=slug,
                        status="completed",
                        priority=0,
                    )
            if issue is not None:
                snapshot = snapshot_from_issue(issue)
                task_slug = issue.slug
                if not thread:
                    thread = issue.slug
            else:
                console.print(
                    f"[yellow]Task {args.task!r} not in this store; "
                    f"sending slug pointer {slug!r} without local snapshot.[/yellow]"
                )
                if not thread:
                    thread = slug

        from_moniker = args.sender or resolve_sender_moniker(
            store_path=store, host_path=Path.cwd()
        )
        try:
            msg, resolved = send_to_repo(
                to_repo,
                from_moniker=from_moniker,
                body=body,
                kind=kind,
                thread=thread,
                task=task_slug,
                task_snapshot=snapshot,
            )
        except (
            RepoNotFoundError,
            AmbiguousRepoMatchError,
            ValueError,
            FileExistsError,
        ) as e:
            console.print(f"[red]Send failed: {e}[/red]")
            return
        console.print(
            f"[bold green]Sent[/bold green] [cyan]{msg.id}[/cyan] → "
            f"[cyan]{resolved.moniker}[/cyan] "
            f"[dim]({resolved.store_path}/.task-agent/inbox/unread/)[/dim]"
        )
        return

    if action == "ack":
        try:
            msg = ack_message(store, args.id)
        except (FileNotFoundError, FileExistsError) as e:
            console.print(f"[red]{e}[/red]")
            return
        console.print(
            f"[bold green]Acked[/bold green] [cyan]{msg.id}[/cyan] → "
            f"[dim]{msg.path}[/dim]"
        )
        if getattr(args, "start", False):
            start_slug = msg.linked_slug
            if not start_slug:
                console.print(
                    "[yellow]--start ignored: message has no task/thread slug. "
                    "Senders must pass --task or --thread for task-created.[/yellow]"
                )
                return
            try:
                manager.move_to_active(start_slug)
                console.print(
                    f"[bold green]Started[/bold green] task "
                    f"[cyan]{start_slug}[/cyan] (status → active)"
                )
            except Exception as e:
                console.print(
                    f"[red]Acked, but could not start task {start_slug!r}: {e}[/red]"
                )
        return

    if action == "gc":
        retention = getattr(args, "days", None)
        if retention is None:
            retention = DEFAULT_RETENTION_DAYS
        dry = bool(getattr(args, "dry_run", False))
        deleted = gc_inbox(store, retention_days=int(retention), dry_run=dry)
        if not deleted:
            console.print(
                f"[dim]Inbox GC: nothing to remove "
                f"(retention={retention}d, dry_run={dry}).[/dim]"
            )
            return
        verb = "Would delete" if dry else "Deleted"
        console.print(
            f"[bold blue]Inbox GC[/bold blue]: {verb} {len(deleted)} day dir(s)"
        )
        for d in deleted:
            console.print(f"  • {d}")
        return

    # Default: banner + list hint
    banner = format_unread_banner(store, moniker=moniker)
    if banner:
        console.print(f"[bold magenta]{banner}[/bold magenta]")
    else:
        console.print("[dim]Inbox empty (no unread).[/dim]")
    console.print(
        "[dim]Commands: ta inbox list | show <id> | send --to <repo> | "
        "ack <id> | gc[/dim]"
    )


def cmd_report(console: Console, manager: TaskAgent, slug: str):
    """View metadata/logs for a task."""
    issue_file = manager.find_issue_file(slug, include_completed=True)
    if not issue_file:
        console.print(f"[red]Issue not found: {slug}[/red]")
        return

    meta_file = issue_file.parent / "meta.json"
    if not meta_file.exists():
        console.print(f"[yellow]No metadata found for {slug}[/yellow]")
        return

    with meta_file.open("r", encoding="utf-8") as f:
        meta = json.load(f)

    console.print(f"[bold blue]Task Report: {slug}[/bold blue]")
    console.print(
        Panel(json.dumps(meta, indent=2), title="Metadata", box=theme.panel_box)
    )

    trace_path = issue_file.parent / meta.get("reasoning_trace", "logs/trace.log")
    if trace_path.exists():
        console.print(f"[bold blue]Reasoning Trace ({trace_path.name}):[/bold blue]")
        console.print(
            Panel(trace_path.read_text(encoding="utf-8"), box=theme.panel_box)
        )
    else:
        console.print("[yellow]Reasoning trace not found.[/yellow]")


def cmd_mcp_api(console: Console):
    """Display the MCP API (tools and docstrings)."""
    mcp_file = Path(__file__).parent / "mcp.py"
    if not mcp_file.exists():
        console.print("[red]Could not find mcp.py[/red]")
        return

    content = mcp_file.read_text(encoding="utf-8")

    # Regex to extract @mcp.tool() decorated functions and their docstrings
    pattern = re.compile(
        r"@mcp\.tool\(\)\ndef\s+(\w+)\(.*?\)\s*->.*?:?\n\s+[\"']{3}(.*?)[\"']{3}",
        re.DOTALL,
    )

    console.print("[bold blue]Available MCP Tools:[/bold blue]\n")
    for match in pattern.finditer(content):
        tool_name = match.group(1)
        docstring = match.group(2).strip()
        console.print(f"[bold cyan]{tool_name}[/bold cyan]")
        console.print(f"  {docstring}\n")


def cmd_soft_delete(console: Console, manager: TaskAgent, slug: str):
    """Soft-delete a task: archive it without committing."""
    try:
        issue = manager.soft_delete_issue(slug)
        console.print(
            f"[bold yellow]Task '{issue.slug}' soft-deleted.[/bold yellow] "
            f"Archived to [bold]docs/tasks/deleted/[/bold]. "
            f"Use [bold]git checkout docs/tasks/deleted/{issue.slug}[/bold] and "
            f"[bold]ta restore {issue.slug}[/bold] to bring it back."
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def detect_current_slug_from_git() -> Optional[str]:
    """Detect the current task slug from the current git branch name."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = result.stdout.strip()
        if branch.startswith("issue/"):
            return branch[len("issue/") :]
    except Exception:
        pass
    return None


def find_worktree_path_for_slug(slug: str) -> Optional[Path]:
    """Find the registered worktree path for a given task slug."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        current_worktree = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current_worktree = Path(line[len("worktree ") :].strip())
            elif line.startswith("branch refs/heads/issue/"):
                branch_slug = line[len("branch refs/heads/issue/") :].strip()
                if branch_slug == slug:
                    return current_worktree
    except Exception:
        pass
    # Fallback to local .gwt/slug if it exists
    p = Path(".gwt") / slug
    if p.exists():
        return p
    return None


def cmd_done(
    console: Console,
    manager: TaskAgent,
    slug: Optional[str] = None,
    commit_message: Optional[str] = None,
    should_commit: bool = True,
    push_mission: bool = False,
    solution: Optional[str] = None,
    no_verify: bool = True,
    metrics: Optional[SubtaskMetric] = None,
):
    """Mark an issue as done."""
    if not slug:
        slug = detect_current_slug_from_git()
        if not slug:
            console.print(
                "[red]Error: Please specify the task slug or run this command from within the task's worktree/branch.[/red]"
            )
            sys.exit(1)

    worktree_path = find_worktree_path_for_slug(slug)
    abs_worktree = (
        worktree_path.resolve() if worktree_path and worktree_path.exists() else None
    )
    is_cwd_inside_worktree = False
    if abs_worktree:
        try:
            is_cwd_inside_worktree = Path.cwd().resolve().is_relative_to(abs_worktree)
        except Exception:
            pass

    try:
        issue, commit_hash = manager.complete_issue(
            slug,
            commit_message=commit_message,
            should_commit=should_commit,
            push_mission=push_mission,
            solution_explanation=solution,
            no_verify=no_verify,
            metrics=metrics,
        )
        console.print(
            f"[bold green]Issue '{issue.slug}' marked as done and "
            f"removed from mission.usv[/bold green]"
        )
        if commit_hash:
            console.print(f"Commit: {commit_hash}")

        # Releases are explicit — do not auto-amend/bump on done (that rewrote
        # history and orphaned tags). Hint when a project version file exists.
        try:
            pv, psrc = get_project_version()
            if psrc and pv != "unknown":
                console.print(
                    f"[dim]Project at v{pv}. Release when ready: "
                    f"ta version release patch[/dim]"
                )
        except Exception:
            pass
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)
    finally:
        # Destroy per-task agent even if commit fails
        agent.destroy_per_task_agent(slug)

    # Perform git worktree and branch cleanup after successful completion
    if worktree_path and worktree_path.exists():
        if is_cwd_inside_worktree:
            console.print(
                f"[yellow]Note: Worktree at '{worktree_path}' and branch 'issue/{slug}' were not removed because your shell is currently inside the worktree directory.[/yellow]"
            )
            console.print(
                "[yellow]To clean up, please change directory to the main repository directory and run:[/yellow]"
            )
            console.print("  [bold]git worktree prune[/bold]")
            console.print(f"  [bold]git branch -D issue/{slug}[/bold]")
        else:
            console.print(f"[blue]Cleaning up git worktree for '{slug}'...[/blue]")
            try:
                # Remove worktree
                subprocess.run(
                    ["git", "worktree", "remove", str(worktree_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                console.print(
                    f"[green]Successfully removed worktree at '{worktree_path}'.[/green]"
                )

                # Delete branch
                branch_name = f"issue/{slug}"
                console.print(f"[blue]Deleting local branch '{branch_name}'...[/blue]")
                subprocess.run(
                    ["git", "branch", "-d", branch_name],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                console.print(
                    f"[green]Successfully deleted branch '{branch_name}'.[/green]"
                )
            except subprocess.CalledProcessError as e:
                console.print(
                    f"[yellow]Warning: Cleanup failed. {e.stderr.strip()}[/yellow]"
                )
                console.print(
                    f"[yellow]You can clean up manually by running: git worktree remove --force {worktree_path} && git branch -D issue/{slug}[/yellow]"
                )
    else:
        # If worktree doesn't exist, we still try to delete the branch if it exists
        branch_name = f"issue/{slug}"
        try:
            result = subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                console.print(f"[blue]Deleting local branch '{branch_name}'...[/blue]")
                subprocess.run(
                    ["git", "branch", "-d", branch_name],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                console.print(
                    f"[green]Successfully deleted branch '{branch_name}'.[/green]"
                )
        except subprocess.CalledProcessError as e:
            console.print(
                f"[yellow]Warning: Could not delete branch '{branch_name}' safely: {e.stderr.strip()}[/yellow]"
            )
            console.print(
                f"[yellow]Run 'git branch -D {branch_name}' to force delete it.[/yellow]"
            )


def cmd_push(console: Console, manager: TaskAgent):
    """Push the mission repository."""
    if not manager.mission_root:
        console.print("[red]Mission repository not detected.[/red]")
        return

    console.print(
        f"[blue]Pushing mission repository at {manager.mission_root}...[/blue]"
    )
    try:
        manager.push_mission_repo()
        console.print(
            "[bold green]Successfully pushed mission repository.[/bold green]"
        )
    except Exception as e:
        console.print(f"[red]Failed to push mission repository: {e}[/red]")


def cmd_commit(
    console: Console,
    manager: TaskAgent,
    message: Optional[str] = None,
    should_push: bool = True,
):
    """Commit and optionally push changes in the tasks/ directory."""
    import subprocess

    # Determine the tasks directory and git root
    tasks_dir = manager.issues_root
    if not tasks_dir or not tasks_dir.exists():
        console.print("[red]Tasks directory not found.[/red]")
        return

    git_root = manager.mission_root
    if not git_root:
        console.print("[red]No git repository found for tasks directory.[/red]")
        return

    # Generate default commit message if not provided
    if not message:
        message = f"Update tasks - {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    console.print(f"[blue]Committing changes in {tasks_dir}...[/blue]")

    try:
        # Add all changes in tasks directory
        resolved_tasks_dir = tasks_dir.resolve()
        subprocess.run(
            ["git", "-C", str(git_root), "add", str(resolved_tasks_dir / ".")],
            check=True,
            capture_output=True,
            text=True,
            shell=(os.name == "nt"),
        )

        # Check if there are changes to commit
        result = subprocess.run(
            ["git", "-C", str(git_root), "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
            shell=(os.name == "nt"),
        )

        if result.returncode == 0:
            console.print("[yellow]No changes to commit in tasks/ directory.[/yellow]")
            return

        # Commit
        subprocess.run(
            ["git", "-C", str(git_root), "commit", "--no-verify", "-m", message],
            check=True,
            capture_output=True,
            text=True,
            shell=(os.name == "nt"),
        )
        console.print(f"[bold green]Committed: {message}[/bold green]")

        # Push if requested
        if should_push:
            if not manager.mission_root:
                console.print(
                    "[yellow]No mission repository configured, skipping push.[/yellow]"
                )
            else:
                remotes = subprocess.run(
                    ["git", "-C", str(manager.mission_root), "remote"],
                    capture_output=True,
                    text=True,
                    shell=(os.name == "nt"),
                )
                if not (remotes.stdout or "").strip():
                    console.print(
                        "[yellow]No git remote on mission store; commit kept local. "
                        "Set one with [bold]ta store remote set <url>[/bold].[/yellow]"
                    )
                else:
                    console.print("[blue]Pushing to remote...[/blue]")
                    manager.push_mission_repo()
                    console.print("[bold green]Successfully pushed.[/bold green]")

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error: {e.stderr}[/red]")
    except Exception as e:
        console.print(f"[red]Unexpected error: {e}[/red]")


def cmd_commit_tasks(
    console: Console,
    message: Optional[str] = None,
    should_push: bool = True,
):
    """Commit and optionally push changes in the task-agent project's task store.

    Always targets the task-agent project (via ``discover`` on its root), so
    centralized data-root stores are committed in the store's own git repo —
    not the host tree that may only hold a symlink.
    """
    project_root = get_task_agent_project_root()
    try:
        manager = discover(project_root)
    except Exception as e:
        console.print(f"[red]Could not discover task-agent store: {e}[/red]")
        return

    console.print(
        f"[dim]task-agent store: {manager.issues_root} "
        f"(mission git: {manager.mission_root or '—'})[/dim]"
    )
    cmd_commit(console, manager, message=message, should_push=should_push)


def is_native_windows() -> bool:
    """True when running under native Windows Python (not WSL/Linux/macOS).

    WSL uses a Linux Python runtime (``sys.platform`` starts with ``linux``),
    so this is False under WSL even when the host machine is Windows.
    """
    return sys.platform == "win32"


_NATIVE_WINDOWS_STORE_OPS_MSG = (
    "Store migrate and eject are not supported on native Windows yet.\n"
    "\n"
    "Reasons:\n"
    "  1. Data roots diverge: native Windows and WSL use different home "
    "directories, so without TA_DATA_ROOT they build disconnected registries "
    "with no error.\n"
    "  2. Git-tracked symlinks often check out as plain text files on Windows "
    "(unless core.symlinks=true and Developer Mode), so migration pointers "
    "break silently.\n"
    "\n"
    "Use WSL or Linux for this command until Windows data-root and symlink "
    "support exists."
)


def refuse_if_native_windows_store_ops(console: Console, command: str) -> None:
    """Refuse store migrate / eject on native Windows; raise SystemExit(1).

    No-op on WSL, Linux, and macOS. Does not affect any other command paths.
    """
    if not is_native_windows():
        return
    console.print(f"[red]Refused:[/red] [bold]{command}[/bold] on native Windows.\n")
    console.print(_NATIVE_WINDOWS_STORE_OPS_MSG)
    raise SystemExit(1)


def cmd_store_help(console: Console) -> None:
    """Show themed help for the full ``ta store`` command group."""
    console.print(
        Panel(
            "[bold]ta store[/bold] — machine-level task store layout, registry, "
            "migration, and remotes",
            box=theme.panel_box,
        )
    )
    table = Table(
        box=theme.table_box,
        header_style=theme.header_style,
        padding=theme.table_padding,
        show_header=True,
    )
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")

    commands = [
        ("data-root", "Print the machine-wide task-agent data home"),
        (
            "path [path]",
            "Print this project's task store directory (under data-root/stores/…)",
        ),
        ("moniker [path]", "Print the moniker for a host path (default: cwd)"),
        (
            "symlink on|off|status",
            "Human-facing docs/tasks → store symlink (gitignore when on)",
        ),
        ("list", "List registered machine task stores"),
        (
            "inspect [path]",
            "Read-only inspect: moniker, legacy store, migration status",
        ),
        ("inspect --json", "Same as inspect, machine-readable JSON"),
        (
            "rebuild-index",
            "Rebuild registry.json by scanning stores/ under the data root",
        ),
        ("migrate [path]", "Move legacy .task-agent/tasks into the machine data root"),
        ("migrate --dry-run", "Plan a migrate without moving data"),
        ("remote show", "List git remotes on the current project's task store"),
        (
            "remote suggest",
            "Suggest tasks remotes via forge plugins (sibling *-tasks)",
        ),
        (
            "remote create [--repo nvim]",
            "Create sibling *-tasks repo via forge API and attach (plugin)",
        ),
        (
            "remote set <url>",
            "Configure remote URL only (no fetch/push)",
        ),
        (
            "remote attach <url>",
            "Connect + publish: rename mismatched remote tips, push main, set default",
        ),
        (
            "rebind [moniker]",
            "Rebind after subject repo rename (moniker, pointers, registry)",
        ),
    ]
    for cmd, desc in commands:
        table.add_row(cmd, desc)

    console.print(table)
    console.print(
        "\n[dim]Run [bold]ta store <command> --help[/bold] for detailed options.[/dim]"
    )
    console.print(
        "[dim]Data root default: "
        "[bold]~/.local/share/task-agent[/bold] "
        "(override with [bold]TA_DATA_ROOT[/bold]).[/dim]"
    )


def _store_host_from_args(console: Console, args) -> Path:
    """Resolve subject host path from --repo (fuzzy), path, or cwd."""
    repo = getattr(args, "repo", None)
    if repo:
        from taskagent.store_registry import (
            AmbiguousRepoMatchError,
            RepoNotFoundError,
            resolve_repo_query,
        )

        try:
            resolved = resolve_repo_query(repo)
        except AmbiguousRepoMatchError as exc:
            console.print(f"[red]Ambiguous --repo {repo!r}:[/red]")
            for c in exc.candidates:
                console.print(
                    f"  [cyan]{c.moniker}[/cyan]  {c.store_path}  [dim]({c.reason})[/dim]"
                )
            raise SystemExit(1) from exc
        except RepoNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            console.print(
                "[dim]Register the project first: "
                "[bold]ta store migrate[/bold] in that repo.[/dim]"
            )
            raise SystemExit(1) from exc
        if resolved.host_paths:
            return Path(resolved.host_paths[0]).expanduser().resolve()
        # Store known but no host path recorded — still useful for path/list style
        console.print(
            f"[yellow]--repo {repo!r} has no host_paths; "
            f"using store {resolved.store_path}[/yellow]"
        )
        return Path(resolved.store_path).resolve()
    if getattr(args, "path", None):
        return Path(args.path).expanduser().resolve()
    return Path.cwd().resolve()


def cmd_store(console: Console, args) -> None:
    """Machine data-root / moniker / registry / migrate commands."""
    from taskagent.store_registry import (
        MachineRegistry,
        get_data_root,
        inspect_host,
        migrate_store,
        resolve_moniker_for_host,
    )

    sub = getattr(args, "store_command", None)
    if not sub:
        cmd_store_help(console)
        return

    if sub == "data-root":
        console.print(str(get_data_root()))
        return

    if sub == "path":
        # Plain path only (script-friendly), like data-root
        host = _store_host_from_args(console, args)
        report = inspect_host(host)
        canonical = Path(report["canonical_store_path"])
        legacy = (
            Path(report["legacy_store_path"])
            if report.get("legacy_store_path")
            else None
        )
        entry = report.get("registry_entry") or {}
        entry_path = Path(entry["store_path"]) if entry.get("store_path") else None

        # Prefer live store: registry path → canonical → legacy pointer target
        for candidate in (entry_path, canonical, legacy):
            if candidate is None:
                continue
            try:
                if candidate.is_dir():
                    console.print(str(candidate.resolve()))
                    return
            except OSError:
                continue

        # Not migrated yet: still print intended canonical path for planning
        console.print(str(canonical))
        return

    if sub == "moniker":
        host = _store_host_from_args(console, args)
        moniker, origin = resolve_moniker_for_host(host)
        console.print(moniker)
        if origin:
            console.print(f"[dim]origin: {origin}[/dim]")
        return

    if sub == "symlink":
        from taskagent.store_registry import (
            StoreSymlinkError,
            docs_tasks_symlink_status,
            normalize_store_symlink_preference,
            set_docs_tasks_symlink,
        )

        action = getattr(args, "symlink_action", None)
        host = _store_host_from_args(console, args)
        if action in (None, "status"):
            # Materialize the implicit store_symlink default once for opted-in
            # hosts (those with an existing .ta-config.json). Inspecting status
            # is exactly when a human would expect this self-heal to run; never
            # creates .ta-config.json where none existed.
            wrote_default = normalize_store_symlink_preference(host)
            st = docs_tasks_symlink_status(host)
            pref = "on" if st["preferred"] else "off"
            console.print(f"[bold]Host[/bold]:       {st['host_path']}")
            console.print(f"[bold]Preference[/bold]: symlink {pref} (.ta-config.json)")
            if wrote_default:
                console.print(
                    "[dim]note: persisted missing store_symlink default "
                    "(true) to .ta-config.json[/dim]"
                )
            console.print(f"[bold]Store[/bold]:      {st.get('store_path') or '—'}")
            for p in st.get("paths") or []:
                rel = p["rel"]
                kind = p["kind"]
                line = f"[bold]{rel}[/bold]: {kind}"
                if p.get("target"):
                    line += f" → {p['target']}"
                if kind == "symlink":
                    ok = p.get("points_to_store")
                    line += (
                        "  [green]points to store[/green]"
                        if ok
                        else "  [yellow]not store[/yellow]"
                    )
                gi = (st.get("gitignore_entries") or {}).get(rel)
                if gi:
                    line += "  [dim](gitignored)[/dim]"
                console.print(line)
            console.print(
                "\n[dim]Human convenience only — agents should use "
                "[bold]ta store path[/bold] / MCP, not docs/tasks.[/dim]"
            )
            return

        if action not in ("on", "off"):
            console.print("[yellow]Usage: ta store symlink {on|off|status}[/yellow]")
            return
        try:
            symlink_result = set_docs_tasks_symlink(host, enabled=(action == "on"))
        except StoreSymlinkError as exc:
            console.print(f"[red]Cannot turn symlink {action}:[/red]\n{exc}")
            raise SystemExit(1)
        except Exception as exc:
            console.print(f"[red]symlink {action} failed:[/red] {exc}")
            raise SystemExit(1)
        console.print(
            f"[bold green]symlink {action}[/bold green] "
            f"for {symlink_result['host_path']}"
        )
        for act in symlink_result.get("actions") or []:
            console.print(f"  ✓ {act}")
        return

    if sub == "list":
        from taskagent.store_registry import mission_remote_status

        reg = MachineRegistry()
        entries = reg.list_entries()
        if not entries:
            console.print(
                f"[dim]No registered stores under {get_data_root()} "
                f"(registry empty or missing).[/dim]"
            )
            return
        table = Table(
            title="Machine task stores",
            box=theme.table_box,
            header_style=theme.header_style,
            padding=theme.table_padding,
            show_header=True,
        )
        table.add_column("Moniker", style="cyan")
        table.add_column("Remote", style="white")
        table.add_column("Status", style="white")
        table.add_column("Store path", style="dim")
        table.add_column("Host paths", style="dim")
        for entry in entries:
            live = mission_remote_status(Path(entry.store_path))
            if live["state"] == "configured":
                status = "[green]remote✓[/green]"
                remote_disp = live.get("origin") or entry.remote or "—"
            elif live["state"] == "local_only":
                status = "[yellow]local only[/yellow]"
                remote_disp = entry.remote or "—"
            else:
                status = "[red]no git[/red]"
                remote_disp = entry.remote or "—"
            table.add_row(
                entry.moniker,
                remote_disp,
                status,
                entry.store_path,
                ", ".join(entry.host_paths) if entry.host_paths else "—",
            )
        console.print(table)
        return

    if sub == "inspect":
        host = _store_host_from_args(console, args)
        report = inspect_host(host)
        if getattr(args, "json", False):
            console.print_json(data=report)
            return
        console.print(f"[bold]Host[/bold]:          {report['host_path']}")
        console.print(f"[bold]Moniker[/bold]:       {report['moniker']}")
        console.print(f"[bold]Host origin[/bold]:   {report['origin'] or '—'}")
        if report.get("subject_origin_recorded"):
            console.print(
                f"[bold]Recorded origin[/bold]: {report['subject_origin_recorded']}"
            )
        remotes = report.get("store_remotes") or {}
        if remotes:
            for rname, rurl in remotes.items():
                console.print(f"[bold]Store remote[/bold]:  {rname} → {rurl}")
        else:
            console.print(
                "[bold]Store remote[/bold]:  — "
                "[dim](none; host_tree migrate does not invent remotes)[/dim]"
            )
        console.print(f"[bold]Data root[/bold]:     {report['data_root']}")
        console.print(
            f"[bold]Canonical store[/bold]: {report['canonical_store_path']} "
            f"({'exists' if report['canonical_store_exists'] else 'missing'})"
        )
        console.print(
            f"[bold]Migrated[/bold]:      {'yes' if report['migrated'] else 'no'}"
        )
        console.print(
            f"[bold]Legacy store[/bold]:  {report['legacy_store_path'] or '—'}"
        )
        if report["legacy_kind"]:
            console.print(
                f"[bold]Legacy kind[/bold]:   {report['legacy_kind']}"
                + (
                    f" (remote: {report['legacy_remote']})"
                    if report["legacy_remote"]
                    else ""
                )
            )
        if report["registry_entry"]:
            console.print(f"[bold]Registry[/bold]:      {report['registry_entry']}")
        else:
            console.print("[bold]Registry[/bold]:      (not registered)")
        if report.get("migrated"):
            if report.get("pointers_ok"):
                console.print(
                    "[bold]Pointers[/bold]:      ok (no host .task-agent/tasks eject)"
                )
            else:
                eject = Path(report["host_path"]) / ".task-agent" / "tasks"
                console.print(
                    f"[bold]Pointers[/bold]:      leftover eject at {eject} "
                    "(remove it, or re-run [bold]ta store migrate[/bold])"
                )
        console.print("\n[dim]Read-only inspect; no files were modified.[/dim]")
        return

    if sub == "rebuild-index":
        reg = MachineRegistry()
        rebuilt = reg.rebuild_from_stores()
        console.print(
            f"[green]Rebuilt registry from {reg.stores_dir} "
            f"({len(rebuilt)} store(s)).[/green]"
        )
        console.print(f"[dim]Wrote {reg.registry_path}[/dim]")
        return

    if sub == "remote":
        from taskagent.store_registry import (
            attach_store_remote,
            create_and_attach_store_remote,
            inspect_host,
            set_store_remote,
            suggest_store_remotes,
            _list_git_remotes,
        )

        rcmd = getattr(args, "remote_command", None)
        host = _store_host_from_args(console, args)
        report = inspect_host(host)
        store_path = Path(
            report["canonical_store_path"]
            if report.get("canonical_store_exists")
            else (report.get("legacy_store_path") or report["canonical_store_path"])
        )

        if rcmd == "show":
            if not store_path.is_dir():
                console.print(f"[red]No store at {store_path}[/red]")
                raise SystemExit(1)
            remotes = _list_git_remotes(store_path)
            if not remotes:
                console.print(f"[dim]No remotes configured on {store_path}[/dim]")
            else:
                for rname, rurl in remotes.items():
                    console.print(f"[cyan]{rname}[/cyan]\t{rurl}")
            from taskagent.store_registry import (
                format_remote_status_line,
                mission_remote_status,
            )

            console.print(format_remote_status_line(mission_remote_status(store_path)))
            return

        if rcmd == "suggest":
            suggestions = suggest_store_remotes(
                host, moniker=report.get("moniker"), origin_url=report.get("origin")
            )
            if not suggestions:
                console.print(
                    "[dim]No provider suggestions "
                    f"(origin={report.get('origin') or '—'}). "
                    "Pass a URL to [bold]ta store remote attach <url>[/bold].[/dim]"
                )
                return
            table = Table(title="Suggested task-store remotes", box=None)
            table.add_column("Provider", style="cyan")
            table.add_column("Label")
            table.add_column("URL")
            table.add_column("Notes", style="dim")
            for s in suggestions:
                table.add_row(s.provider, s.label, s.url, s.notes)
            console.print(table)
            console.print(
                "\n[dim]Create + attach: [bold]ta store remote create[/bold]\n"
                "Configure URL only: [bold]ta store remote set <url>[/bold]\n"
                "Connect + publish: [bold]ta store remote attach <url>[/bold][/dim]"
            )
            return

        if rcmd == "create":
            # Visibility: default match subject; --private / --public override
            private: Optional[bool] = None
            if getattr(args, "private", False):
                private = True
            if getattr(args, "public", False):
                private = False
            try:
                info = create_and_attach_store_remote(
                    host,
                    private=private,
                    name=getattr(args, "name", None),
                    provider_name=getattr(args, "provider", None),
                    attach=not bool(getattr(args, "no_attach", False)),
                    dry_run=bool(getattr(args, "dry_run", False)),
                )
            except Exception as exc:
                console.print(f"[red]remote create failed:[/red] {exc}")
                raise SystemExit(1)

            if info.get("dry_run"):
                console.print("[cyan]Dry-run — no create/attach[/cyan]")
            console.print(f"[bold]Provider[/bold]:   {info.get('provider')}")
            console.print(f"[bold]Moniker[/bold]:    {info.get('moniker')}")
            console.print(f"[bold]Subject[/bold]:    {info.get('subject_origin')}")
            vis = "private" if info.get("private") else "public"
            console.print(
                f"[bold]Visibility[/bold]: {vis} "
                f"[dim]({info.get('visibility_source')})[/dim]"
            )
            if info.get("full_name"):
                console.print(f"[bold]Tasks repo[/bold]: {info.get('full_name')}")
            if info.get("url"):
                console.print(f"[bold]URL[/bold]:        {info.get('url')}")
            if info.get("planned_url"):
                console.print(f"[bold]Planned URL[/bold]: {info.get('planned_url')}")
            if info.get("notes"):
                console.print(f"[dim]{info['notes']}[/dim]")
            if info.get("created") is True:
                console.print("[green]Created new empty tasks repository.[/green]")
            elif info.get("created") is False:
                console.print("[yellow]Tasks repository already existed.[/yellow]")
            if info.get("attach_result"):
                ar = info["attach_result"]
                console.print(
                    f"[bold]Attach[/bold]:     mode={ar.get('mode')} ok={ar.get('ok')}"
                )
                for w in ar.get("warnings") or []:
                    console.print(f"[yellow]Warning:[/yellow] {w}")
            if info.get("ok") and not info.get("dry_run"):
                console.print(
                    f"\n[bold green]Done.[/bold green] Store: {info.get('store_path')}"
                )
            return

        if rcmd == "set":
            raw_url = getattr(args, "url", None)
            if not raw_url or not isinstance(raw_url, str):
                console.print("[red]Usage: ta store remote set <url>[/red]")
                raise SystemExit(1)
            if not store_path.is_dir():
                console.print(
                    f"[red]Store does not exist yet: {store_path}[/red]\n"
                    "[dim]Run [bold]ta store migrate[/bold] first.[/dim]"
                )
                raise SystemExit(1)
            remote_name = getattr(args, "name", None) or "origin"
            if not isinstance(remote_name, str):
                remote_name = "origin"
            try:
                info = set_store_remote(
                    store_path,
                    raw_url,
                    remote_name=remote_name,
                    moniker=report.get("moniker"),
                )
            except Exception as e:
                console.print(f"[red]Failed to set remote:[/red] {e}")
                raise SystemExit(1)
            console.print(
                f"[green]Remote {info['remote_name']} {info['action']}:[/green] {info['url']}"
            )
            console.print(f"[dim]Store: {info['store_path']}[/dim]")
            console.print(
                "[yellow]URL only — not published.[/yellow] "
                "To fetch/publish (incl. unrelated history recovery): "
                f"[bold]ta store remote attach {raw_url}[/bold]"
            )
            return

        if rcmd == "attach":
            raw_url = getattr(args, "url", None)
            if not raw_url or not isinstance(raw_url, str):
                console.print(
                    "[red]Usage: ta store remote attach <url> [--dry-run][/red]"
                )
                raise SystemExit(1)
            if not store_path.is_dir():
                console.print(
                    f"[red]Store does not exist yet: {store_path}[/red]\n"
                    "[dim]Run [bold]ta store migrate[/bold] first.[/dim]"
                )
                raise SystemExit(1)
            remote_name = getattr(args, "name", None) or "origin"
            if not isinstance(remote_name, str):
                remote_name = "origin"
            dry = bool(getattr(args, "dry_run", False))
            try:
                info = attach_store_remote(
                    store_path,
                    raw_url,
                    remote_name=remote_name,
                    moniker=report.get("moniker"),
                    dry_run=dry,
                )
            except Exception as e:
                console.print(f"[red]Attach failed:[/red] {e}")
                raise SystemExit(1)

            console.print(f"[bold]Mode[/bold]:   {info.get('mode')}")
            console.print(f"[bold]Store[/bold]:  {info.get('store_path')}")
            console.print(f"[bold]URL[/bold]:    {info.get('url')}")
            console.print(
                f"[bold]Branch[/bold]: {info.get('local_branch')} → "
                f"{info.get('default_branch')}"
            )
            mismatched = info.get("mismatched") or []
            if mismatched:
                console.print(
                    "\n[bold yellow]Mismatched remote branches "
                    "(kept for comparison — not deleted):[/bold yellow]"
                )
                for m in mismatched:
                    console.print(
                        f"  • was [cyan]origin/{m['original']}[/cyan] "
                        f"({m['sha'][:8]}) → [cyan]{m['renamed_to']}[/cyan]"
                    )
                console.print(
                    "[dim]Compare in git, e.g. "
                    f"[bold]git fetch origin && git log "
                    f"{info.get('default_branch', 'main')}.."
                    f"{mismatched[0]['renamed_to']}[/bold], "
                    "or browse branches in the host web UI.[/dim]"
                )
            console.print("\n[bold]Steps:[/bold]")
            for s in info.get("steps") or []:
                console.print(f"  • {s}")
            for w in info.get("warnings") or []:
                console.print(f"[yellow]Warning:[/yellow] {w}")
            if info.get("ok") and not dry:
                console.print(
                    "\n[bold green]Attach complete.[/bold green] "
                    "Task store remote is ready for push."
                )
            elif dry:
                console.print("\n[cyan]Dry-run only — no publish.[/cyan]")
            return

        console.print(
            "[yellow]Usage: ta store remote {show|suggest|create|set|attach}[/yellow]"
        )
        return

    if sub == "keet":
        from taskagent.store_registry import (
            STORE_META_REL,
            MachineRegistry,
            inspect_host,
            read_store_meta,
            write_store_meta,
        )

        host = _store_host_from_args(console, args)
        report = inspect_host(host)
        store_path = Path(report["canonical_store_path"])
        moniker = report["moniker"]
        action = getattr(args, "action", "show") or "show"
        uri_arg = getattr(args, "uri", None)

        registry = MachineRegistry()
        entry = registry.get(moniker)

        if action == "show":
            meta = read_store_meta(store_path) or {}
            keet_uri = meta.get("keet_room_uri") or (
                entry.keet_room_uri if entry else None
            )
            console.print(f"[bold]Store Moniker[/bold]: {moniker}")
            console.print(f"[bold]Store Path[/bold]:    {store_path}")
            if keet_uri:
                console.print(f"[bold]Keet Room URI[/bold]: [green]{keet_uri}[/green]")
            else:
                console.print(
                    "[bold]Keet Room URI[/bold]: [yellow]none (not configured)[/yellow]"
                )
                console.print(
                    "[dim]Set with: ta store keet set <keet://chat/...>[/dim]"
                )
            return

        if action == "set":
            if not uri_arg:
                console.print(
                    "[red]Error: Keet room URI is required for 'ta store keet set <uri>'.[/red]"
                )
                raise SystemExit(1)
            write_store_meta(
                store_path, moniker=moniker, extra={"keet_room_uri": uri_arg}
            )
            if entry:
                entry.keet_room_uri = uri_arg
                registry.upsert(entry)
            console.print(
                f"[green]✓ Configured secret Keet room URI for store [bold]{moniker}[/bold].[/green]"
            )
            console.print(f"[dim]Saved to {store_path / STORE_META_REL}[/dim]")
            return

        if action == "unset":
            meta = read_store_meta(store_path) or {}
            meta.pop("keet_room_uri", None)
            write_store_meta(store_path, moniker=moniker, extra=meta)
            if entry:
                entry.keet_room_uri = None
                registry.upsert(entry)
            console.print(
                f"[yellow]Cleared Keet room URI for store [bold]{moniker}[/bold].[/yellow]"
            )
            return

    if sub == "matrix":
        from taskagent.store_registry import (
            STORE_META_REL,
            MachineRegistry,
            get_global_matrix_space,
            inspect_host,
            read_store_meta,
            set_global_matrix_space,
            write_store_meta,
        )

        host = _store_host_from_args(console, args)
        report = inspect_host(host)
        store_path = Path(report["canonical_store_path"])
        moniker = report["moniker"]
        action = getattr(args, "action", "show") or "show"
        room_arg = getattr(args, "room_id", None)

        registry = MachineRegistry()
        entry = registry.get(moniker)

        if action == "space":
            space_cmd = getattr(args, "room_id", None) or "show"
            space_arg = getattr(args, "extra_arg", None)
            if space_cmd in ("set", "set-space"):
                val = space_arg or getattr(args, "room_id", None)
                if not val or val == "set":
                    console.print(
                        "[red]Error: Matrix Space link or ID is required: ta store matrix space set '<link>'[/red]"
                    )
                    raise SystemExit(1)
                p = set_global_matrix_space(val)
                console.print(
                    "[green]✓ Configured global machine Matrix Space link.[/green]"
                )
                console.print(f"[dim]Saved to {p}[/dim]")
                return
            if space_cmd == "unset":
                set_global_matrix_space(None)
                console.print(
                    "[yellow]Cleared global machine Matrix Space link.[/yellow]"
                )
                return
            # show
            g_space = get_global_matrix_space()
            if g_space:
                console.print(
                    f"[bold]Global Matrix Space Link[/bold]: [green]{g_space}[/green]"
                )
            else:
                console.print(
                    "[bold]Global Matrix Space Link[/bold]: [yellow]none (not configured)[/yellow]"
                )
                console.print(
                    "[dim]Set with: ta store matrix space set '<https://matrix.to/#/!space_id...>'[/dim]"
                )
        if action == "token":
            from taskagent.store_registry import (
                get_global_matrix_token,
                set_global_matrix_token,
            )

            token_cmd = getattr(args, "room_id", None) or "show"
            token_val = getattr(args, "extra_arg", None)
            if token_cmd in ("set", "set-token"):
                val = token_val or getattr(args, "room_id", None)
                if not val or val == "set":
                    console.print(
                        "[red]Error: Token reference is required: ta store matrix token set 'op://Private/Matrix/access-token'[/red]"
                    )
                    raise SystemExit(1)
                p = set_global_matrix_token(val)
                console.print(
                    "[green]✓ Configured global Matrix token reference.[/green]"
                )
                console.print(f"[dim]Saved to {p} (mode 0600)[/dim]")
                return
            if token_cmd == "unset":
                set_global_matrix_token(None)
                console.print("[yellow]Cleared global Matrix token reference.[/yellow]")
                return
            g_token = get_global_matrix_token()
            if g_token:
                disp = (
                    g_token
                    if g_token.startswith("op://")
                    else (g_token[:8] + "..." + g_token[-4:])
                )
                console.print(
                    f"[bold]Global Matrix Token Reference[/bold]: [green]{disp}[/green]"
                )
            else:
                console.print(
                    "[bold]Global Matrix Token Reference[/bold]: [yellow]none (not configured)[/yellow]"
                )
                console.print(
                    "[dim]Set with: ta store matrix token set 'op://Private/Matrix/access-token'[/dim]"
                )
            return

        if action == "show":
            meta = read_store_meta(store_path) or {}
            matrix_id = meta.get("matrix_room_id") or (
                entry.matrix_room_id if entry else None
            )
            global_space = get_global_matrix_space()
            console.print(f"[bold]Store Moniker[/bold]: {moniker}")
            console.print(f"[bold]Store Path[/bold]:    {store_path}")
            if matrix_id:
                console.print(
                    f"[bold]Matrix Room ID[/bold]: [green]{matrix_id}[/green]"
                )
            elif global_space:
                console.print(
                    f"[bold]Matrix Room ID[/bold]: [cyan]{global_space}[/cyan] [dim](global machine default Space)[/dim]"
                )
            else:
                console.print(
                    "[bold]Matrix Room ID[/bold]: [yellow]none (not configured)[/yellow]"
                )
                console.print(
                    "[dim]Set with: ta store matrix set '<room_or_space_link>'[/dim]"
                )
            return

        if action == "set":
            if not room_arg:
                console.print(
                    "[red]Error: Matrix Room or Space link is required for 'ta store matrix set <link>'.[/red]"
                )
                raise SystemExit(1)
            # If a space link is passed to ta store matrix set, auto-configure global space
            if "matrix.to/#/!" in room_arg or "space" in room_arg.lower():
                set_global_matrix_space(room_arg)
                console.print(
                    "[green]✓ Configured global machine Matrix Space link.[/green]"
                )
            write_store_meta(
                store_path, moniker=moniker, extra={"matrix_room_id": room_arg}
            )
            if entry:
                entry.matrix_room_id = room_arg
                registry.upsert(entry)
            console.print(
                f"[green]✓ Configured Matrix Room/Space ID for store [bold]{moniker}[/bold].[/green]"
            )
            console.print(f"[dim]Saved to {store_path / STORE_META_REL}[/dim]")
            return

        if action == "unset":
            meta = read_store_meta(store_path) or {}
            meta.pop("matrix_room_id", None)
            write_store_meta(store_path, moniker=moniker, extra=meta)
            if entry:
                entry.matrix_room_id = None
                registry.upsert(entry)
            console.print(
                f"[yellow]Cleared Matrix Room ID for store [bold]{moniker}[/bold].[/yellow]"
            )
            return

    if sub == "rebind":
        from taskagent.store_registry import rebind_store_moniker

        host = _store_host_from_args(console, args)
        new_moniker = getattr(args, "moniker", None)
        try:
            info = rebind_store_moniker(host, new_moniker=new_moniker)
        except Exception as e:
            console.print(f"[red]Rebind failed:[/red] {e}")
            raise SystemExit(1)
        console.print(
            f"[green]Rebound[/green] [cyan]{info['old_moniker']}[/cyan] → "
            f"[cyan]{info['new_moniker']}[/cyan]"
        )
        console.print(f"[dim]Store: {info['store_path']} (moved={info['moved']})[/dim]")
        console.print(f"[dim]Host pointer: {info['host_config']}[/dim]")
        if info.get("subject_origin"):
            console.print(f"[dim]Subject origin: {info['subject_origin']}[/dim]")
        return

    if sub == "yazi":
        cmd_yazi(console, None, args)
        return

    if sub == "migrate":
        refuse_if_native_windows_store_ops(console, "ta store migrate")
        host = _store_host_from_args(console, args)
        dry_run = bool(getattr(args, "dry_run", False))
        mig = migrate_store(host, dry_run=dry_run)
        plan = mig.plan
        if getattr(args, "json", False):
            console.print_json(data=mig.to_dict())
            if not mig.success:
                raise SystemExit(1)
            return

        console.print(f"[bold]Host[/bold]:     {plan.host_path}")
        console.print(f"[bold]Moniker[/bold]:  {plan.moniker}")
        console.print(f"[bold]Kind[/bold]:     {plan.kind or '—'}")
        console.print(f"[bold]Source[/bold]:   {plan.source or '—'}")
        console.print(f"[bold]Dest[/bold]:     {plan.destination}")
        if plan.subject_origin:
            console.print(f"[bold]Subject origin[/bold]: {plan.subject_origin}")
        if plan.remotes_before:
            console.print(
                f"[bold]Store remotes (preserve)[/bold]: {plan.remotes_before}"
            )
        if plan.warnings:
            for w in plan.warnings:
                console.print(f"[yellow]Warning:[/yellow] {w}")
        if plan.errors:
            for err in plan.errors:
                console.print(f"[red]Error:[/red] {err}")
        console.print("\n[bold]Steps:[/bold]")
        for s in plan.steps:
            console.print(f"  • {s}")
        if mig.applied_steps and not dry_run:
            console.print("\n[bold]Applied:[/bold]")
            for s in mig.applied_steps:
                console.print(f"  ✓ {s}")

        if mig.success:
            style = "green" if not dry_run else "cyan"
            label = "Dry-run OK" if dry_run else "Success"
            console.print(f"\n[{style}]{label}:[/{style}] {mig.message}")
        else:
            console.print(f"\n[red]Failed:[/red] {mig.message}")
            raise SystemExit(1)
        return

    console.print(f"[red]Unknown store command: {sub}[/red]")


def cmd_yazi(console: Console, manager: Any, args: Any) -> None:
    """Open Yazi terminal file manager in the active store directory."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    # Determine store directory
    target_dir: Optional[Path] = None
    if manager and hasattr(manager, "issues_root") and manager.issues_root:
        target_dir = Path(manager.issues_root)
    else:
        from taskagent.store_registry import inspect_host

        host = getattr(args, "path", None) or Path.cwd()
        try:
            report = inspect_host(host)
            canonical = Path(report["canonical_store_path"])
            entry = report.get("registry_entry") or {}
            entry_path = Path(entry["store_path"]) if entry.get("store_path") else None
            target_dir = (
                entry_path if (entry_path and entry_path.exists()) else canonical
            )
        except Exception:
            target_dir = Path.cwd()

    if target_dir and not target_dir.exists():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    yazi_bin = shutil.which("yazi")

    if not yazi_bin:
        console.print(
            "[bold yellow]Yazi file manager ('yazi') is not installed on PATH.[/bold yellow]"
        )
        console.print(
            "[dim]Yazi is an ultra-fast terminal file manager for browsing task stores.[/dim]\n"
        )

        installed_ok = False
        can_install = (
            shutil.which("uv") is not None or shutil.which("cargo") is not None
        )

        if can_install and sys.stdin.isatty():
            try:
                response = (
                    input("Would you like to install yazi now? [Y/n] ").strip().lower()
                )
            except (EOFError, KeyboardInterrupt):
                response = "n"

            if response in ("", "y", "yes"):
                console.print("[cyan]Installing yazi...[/cyan]")
                if shutil.which("uv"):
                    install_cmd = ["uv", "tool", "install", "yazi-cli"]
                else:
                    install_cmd = [
                        "cargo",
                        "install",
                        "--locked",
                        "yazi-cli",
                        "yazi-fm",
                    ]

                try:
                    res = subprocess.run(install_cmd)
                    if res.returncode == 0 and shutil.which("yazi"):
                        yazi_bin = shutil.which("yazi")
                        installed_ok = True
                        console.print(
                            "[bold green]✓ Yazi installed successfully![/bold green]\n"
                        )
                    else:
                        console.print(
                            "[red]Installation did not complete cleanly or yazi binary was not found on PATH.[/red]\n"
                        )
                except Exception as e:
                    console.print(f"[red]Installation error:[/red] {e}\n")

        if not installed_ok:
            console.print(
                "[bold cyan]To install Yazi manually, run one of the following:[/bold cyan]"
            )
            console.print("  • [yellow]uv tool install yazi-cli[/yellow]")
            console.print(
                "  • [yellow]cargo install --locked yazi-cli yazi-fm[/yellow]"
            )
            console.print("  • [yellow]brew install yazi[/yellow] (macOS)")
            console.print(
                "  • [yellow]sudo apt install yazi[/yellow] (Linux package manager)\n"
            )
            console.print(f"[bold]Store Directory:[/bold] {target_dir}")
            return

    if not yazi_bin:
        return

    console.print(f"[dim]Opening Yazi in: {target_dir}[/dim]")
    try:
        subprocess.run([yazi_bin, str(target_dir)])
    except Exception as e:
        console.print(f"[red]Failed to launch yazi:[/red] {e}")


def cmd_mr_list(console: Console, manager: TaskAgent):
    """List pending merge requests from workers."""
    mr_dir = manager.issues_root / "mr"
    if not mr_dir.exists():
        console.print("[yellow]Merge request directory not found.[/yellow]")
        return

    mrs = list(mr_dir.glob("*.md")) + list(mr_dir.glob("*.json"))
    if not mrs:
        console.print("[blue]No pending merge requests.[/blue]")
        return

    table = Table(
        title="Pending Merge Requests",
        box=theme.table_box,
        header_style=theme.header_style,
        padding=theme.table_padding,
    )
    table.add_column("Slug", style="cyan")
    table.add_column("File", style="dim")

    for mr in mrs:
        table.add_row(mr.stem, str(mr.name))

    console.print(table)


def cmd_merge(
    console: Console,
    manager: TaskAgent,
    slug_part: str,
    message: Optional[str] = None,
    push: bool = False,
):
    """Finalize a task using a merge request datagram."""
    mr_dir = manager.issues_root / "mr"
    # Find the MR file
    matches = list(mr_dir.glob(f"{slug_part}*"))
    if not matches:
        console.print(f"[red]No merge request found for '{slug_part}'.[/red]")
        return

    if len(matches) > 1:
        console.print(f"[yellow]Multiple MRs match '{slug_part}':[/yellow]")
        for m in matches:
            console.print(f"  - {m.name}")
        return

    mr_file = matches[0]
    slug = mr_file.stem
    solution = mr_file.read_text(encoding="utf-8")

    console.print(f"[blue]Merging task [bold]{slug}[/bold]...[/blue]")

    try:
        _, code_hash = manager.complete_issue(
            slug,
            commit_message=message,
            should_commit=True,
            push_mission=push,
            solution_explanation=solution,
        )
        # Remove the MR file after successful merge
        mr_file.unlink()
        console.print(f"[bold green]Successfully merged '{slug}'.[/bold green]")
        if code_hash not in ["unknown", "failed"]:
            console.print(f"[dim]Committed as {code_hash}.[/dim]")
    except Exception as e:
        console.print(f"[red]Merge failed: {e}[/red]")


def cmd_new(
    console: Console,
    manager: TaskAgent,
    title: Optional[str],
    body: str,
    draft: bool,
    as_dir: bool = True,
    completion_criteria: Optional[str] = None,
    interactive: bool = False,
    blocked_by: Optional[str] = None,
    subtask_of: Optional[str] = None,
    bulk: Optional[str] = None,
    repo: Optional[str] = None,
):
    """Create a new issue.

    When ``repo`` is set, resolve a registered store by moniker/host fuzzy match
    and create the task there without touching the current project's mission.
    """
    if repo:
        from taskagent.store_registry import (
            AmbiguousRepoMatchError,
            RepoNotFoundError,
            manager_for_repo_query,
        )

        try:
            manager, resolved = manager_for_repo_query(repo)
        except AmbiguousRepoMatchError as e:
            console.print(f"[red]Ambiguous --repo {repo!r}:[/red]")
            for c in e.candidates:
                console.print(
                    f"  [cyan]{c.moniker}[/cyan]  {c.store_path}  [dim]({c.reason})[/dim]"
                )
            sys.exit(1)
        except RepoNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            console.print(
                "[dim]Register/migrate a project first: "
                "[bold]ta store migrate[/bold] / [bold]ta store list[/bold].[/dim]"
            )
            sys.exit(1)
        console.print(
            f"[blue]Target store:[/blue] [cyan]{resolved.moniker}[/cyan] "
            f"[dim]{resolved.store_path}[/dim] ({resolved.reason})"
        )

    if bulk:
        try:
            if bulk == "-":
                raw_data = sys.stdin.read()
            else:
                raw_data = Path(bulk).read_text(encoding="utf-8")

            tasks = json.loads(raw_data)
            if not isinstance(tasks, list):
                raise ValueError("Bulk JSON must be a list/array of task objects.")

            for idx, t in enumerate(tasks):
                t_title = t.get("title")
                t_criteria = t.get("completion_criteria")
                if not t_title:
                    console.print(
                        f"[red]Error at task {idx}: 'title' is required.[/red]"
                    )
                    continue
                if not t_criteria:
                    console.print(
                        f"[red]Error at task '{t_title}' (index {idx}): 'completion_criteria' is required.[/red]"
                    )
                    continue

                t_body = t.get("body", "")
                t_draft = t.get("draft", draft)
                t_blocked_by = t.get("blocked_by")
                t_subtask_of = t.get("subtask_of")
                t_as_dir = t.get("as_dir", as_dir)

                issue = manager.create_issue(
                    title=t_title,
                    body=t_body,
                    draft=t_draft,
                    as_dir=t_as_dir,
                    completion_criteria=t_criteria,
                    blocked_by=t_blocked_by,
                    subtask_of=t_subtask_of,
                )
                console.print(
                    f"[bold green]Created new issue: {issue.slug}[/bold green]"
                )
                issue_file = manager.find_issue_file(issue.slug)
                console.print(f"File: {issue_file}")
                if issue.subtask_of:
                    console.print(f"Subtask of: {issue.subtask_of}")
            return
        except Exception as e:
            console.print(f"[red]Error processing bulk tasks: {e}[/red]")
            sys.exit(1)

    if interactive:
        editor = get_editor()
        slug = manager.slugify(title or "new-task")
        temp_dir = manager.issues_root / "draft" / slug
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / "README.md"

        # Resolve relations for the template
        final_blocked_by = blocked_by or ""
        final_subtask_of = subtask_of or ""

        created_at = datetime.now().astimezone().isoformat()
        template = f"""---
created_at: {created_at}
---

# {title or "New Task"}

**Subtask of:** {final_subtask_of}
**Blocked by:** {final_blocked_by}

## Description



## Completion Criteria

{completion_criteria or ""}
"""
        temp_file.write_text(template, encoding="utf-8")

        subprocess.run([editor, str(temp_file)], check=True)

        manager.init_project()
        issues = manager.load_mission()
        new_issue = next((i for i in issues if i.slug == slug), None)
        if new_issue:
            console.print(
                f"[bold green]Created new issue: {new_issue.slug}[/bold green]"
            )
            console.print(f"File: {temp_file}")
        else:
            console.print("[yellow]Task not created.[/yellow]")
            shutil.rmtree(temp_dir)
        return

    if not title:
        console.print("[red]Error: title is required for non-interactive mode.[/red]")
        sys.exit(1)

    try:
        issue = manager.create_issue(
            title=title,
            body=body,
            draft=draft,
            as_dir=as_dir,
            completion_criteria=completion_criteria,
            blocked_by=blocked_by,
            subtask_of=subtask_of,
        )
        console.print(f"[bold green]Created new issue: {issue.slug}[/bold green]")
        issue_file = manager.find_issue_file(issue.slug)
        console.print(f"File: {issue_file}")
        if issue.subtask_of:
            console.print(f"Subtask of: {issue.subtask_of}")
        if issue.blocked_by:
            console.print(f"Blocked by: {', '.join(issue.blocked_by)}")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_tree(console: Console, manager: TaskAgent):
    """Display the task hierarchy as a dependency tree."""
    issues = manager.sync_mission()
    if not issues:
        console.print("[yellow]No tasks found.[/yellow]")
        return

    slug_to_issue = {i.slug: i for i in issues}
    completed_slugs = {i.slug for i in issues if i.status == "completed"}
    # Also include completed tasks not in mission.usv
    completed_slugs.update(slug for _, slug in manager.walk_completed())

    # Build children_map using subtask_of (hierarchy)
    children_map: Dict[str, List[str]] = {}
    for i in issues:
        if i.subtask_of and i.subtask_of in slug_to_issue:
            children_map.setdefault(i.subtask_of, []).append(i.slug)

    visited: Set[str] = set()
    tree_lines: List[Tuple[Issue, int]] = []

    def build_rows(issue: Issue, depth: int):
        if issue.slug in visited:
            return
        visited.add(issue.slug)
        tree_lines.append((issue, depth))
        if issue.slug in children_map:
            for child_slug in children_map[issue.slug]:
                if child_slug in slug_to_issue:
                    build_rows(slug_to_issue[child_slug], depth + 1)

    # Root nodes: tasks with no subtask_of parent in the issue set
    for issue in issues:
        if not issue.subtask_of or issue.subtask_of not in slug_to_issue:
            build_rows(issue, 0)

    # Catch any remaining unvisited (shouldn't happen, but defensive)
    for issue in issues:
        if issue.slug not in visited:
            build_rows(issue, 0)

    for issue, depth in tree_lines:
        indent = "  " * depth
        connector = "└─ " if depth > 0 else ""
        status_symbol = {
            "active": "●",
            "pending": "○",
            "draft": "◌",
            "completed": "✔",
        }.get(issue.status, "?")
        active_blockers = [b for b in issue.blocked_by if b not in completed_slugs]
        deps = (
            f"  [dim](blocked by: {', '.join(active_blockers)})[/dim]"
            if active_blockers
            else ""
        )
        console.print(f"{indent}{connector}{status_symbol} {issue.slug}{deps}")


def cmd_list(
    console: Console,
    manager: TaskAgent,
    output_format: str = "table",
    repo: Optional[str] = None,
):
    """List all issues in mission.usv.

    When ``repo`` is set, list the fuzzy-matched registered store instead.
    """
    if repo:
        from taskagent.store_registry import (
            AmbiguousRepoMatchError,
            RepoNotFoundError,
            manager_for_repo_query,
        )

        try:
            manager, resolved = manager_for_repo_query(repo)
        except AmbiguousRepoMatchError as e:
            console.print(f"[red]Ambiguous --repo {repo!r}:[/red]")
            for c in e.candidates:
                console.print(
                    f"  [cyan]{c.moniker}[/cyan]  {c.store_path}  [dim]({c.reason})[/dim]"
                )
            sys.exit(1)
        except RepoNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)
        if output_format != "json":
            console.print(
                f"[blue]Listing store:[/blue] [cyan]{resolved.moniker}[/cyan] "
                f"[dim]{resolved.store_path}[/dim]"
            )

    if output_format == "table":
        show_store_remote_status(console, manager)
        maybe_show_strategy(console, manager)
    elif output_format == "text":
        show_store_remote_status(console, manager)
    issues = manager.sync_mission()
    if not issues:
        if output_format == "json":
            print("[]")
        else:
            console.print(f"[yellow]No issues found in {manager.mission_path}[/yellow]")
        return

    # Build hierarchy for display — nest by subtask_of only, not blocked_by
    slug_to_issue = {i.slug: i for i in issues}
    completed_slugs = {i.slug for i in issues if i.status == "completed"}
    completed_slugs.update(slug for _, slug in manager.walk_completed())
    children_map: Dict[str, List[str]] = {}
    for i in issues:
        if i.subtask_of and i.subtask_of in slug_to_issue:
            if i.subtask_of not in children_map:
                children_map[i.subtask_of] = []
            children_map[i.subtask_of].append(i.slug)

    visited: Set[str] = set()
    rows_to_display: List[Tuple[Issue, int]] = []

    def build_rows(issue: Issue, depth: int):
        if issue.slug in visited:
            return
        visited.add(issue.slug)
        rows_to_display.append((issue, depth))
        if issue.slug in children_map:
            child_issues = [slug_to_issue[s] for s in children_map[issue.slug]]
            for child in child_issues:
                build_rows(child, depth + 1)

    # Root nodes: tasks with no subtask_of parent in the issue set
    for issue in issues:
        if not issue.subtask_of or issue.subtask_of not in slug_to_issue:
            build_rows(issue, 0)

    for issue in issues:
        if issue.slug not in visited:
            build_rows(issue, 0)

    if output_format == "json":
        data = []
        for i, depth in rows_to_display:
            issue_file = manager.find_issue_file(i.slug)
            location = str(issue_file) if issue_file else None
            data.append(
                {
                    "priority": i.priority,
                    "created": get_created_date(manager, i.slug),
                    "status": i.status,
                    "name": i.name,
                    "slug": i.slug,
                    "dependencies": i.dependencies,
                    "location": location,
                    "depth": depth,
                }
            )
        print(json.dumps(data, indent=2))
        return

    if output_format == "text":
        for i, depth in rows_to_display:
            issue_file = manager.find_issue_file(i.slug)
            location = str(issue_file) if issue_file else "MISSING"
            deps = ",".join(i.dependencies)
            indent = "  " * depth
            prefix = "└─ " if depth > 0 else ""
            created_date = get_created_date(manager, i.slug)
            console.print(
                f"{i.priority:<3} {created_date:<16} {i.status:<8} {i.name:<30} {indent}{prefix}{i.slug:<30} {deps:<20} {location}"
            )
        return

    table = Table(
        title="Task Queue",
        box=theme.table_box,
        header_style=theme.header_style,
        padding=theme.table_padding,
    )
    table.add_column("Pri", justify="right", style="cyan", width=3)
    table.add_column("Date", style="dim", width=5)
    table.add_column("Status", width=6)
    table.add_column("Blocked", style="yellow", width=8)
    table.add_column("Slug")

    term_width = getattr(getattr(console, "size", None), "width", 80)
    if not isinstance(term_width, int) or term_width <= 0:
        term_width = 80

    for issue, depth in rows_to_display:
        status_style = "white"
        if issue.status == "active":
            status_style = "bold green"
        elif issue.status == "pending":
            status_style = "bold yellow"
        elif issue.status == "draft":
            status_style = "dim"
        elif issue.status == "completed":
            status_style = "bold blue"

        indent = "  " * (depth - 1) if depth > 1 else ""
        prefix_plain = "└─ " if depth > 0 else ""
        prefix = "[dim cyan]└─[/dim cyan] " if depth > 0 else ""

        # Fixed columns width: Pri (~4) + Date (5) + Status (6) + Blocked (8) + borders/padding (~12)
        available_width = max(10, term_width - 35)
        prefix_len = len(indent) + len(prefix_plain)
        max_slug_len = max(5, available_width - prefix_len)

        slug_str = issue.slug
        if len(slug_str) > max_slug_len:
            slug_str = slug_str[: max_slug_len - 1] + "…"

        display_slug = f"{indent}{prefix}{slug_str}"
        created_date = get_created_date(manager, issue.slug)
        # Shorten to MM-DD
        if len(created_date) >= 10:
            created_date = created_date[5:10]

        active_blockers = [b for b in issue.blocked_by if b not in completed_slugs]
        blocked_str = ""
        if active_blockers:
            # Show priority numbers of blockers
            blocker_priorities = []
            for b in active_blockers:
                if b in slug_to_issue:
                    blocker_priorities.append(str(slug_to_issue[b].priority))
                else:
                    blocker_priorities.append(b[:4])
            blocked_str = " ".join(blocker_priorities)

        table.add_row(
            str(issue.priority),
            f"[dim]{created_date}[/dim]",
            f"[{status_style}]{issue.status.upper()}[/{status_style}]",
            f"[yellow]{blocked_str}[/yellow]" if blocked_str else "",
            display_slug,
        )
    with console.pager(styles=True):
        console.print(table)


def _parse_created_at(manager: TaskAgent, slug: str) -> Optional[datetime]:
    """Parse created_at from frontmatter as a datetime object."""
    try:
        issue_file = manager.find_issue_file(slug, include_completed=True)
        if issue_file and issue_file.exists():
            content = issue_file.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    for line in parts[1].splitlines():
                        if line.strip().startswith("created_at:"):
                            raw_val = line.split(":", 1)[1].strip()
                            try:
                                return datetime.fromisoformat(raw_val)
                            except ValueError:
                                for fmt in (
                                    "%Y-%m-%d %H:%M",
                                    "%Y-%m-%d %H:%M:%S",
                                    "%Y-%m-%d",
                                ):
                                    try:
                                        return datetime.strptime(raw_val, fmt)
                                    except ValueError:
                                        pass
            stat = issue_file.stat()
            birthtime = getattr(stat, "st_birthtime", None)
            t = birthtime if birthtime is not None else stat.st_mtime
            return datetime.fromtimestamp(t)
    except Exception:
        pass
    return None


def _format_age(dt: Optional[datetime]) -> str:
    """Format a timedelta as a human-readable age string."""
    if dt is None:
        return "?"
    now = datetime.now(tz=dt.tzinfo) if dt.tzinfo else datetime.now()
    delta = now - dt
    days = delta.days
    hours = delta.seconds // 3600
    if days > 365:
        y = days // 365
        return f"{y}y"
    if days > 30:
        m = days // 30
        return f"{m}mo"
    if days > 0:
        return f"{days}d"
    if hours > 0:
        return f"{hours}h"
    mins = delta.seconds // 60
    return f"{mins}m"


def cmd_dashboard(console: Console, manager: TaskAgent):
    """Show a live dashboard of all task stations."""
    show_store_remote_status(console, manager)
    issues = manager.sync_mission()

    completed_pairs = manager.walk_completed()
    completed_slugs = {slug for _, slug in completed_pairs}

    # Group by status
    stations: Dict[str, List[Issue]] = {}
    for issue in issues:
        stations.setdefault(issue.status, []).append(issue)

    # ── Station summary table ──
    station_table = Table(
        title="Stations",
        box=theme.table_box,
        header_style=theme.header_style,
        padding=theme.table_padding,
    )
    station_table.add_column("Station", style="cyan", no_wrap=True)
    station_table.add_column("Count", justify="right")
    station_table.add_column("Oldest", style="dim")

    station_order = ["active", "pending", "draft", "mr", "completed", "deleted"]
    total = 0
    for name in station_order:
        if name == "completed":
            count = len(completed_pairs)
        elif name == "deleted":
            deleted_root = manager.issues_root / "deleted"
            if deleted_root.exists():
                count = sum(1 for _ in deleted_root.iterdir())
            else:
                count = 0
        elif name == "mr":
            mr_root = manager.issues_root / "mr"
            if mr_root.exists():
                count = sum(
                    1 for f in mr_root.iterdir() if f.is_file() and f.suffix == ".md"
                )
            else:
                count = 0
        else:
            count = len(stations.get(name, []))
        total += count

        # Find oldest item age in this station
        oldest_dt: Optional[datetime] = None
        if name == "completed":
            for fpath, slug in completed_pairs:
                dt = _parse_created_at(manager, slug)
                if dt and (oldest_dt is None or dt < oldest_dt):
                    oldest_dt = dt
        elif name == "deleted":
            deleted_root = manager.issues_root / "deleted"
            if deleted_root.exists():
                for entry in deleted_root.iterdir():
                    slug = entry.stem if entry.is_file() else entry.name
                    dt = _parse_created_at(manager, slug)
                    if dt and (oldest_dt is None or dt < oldest_dt):
                        oldest_dt = dt
        elif name == "mr":
            mr_root = manager.issues_root / "mr"
            if mr_root.exists():
                for f in mr_root.iterdir():
                    if f.is_file() and f.suffix == ".md":
                        dt = _parse_created_at(manager, f.stem)
                        if dt and (oldest_dt is None or dt < oldest_dt):
                            oldest_dt = dt
        else:
            for issue in stations.get(name, []):
                dt = _parse_created_at(manager, issue.slug)
                if dt and (oldest_dt is None or dt < oldest_dt):
                    oldest_dt = dt

        style = ""
        if name == "active":
            style = "bold green"
        elif name == "pending":
            style = "bold yellow"
        elif name == "draft":
            style = "dim"

        station_table.add_row(
            f"[{style}]{name}[/{style}]" if style else name,
            str(count),
            _format_age(oldest_dt),
        )

    station_table.add_row("", "", "")
    station_table.add_row("[bold]total[/bold]", f"[bold]{total}[/bold]", "")

    console.print(
        Panel(
            station_table,
            title="[bold]Dashboard[/bold]",
            subtitle=f"[dim]{manager.issues_root}[/dim]",
            box=theme.panel_box,
            expand=False,
        )
    )

    # ── Blocked-chain view ──
    slug_to_issue = {i.slug: i for i in issues}
    blocked = [i for i in issues if i.status in ("pending", "draft") and i.blocked_by]
    if blocked:
        bt = Table(
            title="Blocked Tasks",
            box=theme.table_box,
            header_style=theme.header_style,
            padding=theme.table_padding,
        )
        bt.add_column("Task", style="cyan")
        bt.add_column("Blocked by")
        bt.add_column("Age", style="dim")

        for issue in blocked:
            blockers = []
            for b in issue.blocked_by:
                if b in slug_to_issue:
                    blockers.append(f"[yellow]{b}[/yellow]")
                elif b in completed_slugs:
                    blockers.append(f"[green]{b} (done)[/green]")
                else:
                    blockers.append(f"[red]{b} (missing)[/red]")
            dt = _parse_created_at(manager, issue.slug)
            bt.add_row(
                issue.slug,
                ", ".join(blockers),
                _format_age(dt),
            )
        console.print(bt)
    else:
        console.print("[green]No blocked tasks.[/green]")

    # ── Active tasks dwell time ──
    active = stations.get("active", [])
    if active:
        at = Table(
            title="Active Tasks",
            box=theme.table_box,
            header_style=theme.header_style,
            padding=theme.table_padding,
        )
        at.add_column("Slug", style="green")
        at.add_column("Name")
        at.add_column("Dwell", style="yellow")

        for issue in active:
            dt = _parse_created_at(manager, issue.slug)
            at.add_row(issue.slug, issue.name, _format_age(dt))
        console.print(at)


def cmd_ingest(console: Console, manager: TaskAgent):
    """Ingest existing markdown files into mission.usv."""
    cmd_init(console, manager)


def cmd_promote(console: Console, manager: TaskAgent, slug_part: str):
    """Promote an issue from draft to pending."""
    issues = manager.load_mission()
    target = select_issue(console, issues, slug_part, status_filter=["draft"])
    if not target:
        console.print(f"[red]No draft issue found matching '{slug_part}'.[/red]")
        return
    try:
        manager.promote_issue(target.slug)
        console.print(
            f"[bold green]Issue '{target.slug}' promoted to pending.[/bold green]"
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def cmd_demote(console: Console, manager: TaskAgent, slug_part: str):
    """Demote an issue: active -> pending, or pending -> draft."""
    issues = manager.load_mission()
    target = select_issue(
        console, issues, slug_part, status_filter=["pending", "active"]
    )
    if not target:
        console.print(
            f"[red]No pending or active issue found matching '{slug_part}'.[/red]"
        )
        return
    try:
        to_status = "pending" if target.status == "active" else "draft"
        manager.demote_issue(target.slug)
        console.print(
            f"[bold green]Issue '{target.slug}' demoted to {to_status}.[/bold green]"
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def cmd_prompt(
    manager: TaskAgent,
    fmt: str = "default",
    pending_count: bool = False,
) -> None:
    """Print a compact one-liner of the active task, suitable for shell prompts.

    Reads task state directly from the filesystem (no full mission parse, no
    network) for sub-millisecond latency when embedded in a shell prompt.

    Outputs nothing (and exits 0) when there is no active task, so the shell
    prompt string stays clean.

    Formats:
      default   -- [ta:slug]  or  [ta:slug +N]  (with --pending)
      text      -- slug only (machine-readable, whitespace-free)
      json      -- {"active": "slug", "pending": N}  (omits pending key unless requested)
    """
    import json as _json

    active_dir = manager.issues_root / "active"
    active_slugs: List[str] = []
    if active_dir.is_dir():
        for entry in sorted(active_dir.iterdir()):
            if entry.is_dir():
                active_slugs.append(entry.name)
            elif entry.suffix == ".md" and entry.stem != "README":
                active_slugs.append(entry.stem)

    pending_n: Optional[int] = None
    if pending_count:
        pending_dir = manager.issues_root / "pending"
        if pending_dir.is_dir():
            pending_n = sum(
                1
                for e in pending_dir.iterdir()
                if e.is_dir() or (e.suffix == ".md" and e.stem != "README")
            )

    primary = active_slugs[0] if active_slugs else None

    if fmt == "json":
        obj: dict = {"active": primary}
        if pending_count:
            obj["pending"] = pending_n
        print(_json.dumps(obj))
        return

    if fmt == "text":
        if primary:
            print(primary)
        return

    # default: shell-prompt fragment
    if not primary:
        return  # empty output — shell prompt stays clean

    parts = [f"ta:{primary}"]
    if pending_count and pending_n is not None:
        parts.append(f"+{pending_n}")
    print("[" + " ".join(parts) + "]")


def cmd_active(
    console: Console,
    manager: TaskAgent,
    slug_part: Optional[str] = None,
    silent: bool = False,
    list_if_none: bool = False,
) -> Optional[Issue]:
    """Move an issue to active status, or list active tasks."""
    issues = manager.load_mission()
    if not slug_part and list_if_none and not silent:
        maybe_show_strategy(console, manager)
    if not slug_part and list_if_none:
        active_issues = [i for i in issues if i.status == "active"]
        if not silent:
            if active_issues:
                from rich.table import Table

                table = Table(
                    title="Active Tasks",
                    box=theme.table_box,
                    header_style=theme.header_style,
                    padding=theme.table_padding,
                )
                table.add_column("Priority")
                table.add_column("Slug")
                table.add_column("Dependencies")
                for i in active_issues:
                    deps = ", ".join(i.dependencies) if i.dependencies else "-"
                    table.add_row(str(i.priority), i.slug, deps)
                console.print(table)
            else:
                console.print("[yellow]No active tasks found.[/yellow]")
        return None

    target = select_issue(
        console, issues, slug_part, status_filter=["pending", "draft", "active"]
    )
    if not target:
        if slug_part:
            console.print(
                f"[red]No pending/draft issue found matching '{slug_part}'.[/red]"
            )
        else:
            console.print("[yellow]No issues available to mark as active.[/yellow]")
        return None

    try:
        issue = manager.move_to_active(target.slug)
        if not silent:
            console.print(
                f"[bold green]Issue '{issue.slug}' is now active.[/bold green]"
            )
        return issue
    except Exception as e:
        if not silent:
            console.print(f"[red]Error: {e}[/red]")
        return None


def cmd_update(
    console: Console,
    manager: TaskAgent,
    slug_part: str,
    blocked_by: Optional[str] = None,
    subtask_of: Optional[str] = None,
    add_blocked_by: Optional[str] = None,
    remove_blocked_by: Optional[str] = None,
):
    """Update task properties (single slug or comma-separated bulk)."""
    if (
        blocked_by is None
        and subtask_of is None
        and add_blocked_by is None
        and remove_blocked_by is None
    ):
        console.print(
            "[yellow]No updates specified. Use --blocked-by, --add-blocked-by, "
            "--remove-blocked-by, or --subtask-of.[/yellow]"
        )
        return

    raw_parts = [p.strip() for p in slug_part.split(",") if p.strip()]
    is_bulk = len(raw_parts) > 1

    def _resolve_many(parts: list[str]) -> list[str]:
        slugs: list[str] = []
        for p in parts:
            resolved = manager.resolve_issue_slug(p) or manager.slugify(p)
            slugs.append(resolved)
        return slugs

    try:
        if is_bulk:
            slugs = _resolve_many(raw_parts)
            if blocked_by is not None:
                results = manager.bulk_update_dependencies(slugs, blocked_by)
                ok = sum(1 for r in results if r["ok"])
                fail = len(results) - ok
                console.print(
                    f"[bold]blocked_by[/bold] bulk update: "
                    f"[green]{ok} ok[/green], [red]{fail} failed[/red]"
                )
                for r in results:
                    if r["ok"]:
                        console.print(f"  [green]OK[/green] {r['slug']}")
                    else:
                        console.print(f"  [red]FAIL[/red] {r['slug']}: {r['error']}")
            if add_blocked_by is not None:
                for s in slugs:
                    try:
                        manager.add_dependency(s, add_blocked_by)
                        console.print(f"  [green]OK[/green] add-blocked-by {s}")
                    except Exception as e:
                        console.print(f"  [red]FAIL[/red] add-blocked-by {s}: {e}")
            if remove_blocked_by is not None:
                for s in slugs:
                    try:
                        manager.remove_dependency(s, remove_blocked_by)
                        console.print(f"  [green]OK[/green] remove-blocked-by {s}")
                    except Exception as e:
                        console.print(f"  [red]FAIL[/red] remove-blocked-by {s}: {e}")
            if subtask_of is not None:
                parent_slug = subtask_of if subtask_of != "" else None
                results = manager.bulk_update_subtask_of(slugs, parent_slug)
                ok = sum(1 for r in results if r["ok"])
                fail = len(results) - ok
                console.print(
                    f"[bold]subtask_of[/bold] bulk update: "
                    f"[green]{ok} ok[/green], [red]{fail} failed[/red]"
                )
                for r in results:
                    if r["ok"]:
                        console.print(f"  [green]OK[/green] {r['slug']}")
                    else:
                        console.print(f"  [red]FAIL[/red] {r['slug']}: {r['error']}")
            return

        issues = manager.load_mission()
        target = select_issue(
            console, issues, slug_part, status_filter=["pending", "draft", "active"]
        )
        if not target:
            console.print(
                f"[red]No active/pending/draft task found matching '{slug_part}'.[/red]"
            )
            sys.exit(1)

        if blocked_by is not None:
            manager.update_dependencies(target.slug, blocked_by)
            console.print(
                f"[bold green]Successfully updated prerequisites for task '{target.slug}'.[/bold green]"
            )

        if add_blocked_by is not None:
            issue = manager.add_dependency(target.slug, add_blocked_by)
            console.print(
                f"[bold green]Added blockers on '{target.slug}'. "
                f"Now: {', '.join(issue.blocked_by) if issue.blocked_by else '(none)'}[/bold green]"
            )

        if remove_blocked_by is not None:
            issue = manager.remove_dependency(target.slug, remove_blocked_by)
            console.print(
                f"[bold green]Removed blockers from '{target.slug}'. "
                f"Now: {', '.join(issue.blocked_by) if issue.blocked_by else '(none)'}[/bold green]"
            )

        if subtask_of is not None:
            # Empty string means clear the parent
            parent_slug = subtask_of if subtask_of != "" else None
            manager.update_subtask_of(target.slug, parent_slug)
            console.print(
                f"[bold green]Successfully updated parent relationship for task '{target.slug}'.[/bold green]"
            )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_rename(
    console: Console,
    manager: TaskAgent,
    old_slug: str,
    new_title: str,
):
    """Rename a task slug and title."""
    try:
        issue = manager.rename_issue(old_slug, new_title)
        console.print(
            f"[bold green]Successfully renamed task to '{issue.slug}' ({issue.name}).[/bold green]"
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_start(
    console: Console,
    manager: TaskAgent,
    slug_part: Optional[str] = None,
    run: bool = False,
    agent_name: Optional[str] = None,
):
    """Move an issue to active and set up a git worktree."""
    target = cmd_active(console, manager, slug_part, silent=False)
    if not target:
        return

    slug = target.slug
    branch_name = f"issue/{slug}"
    worktree_path = Path(".gwt") / slug

    # Check if a worktree already exists for a different task
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    worktrees = result.stdout.split("\n\n")
    if len(worktrees) > 2:  # main and at least one other
        console.print(
            "[red]Active worktree already exists. Please complete or shelf it first.[/red]"
        )
        if run:
            console.print("[blue]Invoking worker as requested...[/blue]")
            cmd_run(console, manager, slug, agent_name=agent_name)
        return

    console.print(
        f"[blue]Creating branch [bold]{branch_name}[/bold] and worktree at [bold]{worktree_path}[/bold]...[/blue]"
    )

    try:
        Path(".gwt").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
            shell=(os.name == "nt"),
        )
        console.print(f"[bold green]Successfully started issue '{slug}'.[/bold green]")

        if agent_name:
            template_dir = Path(".ta") / "agents" / agent_name
            template_meta = template_dir / "meta.toml"
            if template_meta.exists():
                agent_info = agent.init_per_task_agent(slug, agent_name)
                agent_user = agent_info["user"]
                console.print(
                    f"[dim]Created per-task agent '{agent_user}' "
                    f"from template '{agent_name}'.[/dim]"
                )
            else:
                try:
                    agent_user = agent.get_agent_user(agent_name)
                    agent.set_worktree_permissions(slug, agent_user)
                    console.print(
                        f"[dim]Worktree permissions set for agent '{agent_user}'.[/dim]"
                    )
                except RuntimeError as e:
                    console.print(f"[yellow]Warning: {e}[/yellow]")

        if run:
            console.print("[blue]Invoking worker as requested...[/blue]")
            cmd_run(console, manager, slug, agent_name=agent_name)

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error: {e.stderr.strip()}[/red]")


def cmd_prioritize(
    console: Console, manager: TaskAgent, slug_part: str, direction: str
):
    """Move an issue up or down in priority."""
    issues = manager.load_mission()
    target = select_issue(console, issues, slug_part)
    if not target:
        console.print(f"[red]No issue found matching '{slug_part}'.[/red]")
        return
    try:
        manager.prioritize_issue(target.slug, direction)
        console.print(f"[bold green]Moved '{target.slug}' {direction}.[/bold green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def cmd_self_up(console: Console):
    """Upgrade task-agent tool."""
    console.print("[blue]Upgrading task-agent via uv...[/blue]")
    try:
        subprocess.run(
            ["uv", "tool", "upgrade", "task-agent"], check=True, shell=(os.name == "nt")
        )
        console.print("[bold green]Successfully upgraded task-agent.[/bold green]")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error upgrading task-agent: {e}[/red]")
        if os.name == "nt":
            console.print("\n[yellow]Note for Windows users:[/yellow]")
            console.print(
                "If you see 'The process cannot access the file', it means [bold]ta.exe[/bold] is locked."
            )
            console.print(
                "This happens if an MCP session or another terminal is using it."
            )
            console.print(
                "Please [bold]close all chats and other terminals[/bold], then run:"
            )
            console.print("  [cyan]uv tool upgrade task-agent[/cyan]\n")


def cmd_run(
    console: Console,
    manager: TaskAgent,
    slug_part: Optional[str] = None,
    agent_name: Optional[str] = None,
):
    """Run the sidecar worker for an issue. Optionally run as an agent user."""
    issues = manager.load_mission()
    target = select_issue(console, issues, slug_part, status_filter=["active"])
    if not target:
        if slug_part:
            console.print(f"[red]No active issue found matching '{slug_part}'.[/red]")
        else:
            console.print("[yellow]No active issues found to run.[/yellow]")
        return

    issue_file = manager.find_issue_file(target.slug)
    worker_ext = ".bat" if os.name == "nt" else ""
    worker_executable = Path(".ta") / f"worker{worker_ext}"
    if not worker_executable.exists():
        console.print(f"[red]Sidecar worker not found at {worker_executable}[/red]")
        console.print("[blue]Run 'ta init-worker' to set up a reference worker.[/blue]")
        return

    env = os.environ.copy()
    env["TA_SLUG"] = target.slug
    env["TA_FILE"] = str(issue_file.absolute()) if issue_file else ""
    env["TA_ROOT"] = str(Path.cwd().absolute())

    try:
        if agent_name:
            meta = agent.load_per_task_agent_meta(target.slug)
            if meta:
                agent_user = meta["user"]
            else:
                template_dir = Path(".ta") / "agents" / agent_name
                template_meta = template_dir / "meta.toml"
                if template_meta.exists():
                    result = agent.init_per_task_agent(target.slug, agent_name)
                    agent_user = result["user"]
                else:
                    agent_user = agent.get_agent_user(agent_name)

            worktree_path = agent.get_worktree_path(target.slug)

            ta_file = shlex.quote(str(issue_file.absolute())) if issue_file else ""
            shell_cmd = (
                f"cd {shlex.quote(str(worktree_path))} && "
                f"exec env "
                f"TA_SLUG={shlex.quote(target.slug)} "
                f"TA_FILE={ta_file} "
                f"TA_ROOT={shlex.quote(str(Path.cwd().absolute()))} "
                f"{shlex.quote(str(worker_executable.absolute()))}"
            )
            subprocess.run(
                ["sudo", "-u", agent_user, "bash", "-l", "-c", shell_cmd],
                check=True,
            )
        else:
            subprocess.run(
                [str(worker_executable.absolute())],
                env=env,
                check=True,
                shell=(os.name == "nt"),
            )
        console.print(
            f"[bold green]Worker for '{target.slug}' finished successfully.[/bold green]"
        )
    except Exception as e:
        console.print(f"[red]Worker failed: {e}[/red]")


def cmd_plan(console: Console, manager: TaskAgent):
    """View or edit the project plan."""
    plan_file = manager.get_or_create_plan()
    editor = get_editor()
    subprocess.run([editor, str(plan_file)])


def cmd_init(console: Console, manager: TaskAgent):
    """Initialize or heal the project."""
    console.print("[blue]Initializing Task Agent project...[/blue]")
    num_new, num_removed = manager.init_project()
    console.print(
        f"[bold green]Task Agent initialized at {manager.issues_root}[/bold green]"
    )
    if num_new > 0 or num_removed > 0:
        console.print(
            f"[dim]Ingested {num_new} new issues, removed {num_removed} missing ones.[/dim]"
        )
    console.print("[dim]Mission files are protected (Read-Only).[/dim]")


def cmd_list_templates(console: Console):
    """List available agent templates from .ta/agents/."""
    from taskagent import templates

    agents_dir = Path(".ta") / "agents"
    if not agents_dir.is_dir():
        console.print("[yellow]No templates found in .ta/agents/[/yellow]")
        return

    table = Table(
        title="Available Templates",
        box=theme.table_box,
        header_style=theme.header_style,
        padding=theme.table_padding,
    )
    table.add_column("Name", style="cyan")
    table.add_column("Description")

    for d in sorted(agents_dir.iterdir()):
        if d.is_dir():
            meta_file = d / "meta.toml"
            if meta_file.exists():
                try:
                    t = templates.load_template(d.name)
                    table.add_row(t.name, t.description)
                except Exception:
                    table.add_row(d.name, "[dim]invalid meta.toml[/dim]")
            else:
                table.add_row(d.name, "[dim]no meta.toml[/dim]")

    console.print(table)


def cmd_init_agent(
    console: Console, name: str, template: Optional[str] = None, op_timeout: int = 30
):
    """Create a dedicated Linux user for agent isolation."""
    try:
        result = agent.init_agent(name, template_name=template, op_timeout=op_timeout)
        console.print(
            f"[bold green]Agent user '{result['user']}' created.[/bold green]"
        )
        console.print(f"  Home:    [cyan]{result['home']}[/cyan]")
        console.print(f"  SSH key: [cyan]{result['ssh_key']}[/cyan]")
        console.print(f"  Gitconfig: [cyan]{result['gitconfig']}[/cyan]")
        console.print(f"  Sudoers:  [cyan]{result['sudoers']}[/cyan]")
        console.print(
            "\n[dim]Use [bold]ta run <slug> --agent <name>[/bold] "
            "to run tasks as this agent.[/dim]"
        )
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_destroy_agent(console: Console, name: str):
    """Remove an agent Linux user."""
    try:
        agent.destroy_agent(name)
        console.print(f"[bold green]Agent user 'agent-{name}' removed.[/bold green]")
    except RuntimeError as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


def cmd_init_worker(console: Console, template: str = "adk"):
    """Scaffold a sidecar worker in the current project."""
    target_ta_dir = Path(".ta")
    target_sidecar_dir = target_ta_dir / "sidecars" / f"{template}-worker"

    if target_sidecar_dir.exists():
        console.print(
            f"[yellow]Sidecar worker already exists at {target_sidecar_dir}.[/yellow]"
        )
        return

    pkg_root = Path(__file__).parent.parent.parent
    source_dir = pkg_root / "sidecars" / f"{template}-worker"
    if not source_dir.exists():
        import importlib.resources

        try:
            traversable_root = importlib.resources.files("taskagent")
            source_dir = (
                Path(str(traversable_root)).parent / "sidecars" / f"{template}-worker"
            )
        except Exception:
            pass

    if not source_dir.exists():
        console.print(f"[red]Error: Template '{template}' not found.[/red]")
        return

    target_sidecar_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        if item.is_file():
            shutil.copy(str(item), str(target_sidecar_dir / item.name))

    worker_script = target_ta_dir / ("worker.bat" if os.name == "nt" else "worker")
    if os.name == "nt":
        script_content = f"@echo off\nuv run --project {target_sidecar_dir} python {target_sidecar_dir}/worker.py %*\n"
    else:
        script_content = f'#!/usr/bin/env bash\nuv run --project {target_sidecar_dir} python {target_sidecar_dir}/worker.py "$@"\n'

    worker_script.write_text(script_content, encoding="utf-8")
    if os.name != "nt":
        worker_script.chmod(0o755)
    console.print(
        f"[bold green]Successfully initialized {template} worker![/bold green]"
    )


def cmd_mcp():
    """Launch the Model Context Protocol server."""
    from taskagent.mcp import run_mcp_server

    run_mcp_server()


# ... (in imports)


def _agy_mcp_config_path(scope: str = "user") -> Path:
    """Return the Antigravity CLI mcp_config.json path for the given scope.

    - user: ``~/.gemini/antigravity-cli/mcp_config.json`` (CLI app data)
    - project: ``.agents/mcp_config.json`` under the current working directory
    """
    if scope == "project":
        return Path.cwd().resolve() / ".agents" / "mcp_config.json"
    return Path.home() / ".gemini" / "antigravity-cli" / "mcp_config.json"


def _merge_mcp_server_config(
    config_path: Path,
    server_name: str,
    server_entry: dict,
) -> Path:
    """Merge a single MCP server entry into an mcp_config.json file.

    Creates parent directories and a minimal file when missing. Preserves
    unrelated keys (e.g. experimental blocks) and other servers.
    """
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            raw = f.read().strip()
            config = json.loads(raw) if raw else {}
    else:
        config = {}

    if not isinstance(config, dict):
        config = {}

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[server_name] = server_entry
    config["mcpServers"] = servers

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return config_path


def cmd_init_mcp(
    console: Console,
    agent: str = "gemini",
    print_json: bool = False,
    scope: str = "project",
    claude: bool = False,
    agy: bool = False,
    copilot: bool = False,
    opencode: bool = False,
):
    """Register the Task Agent as an MCP server.

    Uses ``uv run --project <root> ta mcp`` so the server can be spawned
    without the project's virtualenv being active in the calling shell.
    """
    if opencode or agent == "opencode":
        agent = "opencode"

    project_root = Path.cwd().resolve()
    if shutil.which("ta"):
        mcp_command = "ta"
        mcp_args = ["mcp"]
    else:
        mcp_command = "uv"
        mcp_args = ["run", "--project", str(project_root), "ta", "mcp"]

    mcp_config = {
        "mcpServers": {
            "task_agent": {
                "command": mcp_command,
                "args": mcp_args,
            }
        }
    }

    if print_json:
        console.print(json.dumps(mcp_config, indent=2))
        return

    if claude or agent == "claude":
        console.print("[blue]Registering Task Agent MCP with Claude Code...[/blue]")
        try:
            command = [
                "claude",
                "mcp",
                "add",
                "task_agent",
                "--",
                mcp_command,
            ] + mcp_args
            subprocess.run(command, check=True, shell=(os.name == "nt"))
            console.print(
                "[bold green]Successfully registered Task Agent MCP with Claude Code![/bold green]"
            )
        except subprocess.CalledProcessError as e:
            console.print(
                f"[red]Failed to register Claude MCP server via 'claude mcp add': {e}[/red]"
            )
            console.print(
                "[yellow]Make sure Claude Code CLI is installed and in your PATH.[/yellow]"
            )
        except FileNotFoundError:
            console.print("[red]Error: 'claude' command not found.[/red]")
            console.print(
                "[yellow]Make sure Claude Code CLI is installed and available in your PATH.[/yellow]"
            )
        return

    if copilot or agent == "copilot":
        console.print(
            "[blue]Registering Task Agent MCP with GitHub Copilot globally...[/blue]"
        )
        try:
            command = [
                "copilot",
                "mcp",
                "add",
                "task_agent",
                "--",
                mcp_command,
            ] + mcp_args
            subprocess.run(command, check=True, shell=(os.name == "nt"))
            console.print(
                "[bold green]Successfully registered Task Agent MCP with GitHub Copilot![/bold green]"
            )
        except subprocess.CalledProcessError as e:
            console.print(
                f"[red]Failed to register GitHub Copilot MCP server: {e}[/red]"
            )
        except FileNotFoundError:
            console.print("[red]Error: 'copilot' command not found.[/red]")
            console.print(
                "[yellow]Make sure GitHub Copilot CLI is installed and available in your PATH.[/yellow]"
            )
        return

    if agy or agent == "agy":
        # --agy defaults to user-global CLI config (HOME). Pass --scope project
        # for workspace .agents/mcp_config.json. When callers omit --scope,
        # main() maps agy → user (see init-mcp dispatch).
        config_path = _agy_mcp_config_path(scope)
        console.print(
            f"[blue]Registering Task Agent MCP with Antigravity CLI "
            f"({scope} → {config_path})...[/blue]"
        )
        server_entry = {
            "command": mcp_command,
            "args": mcp_args,
            "trust": True,
        }
        try:
            written = _merge_mcp_server_config(config_path, "task_agent", server_entry)
            console.print(
                f"[bold green]Successfully registered Task Agent MCP at {written}![/bold green]"
            )
            console.print(
                "[dim]Restart agy or reload MCP if the server list was already cached.[/dim]"
            )
        except Exception as e:
            console.print(f"[red]Failed to register Antigravity MCP server: {e}[/red]")
        return

    if agent == "gemini":
        console.print(
            f"[blue]Registering Task Agent as an MCP server ({scope} scope)...[/blue]"
        )
        command = [
            "gemini",
            "mcp",
            "add",
            "task_agent",
            mcp_command,
            *mcp_args,
            "--trust",
            "--scope",
            scope,
        ]
        try:
            subprocess.run(command, check=True, shell=(os.name == "nt"))
            console.print(
                "[bold green]Successfully registered Task Agent MCP server![/bold green]"
            )
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to register MCP server: {e}[/red]")
    elif agent == "opencode":
        if scope == "user":
            config_path = Path.home() / ".config" / "opencode" / "opencode.json"
            console.print(
                f"[blue]Installing Task Agent MCP globally at {config_path}...[/blue]"
            )
        else:
            config_path = Path.cwd() / "opencode.json"

        try:
            if config_path.exists():
                with config_path.open("r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
            if "mcp" not in config:
                config["mcp"] = {}
            config["mcp"]["task_agent"] = {
                "type": "local",
                "command": [mcp_command, *mcp_args],
            }
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with config_path.open("w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            console.print(
                f"[bold green]Successfully registered Task Agent MCP at {config_path}![/bold green]"
            )
        except Exception as e:
            console.print(f"[red]Failed to register Task Agent MCP: {e}[/red]")


def cmd_init_plugin(
    console: Console,
    claude: bool = False,
    agy: bool = False,
    opencode: bool = False,
    copilot: bool = False,
    grok: bool = False,
    codex: bool = False,
    scope: str = "user",
) -> None:
    from taskagent.agent_registry import (
        get_agent_cli_registry,
        get_disabled_agent_plugins,
        inspect_all_agent_clis,
    )

    repo_root = Path(__file__).parent.parent.parent
    registry = get_agent_cli_registry()

    targets: List[str] = []
    if claude:
        targets.append("claude")
    if agy:
        targets.append("agy")
    if opencode:
        targets.append("opencode")
    if copilot:
        targets.append("copilot")
    if grok:
        targets.append("grok")
    if codex:
        targets.append("codex")

    if not targets:
        installed = inspect_all_agent_clis()
        for item in installed:
            if item["installed"] and item["plugin_support"]:
                targets.append(item["id"])

    disabled = get_disabled_agent_plugins()
    targets = [t for t in targets if t not in disabled]

    if not targets:
        console.print(
            "[yellow]No supported agent CLIs detected or specified for plugin installation.[/yellow]"
        )
        return

    import shutil

    skills_src = repo_root / "skills"

    for target in sorted(set(targets)):
        if target not in registry:
            continue
        info = registry[target]

        # 1. Install Plugin Bundle
        if info.plugin_path:
            dest = info.plugin_path / "task-agent"
            dest.parent.mkdir(parents=True, exist_ok=True)

            tmpl_name = info.plugin_template or "antigravity"
            tmpl_dir = repo_root / "plugins" / tmpl_name
            if not tmpl_dir.is_dir():
                tmpl_dir = repo_root / "plugins" / "antigravity"
            if not tmpl_dir.is_dir():
                tmpl_dir = repo_root / "plugins" / "claude-code"

            if tmpl_dir.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(tmpl_dir, dest)
                console.print(
                    f"[bold green]Successfully installed {info.name} plugin at {dest}![/bold green]"
                )

        # 2. Sync Custom Skills / Slash Commands
        if info.skills_path:
            info.skills_path.mkdir(parents=True, exist_ok=True)
            if skills_src.is_dir():
                for sdir in skills_src.iterdir():
                    if sdir.is_dir() and (sdir / "SKILL.md").exists():
                        if "commands" in str(info.skills_path):
                            shutil.copy2(
                                sdir / "SKILL.md",
                                info.skills_path / f"{sdir.name}.md",
                            )
                        else:
                            target_sdir = info.skills_path / sdir.name
                            target_sdir.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(sdir / "SKILL.md", target_sdir / "SKILL.md")
                console.print(
                    f"[bold green]Successfully synced Task Agent skills to {info.skills_path}![/bold green]"
                )

        # 3. Register MCP Server
        if info.mcp_support:
            cmd_init_mcp(console, agent=target, scope=scope)


def cmd_agents_list(
    console: Console,
    agent_id: Optional[str] = None,
    json_format: bool = False,
) -> None:
    """Inspect local installation and MCP/plugin registration status for agent CLIs."""
    from taskagent.agent_registry import inspect_agent_cli, inspect_all_agent_clis

    if agent_id:
        try:
            results = [inspect_agent_cli(agent_id)]
        except ValueError as e:
            console.print(f"[red]Error: {e}[/red]")
            return
    else:
        results = inspect_all_agent_clis()

    if json_format:
        import json as _json

        print(_json.dumps(results, indent=2))
        return

    table = Table(
        title="Agent CLI Registry & Local Integration Status", show_header=True
    )
    table.add_column("Agent CLI", style="cyan", no_wrap=True)
    table.add_column("Binary", style="dim")
    table.add_column("Installed", justify="center")
    table.add_column("MCP Status", justify="center")
    table.add_column("Plugin Status", justify="center")
    table.add_column("Register Command", style="bold green")

    for item in results:
        installed_str = "[green]Yes[/green]" if item["installed"] else "[dim]No[/dim]"

        if not item["mcp_support"]:
            mcp_str = "[dim]N/A[/dim]"
        elif item["mcp_registered"]:
            mcp_str = "[green]Registered[/green]"
        else:
            mcp_str = "[yellow]Not registered[/yellow]"

        if not item["plugin_support"]:
            plugin_str = "[dim]N/A[/dim]"
        elif item["plugin_installed"]:
            plugin_str = "[green]Installed[/green]"
        else:
            plugin_str = "[dim]Not installed[/dim]"

        cmd = item["mcp_command_example"] or ""
        table.add_row(
            item["name"],
            item["binary"],
            installed_str,
            mcp_str,
            plugin_str,
            cmd,
        )

    console.print(table)


def cmd_agent_import(
    console: Console,
    manager: TaskAgent,
    slug: Optional[str] = None,
    agent_type: str = "antigravity",
    file_path: Optional[str] = None,
    json_format: bool = False,
) -> None:
    """Handler for 'ta agent import' subcommand."""
    try:
        res = manager.import_agent_tasks(
            slug=slug,
            agent_type=agent_type,
            file_path=file_path,
        )
        if json_format:
            print(json.dumps(res, indent=2))
        else:
            console.print(
                f"[green]✔ Successfully imported {res['count']} tasks from '{res['agent_type']}' "
                f"into working task '{res['slug']}'.[/green]\n"
                f"[dim]Saved to: {res['path']}[/dim]"
            )
    except Exception as e:
        if json_format:
            print(json.dumps({"error": str(e)}, indent=2))
            sys.exit(1)
        else:
            console.print(f"[red]Error importing agent tasks: {e}[/red]")
            sys.exit(1)


def cmd_agent_last_used(
    console: Console,
    path_arg: Optional[str] = None,
    limit: int = 5,
    json_format: bool = False,
) -> None:
    """Handler for 'ta agent last-used' subcommand."""
    from taskagent.chat.last_used import get_last_active_agents

    target_path = Path(path_arg).resolve() if path_arg else Path.cwd()
    results = get_last_active_agents(project_dir=target_path, limit=limit)

    if json_format:
        output = [
            {
                "agent_id": item.agent_id,
                "agent_name": item.agent_name,
                "description": item.description,
                "last_active": item.last_active.isoformat(),
                "last_user_comment": item.last_user_comment,
                "log_path": str(item.log_path),
            }
            for item in results
        ]
        print(json.dumps(output, indent=2))
        return

    if not results:
        console.print(
            f"[yellow]No agent chat sessions found for repository at [bold]{target_path}[/bold].[/yellow]"
        )
        return

    console.print(
        f"\n[bold green]🤖 Recently Active AI Agents[/bold green] [dim]({target_path})[/dim]\n"
    )

    table = Table(
        box=None,
        padding=(0, 2, 0, 0),
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=3)
    table.add_column("Agent CLI", style="bold yellow")
    table.add_column("Last Active", style="green")
    table.add_column("Last Comment Snippet", style="dim white")

    for idx, item in enumerate(results, start=1):
        rel_time = item.last_active.strftime("%Y-%m-%d %H:%M:%S UTC")
        comment_snippet = (
            item.last_user_comment
            if item.last_user_comment
            else "[dim](no prompt comment logged)[/dim]"
        )
        table.add_row(
            str(idx),
            item.agent_name,
            rel_time,
            comment_snippet,
        )

    console.print(table)


def cmd_version(
    console: Console,
    promote: Optional[str] = None,
    tag: bool = False,
    push: bool = True,
    release: Optional[str] = None,
    push_branch: bool = True,
):
    """Show project version, promote it, tag it, or run a full release."""
    display_version_info(console)

    try:
        if release:
            # Atomic: bump+commit, then tag, then push branch+tag
            new_v = promote_project_version(console, release, allow_amend=True)
            console.print(f"[blue]Release {new_v}: tagging and pushing...[/blue]")
            tag_project_version(console, push=push, push_branch=push_branch)
            console.print(f"[bold green]Release v{new_v} complete.[/bold green]")
            return

        if promote:
            new_v = promote_project_version(console, promote, allow_amend=True)
            console.print(
                "[dim]Next: ta version tag[/dim]  "
                "[dim](or use ta version release for one-shot)[/dim]"
            )
            console.print(
                f"[dim]HEAD is now v{new_v}; tag when the commit is ready to publish.[/dim]"
            )
            return

        if tag:
            tag_project_version(console, push=push, push_branch=push_branch)
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[red]Error during version operation: {e}[/red]")
        sys.exit(1)


def format_git_log_rich(raw_log: str) -> str:
    """Highlight git commit log with Rich markup for hashes, authors, dates, and diff stats."""
    lines = raw_log.splitlines()
    formatted_lines = []
    for line in lines:
        if line.startswith("commit "):
            parts = line.split(" ", 1)
            formatted_lines.append(
                f"[bold yellow]commit[/bold yellow] [yellow]{parts[1]}[/yellow]"
            )
        elif line.startswith("Author: "):
            parts = line.split(": ", 1)
            formatted_lines.append(
                f"[bold cyan]Author:[/bold cyan] [cyan]{parts[1]}[/cyan]"
            )
        elif line.startswith("Date: "):
            formatted_lines.append(f"[dim]{line}[/dim]")
        elif " | " in line and ("+" in line or "-" in line):
            formatted_lines.append(f"[dim green]{line}[/dim green]")
        elif "insertions(+)" in line or "deletions(-)" in line or "changed" in line:
            formatted_lines.append(f"[bold blue]{line}[/bold blue]")
        elif line.startswith("    "):
            formatted_lines.append(f"  [bold white]{line.strip()}[/bold white]")
        else:
            formatted_lines.append(line)
    return "\n".join(formatted_lines)


def cmd_log(
    console: Console,
    manager: TaskAgent,
    extra_args: Optional[List[str]] = None,
    repo: Optional[str] = None,
) -> None:
    """Pass-through CLI handler for git log with Rich styling and interactive less paging."""
    output = manager.get_store_log(repo=repo, extra_args=extra_args)
    if not output.strip():
        console.print("[dim]No git log history found.[/dim]")
        return

    formatted_output = format_git_log_rich(output)

    if console.is_terminal:
        with console.pager(styles=True):
            console.print(formatted_output)
    else:
        console.print(formatted_output)


def cmd_perf(console: Console, manager: TaskAgent, action: Optional[str] = "status"):
    """Handle performance monitoring logging control and log display."""
    from taskagent.perf import (
        is_perf_logging_enabled,
        set_perf_logging_enabled,
        get_perf_logger,
        ENV_VAR_PERF_LOG,
    )

    action = (action or "status").lower()

    if action in ("on", "enable"):
        set_perf_logging_enabled(True)
        console.print(
            f"[bold green]✓ Performance monitoring logging ENABLED[/bold green] ({ENV_VAR_PERF_LOG}=1)"
        )
    elif action in ("off", "disable"):
        set_perf_logging_enabled(False)
        console.print(
            f"[bold yellow]✓ Performance monitoring logging DISABLED[/bold yellow] ({ENV_VAR_PERF_LOG}=0)"
        )

    logger = get_perf_logger(manager.issues_root)
    enabled = is_perf_logging_enabled()
    status_str = (
        "[bold green]ACTIVE[/bold green]"
        if enabled
        else "[dim yellow]INACTIVE[/dim yellow]"
    )

    console.print(
        Panel(
            f"[bold]Performance Monitoring Logging[/bold]\n\n"
            f"Status: {status_str}\n"
            f"Environment Variable: [cyan]{ENV_VAR_PERF_LOG}={os.environ.get(ENV_VAR_PERF_LOG, '0')}[/cyan]\n"
            f"Log Directory: [dim]{logger.log_dir}[/dim]",
            expand=False,
            box=theme.panel_box,
        )
    )

    metrics = logger.get_recent_metrics(limit=25)
    if metrics:
        table = Table(
            title="Recent Performance Metrics",
            box=theme.table_box,
            header_style=theme.header_style,
            padding=theme.table_padding,
        )
        table.add_column("Timestamp", style="dim")
        table.add_column("Operation", style="cyan")
        table.add_column("Duration (ms)", justify="right", style="magenta")
        table.add_column("Status", style="bold")

        for m in metrics:
            ts = m.get("timestamp", "")[:19].replace("T", " ")
            op = m.get("operation", "unknown")
            dur = f"{m.get('duration_ms', 0):.2f}"
            succ = "[green]✓[/green]" if m.get("success", True) else "[red]✗[/red]"
            table.add_row(ts, op, dur, succ)

        console.print(table)
    else:
        console.print("[dim]No performance metric logs recorded yet.[/dim]")


def promote_version(console: Console, manager: TaskAgent):
    """Deprecated auto-promote path (no longer called from ``ta done``).

    Kept for compatibility if external callers import it; uses the safe
    promote implementation (amend only when unpublished).
    """
    project_root = None
    if manager.issues_root:
        curr = Path(manager.issues_root).resolve()
        while curr.parent != curr:
            if (
                (curr / "pyproject.toml").exists()
                or (curr / "package.json").exists()
                or (curr / ".git").exists()
            ):
                project_root = curr
                break
            curr = curr.parent

    if not project_root:
        return

    try:
        promote_project_version(
            console, "patch", project_root=project_root, allow_amend=True
        )
    except Exception as e:
        console.print(f"[yellow]Version promote skipped: {e}[/yellow]")


def cmd_worktree(console: Console, manager: TaskAgent, args):
    """Manage git worktrees for branches, tags, and commits."""
    import subprocess
    import os
    from pathlib import Path

    # Default worktree directory
    worktree_base = Path(".gwt")
    worktree_base.mkdir(exist_ok=True)

    # Show help if no action provided
    if not args.action:
        console.print("[bold blue]Worktree Management[/bold blue]")
        console.print()
        console.print("[bold]Available actions:[/bold]")
        console.print("  [cyan]add[/cyan]    - Create a new worktree (requires target)")
        console.print("  [cyan]list[/cyan]   - List all worktrees")
        console.print("  [cyan]remove[/cyan] - Remove a worktree (requires target)")
        console.print("  [cyan]prune[/cyan]   - Remove stale worktree information")
        console.print()
        console.print("[dim]Run 'ta worktree add --help' for detailed options.[/dim]")
        console.print(
            "[dim]Run 'ta worktree <action> --help' for action-specific help.[/dim]"
        )
        return

    if args.action == "add":
        if not args.target:
            console.print(
                "[red]Error: target (branch/tag/commit) is required for add action[/red]"
            )
            return

        # Determine what we're checking out
        if args.tag:
            ref = f"tags/{args.target}"
            display_name = f"tag:{args.target}"
        elif args.commit:
            ref = args.target
            display_name = f"commit:{args.target[:8]}"
        else:
            ref = args.target
            display_name = f"branch:{args.target}"

        # Create worktree path
        worktree_path = worktree_base / args.target
        worktree_path.mkdir(parents=True, exist_ok=True)

        try:
            # Add the worktree
            if args.tag or args.commit:
                # For tags/commits, we need to checkout the specific ref
                subprocess.run(
                    ["git", "worktree", "add", str(worktree_path), ref],
                    check=True,
                    capture_output=True,
                    text=True,
                    shell=(os.name == "nt"),
                )
            else:
                # For branches, create new branch if it doesn't exist
                subprocess.run(
                    ["git", "worktree", "add", "-B", args.target, str(worktree_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                    shell=(os.name == "nt"),
                )

            console.print(
                f"[green]Added worktree for {display_name} at {worktree_path}[/green]"
            )

            # Set permissions if specified
            if args.permissions:
                try:
                    perms = int(args.permissions, 8)
                    os.chmod(worktree_path, perms)
                    console.print(f"[dim]Set permissions to {args.permissions}[/dim]")
                except ValueError:
                    console.print(
                        f"[yellow]Warning: Invalid permissions '{args.permissions}', using default[/yellow]"
                    )

            # Copy files if requested
            copy_patterns = args.copy or []
            if not args.no_symlinks:
                copy_patterns.append("symlinks")
            if not args.no_env:
                copy_patterns.append("*.env")

            if copy_patterns:
                _copy_files_to_worktree(console, worktree_path, copy_patterns)

            # Configure git user for this worktree if needed
            _configure_git_user_for_worktree(console, worktree_path, args.target)

        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error creating worktree: {e.stderr}[/red]")
            # Clean up on failure
            if worktree_path.exists():
                subprocess.run(
                    ["git", "worktree", "remove", str(worktree_path)],
                    check=False,
                    shell=(os.name == "nt"),
                )
                try:
                    if not any(worktree_path.iterdir()):
                        worktree_path.rmdir()
                except (OSError, StopIteration):
                    pass

    elif args.action == "list":
        try:
            result = subprocess.run(
                ["git", "worktree", "list"],
                capture_output=True,
                text=True,
                check=True,
                shell=(os.name == "nt"),
            )
            if result.stdout.strip():
                console.print("[bold blue]Worktrees:[/bold blue]")
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        console.print(f"  {line}")
            else:
                console.print("[yellow]No worktrees found[/yellow]")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error listing worktrees: {e.stderr}[/red]")

    elif args.action == "remove":
        if not args.target:
            console.print(
                "[red]Error: target (worktree path) is required for remove action[/red]"
            )
            return

        worktree_path = Path(args.target)
        if not worktree_path.exists():
            console.print(
                f"[yellow]Worktree path {worktree_path} does not exist[/yellow]"
            )
            return

        try:
            subprocess.run(
                ["git", "worktree", "remove", str(worktree_path)],
                check=True,
                capture_output=True,
                text=True,
                shell=(os.name == "nt"),
            )
            console.print(f"[green]Removed worktree at {worktree_path}[/green]")
            # Try to remove the directory if empty
            try:
                worktree_path.rmdir()
            except OSError:
                pass  # Directory not empty, leave it
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error removing worktree: {e.stderr}[/red]")

    elif args.action == "prune":
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                check=True,
                capture_output=True,
                text=True,
                shell=(os.name == "nt"),
            )
            console.print("[green]Pruned stale worktrees[/green]")
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Error pruning worktrees: {e.stderr}[/red]")


def cmd_github(console: Console, manager: TaskAgent, args):
    """Sync with GitHub Issues."""
    try:
        from taskagent.plugins.github import GitHubPlugin
    except ImportError:
        console.print("[red]GitHub plugin not installed. Run: uv add githubkit[/red]")
        return

    # Load config from .task-agent/worktree-config.json
    config = {}
    config_file = Path(".task-agent/worktree-config.json")
    if config_file.exists():
        try:
            with config_file.open("r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    # Override repo if specified in args
    if hasattr(args, "repo") and args.repo:
        if "github" not in config:
            config["github"] = {}
        config["github"]["repo"] = args.repo

    try:
        plugin = GitHubPlugin(config)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return

    if args.github_command == "sync":
        try:
            issues = plugin.sync_from_github()
            console.print(f"[green]Imported {len(issues)} issues from GitHub[/green]")

            # Add issues to task-agent
            for issue in issues:
                try:
                    manager.create_issue(issue.name, body="Imported from GitHub")
                    console.print(f"  Added: {issue.name}")
                except Exception as e:
                    console.print(f"  [yellow]Skipped {issue.slug}: {e}[/yellow]")

            # Save mission
            manager.save_mission(manager.load_mission())
            console.print("[green]Mission file updated[/green]")
        except Exception as e:
            console.print(f"[red]Error syncing: {e}[/red]")

    elif args.github_command == "push":
        try:
            # Fuzzy find the issue by slug
            pat_norm = normalize(args.slug)
            matches = [
                i.slug for i in manager.load_mission() if fuzzy_match(i.slug, pat_norm)
            ]
            if not matches:
                for _, completed_slug in manager.walk_completed():
                    if fuzzy_match(completed_slug, pat_norm):
                        matches.append(completed_slug)

            if not matches:
                console.print(f"[red]Issue '{args.slug}' not found[/red]")
                return
            if len(matches) > 1:
                console.print(
                    f"[red]Multiple issues match '{args.slug}'. Please be more specific.[/red]"
                )
                for m in matches:
                    console.print(f"  - {m}")
                return

            args.slug = matches[0]
            issue_file = manager.find_issue_file(args.slug, include_completed=True)
            if not issue_file:
                console.print(f"[red]Issue '{args.slug}' not found[/red]")
                return

            # Load issue details
            name = manager.extract_title(issue_file)

            # Create GitHub issue
            from taskagent.models.issue import Issue

            temp_issue = Issue(name=name, slug=args.slug, dependencies=[])
            result = plugin.create_github_issue(temp_issue)

            console.print(f"[green]Created GitHub Issue #{result['number']}[/green]")
            console.print(f"URL: {result['url']}")
        except Exception as e:
            console.print(f"[red]Error creating issue: {e}[/red]")
    else:
        console.print("[yellow]Use 'sync' or 'push' subcommand[/yellow]")


def _copy_files_to_worktree(console: Console, worktree_path: Path, patterns: list):
    """Copy files matching patterns to the worktree directory."""
    import shutil

    repo_root = Path.cwd()

    for pattern in patterns:
        if pattern == "symlinks":
            # Find and copy symlinks
            for item in repo_root.rglob("*"):
                if item.is_symlink() and not any(
                    part.startswith(".gwt") for part in item.parts
                ):
                    relative_path = item.relative_to(repo_root)
                    target_path = worktree_path / relative_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(item, target_path)
                        console.print(f"[dim]Copied symlink: {relative_path}[/dim]")
                    except Exception as e:
                        console.print(
                            f"[yellow]Warning: Failed to copy symlink {relative_path}: {e}[/yellow]"
                        )
        else:
            # Handle glob patterns
            for item in repo_root.glob(pattern):
                if not any(part.startswith(".gwt") for part in item.parts):
                    relative_path = item.relative_to(repo_root)
                    target_path = worktree_path / relative_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        if item.is_dir():
                            shutil.copytree(item, target_path, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, target_path)
                        console.print(f"[dim]Copied: {relative_path}[/dim]")
                    except Exception as e:
                        console.print(
                            f"[yellow]Warning: Failed to copy {relative_path}: {e}[/yellow]"
                        )


def _configure_git_user_for_worktree(
    console: Console, worktree_path: Path, branch_name: str
):
    """Configure git user.email and user.name for a worktree based on branch."""
    import subprocess
    import json
    from pathlib import Path

    # Default to current user's git config
    try:
        # Get current git config
        user_name_result = subprocess.run(
            ["git", "config", "--get", "user.name"],
            capture_output=True,
            text=True,
            check=False,
        )
        user_email_result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            check=False,
        )

        user_name = (
            user_name_result.stdout.strip() if user_name_result.returncode == 0 else ""
        )
        user_email = (
            user_email_result.stdout.strip()
            if user_email_result.returncode == 0
            else ""
        )

        # Check for branch-specific git config
        # Look for .task-agent/worktree-config.json
        config_path = Path(".task-agent/worktree-config.json")
        if config_path.exists():
            try:
                with config_path.open("r", encoding="utf-8") as f:
                    config = json.load(f)
                    # Check if there's a specific config for this branch
                    branch_config = config.get("branches", {}).get(branch_name, {})
                    if branch_config:
                        user_name = branch_config.get("user.name", user_name)
                        user_email = branch_config.get("user.email", user_email)
                        console.print(
                            f"[dim]Using branch-specific config for '{branch_name}'[/dim]"
                        )
                    # Check for default config
                    elif "default" in config.get("branches", {}):
                        default_config = config["branches"]["default"]
                        user_name = default_config.get("user.name", user_name)
                        user_email = default_config.get("user.email", user_email)
                        console.print("[dim]Using default worktree config[/dim]")
            except Exception as e:
                console.print(
                    f"[yellow]Warning: Failed to read worktree config: {e}[/yellow]"
                )

        if user_name and user_email:
            # Set git config locally for this worktree
            subprocess.run(
                ["git", "-C", str(worktree_path), "config", "user.name", user_name],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(worktree_path), "config", "user.email", user_email],
                check=False,
                capture_output=True,
            )
            console.print(
                f"[dim]Configured git user for worktree: {user_name} <{user_email}>[/dim]"
            )
        else:
            console.print(
                "[yellow]Warning: Could not determine git user from current config[/yellow]"
            )

    except Exception as e:
        console.print(
            f"[yellow]Warning: Failed to configure git user for worktree: {e}[/yellow]"
        )


def cmd_restore(
    console: Console, manager: TaskAgent, slug_part: str, to_status: str = "pending"
):
    """Restore a completed issue."""
    try:
        # Search including completed to find the full slug
        issue_file = manager.find_issue_file(slug_part, include_completed=True)
        if not issue_file:
            console.print(f"[red]No issue found matching '{slug_part}'.[/red]")
            return

        # Determine slug from file name or parent dir
        is_dir_based = issue_file.name == "README.md"
        slug = issue_file.parent.name if is_dir_based else issue_file.stem

        issue = manager.restore_issue(slug, to_status=to_status)
        console.print(
            f"[bold green]Issue '{issue.slug}' restored to {to_status}.[/bold green]"
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def cmd_triage(
    console: Console, manager: TaskAgent, search_query: Optional[str] = None
):
    """Interactively prioritize and promote tasks."""
    show_completed = False

    def get_display_issues(search: Optional[str] = None, completed: bool = False):
        if completed:
            # Load completed issues from disk since they aren't in mission.usv
            all_issues = []
            for f, slug in manager.walk_completed():
                name = manager.extract_title(f)
                all_issues.append(
                    Issue(name=name, slug=slug, status="completed", priority=0)
                )
            issues = all_issues
        else:
            issues = manager.sync_mission()

        if search:
            issues = [i for i in issues if search.lower() in i.slug.lower()]
        return issues

    def build_hierarchy(issues: List[Issue]) -> List[Tuple[Issue, int]]:
        """Build a flat list with depth info for dependency hierarchy."""
        slug_to_issue = {i.slug: i for i in issues}
        children_map: Dict[str, List[str]] = {}
        for i in issues:
            for dep in i.dependencies:
                if dep in slug_to_issue:
                    if dep not in children_map:
                        children_map[dep] = []
                    children_map[dep].append(i.slug)

        visited: Set[str] = set()
        rows: List[Tuple[Issue, int]] = []

        def build_rows(issue: Issue, depth: int):
            if issue.slug in visited:
                return
            visited.add(issue.slug)
            rows.append((issue, depth))
            if issue.slug in children_map:
                for child_slug in children_map[issue.slug]:
                    if child_slug in slug_to_issue:
                        build_rows(slug_to_issue[child_slug], depth + 1)

        for issue in issues:
            has_internal_dep = any(dep in slug_to_issue for dep in issue.dependencies)
            if not has_internal_dep:
                build_rows(issue, 0)

        for issue in issues:
            if issue.slug not in visited:
                build_rows(issue, 0)

        return rows

    def find_slug_index(
        target_slug: str, indexed: List[Tuple[Issue, int]]
    ) -> Optional[int]:
        for idx, (iss, _) in enumerate(indexed):
            if iss.slug == target_slug:
                return idx
        return None

    def get_subtree_indices(indexed: List[Tuple[Issue, int]], idx: int) -> List[int]:
        if idx < 0 or idx >= len(indexed):
            return []
        base_depth = indexed[idx][1]
        res = [idx]
        for j in range(idx + 1, len(indexed)):
            if indexed[j][1] > base_depth:
                res.append(j)
            else:
                break
        return res

    def move_triage_item(
        manager: TaskAgent,
        indexed: List[Tuple[Issue, int]],
        idx: int,
        direction: str,
    ) -> Optional[str]:
        if idx < 0 or idx >= len(indexed):
            return None

        target_issue, target_depth = indexed[idx]
        target_subtask_of = target_issue.subtask_of
        target_slug = target_issue.slug

        moved_indices = get_subtree_indices(indexed, idx)
        if not moved_indices:
            return None

        sibling_idx = None
        if direction == "up":
            j = idx - 1
            while j >= 0:
                if indexed[j][1] == target_depth:
                    if indexed[j][0].subtask_of == target_subtask_of:
                        sibling_idx = j
                        break
                    else:
                        break
                elif indexed[j][1] < target_depth:
                    break
                j -= 1
        elif direction == "down":
            next_start = max(moved_indices) + 1
            j = next_start
            while j < len(indexed):
                if indexed[j][1] == target_depth:
                    if indexed[j][0].subtask_of == target_subtask_of:
                        sibling_idx = j
                        break
                    else:
                        break
                elif indexed[j][1] < target_depth:
                    break
                j += 1

        if sibling_idx is None:
            manager.prioritize_issue(target_slug, direction)
            return target_slug

        sibling_indices = get_subtree_indices(indexed, sibling_idx)
        moved_slugs = [indexed[k][0].slug for k in moved_indices]
        sibling_slugs = [indexed[k][0].slug for k in sibling_indices]

        all_issues = manager.load_mission()
        moved_set = set(moved_slugs)
        moved_objs = [i for i in all_issues if i.slug in moved_set]
        remaining = [i for i in all_issues if i.slug not in moved_set]

        sibling_set = set(sibling_slugs)
        ref_idx = -1
        if direction == "up":
            for r_i, item in enumerate(remaining):
                if item.slug in sibling_set:
                    ref_idx = r_i
                    break
        else:
            for r_i, item in enumerate(remaining):
                if item.slug in sibling_set:
                    ref_idx = r_i

        if ref_idx != -1:
            if direction == "up":
                new_issues = remaining[:ref_idx] + moved_objs + remaining[ref_idx:]
            else:
                new_issues = (
                    remaining[: ref_idx + 1] + moved_objs + remaining[ref_idx + 1 :]
                )
            for i, issue in enumerate(new_issues, 1):
                issue.priority = i
            manager.save_mission(new_issues)
            manager.sync_mission()
        else:
            manager.prioritize_issue(target_slug, direction)

        return target_slug

    issues = get_display_issues(search_query, show_completed)
    if not issues and not search_query:
        console.print("[yellow]No issues to triage.[/yellow]")
        return

    # Build hierarchy for cursor mapping
    hierarchy = build_hierarchy(issues)
    # Map flat index to (issue, depth)
    indexed_issues = [(issue, depth) for issue, depth in hierarchy]
    cursor = 0
    last_key = ""

    with Live(auto_refresh=False, console=console, screen=True) as live:
        while True:
            # Calculate viewport: reserve lines for title, header, help, borders
            term_height = getattr(getattr(console, "size", None), "height", 24)
            if not isinstance(term_height, int):
                term_height = 24
            # Table chrome: title(1) + header(1) + border lines(~4) + help subtitle(1) + panel border(2)
            chrome_lines = 10
            visible_rows = max(3, term_height - chrome_lines)

            # Compute scroll offset to keep cursor visible
            total = len(indexed_issues)
            # Clamp scroll so cursor is always within the visible window
            scroll_offset = max(0, min(cursor - visible_rows + 1, total - visible_rows))
            scroll_offset = max(0, min(scroll_offset, cursor))
            window_end = min(scroll_offset + visible_rows, total)

            # Render
            title = "[bold blue]Triage Mode[/bold blue]"
            if show_completed:
                title = "[bold magenta]Triage Mode (COMPLETED)[/bold magenta]"
            if search_query:
                title += f" [dim](Search: {search_query})[/dim]"
            if total > visible_rows:
                title += f" [dim]({scroll_offset + 1}-{window_end} of {total})[/dim]"

            table = Table(
                title=title,
                box=theme.table_box,
                show_header=True,
                header_style=theme.header_style,
                padding=theme.table_padding,
            )
            table.add_column("Pos", justify="right", style="dim")
            table.add_column("Date", style="dim", width=5)
            table.add_column("Status", width=10)
            table.add_column("Slug")

            term_width = getattr(getattr(console, "size", None), "width", 80)
            if not isinstance(term_width, int) or term_width <= 0:
                term_width = 80

            for idx in range(scroll_offset, window_end):
                issue, depth = indexed_issues[idx]
                style = "bold cyan" if idx == cursor else "white"
                indent = "  " * (depth - 1) if depth > 1 else ""
                prefix_plain = "└─ " if depth > 0 else ""
                prefix = "[dim cyan]└─[/dim cyan] " if depth > 0 else ""

                # Fixed columns width: Pos (~4) + Date (5) + Status (10) + borders/padding (~10)
                available_width = max(10, term_width - 30)
                prefix_len = len(indent) + len(prefix_plain)
                max_slug_len = max(5, available_width - prefix_len)

                slug_str = issue.slug
                if len(slug_str) > max_slug_len:
                    slug_str = slug_str[: max_slug_len - 1] + "…"

                if idx == cursor:
                    display_slug = f"[reverse]{indent}{prefix}{slug_str}[/reverse]"
                else:
                    display_slug = f"{indent}{prefix}{slug_str}"

                status_style = "white"
                if issue.status == "active":
                    status_style = "bold green"
                elif issue.status == "pending":
                    status_style = "bold yellow"
                elif issue.status == "draft":
                    status_style = "dim"
                elif issue.status == "completed":
                    status_style = "bold blue"

                created_date = get_created_date(manager, issue.slug)
                if len(created_date) >= 10:
                    created_date = created_date[5:10]

                table.add_row(
                    str(idx + 1) if not show_completed else "-",
                    f"[dim]{created_date}[/dim]",
                    f"[{status_style}]{issue.status.upper()}[/{status_style}]",
                    display_slug,
                    style=style,
                )

            help_text = "[dim]j/k: move | G/gg: bottom/top | ctrl+k/j: prio | p: prom | d: dem | v: view | e: edit | a: add | D: done | A: active | l/h: link/unlink | /: search | y: copy | q: exit[/dim]"

            live.update(
                Panel(table, subtitle=help_text, box=theme.panel_box), refresh=True
            )

            # Input
            key = get_key()
            prev_key = last_key
            last_key = ""

            if key in ["q", "\x1b"]:  # q, esc
                break
            elif key == "\r":  # enter (return)
                break
            elif key == "G":  # shift+g: jump to bottom
                cursor = len(indexed_issues) - 1
            elif key == "g":  # gg: jump to top
                if prev_key == "g":
                    cursor = 0
                else:
                    last_key = "g"
                continue
            elif key in ["k", "\x1b[A"]:  # up
                cursor = max(0, cursor - 1)
            elif key in ["j", "\x1b[B"]:  # down
                cursor = min(len(indexed_issues) - 1, cursor + 1)
            elif key == "/":
                live.stop()
                search_query = questionary.text("Search slug:").ask()
                issues = get_display_issues(search_query, show_completed)
                indexed_issues = build_hierarchy(issues)
                cursor = 0
                live.start()
            elif key == "y":
                slug = indexed_issues[cursor][0].slug
                pyperclip.copy(slug)
                console.print(
                    f"[bold green]Copied slug to clipboard: {slug}[/bold green]"
                )
                questionary.press_any_key_to_continue().ask()
            elif key == "c":
                show_completed = not show_completed
                issues = get_display_issues(search_query, show_completed)
                indexed_issues = build_hierarchy(issues)
                cursor = 0

            elif key == "v":
                live.stop()
                issue = indexed_issues[cursor][0]
                issue_file = manager.find_issue_file(
                    issue.slug, include_completed=show_completed
                )
                if issue_file:
                    render_issue(console, issue, issue_file, issues, manager=manager)
                else:
                    console.print(f"[red]Issue file not found for {issue.slug}[/red]")
                questionary.press_any_key_to_continue().ask()
                live.start()
            elif key == "e":
                live.stop()
                issue = indexed_issues[cursor][0]
                issue_file = manager.find_issue_file(
                    issue.slug, include_completed=show_completed
                )
                if issue_file:
                    editor = get_editor()
                    subprocess.run([editor, str(issue_file)])
                    manager.init_project()
                    issues = get_display_issues(search_query, show_completed)
                    indexed_issues = build_hierarchy(issues)
                else:
                    console.print(f"[red]Issue file not found for {issue.slug}[/red]")
                    questionary.press_any_key_to_continue().ask()
                live.start()
            elif key == "a" and not show_completed:
                live.stop()
                title = questionary.text("Issue title:").ask()
                if title:
                    body = questionary.text("Issue body (optional):").ask() or ""
                    draft = questionary.confirm("Create as draft?").ask()
                    try:
                        issue = manager.create_issue(title, body, draft)
                        console.print(f"[bold green]Created: {issue.slug}[/bold green]")
                        manager.init_project()
                        issues = get_display_issues(search_query, show_completed)
                        indexed_issues = build_hierarchy(issues)
                        new_idx = find_slug_index(issue.slug, indexed_issues)
                        if new_idx is not None:
                            cursor = new_idx
                    except Exception as e:
                        console.print(f"[red]Error: {e}[/red]")
                        questionary.press_any_key_to_continue().ask()
                live.start()
            elif key == "\x0b" and not show_completed:  # ctrl+k
                try:
                    target_slug = move_triage_item(
                        manager, indexed_issues, cursor, "up"
                    )
                    issues = get_display_issues(search_query, show_completed)
                    indexed_issues = build_hierarchy(issues)
                    if target_slug:
                        new_idx = find_slug_index(target_slug, indexed_issues)
                        if new_idx is not None:
                            cursor = new_idx
                except Exception:
                    pass
            elif key == "\x0a" and not show_completed:  # ctrl+j (often \n)
                try:
                    target_slug = move_triage_item(
                        manager, indexed_issues, cursor, "down"
                    )
                    issues = get_display_issues(search_query, show_completed)
                    indexed_issues = build_hierarchy(issues)
                    if target_slug:
                        new_idx = find_slug_index(target_slug, indexed_issues)
                        if new_idx is not None:
                            cursor = new_idx
                except Exception:
                    pass
            elif key == "p" and not show_completed:  # promote
                issue = indexed_issues[cursor][0]
                if issue.status == "draft":
                    try:
                        manager.promote_issue(issue.slug)
                        issues = get_display_issues(search_query, show_completed)
                        indexed_issues = build_hierarchy(issues)
                        new_idx = find_slug_index(issue.slug, indexed_issues)
                        if new_idx is not None:
                            cursor = new_idx
                    except Exception:
                        pass
            elif key == "d" and not show_completed:  # demote
                issue = indexed_issues[cursor][0]
                if issue.status in ("pending", "active"):
                    try:
                        manager.demote_issue(issue.slug)
                        issues = get_display_issues(search_query, show_completed)
                        indexed_issues = build_hierarchy(issues)
                        new_idx = find_slug_index(issue.slug, indexed_issues)
                        if new_idx is not None:
                            cursor = new_idx
                    except Exception:
                        pass
            elif key == "r" and show_completed:  # restore
                target = indexed_issues[cursor][0]
                try:
                    manager.restore_issue(target.slug, to_status="pending")
                    issues = get_display_issues(search_query, show_completed)
                    indexed_issues = build_hierarchy(issues)
                    new_idx = find_slug_index(target.slug, indexed_issues)
                    if new_idx is not None:
                        cursor = new_idx
                    else:
                        cursor = min(len(indexed_issues) - 1, cursor)
                except Exception:
                    pass
            elif key == "A" and not show_completed:  # active
                issue = indexed_issues[cursor][0]
                try:
                    manager.move_to_active(issue.slug)
                    issues = get_display_issues(search_query, show_completed)
                    indexed_issues = build_hierarchy(issues)
                    new_idx = find_slug_index(issue.slug, indexed_issues)
                    if new_idx is not None:
                        cursor = new_idx
                except Exception:
                    pass
            elif key == "D" and not show_completed:  # done
                live.stop()
                issue = indexed_issues[cursor][0]
                solution = questionary.text("Solution explanation (optional):").ask()
                try:
                    cmd_done(console, manager, issue.slug, solution=solution or None)
                    issues = get_display_issues(search_query, show_completed)
                    indexed_issues = build_hierarchy(issues)
                    cursor = min(len(indexed_issues) - 1, max(0, cursor))
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                    questionary.press_any_key_to_continue().ask()
                live.start()
            elif key == "l" and not show_completed:  # make current depend on above
                if cursor > 0:
                    current_issue = indexed_issues[cursor][0]
                    current_depth = indexed_issues[cursor][1]
                    target_issue = None

                    if current_depth == 0:
                        # Link to the root of the above tree
                        root_idx = cursor - 1
                        while root_idx >= 0 and indexed_issues[root_idx][1] > 0:
                            root_idx -= 1
                        if root_idx >= 0:
                            target_issue = indexed_issues[root_idx][0]
                    else:
                        # Link to the sibling immediately above it at the same depth
                        sibling_idx = cursor - 1
                        while (
                            sibling_idx >= 0
                            and indexed_issues[sibling_idx][1] > current_depth
                        ):
                            sibling_idx -= 1
                        if (
                            sibling_idx >= 0
                            and indexed_issues[sibling_idx][1] == current_depth
                        ):
                            target_issue = indexed_issues[sibling_idx][0]

                    if target_issue:
                        live.stop()
                        try:
                            choice = questionary.select(
                                f"Link '{current_issue.slug}' to '{target_issue.slug}' as:",
                                choices=[
                                    f"Subtask of (Hierarchy: parent '{target_issue.slug}' is blocked until child '{current_issue.slug}' is done)",
                                    f"Blocked by (Ordering: child '{current_issue.slug}' cannot start/finish until parent '{target_issue.slug}' is done)",
                                    "Cancel",
                                ],
                            ).ask()
                        except (EOFError, Exception):
                            choice = "Blocked by"

                        if choice and not choice.startswith("Cancel"):
                            try:
                                if choice.startswith("Subtask of"):
                                    # If it was blocked_by, remove to avoid redundancy
                                    if target_issue.slug in current_issue.blocked_by:
                                        manager.remove_dependency(
                                            current_issue.slug,
                                            target_issue.slug,
                                        )
                                    manager.update_subtask_of(
                                        current_issue.slug, target_issue.slug
                                    )
                                else:
                                    # Blocked by
                                    # If it was subtask_of, remove to avoid conflicts
                                    if current_issue.subtask_of == target_issue.slug:
                                        manager.update_subtask_of(
                                            current_issue.slug, None
                                        )

                                    # Check transitive dependencies recursively to avoid redundancy
                                    all_issues = manager.load_mission()

                                    def is_ancestor(ancestor_slug, desc_slug):
                                        slug_to_issue = {i.slug: i for i in all_issues}
                                        if desc_slug not in slug_to_issue:
                                            return False
                                        todo = list(
                                            slug_to_issue[desc_slug].dependencies
                                        )
                                        visited = set(todo)
                                        while todo:
                                            curr = todo.pop()
                                            if curr == ancestor_slug:
                                                return True
                                            if curr in slug_to_issue:
                                                for dep in slug_to_issue[
                                                    curr
                                                ].dependencies:
                                                    if dep not in visited:
                                                        visited.add(dep)
                                                        todo.append(dep)
                                        return False

                                    manager.add_dependency(
                                        current_issue.slug, target_issue.slug
                                    )

                                    # Remove redundant direct dependencies that are transitively covered
                                    for dep in list(current_issue.dependencies):
                                        if dep != target_issue.slug and is_ancestor(
                                            dep, target_issue.slug
                                        ):
                                            manager.remove_dependency(
                                                current_issue.slug, dep
                                            )

                                issues = get_display_issues(
                                    search_query, show_completed
                                )
                                indexed_issues = build_hierarchy(issues)
                                new_idx = find_slug_index(
                                    current_issue.slug, indexed_issues
                                )
                                if new_idx is not None:
                                    cursor = new_idx
                            except Exception as e:
                                console.print(f"[red]Error: {e}[/red]")
                                questionary.press_any_key_to_continue().ask()
                        live.start()
            elif key == "h" and not show_completed:  # remove dependency on above
                if cursor > 0:
                    current_issue = indexed_issues[cursor][0]
                    above_issue = indexed_issues[cursor - 1][0]
                    try:
                        updated = False
                        if current_issue.subtask_of == above_issue.slug:
                            manager.update_subtask_of(current_issue.slug, None)
                            updated = True
                        elif above_issue.slug in current_issue.blocked_by:
                            parents = list(above_issue.dependencies)
                            manager.remove_dependency(
                                current_issue.slug, above_issue.slug
                            )
                            # Move up: inherit the parent's dependencies
                            for parent in parents:
                                manager.add_dependency(current_issue.slug, parent)
                            updated = True

                        if updated:
                            issues = get_display_issues(search_query, show_completed)
                            indexed_issues = build_hierarchy(issues)
                            new_idx = find_slug_index(
                                current_issue.slug, indexed_issues
                            )
                            if new_idx is not None:
                                cursor = new_idx
                    except Exception as e:
                        console.print(f"[red]Error: {e}[/red]")
                        questionary.press_any_key_to_continue().ask()


def display_overview(console: Console, manager: TaskAgent):
    """Display a rich overview of the task agent state and available commands."""
    from taskagent.store_registry import mission_remote_status

    v = get_tool_version()
    repo_info = ""
    if manager.is_dual_repo and manager.mission_root:
        repo_info = (
            f" [bold magenta](Dual-Repo: {manager.mission_root.name})[/bold magenta]"
        )
    rstat = mission_remote_status(manager.mission_root, issues_root=manager.issues_root)
    if rstat["state"] == "configured":
        remote_badge = " [bold green]remote✓[/bold green]"
    elif rstat["state"] == "local_only":
        remote_badge = " [bold yellow]local-only[/bold yellow]"
    else:
        remote_badge = " [bold red]no-git[/bold red]"

    console.print(
        Panel(
            f"[bold core]Task Agent[/bold core] [dim]v{v}[/dim]{repo_info}{remote_badge}",
            expand=False,
            box=theme.panel_box,
        )
    )
    show_store_remote_status(console, manager)

    # Plan
    plan_path = manager.plan_path
    if plan_path.exists():
        plan_content = plan_path.read_text().strip()
        if plan_content:
            console.print(Markdown(plan_content))

    # Task Summary

    stats_table = Table.grid(padding=theme.table_padding)
    console.print(stats_table)
    console.print()

    # Commands Table
    table = Table(
        title="Available Commands",
        box=None,
        show_header=False,
        padding=theme.table_padding,
    )
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Description", style="white")

    commands = [
        ("next", "Show the highest priority task (try -t/--text)"),
        ("prior", "Interactively prioritize and promote tasks"),
        ("list", "List all tasks in the queue (try --json or --text)"),
        ("search", "Search for tasks by slug pattern"),
        ("new", "Create a new task"),
        ("start", "Start a task (creates branch & worktree)"),
        ("done", "Complete a task (moves file & commits)"),
        ("init", "Initialize or heal the Task Agent project"),
        ("plan", "View or edit the project plan"),
        ("push", "Push the mission repository to origin"),
        ("commit", "Commit pending changes in the active task directory"),
        (
            "eject-mission",
            "Deprecated: legacy in-repo eject (prefer ta store migrate)",
        ),
        (
            "store",
            "Machine data root / moniker / registry / migrate",
        ),
        ("", ""),  # Spacer
        ("active", "Mark a task as active without starting a worktree"),
        ("promote", "Promote a draft task to pending"),
        ("demote", "Demote a pending task back to draft"),
        ("up/down", "Adjust task priority"),
        (
            "show",
            "View task(s); --children expands dependents, --completed includes done ones",
        ),
        ("document", "Add or list secondary documents on a task"),
        ("inbox", "Cross-store inbox: list / send / ack / gc"),
        ("ingest", "Scan disk for new markdown tasks"),
        ("triage", "(alias for prior)"),
        ("prompt", "Print active task as a shell-prompt fragment (bash/zsh/Starship)"),
        ("", ""),  # Spacer
        ("init-worker", "Scaffold an autonomous sidecar worker"),
        ("init-mcp", "Register Task Agent MCP (Claude Code, Gemini CLI, etc.)"),
        ("mcp", "Run the MCP server"),
        ("mcp-api", "Display the MCP API (tools and docstrings)"),
        ("version", "Manage project versioning"),
    ]

    for cmd, desc in commands:
        table.add_row(cmd, desc)

    console.print(table)
    console.print(
        "\n[dim]Run [bold]ta <command> --help[/bold] for detailed options.[/dim]"
    )


def main():
    if "LESS" not in os.environ:
        os.environ["LESS"] = "RFX"
    parser = argparse.ArgumentParser(description="Task Agent CLI")
    parser.add_argument("-V", "--version", action="store_true")
    parser.add_argument("-C", "--config-dir")
    subparsers = parser.add_subparsers(dest="command")

    next_parser = subparsers.add_parser("next", help="Show the top issue")
    next_parser.add_argument(
        "-t",
        "--text",
        action="store_true",
        help="Output plain text without using a pager",
    )
    subparsers.add_parser("init", help="Initialize or heal the project")
    triage_parser = subparsers.add_parser(
        "triage", help="Interactively prioritize and promote tasks"
    )
    triage_parser.add_argument(
        "search", nargs="?", help="Optional search query to filter by slug"
    )
    search_parser = subparsers.add_parser(
        "search", help="Search for tasks by slug pattern"
    )
    search_parser.add_argument(
        "pattern", help="Pattern to match against slug (wildcard end)"
    )
    prior_parser = subparsers.add_parser(
        "prior", help="Interactively prioritize and promote tasks"
    )
    prior_parser.add_argument(
        "search", nargs="?", help="Optional search query to filter by slug"
    )

    restore_parser = subparsers.add_parser("restore", help="Restore a completed issue")
    restore_parser.add_argument("slug", help="Slug (or partial slug) of the issue")
    restore_parser.add_argument(
        "-s",
        "--status",
        choices=["pending", "draft", "active"],
        default="pending",
        help="Target status (default: pending)",
    )

    report_parser = subparsers.add_parser(
        "report", help="View metadata/logs for a task"
    )
    report_parser.add_argument("slug", help="Slug of the issue")

    show_parser = subparsers.add_parser(
        "show",
        help="View task(s) README and secondary Markdown documents",
    )
    show_parser.add_argument(
        "slug",
        nargs="+",
        help="Slug(s) or title(s) of the issue(s) to show",
    )
    show_parser.add_argument(
        "--children",
        "-c",
        action="store_true",
        help=(
            "Also show all transitive dependents (subtasks and tasks "
            "blocked by the given task(s))"
        ),
    )
    show_parser.add_argument(
        "--completed",
        action="store_true",
        help="When used with --children, include completed dependents",
    )

    document_parser = subparsers.add_parser(
        "document",
        aliases=["doc"],
        help="Add or list secondary Markdown documents on a task",
    )
    document_sub = document_parser.add_subparsers(dest="document_command")
    doc_add = document_sub.add_parser(
        "add", help="Add a secondary Markdown document to a task folder"
    )
    doc_add.add_argument("slug", help="Slug or title of the issue")
    doc_add.add_argument(
        "filename",
        help="Document basename (e.g. findings.md); .md appended if omitted",
    )
    doc_add.add_argument(
        "-b",
        "--body",
        default="",
        help="Document content (Markdown)",
    )
    doc_add.add_argument(
        "-f",
        "--file",
        help="Read document content from a file path",
    )
    doc_add.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing document with the same name",
    )
    doc_list = document_sub.add_parser(
        "list", help="List secondary Markdown documents on a task"
    )
    doc_list.add_argument("slug", help="Slug or title of the issue")

    inbox_parser = subparsers.add_parser(
        "inbox",
        help="Ack-gated inbox messaging between stores (shared filesystem)",
    )
    inbox_sub = inbox_parser.add_subparsers(dest="inbox_command")
    inbox_list = inbox_sub.add_parser("list", help="List unread messages")
    inbox_list.add_argument(
        "--thread",
        help="Only messages with this thread (task slug)",
    )
    inbox_show = inbox_sub.add_parser("show", help="Show one unread message")
    inbox_show.add_argument("id", help="Message id (or unique prefix)")
    inbox_send = inbox_sub.add_parser(
        "send", help="Deliver a message to another store's inbox/unread/"
    )
    inbox_send.add_argument(
        "--to",
        required=True,
        help="Target store moniker/host fragment (fuzzy, e.g. task-agent)",
    )
    inbox_send.add_argument(
        "-b",
        "--body",
        default="",
        help="Message body (Markdown)",
    )
    inbox_send.add_argument(
        "-f",
        "--file",
        help="Read body from a file",
    )
    inbox_send.add_argument(
        "--kind",
        default="info",
        choices=[
            "task-created",
            "question",
            "update",
            "comment",
            "ack-request",
            "info",
        ],
        help="Message kind (default: info)",
    )
    inbox_send.add_argument(
        "--thread",
        help="Optional task slug this message is about",
    )
    inbox_send.add_argument(
        "--task",
        help="Local task slug/title to embed as a snapshot (+ default thread)",
    )
    inbox_send.add_argument(
        "--from",
        dest="sender",
        default=None,
        help="Override sender moniker (default: current store/host moniker)",
    )
    inbox_ack = inbox_sub.add_parser(
        "ack",
        help="Mark a message read (move to read/YYYY/MM/DD/); optional --start",
    )
    inbox_ack.add_argument("id", help="Message id (or unique prefix)")
    inbox_ack.add_argument(
        "--start",
        action="store_true",
        help=(
            "After ack, mark the linked task (task/thread frontmatter) active "
            "in this store"
        ),
    )
    inbox_gc = inbox_sub.add_parser(
        "gc",
        help="Delete read/ day dirs older than retention (name-only, no file opens)",
    )
    inbox_gc.add_argument(
        "--days",
        type=int,
        default=None,
        help=f"Retention days (default: {7})",
    )
    inbox_gc.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without removing",
    )

    subparsers.add_parser(
        "dashboard", help="Show a live dashboard of all task stations"
    )
    list_parser = subparsers.add_parser("list")
    subparsers.add_parser("tree", help="Display task hierarchy as a dependency tree")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--text", action="store_true")
    list_parser.add_argument(
        "--repo",
        help="Fuzzy moniker/host match for another registered store (zoxide-style)",
    )
    history_parser = subparsers.add_parser("history")
    history_parser.add_argument(
        "-n", "--limit", type=int, default=20, help="Number of items to show"
    )
    subparsers.add_parser(
        "recover-history",
        help="Recover deleted task files from git history and recover task creation dates into frontmatter",
    )
    subparsers.add_parser("ingest")
    subparsers.add_parser("mcp-api", help="List available MCP tools and API")
    subparsers.add_parser("self-up")
    worktree_parser = subparsers.add_parser(
        "worktree", help="Manage git worktrees with advanced features"
    )
    worktree_parser.add_argument(
        "action",
        nargs="?",
        choices=["add", "list", "remove", "prune"],
        help="Worktree action to perform (shows help if omitted)",
    )
    worktree_parser.add_argument(
        "target", nargs="?", help="Branch, tag, or commit SHA (for add action)"
    )
    worktree_parser.add_argument(
        "--tag", action="store_true", help="Create worktree from tag instead of branch"
    )
    worktree_parser.add_argument(
        "--commit", action="store_true", help="Create worktree from specific commit SHA"
    )
    worktree_parser.add_argument(
        "--copy",
        action="append",
        help="Glob patterns to copy to worktree (can be specified multiple times)",
    )
    worktree_parser.add_argument(
        "--permissions",
        help="Octal permissions for worktree directory (e.g., 700, 755)",
    )
    worktree_parser.add_argument(
        "--no-symlinks", action="store_true", help="Do not copy symlinks to worktree"
    )
    worktree_parser.add_argument(
        "--no-env", action="store_true", help="Do not copy .env files to worktree"
    )

    # GitHub integration
    github_parser = subparsers.add_parser("github", help="Sync with GitHub Issues")
    github_sub = github_parser.add_subparsers(dest="github_command")

    sync_parser = github_sub.add_parser("sync", help="Import issues from GitHub")
    sync_parser.add_argument("--repo", help="Repository (owner/repo) override")

    push_parser = github_sub.add_parser("push", help="Push task to create GitHub issue")
    push_parser.add_argument("slug", help="Task slug to push as GitHub issue")

    # Add other commands as needed

    up_parser = subparsers.add_parser("up")
    up_parser.add_argument("slug")
    down_parser = subparsers.add_parser("down")
    down_parser.add_argument("slug")
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("slug")
    demote_parser = subparsers.add_parser("demote")
    demote_parser.add_argument("slug")
    active_parser = subparsers.add_parser(
        "active",
        help="Move an issue to active status, or list active tasks if no slug is provided",
    )
    active_parser.add_argument(
        "slug", nargs="?", help="Optional slug of the task to mark as active"
    )
    prompt_parser = subparsers.add_parser(
        "prompt",
        help=(
            "Print active task as a shell-prompt fragment — fast, no network, "
            "suitable for $PROMPT_COMMAND or Starship custom modules"
        ),
    )
    prompt_parser.add_argument(
        "--format",
        choices=["default", "text", "json"],
        default="default",
        help=(
            "Output format: 'default' → [ta:slug], "
            "'text' → slug only, "
            '\'json\' → {"active":"slug"}'
        ),
    )
    prompt_parser.add_argument(
        "--pending",
        action="store_true",
        default=False,
        help="Also include the count of pending tasks in the output",
    )
    update_parser = subparsers.add_parser(
        "update",
        help="Update task relationships (blocked_by / parent) for one or many tasks",
    )
    update_parser.add_argument(
        "slug",
        help=(
            "Slug of the task to update, or comma-separated slugs for bulk "
            "(e.g. task-a,task-b,task-c)"
        ),
    )
    update_parser.add_argument(
        "--blocked-by",
        help="Replace blocked_by with this comma-separated list (empty string clears / removes property)",
    )
    update_parser.add_argument(
        "--add-blocked-by",
        help="Append these comma-separated blocker slugs without replacing existing ones",
    )
    update_parser.add_argument(
        "--remove-blocked-by",
        help="Remove these comma-separated blocker slugs (removes property if last)",
    )
    update_parser.add_argument(
        "--subtask-of",
        help="Slug of the parent task this task is a subtask of (use empty string to clear)",
    )
    rename_parser = subparsers.add_parser(
        "rename",
        help="Safely rename a task slug and update references across the project",
    )
    rename_parser.add_argument(
        "slug",
        help="Current slug or title of the task to rename",
    )
    rename_parser.add_argument(
        "new_title",
        help="New title for the task (generates the new slug)",
    )
    start_parser = subparsers.add_parser(
        "start",
        help="Activate a task and set up its git worktree/branch",
        description="""
Start working on a task. This command automates the following workflow:
  1. Marks the task (identified by its slug) as ACTIVE in the task manager.
     If no slug is provided, prompts you to select one from pending tasks.
  2. Creates a new git branch named 'issue/<slug>' (branched from main/current).
  3. Creates and checks out a new git worktree located at '.gwt/<slug>'.
  4. If --agent is specified:
     - If it matches a template in '.ta/agents/<agent_name>/meta.toml',
       creates a dedicated per-task agent user 'agent-<slug>-<agent_name>'.
     - Otherwise, configures worktree permissions for an existing agent user.
  5. If --run is specified, immediately runs the sidecar worker on the worktree.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    start_parser.add_argument(
        "slug", nargs="?", help="Slug or partial slug of the task to start"
    )
    start_parser.add_argument(
        "--run", action="store_true", help="Immediately run the sidecar worker"
    )
    start_parser.add_argument(
        "--agent",
        help="Template name (creates per-task agent) or existing agent user name",
    )
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("slug", nargs="?")
    run_parser.add_argument(
        "--agent",
        help="Template name (creates per-task agent) or existing agent user name",
    )
    init_parser = subparsers.add_parser("init-worker")

    init_agent_parser = subparsers.add_parser(
        "init-agent", help="Create a dedicated Linux user for agent isolation"
    )
    init_agent_parser.add_argument(
        "name", nargs="?", help="Agent name (creates user agent-<name>)"
    )
    init_agent_parser.add_argument(
        "--template",
        help="Template name from .ta/agents/<name>/meta.toml",
    )
    init_agent_parser.add_argument(
        "--list-templates",
        action="store_true",
        help="List available agent templates",
    )
    init_agent_parser.add_argument(
        "--op-timeout",
        type=int,
        default=30,
        help="Timeout in seconds for 1Password CLI operations (default: 30)",
    )

    destroy_agent_parser = subparsers.add_parser(
        "destroy-agent",
        help="Remove an agent Linux user created by init-agent",
    )
    destroy_agent_parser.add_argument("name", help="Agent name to remove")
    init_parser.add_argument("--template", default="adk")

    # mcp
    subparsers.add_parser("mcp", help="Run the Model Context Protocol server")

    # init-mcp
    init_mcp_parser = subparsers.add_parser(
        "init-mcp",
        help=(
            "Register Task Agent as an MCP server "
            "(Claude Code, GitHub Copilot, Antigravity/agy, Gemini CLI, OpenCode)"
        ),
    )
    init_mcp_parser.add_argument(
        "--claude",
        action="store_true",
        help="Register with Claude Code (via 'claude mcp add')",
    )
    init_mcp_parser.add_argument(
        "--copilot",
        action="store_true",
        help="Register globally with GitHub Copilot CLI (via 'copilot mcp add')",
    )
    init_mcp_parser.add_argument(
        "--agy",
        action="store_true",
        help=(
            "Register with Antigravity CLI (writes mcp_config.json; "
            "defaults to user scope ~/.gemini/antigravity-cli/)"
        ),
    )
    init_mcp_parser.add_argument(
        "--opencode",
        action="store_true",
        help=(
            "Register globally with OpenCode (writes opencode.json; "
            "defaults to user scope ~/.config/opencode/opencode.json)"
        ),
    )
    init_mcp_parser.add_argument(
        "--agent",
        choices=["gemini", "opencode"],
        default="gemini",
        help="MCP agent to configure (default: gemini)",
    )
    init_mcp_parser.add_argument(
        "--print", action="store_true", help="Print MCP configuration JSON"
    )
    init_mcp_parser.add_argument(
        "--scope",
        choices=["project", "user"],
        default="project",
        help=(
            "Registration scope (default: project; for --agy and OpenCode, default becomes "
            "user unless --scope is passed explicitly)"
        ),
    )

    # init-plugin
    init_plugin_parser = subparsers.add_parser(
        "init-plugin",
        help="Install task-agent plugin package, skills, and MCP server for host agent CLIs",
    )
    init_plugin_parser.add_argument(
        "--claude",
        action="store_true",
        help="Install plugin package for Claude Code",
    )
    init_plugin_parser.add_argument(
        "--agy",
        action="store_true",
        help="Install plugin package for Antigravity CLI",
    )
    init_plugin_parser.add_argument(
        "--scope",
        choices=["project", "user"],
        default="user",
        help="Installation scope (default: user)",
    )

    # agents
    agents_parser = subparsers.add_parser(
        "agents",
        help="Inspect installed agent CLIs and task-agent MCP/plugin integration status",
    )
    agents_parser.add_argument(
        "agent_name",
        nargs="?",
        default=None,
        help="Specific agent CLI ID to inspect (e.g. claude, agy, opencode, copilot)",
    )
    agents_parser.add_argument(
        "--json",
        action="store_true",
        help="Output inspection data as JSON",
    )

    # agent
    agent_parser = subparsers.add_parser(
        "agent",
        help="Agent management and task integration",
    )
    agent_sub = agent_parser.add_subparsers(
        dest="agent_subcommand", help="Agent subcommand"
    )
    agent_import_p = agent_sub.add_parser(
        "import",
        help="Import tasks from external AI agent task runners into working task",
    )
    agent_import_p.add_argument(
        "--slug",
        default=None,
        help="Target working task slug (resolves active working task if omitted)",
    )
    agent_import_p.add_argument(
        "--agent",
        default="antigravity",
        help="Agent system moniker (e.g. antigravity, claude-code, generic)",
    )
    agent_import_p.add_argument(
        "--file",
        default=None,
        help="Path to agent task state file to import",
    )
    agent_import_p.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON",
    )
    agent_recent_p = agent_sub.add_parser(
        "recent",
        aliases=["last-used"],
        help="List AI coding agents recently active in this repository",
    )
    agent_recent_p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Target repository directory (defaults to cwd)",
    )
    agent_recent_p.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum number of active agents to return (default: 5)",
    )
    agent_recent_p.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON",
    )

    # plan
    subparsers.add_parser("plan", help="View or edit the project plan")

    # strategy
    strategy_parser = subparsers.add_parser(
        "strategy",
        help="View, edit, or initialize the project strategy",
        description="""
Manage the project's strategic direction document.

The strategy is a concise statement of the project's current direction, goals,
and priorities. It is displayed periodically at the top of 'list', 'next', and
'active' commands to keep all workers aligned.

Usage:
  ta strategy                   View the current strategy
  ta strategy edit              Open the strategy in your $EDITOR
  ta strategy init              Create a starter strategy file
  ta strategy cooldown          Show the display cooldown (hours)
  ta strategy cooldown <hours>  Set the display cooldown (0 = every time)

The cooldown can also be overridden per-shell with the
TA_STRATEGY_COOLDOWN_HOURS environment variable.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    strategy_parser.add_argument(
        "action",
        nargs="?",
        choices=["edit", "init", "cooldown"],
        help="Action to perform (default: view)",
    )
    strategy_parser.add_argument(
        "value",
        nargs="?",
        help="Hours for 'cooldown' (e.g. 0.5; 0 shows the strategy every time)",
    )

    # push
    subparsers.add_parser("push", help="Push the mission repository to origin")

    # commit - commit changes to task-agent's own tasks or the host project's tasks
    commit_parser = subparsers.add_parser(
        "commit", help="Commit changes in the tasks directory"
    )
    commit_parser.add_argument(
        "target",
        choices=["repo", "tasks"],
        help="'repo' commits the current project's tasks, 'tasks' commits the task-agent's own tasks",
    )
    commit_parser.add_argument(
        "-m", "--message", help="Commit message (default: auto-generated)"
    )
    commit_parser.add_argument(
        "--no-push",
        dest="push",
        action="store_false",
        help="Do not push to remote",
    )
    commit_parser.set_defaults(push=True)

    # mr
    mr_parser = subparsers.add_parser("mr", help="Manage merge requests from workers")
    mr_sub = mr_parser.add_subparsers(dest="mr_command")
    mr_sub.add_parser("list", help="List pending merge requests")

    # merge
    merge_parser = subparsers.add_parser(
        "merge", help="Merge a task completion datagram"
    )
    merge_parser.add_argument("slug", help="Slug of the task to merge")
    merge_parser.add_argument("-m", "--message", help="Git commit message")
    merge_parser.add_argument(
        "--push", action="store_true", help="Push mission repo after merge"
    )

    # store — machine data root / moniker / registry (Phase 1: no migration)
    store_parser = subparsers.add_parser(
        "store",
        help="Inspect machine-level task store layout (data root, moniker, registry)",
    )
    store_sub = store_parser.add_subparsers(dest="store_command")
    store_sub.add_parser(
        "data-root",
        help="Print the machine-wide task-agent data home (~/.local/share/task-agent)",
    )
    path_p = store_sub.add_parser(
        "path",
        help="Print this project's task store directory (data-root/stores/<moniker>)",
    )
    path_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    path_p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: current directory)",
    )
    moniker_p = store_sub.add_parser(
        "moniker", help="Print the moniker for a host path (default: cwd)"
    )
    moniker_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    moniker_p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: current directory)",
    )
    symlink_p = store_sub.add_parser(
        "symlink",
        help=(
            "Manage human-facing docs/tasks → store symlink "
            "(on ensures .gitignore; fails if a real docs/tasks exists)"
        ),
    )
    symlink_p.add_argument(
        "symlink_action",
        nargs="?",
        choices=["on", "off", "status"],
        default="status",
        help="on | off | status (default: status)",
    )
    symlink_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    symlink_p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: cwd)",
    )
    store_sub.add_parser("list", help="List registered machine task stores")
    inspect_p = store_sub.add_parser(
        "inspect",
        help="Read-only inspect: moniker, legacy store, migration status",
    )
    inspect_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    inspect_p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: current directory)",
    )
    inspect_p.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    store_sub.add_parser(
        "rebuild-index",
        help="Rebuild registry.json by scanning stores/ under the data root",
    )
    migrate_p = store_sub.add_parser(
        "migrate",
        help="Move legacy .task-agent/tasks into the machine data root store",
    )
    migrate_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    migrate_p.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: current directory)",
    )
    migrate_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; do not move data or rewrite pointers",
    )
    migrate_p.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON"
    )
    remote_parser = store_sub.add_parser(
        "remote",
        help="Show, suggest, or set the git remote for a task store",
    )
    remote_sub = remote_parser.add_subparsers(dest="remote_command")
    remote_show = remote_sub.add_parser(
        "show", help="List git remotes on the current project's task store"
    )
    remote_show.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    remote_show.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: cwd)",
    )
    remote_suggest = remote_sub.add_parser(
        "suggest",
        help="Suggest sibling *-tasks remotes via forge plugins",
    )
    remote_suggest.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    remote_suggest.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: cwd)",
    )
    remote_create = remote_sub.add_parser(
        "create",
        help=(
            "Create a sibling *-tasks repo via forge API (plugin) and attach; "
            "visibility defaults to match the subject repo"
        ),
    )
    remote_create.add_argument(
        "--private",
        action="store_true",
        help="Force private tasks repo (default: match subject visibility)",
    )
    remote_create.add_argument(
        "--public",
        action="store_true",
        help="Force public tasks repo (default: match subject visibility)",
    )
    remote_create.add_argument(
        "--name",
        default=None,
        help="Tasks repo full name owner/repo-tasks (default: {subject}-tasks)",
    )
    remote_create.add_argument(
        "--provider",
        default=None,
        help="Forge plugin name (default: auto from subject origin; e.g. github)",
    )
    remote_create.add_argument(
        "--no-attach",
        action="store_true",
        help="Create/set URL only; do not fetch/push publish",
    )
    remote_create.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; do not call forge API or attach",
    )
    remote_create.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim) instead of cwd/path",
    )
    remote_create.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: cwd)",
    )
    remote_set = remote_sub.add_parser(
        "set",
        help="Set the store's git remote URL only (no fetch/push)",
    )
    remote_set.add_argument("url", help="Git remote URL")
    remote_set.add_argument(
        "--name",
        default="origin",
        help="Remote name (default: origin)",
    )
    remote_set.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    remote_set.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: cwd)",
    )
    remote_attach = remote_sub.add_parser(
        "attach",
        help=(
            "Connect store to an existing remote and publish: rename mismatched "
            "remote tips for comparison, push main, set default branch"
        ),
    )
    remote_attach.add_argument("url", help="Git remote URL")
    remote_attach.add_argument(
        "--name",
        default="origin",
        help="Remote name (default: origin)",
    )
    remote_attach.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; set-url may still run, no fetch/push publish",
    )
    remote_attach.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    remote_attach.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Host project path (default: cwd)",
    )
    rebind_p = store_sub.add_parser(
        "rebind",
        help="Rebind store moniker after subject repo rename",
    )
    rebind_p.add_argument(
        "moniker",
        nargs="?",
        default=None,
        help="New moniker (default: derive from current host origin)",
    )
    rebind_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. nvim)",
    )
    rebind_p.add_argument(
        "--path",
        default=None,
        help="Host project path (default: cwd)",
    )
    keet_p = store_sub.add_parser(
        "keet",
        help="Manage secret Keet P2P room URI binding for a task store",
    )
    keet_p.add_argument(
        "action",
        nargs="?",
        choices=["show", "set", "unset"],
        default="show",
        help="Action: show (default), set <uri>, or unset",
    )
    keet_p.add_argument(
        "uri",
        nargs="?",
        default=None,
        help="Keet room URI (keet://chat/...) when setting",
    )
    keet_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. InTEGr8or/task-agent)",
    )
    matrix_p = store_sub.add_parser(
        "matrix",
        help="Manage secret Matrix Room or Space ID binding for a task store",
    )
    matrix_p.add_argument(
        "action",
        nargs="?",
        choices=["show", "set", "unset", "space", "token"],
        default="show",
        help="Action: show (default), set <room_id>, unset, space, or token",
    )
    matrix_p.add_argument(
        "room_id",
        nargs="?",
        default=None,
        help="Matrix Room or Space ID when setting",
    )
    matrix_p.add_argument(
        "extra_arg",
        nargs="?",
        default=None,
        help="Space link argument (e.g. ta store matrix space set '<link>')",
    )
    matrix_p.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match (e.g. InTEGr8or/task-agent)",
    )
    store_yazi_p = store_sub.add_parser(
        "yazi",
        help="Open Yazi terminal file manager in the active task store directory",
    )
    store_yazi_p.add_argument(
        "--path",
        default=None,
        help="Host project path (default: cwd)",
    )

    delete_parser = subparsers.add_parser(
        "delete", help="Soft-delete a task (archive without commit, restorable)"
    )
    delete_parser.add_argument("slug")
    done_parser = subparsers.add_parser("done")
    done_parser.add_argument("slug", nargs="?")
    done_parser.add_argument("-m", "--message")
    done_parser.add_argument("-s", "--solution", help="Solution explanation")
    done_parser.add_argument(
        "--push", action="store_true", help="Push the mission repo after completion"
    )
    done_parser.add_argument(
        "--no-verify",
        action="store_true",
        default=True,
        help="Skip running git pre-commit hooks (default)",
    )
    done_parser.add_argument(
        "--hooks",
        dest="no_verify",
        action="store_false",
        help="Force running git pre-commit hooks",
    )
    # Optional agent self-report for cost optimization
    done_parser.add_argument(
        "--model", help="Primary model id used (e.g. claude-opus-4)"
    )
    done_parser.add_argument("--model-version", help="Model version / snapshot id")
    done_parser.add_argument(
        "--provider", help="Provider (anthropic, openai, xai, google, …)"
    )
    done_parser.add_argument(
        "--agent-harness",
        help="Agent harness (claude-code, codex, cursor, grok, antigravity, …)",
    )
    done_parser.add_argument(
        "--input-tokens", type=int, help="Prompt/context tokens consumed"
    )
    done_parser.add_argument(
        "--output-tokens", type=int, help="Completion tokens produced"
    )
    done_parser.add_argument(
        "--tokens-accuracy",
        choices=["measured", "estimated", "unknown"],
        help="Whether token counts are measured or estimated",
    )
    done_parser.add_argument(
        "--duration-seconds", type=float, help="Wall-clock seconds spent on the task"
    )
    done_parser.add_argument(
        "--cost-usd", type=float, help="Estimated or billed cost in USD"
    )
    done_parser.add_argument("--started-at", help="ISO-8601 start time of agent work")
    done_parser.add_argument("--ended-at", help="ISO-8601 end time of agent work")
    done_parser.add_argument("--metrics-notes", help="Free-form cost-relevant notes")

    path_parser = subparsers.add_parser("path", help="Get the absolute path to a task")
    path_parser.add_argument("slug", help="Task slug")

    yazi_parser = subparsers.add_parser(
        "yazi", help="Open Yazi terminal file manager in the active store directory"
    )
    yazi_parser.add_argument(
        "--path", default=None, help="Host project path (default: cwd)"
    )

    new_parser = subparsers.add_parser("new")
    new_parser.add_argument("title", nargs="?")
    new_parser.add_argument("-b", "--body", default="")
    new_parser.add_argument("-c", "--criteria", help="Completion criteria")
    new_parser.add_argument("-d", "--draft", action="store_true")
    new_parser.add_argument(
        "--file",
        action="store_true",
        help="Create as single file instead of folder (default: folder)",
    )
    new_parser.add_argument(
        "--dir", action="store_true", help="Create as folder (default)"
    )
    new_parser.add_argument(
        "-i", "--interactive", action="store_true", help="Open editor to fill in task"
    )
    new_parser.add_argument(
        "--blocked-by",
        help="Comma-separated slugs of prerequisite tasks that block this task, e.g. 'setup-ci,build-artifacts'.",
    )
    new_parser.add_argument(
        "--subtask-of",
        help="Slug of the parent task this task is a subtask of, e.g. 'cli-consolidation'.",
    )
    new_parser.add_argument(
        "--bulk",
        help="Path to a JSON file containing an array of task definitions, or '-' to read JSON from stdin.",
    )
    new_parser.add_argument(
        "--repo",
        help=(
            "Create in another registered store: fuzzy moniker or host path "
            "(e.g. 'stations', 'InTEGr8or/task-agent'). Does not touch the current mission."
        ),
    )
    version_parser = subparsers.add_parser(
        "version",
        help="Show version, promote, tag, or run a full release",
    )
    v_sub = version_parser.add_subparsers(dest="version_command")
    p_v = v_sub.add_parser(
        "promote",
        help=(
            "Bump semver and commit it (amends only if HEAD is unpushed/untagged; "
            "otherwise creates chore(release): vX.Y.Z)"
        ),
    )
    p_v.add_argument("part", choices=["major", "minor", "patch"])
    tag_parser = v_sub.add_parser(
        "tag",
        help="Tag HEAD as vX.Y.Z from committed version; push branch then tag",
    )
    tag_parser.add_argument(
        "--no-push",
        dest="push",
        action="store_false",
        help="Create the local tag only (do not push branch or tag)",
    )
    tag_parser.add_argument(
        "--no-push-branch",
        dest="push_branch",
        action="store_false",
        help="When pushing, push only the tag (not the branch)",
    )
    tag_parser.set_defaults(push=True, push_branch=True)
    release_parser = v_sub.add_parser(
        "release",
        help="Atomic promote + tag + push branch + push tag",
    )
    release_parser.add_argument("part", choices=["major", "minor", "patch"])
    release_parser.add_argument(
        "--no-push",
        dest="push",
        action="store_false",
        help="Promote and tag locally only",
    )
    release_parser.set_defaults(push=True, push_branch=True)

    perf_parser = subparsers.add_parser(
        "perf",
        help="Manage performance monitoring logging (status, enable, disable, log)",
    )
    perf_parser.add_argument(
        "action",
        nargs="?",
        choices=["status", "on", "off", "enable", "disable", "log", "logs"],
        default="status",
        help="Action: status (default), on/enable, off/disable, or log/logs",
    )

    log_parser = subparsers.add_parser(
        "log",
        help="Inspect git log history for task station store path",
    )
    log_parser.add_argument(
        "--repo",
        default=None,
        help="Fuzzy moniker/host match for task station store path",
    )
    log_parser.add_argument(
        "extra_args",
        nargs=argparse.REMAINDER,
        help="Flags and arguments forwarded directly to git log",
    )

    args, unknown_args = parser.parse_known_args()
    console = Console()
    if args.version:
        display_version_info(console)
        return

    try:
        manager = discover(Path(args.config_dir) if args.config_dir else None)
    except (RuntimeError, ValueError, OSError) as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)

    # Unread inbox banner (idempotent; never mutates). Skip noisy/server cmds.
    if args.command not in (
        "mcp",
        "mcp-api",
        "version",
        "self-up",
        "init-mcp",
        "prompt",
        "perf",
    ):
        maybe_show_inbox_banner(console, manager)
        notify_perf_logging_if_enabled(console, manager.issues_root)

    if args.command == "prompt":
        cmd_prompt(
            manager,
            fmt=getattr(args, "format", "default"),
            pending_count=getattr(args, "pending", False),
        )
    elif args.command == "yazi":
        cmd_yazi(console, manager, args)
    elif args.command == "path":
        issue_file = manager.find_issue_file(args.slug)
        if issue_file:
            print(issue_file.absolute())
        else:
            console.print(f"[red]Task '{args.slug}' not found.[/red]")
            sys.exit(1)
    elif args.command == "next":
        cmd_next(console, manager, text_mode=getattr(args, "text", False))
    elif args.command == "init":
        cmd_init(console, manager)
    elif args.command == "triage":
        cmd_triage(console, manager, search_query=args.search)
    elif args.command == "prior":
        cmd_triage(console, manager, search_query=args.search)
    elif args.command == "search":
        cmd_search(console, manager, args.pattern)
    elif args.command == "restore":
        cmd_restore(console, manager, args.slug, to_status=args.status)
    elif args.command == "report":
        cmd_report(console, manager, args.slug)
    elif args.command == "dashboard":
        cmd_dashboard(console, manager)
    elif args.command == "list":
        fmt = "table"
        if args.json:
            fmt = "json"
        elif args.text:
            fmt = "text"
        cmd_list(
            console,
            manager,
            fmt,
            repo=getattr(args, "repo", None),
        )
    elif args.command == "tree":
        cmd_tree(console, manager)
    elif args.command == "history":
        cmd_history(console, manager, args.limit)
    elif args.command == "recover-history":
        cmd_recover_history(console, manager)
    elif args.command == "ingest":
        cmd_ingest(console, manager)
    elif args.command == "show":
        cmd_show(
            console,
            manager,
            args.slug,
            children=bool(getattr(args, "children", False)),
            include_completed=bool(getattr(args, "completed", False)),
        )
    elif args.command in ("document", "doc"):
        cmd_document(console, manager, args)
    elif args.command == "inbox":
        cmd_inbox(console, manager, args)
    elif args.command == "mcp-api":
        cmd_mcp_api(console)
    elif args.command == "self-up":
        cmd_self_up(console)
    elif args.command == "perf":
        cmd_perf(console, manager, action=args.action)
    elif args.command == "up":
        cmd_prioritize(console, manager, args.slug, "up")
    elif args.command == "down":
        cmd_prioritize(console, manager, args.slug, "down")
    elif args.command == "promote":
        cmd_promote(console, manager, args.slug)
    elif args.command == "demote":
        cmd_demote(console, manager, args.slug)
    elif args.command == "active":
        cmd_active(console, manager, args.slug, list_if_none=True)
    elif args.command == "update":
        cmd_update(
            console,
            manager,
            args.slug,
            blocked_by=args.blocked_by,
            subtask_of=args.subtask_of,
            add_blocked_by=args.add_blocked_by,
            remove_blocked_by=args.remove_blocked_by,
        )
    elif args.command == "rename":
        cmd_rename(
            console,
            manager,
            args.slug,
            args.new_title,
        )

    elif args.command == "start":
        cmd_start(console, manager, args.slug, run=args.run, agent_name=args.agent)
    elif args.command == "run":
        cmd_run(console, manager, args.slug, agent_name=args.agent)
    elif args.command == "init-agent":
        if args.list_templates:
            cmd_list_templates(console)
        elif not args.name:
            init_agent_parser.error("the following arguments are required: name")
        else:
            cmd_init_agent(
                console, args.name, template=args.template, op_timeout=args.op_timeout
            )
    elif args.command == "destroy-agent":
        cmd_destroy_agent(console, args.name)
    elif args.command == "init-worker":
        cmd_init_worker(console, args.template)
    elif args.command == "mcp":
        cmd_mcp()
    elif args.command == "init-mcp":
        # --agy and OpenCode prefer user-global CLI config unless the user passed --scope.
        agy = bool(getattr(args, "agy", False))
        opencode = bool(getattr(args, "opencode", False))
        agent = "opencode" if opencode else args.agent
        scope = args.scope
        if (agy or agent == "opencode") and "--scope" not in sys.argv:
            scope = "user"
        cmd_init_mcp(
            console,
            agent=agent,
            print_json=args.print,
            scope=scope,
            claude=args.claude,
            agy=agy,
            copilot=args.copilot,
            opencode=opencode,
        )
    elif args.command == "init-plugin":
        cmd_init_plugin(
            console,
            claude=args.claude,
            agy=args.agy,
            scope=args.scope,
        )
    elif args.command == "log":
        extra = unknown_args + (args.extra_args if hasattr(args, "extra_args") else [])
        cmd_log(console, manager, extra_args=extra, repo=args.repo)
    elif args.command == "agents":
        cmd_agents_list(console, agent_id=args.agent_name, json_format=args.json)
    elif args.command == "agent":
        if args.agent_subcommand == "import":
            cmd_agent_import(
                console,
                manager,
                slug=args.slug,
                agent_type=args.agent,
                file_path=args.file,
                json_format=args.json,
            )
        elif args.agent_subcommand in ("recent", "last-used"):
            cmd_agent_last_used(
                console,
                path_arg=args.path,
                limit=args.limit,
                json_format=args.json,
            )
        else:
            console.print(
                "[yellow]Unknown agent subcommand. Use 'ta agent import' or 'ta agent recent'.[/yellow]"
            )
    elif args.command == "push":
        cmd_push(console, manager)
    elif args.command == "plan":
        cmd_plan(console, manager)
    elif args.command == "strategy":
        cmd_strategy(console, manager, action=args.action, value=args.value)
    elif args.command == "commit":
        if args.target == "repo":
            cmd_commit(console, manager, message=args.message, should_push=args.push)
        elif args.target == "tasks":
            cmd_commit_tasks(console, message=args.message, should_push=args.push)
    elif args.command == "mr":
        if args.mr_command == "list":
            cmd_mr_list(console, manager)
        else:
            console.print("[yellow]Unknown mr command. Use 'ta mr list'.[/yellow]")
    elif args.command == "merge":
        cmd_merge(console, manager, args.slug, message=args.message, push=args.push)
    elif args.command == "store":
        cmd_store(console, args)
    elif args.command == "done":
        done_metrics = SubtaskMetric.from_completion_args(
            model=getattr(args, "model", None),
            provider=getattr(args, "provider", None),
            model_version=getattr(args, "model_version", None),
            agent_harness=getattr(args, "agent_harness", None),
            input_tokens=getattr(args, "input_tokens", None),
            output_tokens=getattr(args, "output_tokens", None),
            tokens_accuracy=getattr(args, "tokens_accuracy", None),
            duration_seconds=getattr(args, "duration_seconds", None),
            cost_usd=getattr(args, "cost_usd", None),
            started_at=getattr(args, "started_at", None),
            ended_at=getattr(args, "ended_at", None),
            notes=getattr(args, "metrics_notes", None),
        )
        cmd_done(
            console,
            manager,
            args.slug,
            args.message,
            True,
            args.push,
            args.solution,
            args.no_verify,
            metrics=done_metrics,
        )
    elif args.command == "delete":
        cmd_soft_delete(console, manager, args.slug)
    elif args.command == "new":
        cmd_new(
            console=console,
            manager=manager,
            title=args.title,
            body=args.body,
            draft=args.draft,
            as_dir=not args.file,
            completion_criteria=args.criteria,
            interactive=args.interactive,
            blocked_by=args.blocked_by,
            subtask_of=args.subtask_of,
            bulk=args.bulk,
            repo=getattr(args, "repo", None),
        )
    elif args.command == "worktree":
        cmd_worktree(console, manager, args)
    elif args.command == "github":
        cmd_github(console, manager, args)
    elif args.command == "version":
        if args.version_command == "promote":
            cmd_version(console, promote=args.part)
        elif args.version_command == "tag":
            cmd_version(
                console,
                tag=True,
                push=args.push,
                push_branch=getattr(args, "push_branch", True),
            )
        elif args.version_command == "release":
            cmd_version(
                console,
                release=args.part,
                push=args.push,
                push_branch=getattr(args, "push_branch", True),
            )
        else:
            cmd_version(console)
    else:
        display_overview(console, manager)


if __name__ == "__main__":
    main()
