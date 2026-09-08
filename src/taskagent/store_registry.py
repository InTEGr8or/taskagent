"""Machine-level task store data root, monikers, registry, and migration.

Phases:
  1. Data root, moniker, registry (read-only inspect)
  2. ``migrate_store`` — move legacy in-host stores into the data root
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

REGISTRY_VERSION = 1
STORE_META_REL = Path(".task-agent") / "store.json"

# Files/dirs that indicate a real station store (not an empty placeholder).
_STORE_MARKERS = (
    Path(".task-agent") / "mission.usv",
    Path("mission.usv"),
    Path("pending"),
    Path("active"),
    Path("draft"),
    Path("completed"),
)


def get_data_root() -> Path:
    """Return the machine-level task-agent data root.

    Resolution order:
    1. ``TA_DATA_ROOT`` environment variable
    2. ``$XDG_DATA_HOME/task-agent`` when XDG_DATA_HOME is set
    3. ``~/.local/share/task-agent``
    """
    override = os.environ.get("TA_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return (Path(xdg).expanduser() / "task-agent").resolve()

    return (Path.home() / ".local" / "share" / "task-agent").resolve()


def get_stores_dir(data_root: Optional[Path] = None) -> Path:
    """Return the directory that holds per-project store folders."""
    root = data_root if data_root is not None else get_data_root()
    return root / "stores"


def get_global_matrix_space(data_root: Optional[Path] = None) -> Optional[str]:
    """Return machine-wide default Matrix Space ID if configured."""
    root = data_root if data_root is not None else get_data_root()
    cfg_file = root / "matrix.json"
    if not cfg_file.is_file():
        return None
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        return data.get("matrix_space_id")
    except Exception:
        return None


def set_global_matrix_space(
    space_id: Optional[str], data_root: Optional[Path] = None
) -> Path:
    """Save machine-wide default Matrix Space ID."""
    root = data_root if data_root is not None else get_data_root()
    root.mkdir(parents=True, exist_ok=True)
    cfg_file = root / "matrix.json"
    data: Dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if space_id:
        data["matrix_space_id"] = space_id
    else:
        data.pop("matrix_space_id", None)
    tmp = cfg_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(cfg_file)
    return cfg_file


def get_global_matrix_token(data_root: Optional[Path] = None) -> Optional[str]:
    """Return machine-wide Matrix access token or secret reference if configured."""
    root = data_root if data_root is not None else get_data_root()
    cfg_file = root / "matrix.json"
    if not cfg_file.is_file():
        return None
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        return data.get("matrix_access_token")
    except Exception:
        return None


def set_global_matrix_token(
    token_ref: Optional[str], data_root: Optional[Path] = None
) -> Path:
    """Save machine-wide Matrix access token or 1Password secret reference."""
    root = data_root if data_root is not None else get_data_root()
    root.mkdir(parents=True, exist_ok=True)
    cfg_file = root / "matrix.json"
    data: Dict[str, Any] = {}
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if token_ref:
        data["matrix_access_token"] = token_ref
    else:
        data.pop("matrix_access_token", None)
    tmp = cfg_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(cfg_file)
    try:
        os.chmod(cfg_file, 0o600)
    except Exception:
        pass
    return cfg_file


def moniker_from_remote(url: str) -> str:
    """Derive a stable moniker from a git remote URL.

    Examples:
        git@github.com:bizkite-co/stations.git -> bizkite-co/stations
        https://github.com/InTEGr8or/task-agent.git -> InTEGr8or/task-agent
        ssh://git@gitlab.com/group/sub/repo.git -> group/sub/repo
    """
    if not url or not url.strip():
        raise ValueError("Empty remote URL")

    raw = url.strip()

    # scp-like: git@host:path/to/repo.git
    if "://" not in raw and re.match(r"^[^/\s]+@[^:\s]+:.+", raw):
        path = raw.split(":", 1)[1]
    else:
        # Normalize git@host:path already handled; support ssh:// and https://
        parsed = urlparse(raw if "://" in raw else f"ssh://{raw}")
        path = parsed.path or ""
        # urlparse("ssh://git@host/owner/repo") puts host correctly
        if not path and "@" in raw and ":" in raw:
            path = raw.rsplit(":", 1)[-1]

    path = path.lstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.strip("/")

    if not path:
        raise ValueError(f"Could not parse moniker from remote URL: {url!r}")

    return path


def moniker_to_dir_name(moniker: str) -> str:
    """Map a moniker to a single filesystem directory name under stores/."""
    name = moniker.strip().strip("/")
    # Collapse path separators and unsafe characters
    name = re.sub(r"[/\\]+", "_", name)
    name = re.sub(r"[^\w.\-@+]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"_+", "_", name).strip("._")
    if not name:
        raise ValueError(f"Moniker produced empty directory name: {moniker!r}")
    return name


def moniker_from_path(path: Path) -> str:
    """Derive a moniker for a non-repo (or remote-less) host path.

    Uses a path-stable form so the same folder re-registers consistently.
    """
    resolved = path.expanduser().resolve()
    # Stable across processes (do not use built-in hash(); it is salted per process)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:8]
    safe_name = re.sub(r"[^\w.\-]+", "-", resolved.name, flags=re.UNICODE).strip("-")
    if not safe_name:
        safe_name = "folder"
    return f"local/{safe_name}-{digest}"


def store_path_for_moniker(moniker: str, data_root: Optional[Path] = None) -> Path:
    """Return the canonical store directory for a moniker under the data root."""
    return get_stores_dir(data_root) / moniker_to_dir_name(moniker)


def mission_remote_status(
    mission_root: Optional[Path],
    *,
    issues_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Summarize whether a mission/task store has a git remote.

    Intended for front-of-CLI UX so users always see durability state.

    Returns keys:
      state: ``no_git`` | ``local_only`` | ``configured``
      remotes: dict name→url
      origin: preferred origin URL if any
      label: short human label
      detail: one-line detail for dim text
    """
    if mission_root is None:
        return {
            "state": "no_git",
            "remotes": {},
            "origin": None,
            "label": "no git",
            "detail": "Task store is not inside a git repository",
        }
    root = mission_root.expanduser().resolve()
    if not (root / ".git").exists() and git_toplevel(root) is None:
        return {
            "state": "no_git",
            "remotes": {},
            "origin": None,
            "label": "no git",
            "detail": f"No git at {root}",
        }
    remotes = _list_git_remotes(root)
    origin = remotes.get("origin") or (
        next(iter(remotes.values())) if remotes else None
    )
    if not remotes:
        return {
            "state": "local_only",
            "remotes": {},
            "origin": None,
            "label": "local only",
            "detail": "No remote — task history is only on this machine",
        }
    return {
        "state": "configured",
        "remotes": remotes,
        "origin": origin,
        "label": "remote ok",
        "detail": origin or ", ".join(f"{k}={v}" for k, v in remotes.items()),
    }


def format_remote_status_line(status: Dict[str, Any]) -> str:
    """Rich markup one-liner for task-store remote durability."""
    state = status.get("state")
    if state == "configured":
        return (
            f"[bold green]Store remote[/bold green]: "
            f"[cyan]{status.get('origin') or status.get('detail')}[/cyan]"
        )
    if state == "local_only":
        return (
            "[bold yellow]Store remote[/bold yellow]: "
            "[yellow]none (local only)[/yellow]  "
            "[dim]ta store remote set <url>[/dim]"
        )
    return (
        "[bold red]Store remote[/bold red]: "
        "[red]no git[/red]  "
        "[dim]ta store migrate[/dim]"
    )


def git_remote_url(repo_path: Path, remote: str = "origin") -> Optional[str]:
    """Return the URL for ``remote`` if ``repo_path`` is inside a git worktree."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", remote],
            capture_output=True,
            text=True,
            check=True,
            shell=(os.name == "nt"),
        )
        url = res.stdout.strip()
        return url or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_toplevel(path: Path) -> Optional[Path]:
    """Return the git toplevel for ``path``, or None if not in a repository."""
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            shell=(os.name == "nt"),
        )
        return Path(res.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def project_host_root(path: Path) -> Path:
    """Resolve the *main* project root for task-store discovery and migrate.

    Task stores live on the primary project tree (e.g. ``turboship/.task-agent/tasks``),
    never on a linked worktree. Callers often run ``ta`` from ``.gwt/<slug>`` or
    another git worktree whose ``rev-parse --show-toplevel`` is the worktree
    path — that must not be treated as the host.

    Resolution order:
    1. Unwrap ``.gwt/<slug>`` (task-agent worktrees) by walking parents.
    2. ``git rev-parse --git-common-dir`` → directory that owns ``.git/`` (main
       worktree), works for any git worktree. Walks up if ``path`` is not yet
       a directory that git accepts.
    3. Fall back to ``git_toplevel``, still unwrapping ``.gwt``.
    4. Resolved path itself.
    """
    p = path.expanduser().resolve()

    # 1) Explicit .gwt unwrap (works even without a valid git context)
    for cur in [p, *p.parents]:
        if cur.parent.name == ".gwt":
            return cur.parent.parent.resolve()
        if cur.name == ".gwt":
            return cur.parent.resolve()

    # 2) Main worktree via git common dir — try path and parents so nested
    #    non-existent or non-git dirs still resolve (e.g. worktree/subdir).
    for cur in [p, *p.parents]:
        try:
            res = subprocess.run(
                ["git", "-C", str(cur), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=True,
                shell=(os.name == "nt"),
            )
            common = Path(res.stdout.strip())
            if not common.is_absolute():
                common = (cur / common).resolve()
            else:
                common = common.resolve()
            # common is typically <main>/.git  (dir) or under it
            if common.name == ".git":
                return common.parent.resolve()
            for parent in common.parents:
                if parent.name == ".git":
                    return parent.parent.resolve()
            break
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            continue

    # 3) Toplevel with .gwt unwrap
    for cur in [p, *p.parents]:
        top = git_toplevel(cur)
        if top is not None:
            top = top.resolve()
            if top.parent.name == ".gwt":
                return top.parent.parent.resolve()
            return top

    return p


def resolve_moniker_for_host(host_path: Path) -> tuple[str, Optional[str]]:
    """Resolve (moniker, origin_url_or_none) for a host project path.

    Prefers origin remote on the **main** project root; falls back to path moniker.
    """
    top = project_host_root(host_path)
    origin = git_remote_url(top)
    if origin:
        try:
            return moniker_from_remote(origin), origin
        except ValueError:
            pass
    return moniker_from_path(top), origin


def legacy_store_candidates(host_path: Path) -> List[Path]:
    """Paths checked for a legacy in-host task store (main project root)."""
    host = project_host_root(host_path)
    return [
        host / ".task-agent" / "tasks",
        host / "docs" / "tasks",
        host / "docks" / "tasks",
    ]


def detect_legacy_store(host_path: Path) -> Optional[Path]:
    """Locate a legacy in-host task store if present.

    Always resolves against the main project root (not a ``.gwt`` worktree).

    Checks, in order:
    - ``{host}/.task-agent/tasks`` (current eject target; may be nested git)
    - ``{host}/docs/tasks`` / ``docks/tasks`` (dir or symlink into store)
    """
    for cand in legacy_store_candidates(host_path):
        if cand.is_dir() and not cand.is_symlink():
            return cand.resolve()
        if cand.is_symlink():
            try:
                target = cand.resolve()
                if target.is_dir():
                    return target
            except OSError:
                continue
    return None


def is_nested_git_repo(path: Path) -> bool:
    """True if ``path`` is the root of its own git repository (not the host)."""
    top = git_toplevel(path)
    if top is None:
        return False
    try:
        return top.resolve() == path.expanduser().resolve()
    except OSError:
        return False


def read_store_meta(store_path: Path) -> Optional[Dict[str, Any]]:
    """Read ``.task-agent/store.json`` from a store, if present."""
    meta_path = store_path / STORE_META_REL
    if not meta_path.is_file():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_store_meta(
    store_path: Path,
    moniker: str,
    remote: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write store identity metadata inside the store (source for registry rebuild)."""
    meta_path = store_path / STORE_META_REL
    existing = read_store_meta(store_path) or {}
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        **existing,
        "version": REGISTRY_VERSION,
        "moniker": moniker,
    }
    if remote:
        payload["remote"] = remote
    if extra:
        payload.update(extra)
    # Atomic replace
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    tmp.replace(meta_path)
    return meta_path


@dataclass
class StoreEntry:
    """One registered project → store binding."""

    moniker: str
    store_path: str
    host_paths: List[str] = field(default_factory=list)
    remote: Optional[str] = None
    registered_at: Optional[str] = None
    keet_room_uri: Optional[str] = None
    matrix_room_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        # Drop Nones for cleaner JSON
        return {k: v for k, v in data.items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StoreEntry":
        return cls(
            moniker=data["moniker"],
            store_path=data["store_path"],
            host_paths=list(data.get("host_paths") or []),
            remote=data.get("remote"),
            registered_at=data.get("registered_at"),
            keet_room_uri=data.get("keet_room_uri"),
            matrix_room_id=data.get("matrix_room_id"),
        )


class MachineRegistry:
    """Machine registry of moniker → store root.

    The registry file is a rebuildable *index* over ``stores/`` metadata plus
    optional log-grade ``host_paths`` that may not be discoverable by scan alone.
    Writes use atomic replace.
    """

    def __init__(self, data_root: Optional[Path] = None):
        self.data_root = (
            data_root.expanduser().resolve()
            if data_root is not None
            else get_data_root()
        )
        self.registry_path = self.data_root / "registry.json"
        self.stores_dir = get_stores_dir(self.data_root)

    def ensure_layout(self) -> None:
        """Create data root and stores/ if missing (does not create a store)."""
        self.stores_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, StoreEntry]:
        """Load moniker → StoreEntry map. Missing file → empty."""
        if not self.registry_path.is_file():
            return {}
        try:
            raw = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        stores = raw.get("stores") or {}
        result: Dict[str, StoreEntry] = {}
        for moniker, entry in stores.items():
            if not isinstance(entry, dict):
                continue
            # moniker key wins if body omits it
            body = {**entry, "moniker": entry.get("moniker") or moniker}
            try:
                result[body["moniker"]] = StoreEntry.from_dict(body)
            except KeyError:
                continue
        return result

    def save(self, entries: Dict[str, StoreEntry]) -> None:
        """Atomically write the full registry."""
        self.ensure_layout()
        payload = {
            "version": REGISTRY_VERSION,
            "stores": {
                moniker: entry.to_dict()
                for moniker, entry in sorted(entries.items(), key=lambda kv: kv[0])
            },
        }
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.registry_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(self.registry_path)

    def get(self, moniker: str) -> Optional[StoreEntry]:
        return self.load().get(moniker)

    def find_by_host_path(self, host_path: Path) -> Optional[StoreEntry]:
        """Find an entry whose host_paths contains this path (or a parent)."""
        resolved = host_path.expanduser().resolve()
        best: Optional[StoreEntry] = None
        best_len = -1
        for entry in self.load().values():
            for hp in entry.host_paths:
                try:
                    p = Path(hp).expanduser().resolve()
                except OSError:
                    continue
                if resolved == p or resolved.is_relative_to(p):
                    if len(str(p)) > best_len:
                        best = entry
                        best_len = len(str(p))
        return best

    def upsert(self, entry: StoreEntry) -> StoreEntry:
        """Insert or update an entry by moniker; merges host_paths."""
        entries = self.load()
        existing = entries.get(entry.moniker)
        if existing:
            hosts = list(dict.fromkeys([*existing.host_paths, *entry.host_paths]))
            merged = StoreEntry(
                moniker=entry.moniker,
                store_path=entry.store_path or existing.store_path,
                host_paths=hosts,
                remote=entry.remote if entry.remote is not None else existing.remote,
                registered_at=existing.registered_at or entry.registered_at,
            )
            entries[entry.moniker] = merged
            result = merged
        else:
            if not entry.registered_at:
                entry.registered_at = datetime.now(timezone.utc).isoformat()
            entries[entry.moniker] = entry
            result = entry
        self.save(entries)
        return result

    def list_entries(self) -> List[StoreEntry]:
        return [self.load()[k] for k in sorted(self.load())]

    def rebuild_from_stores(self) -> Dict[str, StoreEntry]:
        """Rebuild index fields by scanning ``stores/``.

        Preserves existing ``host_paths`` and ``registered_at`` when moniker
        already known (log-grade fields). Drops monikers whose store dirs are
        gone unless they only had host_paths — actually gone stores are removed
        from the index (rebuildable index discipline).
        """
        previous = self.load()
        rebuilt: Dict[str, StoreEntry] = {}

        if self.stores_dir.is_dir():
            for child in sorted(self.stores_dir.iterdir()):
                if not child.is_dir():
                    continue
                # Skip junk / partial dirs (e.g. a stray stores/docs/ from tests)
                if not looks_like_store(child):
                    continue
                meta = read_store_meta(child)
                moniker: Optional[str] = None
                remote: Optional[str] = None
                if meta and meta.get("moniker"):
                    moniker = str(meta["moniker"])
                    remote = meta.get("remote") or git_remote_url(child)
                else:
                    # Fallback: nested git origin, else directory name as moniker
                    remote = git_remote_url(child)
                    if remote:
                        try:
                            moniker = moniker_from_remote(remote)
                        except ValueError:
                            moniker = child.name.replace("_", "/", 1)
                    else:
                        # Dir name uses _ as filesystem encoding of /
                        moniker = child.name.replace("_", "/", 1)

                assert moniker is not None
                prev = previous.get(moniker)
                keet_uri = (meta.get("keet_room_uri") if meta else None) or (
                    prev.keet_room_uri if prev else None
                )
                matrix_id = (meta.get("matrix_room_id") if meta else None) or (
                    prev.matrix_room_id if prev else None
                )
                rebuilt[moniker] = StoreEntry(
                    moniker=moniker,
                    store_path=str(child.resolve()),
                    host_paths=list(prev.host_paths) if prev else [],
                    remote=remote or (prev.remote if prev else None),
                    registered_at=prev.registered_at if prev else None,
                    keet_room_uri=keet_uri,
                    matrix_room_id=matrix_id,
                )

        self.save(rebuilt)
        return rebuilt


def _list_git_remotes(repo_path: Path) -> Dict[str, str]:
    """Return ``{remote_name: url}`` for fetch URLs (empty if not a repo)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "-v"],
            capture_output=True,
            text=True,
            check=True,
            shell=(os.name == "nt"),
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    remotes: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "(fetch)":
            remotes[parts[0]] = parts[1]
    return remotes


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def looks_like_store(path: Path) -> bool:
    """True if ``path`` has station/mission markers."""
    if not path.is_dir():
        return False
    for rel in _STORE_MARKERS:
        if (path / rel).exists():
            return True
    return False


def verify_store(
    store_path: Path, *, remotes_expected: Optional[Dict[str, str]] = None
) -> List[str]:
    """Return a list of verification errors (empty = ok)."""
    errors: List[str] = []
    if not store_path.is_dir():
        return [f"Store path is not a directory: {store_path}"]
    if not looks_like_store(store_path):
        errors.append(
            f"Store lacks mission/station markers under {store_path} "
            f"(expected mission.usv or pending/active/draft/completed)"
        )
    if remotes_expected is not None:
        actual = _list_git_remotes(store_path)
        for name, url in remotes_expected.items():
            if actual.get(name) != url:
                errors.append(
                    f"Remote {name!r} mismatch: expected {url!r}, got {actual.get(name)!r}"
                )
    return errors


def _update_env_var(env_path: Path, key: str, value: str) -> None:
    """Set key=value in a .env file (create or replace)."""
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        return
    lines = env_path.read_text(encoding="utf-8").splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _replace_with_symlink(link_path: Path, target: Path) -> None:
    """Make ``link_path`` an absolute symlink to ``target`` (replace existing)."""
    target_abs = str(target.resolve())
    if link_path.is_symlink() or link_path.is_file():
        link_path.unlink()
    elif link_path.is_dir():
        # Only remove empty dirs; callers must have moved contents already
        try:
            link_path.rmdir()
        except OSError as e:
            raise RuntimeError(
                f"Cannot replace non-empty directory with symlink: {link_path}"
            ) from e
    link_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target_abs, str(link_path))


def _count_entries(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in path.rglob("*"))


@dataclass
class MigrationPlan:
    """Describes a migrate operation before/without applying it."""

    host_path: str
    moniker: str
    source: Optional[str]
    destination: str
    kind: Optional[str]  # nested_git | host_tree | already_migrated | none
    remotes_before: Dict[str, str] = field(default_factory=dict)
    subject_origin: Optional[str] = (
        None  # host code remote (identity), not always store remote
    )
    already_migrated: bool = False
    steps: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def read_host_store_config(host_path: Path) -> Dict[str, Any]:
    """Read ``.ta-config.json`` from the main project root (empty dict if missing)."""
    host = project_host_root(host_path)
    path = host / ".ta-config.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_host_store_config(
    host_path: Path,
    moniker: Optional[str] = None,
    *,
    store_symlink: Optional[bool] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write/update ``.ta-config.json`` host binding.

    Keys:
      - store_moniker: stable identity for the task store
      - store_symlink: whether docs/tasks should be a human-facing symlink to the store
    """
    host = project_host_root(host_path)
    path = host / ".ta-config.json"
    data = read_host_store_config(host)
    if moniker is not None:
        data["store_moniker"] = moniker
    if store_symlink is not None:
        data["store_symlink"] = bool(store_symlink)
    if extra:
        data.update(extra)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def store_symlink_preferred(host_path: Path) -> bool:
    """Whether docs/tasks human symlink should exist (default True if unset)."""
    cfg = read_host_store_config(host_path)
    if "store_symlink" not in cfg:
        return True
    return bool(cfg.get("store_symlink"))


def ensure_gitignore_entry(host_path: Path, entry: str = "docs/tasks") -> bool:
    """Ensure ``entry`` is listed in host ``.gitignore``. Returns True if added."""
    host = project_host_root(host_path)
    gitignore = host / ".gitignore"
    line = entry.strip().strip("/")
    # Accept docs/tasks or /docs/tasks
    patterns = {line, f"/{line}", f"{line}/", f"/{line}/"}
    if gitignore.is_file():
        text = gitignore.read_text(encoding="utf-8")
        existing = {ln.strip() for ln in text.splitlines() if ln.strip()}
        if existing & patterns:
            return False
        with gitignore.open("a", encoding="utf-8") as f:
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write(f"\n# task-agent: human-facing store symlink\n{line}\n")
        return True
    gitignore.write_text(
        f"# task-agent: human-facing store symlink\n{line}\n", encoding="utf-8"
    )
    return True


def remove_gitignore_entry(host_path: Path, entry: str = "docs/tasks") -> bool:
    """Remove ``entry`` from host ``.gitignore``. Returns True if anything was stripped.

    Phase 1 (broad strip): removes every line matching ``entry`` variants
    (``docs/tasks``, ``/docs/tasks``, ``docs/tasks/``, ``/docs/tasks/``), plus a
    directly-preceding ``# task-agent: human-facing store symlink`` comment line
    that bracketed a removed entry. Idempotent; no-op when ``.gitignore`` absent.

    TODO(T-A): tighten to bracket-only mode — only strip entries sitting directly
    beneath a task-agent comment marker. Broad strip is a deliberate one-release
    bridge so existing installed bases (whose .gitignore entries predate the
    bracketing convention) get cleaned up; once those are flushed, switch to
    bracket-only removal so user-managed entries survive.
    """
    host = project_host_root(host_path)
    gitignore = host / ".gitignore"
    if not gitignore.is_file():
        return False
    line = entry.strip().strip("/")
    patterns = {line, f"/{line}", f"{line}/", f"/{line}/"}
    marker = "# task-agent: human-facing store symlink"
    text = gitignore.read_text(encoding="utf-8")
    raw_lines = text.splitlines()
    kept: List[str] = []
    changed = False
    i = 0
    while i < len(raw_lines):
        cur = raw_lines[i]
        cur_stripped = cur.strip()
        # Recognize a task-agent bracket comment directly preceding a matching entry:
        # drop the comment together with the entry.
        if cur_stripped == marker and i + 1 < len(raw_lines):
            nxt_stripped = raw_lines[i + 1].strip()
            if nxt_stripped in patterns:
                changed = True
                i += 2  # skip comment + entry
                continue
        if cur_stripped in patterns:
            changed = True
            i += 1
            continue
        kept.append(cur)
        i += 1
    if not changed:
        return False
    # Preserve a trailing newline if the original had one.
    new_text = "\n".join(kept)
    if text.endswith("\n") and new_text:
        new_text += "\n"
    gitignore.write_text(new_text, encoding="utf-8")
    return True


def _gitignore_has_entry(host: Path, entry: str) -> bool:
    gitignore = host / ".gitignore"
    if not gitignore.is_file():
        return False
    existing = {ln.strip() for ln in gitignore.read_text(encoding="utf-8").splitlines()}
    patterns = {entry, f"/{entry}", f"{entry}/", f"/{entry}/"}
    return bool(existing & patterns)


def normalize_store_symlink_preference(host_path: Path) -> bool:
    """Materialize the implicit ``store_symlink=true`` default when a config exists.

    Returns True iff a write actually happened. Never creates ``.ta-config.json``
    where none existed — only fills in the missing key in an existing file, so
    hosts that have not opted in stay untouched.

    Safe to call from inspection entry points (e.g. ``ta store symlink status``);
    wraps the write in try/except so a read failure can never break discovery.
    """
    host = project_host_root(host_path)
    path = host / ".ta-config.json"
    if not path.is_file():
        return False
    cfg = read_host_store_config(host)
    if "store_symlink" in cfg:
        return False
    try:
        write_host_store_config(host, store_symlink=True)
    except (OSError, json.JSONDecodeError):
        return False
    return True


def _safe_resolve_moniker(host: Path) -> str:
    """Resolve a moniker for the host without ever raising (returns "" on failure).

    Used by ``set_docs_tasks_symlink`` so the durable config write can record the
    moniker when one is cheaply available, but never abort the write when it is not.
    """
    try:
        return resolve_moniker_for_host(host)[0] or ""
    except Exception:
        return ""


class StoreSymlinkError(RuntimeError):
    """Raised when docs/tasks cannot safely become a store symlink."""


# Host-side local task link paths task-agent recognizes for symlink management.
#   off (cleanup):    all three — hygiene, all are candidates task-agent may have
#                     created at some point.
#   on  (creation):   only docs/tasks and docks/tasks — .task-agent/tasks is
#                     vestigial (untracked + gitignored in cocli) and must not be
#                     recreated. On-branch tuple lists docks first so any
#                     alias-selection from ON_RELS naturally prefers docks,
#                     matching discovery precedence.
_LOCAL_TASK_LINK_RELS = (
    Path("docs") / "tasks",
    Path("docks") / "tasks",
    Path(".task-agent") / "tasks",
)
_ON_TASK_LINK_RELS = (Path("docks") / "tasks", Path("docs") / "tasks")
# Explicit precedence for legacy single-link alias selection in status output:
# docks > docs > .task-agent, matching discovery.py's docks-preferred behavior.
_ALIAS_PRECEDENCE = (
    Path("docks") / "tasks",
    Path("docs") / "tasks",
    Path(".task-agent") / "tasks",
)


def docs_tasks_symlink_status(
    host_path: Path, store_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Describe the task-agent local link state for the host project.

    Reports every candidate path in ``_LOCAL_TASK_LINK_RELS``
    (``docs/tasks``, ``docks/tasks``, ``.task-agent/tasks``) as a ``paths`` list.
    Top-level ``kind`` / ``target`` / ``points_to_store`` are legacy aliases
    selected by explicit docks > docs > .task-agent precedence (not tuple order),
    matching discovery's docks-preferred behavior so callers that still read the
    legacy fields see the same link discovery would actually use.
    """
    host = project_host_root(host_path)
    preferred = store_symlink_preferred(host)
    store = store_path
    if store is None:
        try:
            report = inspect_host(host)
            cand = report.get("canonical_store_path")
            if cand:
                store = Path(cand)
            if (
                store is not None
                and not store.is_dir()
                and report.get("legacy_store_path")
            ):
                store = Path(report["legacy_store_path"])
        except Exception:
            store = None

    store_resolved: Optional[Path] = None
    if store is not None and store.exists():
        try:
            store_resolved = store.resolve()
        except OSError:
            store_resolved = store

    paths: List[Dict[str, Any]] = []
    for rel in _LOCAL_TASK_LINK_RELS:
        link = host / rel
        rel_paths_entry: Dict[str, Any] = {
            "rel": rel.as_posix(),
            "link_path": str(link),
            "kind": "absent",
            "target": None,
            "points_to_store": False,
        }
        if link.is_symlink():
            rel_paths_entry["kind"] = "symlink"
            try:
                resolved = link.resolve()
                rel_paths_entry["target"] = str(resolved)
                if store_resolved is not None:
                    try:
                        rel_paths_entry["points_to_store"] = (
                            resolved.resolve() == store_resolved
                        )
                    except OSError:
                        rel_paths_entry["points_to_store"] = False
            except OSError:
                rel_paths_entry["kind"] = "broken_symlink"
                try:
                    rel_paths_entry["target"] = str(link.readlink())
                except OSError:
                    rel_paths_entry["target"] = None
        elif link.is_dir():
            rel_paths_entry["kind"] = "directory"
        elif link.is_file():
            rel_paths_entry["kind"] = "file"
        elif link.exists():
            rel_paths_entry["kind"] = "other"
        paths.append(rel_paths_entry)

    # Legacy alias: first present link by explicit docks > docs > .task-agent order.
    alias: Optional[Dict[str, Any]] = None
    for pref_rel in _ALIAS_PRECEDENCE:
        for p in paths:
            if p["rel"] == pref_rel.as_posix() and p["kind"] != "absent":
                alias = p
                break
        if alias is not None:
            break

    return {
        "host_path": str(host),
        "paths": paths,
        "link_path": alias["link_path"] if alias else str(host / "docs" / "tasks"),
        "kind": alias["kind"] if alias else "absent",
        "target": alias["target"] if alias else None,
        "store_path": str(store_resolved) if store_resolved is not None else None,
        "points_to_store": alias["points_to_store"] if alias else False,
        "preferred": preferred,
        "gitignore_entries": {
            rel.as_posix(): _gitignore_has_entry(host, rel.as_posix())
            for rel in _LOCAL_TASK_LINK_RELS
        },
        # Deprecated: retained for older callers keyed on the docs/tasks entry only.
        "gitignore_has_docs_tasks": _gitignore_has_entry(host, "docs/tasks"),
    }


def _resolve_store_for_link_management(
    host: Path, store_path: Optional[Path]
) -> tuple[Optional[Path], Dict[str, Any]]:
    """Best-effort store resolution for symlink on/off.

    Returns ``(store_or_None, report)``. ``report`` is preserved (especially the
    ``moniker`` entry) even when ``store`` is None, using per-step ``.get()``
    degradation so a single missing/malformed field does not wipe the rest.

    On the off path, ``store is None`` is a *tolerable* state — the off branch
    must never raise just because the store can't be located (that was the
    original serialization bug this function had). On the on path it is a hard
    error — ``on`` must point at something.
    """
    report: Dict[str, Any] = {}
    store: Optional[Path] = store_path
    try:
        report = inspect_host(host)
    except Exception:
        report = {}
    if store is None:
        cand_str = report.get("canonical_store_path")
        if cand_str:
            cand = Path(cand_str)
            if cand.is_dir():
                store = cand
        reg = report.get("registry_entry") or {}
        reg_sp = reg.get("store_path")
        if store is None and reg_sp:
            cand2 = Path(reg_sp)
            if cand2.is_dir():
                store = cand2
    if store is not None and not (store.is_dir() and looks_like_store(store)):
        # fall back to legacy resolved path if still on host
        try:
            leg = detect_legacy_store(host)
        except Exception:
            leg = None
        if leg is not None and looks_like_store(leg):
            store = leg
        else:
            store = None
    if store is not None:
        store = store.resolve()
    return store, report


def _kind_of(link: Path) -> str:
    """Categorize a host-side task link path.

    A symlink is "broken" when its target does not exist. On Python 3.6+
    ``Path.resolve(strict=False)`` (the default) does *not* raise on a dangling
    symlink, so we detect broken-ness via ``link.exists()`` (which follows the
    link). ``OSError`` from a symlink loop or permission error is also treated
    as broken — the off-branch will then ``unlink`` it.
    """
    if link.is_symlink():
        try:
            if link.exists():  # follows the link to test the target
                return "symlink"
            return "broken_symlink"
        except OSError:
            return "broken_symlink"
    if link.is_dir():
        return "directory"
    if link.is_file():
        return "file"
    if link.exists():
        return "other"
    return "absent"


def set_docs_tasks_symlink(
    host_path: Path,
    *,
    enabled: bool,
    store_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Turn the task-agent host→store symlinks on or off.

    Manages the three host-side local task-link paths in
    ``_LOCAL_TASK_LINK_RELS`` for **off** (cleanup): ``docs/tasks``,
    ``docks/tasks``, ``.task-agent/tasks``. Only ``docs/tasks`` and
    ``docks/tasks`` are managed by **on** — ``.task-agent/tasks`` is vestigial
    (untracked + gitignored) and must not be recreated.

    Invariants (see incident-ta-store-symlink-off-...):

    1. The **off** branch never raises. Foreign/broken-store situations are
       collected as refused actions and the loop continues. The durable
       ``store_symlink=false`` config write is *unconditional* — nothing may
       abort between link inspection and that write.
    2. Foreign symlinks on **off** never raise. The loop records a refusal,
       leaves the link in place, and continues to the next rel.
    3. **on** is all-or-nothing on the filesystem. A two-pass pre-flight inspects
       every rel for conflicts and raises ``StoreSymlinkError`` *before* any
       mutation; pass 2 runs only if pass 1 was clean.
    4. Neither branch ever moves store data. Only ``unlink``, symlink creation,
       ``.gitignore`` writes, and ``.ta-config.json`` writes occur here.

    Does **not** delete the centralized store. Binding remains via moniker/registry.
    """
    host = project_host_root(host_path)
    store, report = _resolve_store_for_link_management(host, store_path)

    actions: List[str] = []
    rels = _ON_TASK_LINK_RELS if enabled else _LOCAL_TASK_LINK_RELS

    if enabled:
        if store is None:
            raise StoreSymlinkError(
                f"Cannot turn symlink on: no task store located for {host}. "
                "Run `ta store migrate` first, or ensure the data-root store exists."
            )
        # Pass 1: pre-flight, no mutation. Collect every rel's conflict so the
        # user gets one complete error message listing all blockers, not just the
        # first. Mutating nothing here is what makes on genuinely all-or-nothing.
        conflicts: List[str] = []
        for rel in rels:
            link = host / rel
            kind = _kind_of(link)
            if kind == "broken_symlink":
                continue  # pass 2 will repair it
            if kind == "symlink":
                try:
                    current = link.resolve()
                except OSError:
                    continue
                if current != store:
                    conflicts.append(f"{link} → {current} (expected {store})")
            elif kind == "directory":
                if any(link.iterdir()):
                    conflicts.append(f"{link} is a non-empty real directory")
                # empty directory is OK; pass 2 will rmdir + symlink
            elif kind == "file":
                conflicts.append(f"{link} is a regular file")
            elif kind == "other":
                conflicts.append(f"{link} exists with an unexpected type")
        if conflicts:
            raise StoreSymlinkError(
                "Conflicts prevent `ta store symlink on`:\n  - "
                + "\n  - ".join(conflicts)
                + "\nResolve manually, then re-run `ta store symlink on`."
            )
        # Pass 2: mutate. Guaranteed clean — no rel's mutation can fail on a
        # foreign/conflict state because pass 1 already refused them.
        for rel in rels:
            link = host / rel
            kind = _kind_of(link)
            skip_create = False
            if kind == "broken_symlink":
                link.unlink()
                actions.append(f"removed broken symlink {link}")
            elif kind == "symlink":
                try:
                    if link.resolve() == store:
                        actions.append(f"symlink already correct: {link} → {store}")
                        skip_create = True
                    else:
                        link.unlink()
                        actions.append(f"removed foreign symlink {link}")
                except OSError:
                    link.unlink()
                    actions.append(f"removed broken symlink {link}")
            elif kind == "directory":
                if any(link.iterdir()):
                    # Should be unreachable post-pre-flight; defensive.
                    actions.append(f"skipped non-empty {link}")
                    skip_create = True
                else:
                    link.rmdir()
                    actions.append(f"removed empty directory {link}")
            elif kind == "absent":
                pass
            else:  # file / other — unreachable post-pre-flight; defensive
                actions.append(f"skipped {link} ({kind} at {rel})")
                skip_create = True
            if not skip_create:
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(str(store), str(link))
                actions.append(f"created symlink {link} → {store}")
            if ensure_gitignore_entry(host, rel.as_posix()):
                actions.append(f"added {rel.as_posix()} to .gitignore")
            else:
                actions.append(f"{rel.as_posix()} already in .gitignore")
        moniker = report.get("moniker") or _safe_resolve_moniker(host)
        write_host_store_config(host, moniker=moniker, store_symlink=True)
        actions.append("set store_symlink=true in .ta-config.json")
    else:
        # off — the serialization-invariant branch. Never raises.
        for rel in rels:
            link = host / rel
            kind = _kind_of(link)
            if kind == "broken_symlink":
                link.unlink()
                actions.append(f"removed broken symlink {link}")
                if remove_gitignore_entry(host, rel.as_posix()):
                    actions.append(f"stripped {rel.as_posix()} from .gitignore")
            elif kind == "symlink":
                try:
                    current = link.resolve()
                except OSError:
                    link.unlink()
                    actions.append(f"removed broken symlink {link}")
                    if remove_gitignore_entry(host, rel.as_posix()):
                        actions.append(f"stripped {rel.as_posix()} from .gitignore")
                    continue
                if store is not None and current == store:
                    link.unlink()
                    actions.append(f"removed symlink {link}")
                    if remove_gitignore_entry(host, rel.as_posix()):
                        actions.append(f"stripped {rel.as_posix()} from .gitignore")
                elif store is not None and current != store:
                    actions.append(
                        f"refused foreign symlink {link} → {current} (expected {store})"
                    )
                else:  # store is None
                    actions.append(
                        f"refused symlink {link}: store unlocatable; "
                        "remove manually if trusted"
                    )
            elif kind == "directory" or kind == "file" or kind == "other":
                actions.append(f"left {link} in place ({kind})")
            else:  # absent
                actions.append(f"{link} already absent")
                # No .gitignore-strip for the absent case — nothing on disk
                # indicates task-agent ever created a symlink here. Stripping
                # an unbracketed user-managed entry would be unsafe; T-A will
                # revisit bracketed vs. broad strip semantics separately.
        moniker = report.get("moniker") or _safe_resolve_moniker(host)
        write_host_store_config(host, moniker=moniker, store_symlink=False)
        actions.append("set store_symlink=false in .ta-config.json")

    return {
        "enabled": enabled,
        "host_path": str(host),
        "store_path": str(store) if store is not None else None,
        "link_path": str(host / "docs" / "tasks"),
        "actions": actions,
        "status": docs_tasks_symlink_status(host, store if store is not None else None),
    }


@dataclass
class MigrationResult:
    plan: MigrationPlan
    dry_run: bool
    success: bool
    message: str
    applied_steps: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "dry_run": self.dry_run,
            "success": self.success,
            "message": self.message,
            "applied_steps": self.applied_steps,
        }


def plan_migrate(host_path: Path, data_root: Optional[Path] = None) -> MigrationPlan:
    """Build a migration plan for the host project (no side effects)."""
    host = host_path.expanduser().resolve()
    # Always main project root — never a .gwt/<slug> worktree path
    top = project_host_root(host)
    moniker, subject_origin = resolve_moniker_for_host(top)
    root = data_root if data_root is not None else get_data_root()
    dest = store_path_for_moniker(moniker, root)
    steps: List[str] = []
    warnings: List[str] = []
    errors: List[str] = []
    if subject_origin:
        steps.append(f"Subject host origin (identity): {subject_origin}")

    # Prefer the physical eject location as the source to move.
    eject_path = top / ".task-agent" / "tasks"
    source: Optional[Path] = None
    kind: Optional[str] = None

    if eject_path.is_symlink():
        try:
            target = eject_path.resolve()
        except OSError:
            target = None
        if target is not None and target.resolve() == dest.resolve() and dest.is_dir():
            return MigrationPlan(
                host_path=str(top),
                moniker=moniker,
                source=str(eject_path),
                destination=str(dest),
                kind="already_migrated",
                already_migrated=True,
                subject_origin=subject_origin,
                steps=[
                    "Already migrated (data-root store present)",
                    f"Remove leftover host eject symlink {eject_path}",
                    "Refresh moniker config / registry / optional docs symlink",
                ],
            )
        if target is not None and target.is_dir() and looks_like_store(target):
            # Symlink points elsewhere (old custom eject) — migrate that target
            source = target
    elif eject_path.is_dir() and looks_like_store(eject_path):
        source = eject_path.resolve()

    if source is None:
        # Fall back to docs/tasks only if it is a real directory (not symlink)
        docs = top / "docs" / "tasks"
        if docs.is_dir() and not docs.is_symlink() and looks_like_store(docs):
            source = docs.resolve()
            warnings.append(
                "Source is docs/tasks (in-tree); prefer eject layout for new projects"
            )

    if source is None:
        # Maybe already only at canonical
        if dest.is_dir() and looks_like_store(dest):
            return MigrationPlan(
                host_path=str(top),
                moniker=moniker,
                source=None,
                destination=str(dest),
                kind="already_migrated",
                already_migrated=True,
                subject_origin=subject_origin,
                steps=[
                    "No-op: canonical store already exists; no in-host source to move"
                ],
                warnings=["Consider repairing host symlinks with another migrate run"],
            )
        checked = ", ".join(str(c) for c in legacy_store_candidates(top))
        return MigrationPlan(
            host_path=str(top),
            moniker=moniker,
            source=None,
            destination=str(dest),
            kind="none",
            subject_origin=subject_origin,
            errors=[
                "No legacy task store found under main project root. "
                f"Checked: {checked}. "
                "If you ran from a worktree (.gwt/…), store lives on the main tree."
            ],
        )

    # If source is already the destination, done
    if source.resolve() == dest.resolve():
        return MigrationPlan(
            host_path=str(top),
            moniker=moniker,
            source=str(source),
            destination=str(dest),
            kind="already_migrated",
            already_migrated=True,
            steps=["No-op: source is already the canonical store path"],
        )

    nested = is_nested_git_repo(source)
    kind = "nested_git" if nested else "host_tree"
    # Always inspect remotes on the source path (may walk to host for host_tree)
    raw_remotes = _list_git_remotes(source)
    if nested:
        remotes_before = raw_remotes
        if remotes_before:
            steps.append(f"Preserve nested store remotes: {remotes_before}")
        else:
            warnings.append("Nested git store has no remotes configured")
    else:
        # host_tree: remotes belong to the host worktree, not a dedicated tasks repo
        remotes_before = {}
        if raw_remotes:
            warnings.append(
                f"Source is host_tree (no nested .git). Visible remotes {raw_remotes} "
                "belong to the subject host repo and will NOT be copied as store remotes. "
                "After migrate: `ta store remote suggest` / `ta store remote set <url>`."
            )
        steps.append("git init store without inventing a remote (host_tree)")

    # Destination conflicts
    if dest.exists():
        if dest.is_dir() and looks_like_store(dest):
            # Same moniker store already present — only repair pointers if content matches
            errors.append(
                f"Destination already exists and looks like a store: {dest}. "
                "Refusing to overwrite. Remove or rename it, or fix host pointers manually."
            )
        elif dest.is_dir() and any(dest.iterdir()):
            errors.append(f"Destination exists and is non-empty: {dest}")
        elif dest.is_file() or dest.is_symlink():
            errors.append(
                f"Destination path exists and is not a free directory: {dest}"
            )

    if not looks_like_store(source):
        errors.append(f"Source does not look like a task store: {source}")

    steps.append(f"Create parent directory {dest.parent}")
    if nested:
        steps.append(
            f"Move nested git store {source} → {dest} (preserve .git and remotes)"
        )
        if remotes_before:
            steps.append(f"Verify remotes after move: {remotes_before}")
    else:
        steps.append(f"Move host_tree store {source} → {dest}")
        steps.append("Create initial commit of station tree")

    steps.append("Write .task-agent/store.json moniker metadata (+ subject_origin)")
    steps.append(f"Write host .ta-config.json store_moniker={moniker}")
    steps.append("Register moniker in machine registry.json")
    steps.append(
        f"Remove host eject path {eject_path} (no longer used; store is data-root)"
    )
    steps.append("Optional human symlink docs/tasks → store when store_symlink is on")
    steps.append("Update .env TA_EJECTED_TASKS_PATH to data-root store (not host path)")
    steps.append("Verify store markers (and remotes if nested_git)")

    if _path_is_under(dest, top):
        warnings.append(
            "Destination is under the host tree; prefer TA_DATA_ROOT outside the repo"
        )

    return MigrationPlan(
        host_path=str(top),
        moniker=moniker,
        source=str(source),
        destination=str(dest),
        kind=kind,
        remotes_before=remotes_before,
        subject_origin=subject_origin,
        already_migrated=False,
        steps=steps,
        warnings=warnings,
        errors=errors,
    )


def _git_init_and_commit(store_path: Path, message: str) -> None:
    subprocess.run(
        ["git", "-C", str(store_path), "init"],
        check=True,
        capture_output=True,
        shell=(os.name == "nt"),
    )
    subprocess.run(
        ["git", "-C", str(store_path), "add", "-A"],
        check=True,
        capture_output=True,
        shell=(os.name == "nt"),
    )
    # Allow empty? No — store should have files
    env = os.environ.copy()
    # Avoid depending on user identity in tests
    env.setdefault("GIT_AUTHOR_NAME", "task-agent")
    env.setdefault("GIT_AUTHOR_EMAIL", "task-agent@localhost")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    subprocess.run(
        ["git", "-C", str(store_path), "commit", "-m", message],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        shell=(os.name == "nt"),
    )


def _move_store_tree(source: Path, dest: Path) -> None:
    """Move source directory to dest (same-filesystem rename when possible)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise RuntimeError(f"Destination already exists: {dest}")
    shutil.move(str(source), str(dest))


def _remove_host_eject_path(host: Path) -> List[str]:
    """Remove legacy ``.task-agent/tasks`` host eject path after migrate.

    Migrated stores live only under the machine data root. Host eject symlinks
    (and empty residual dirs) are leftover convenience paths that agents should
    not use; moniker + registry + ``ta store path`` resolve the store.
    """
    applied: List[str] = []
    eject = host / ".task-agent" / "tasks"
    if eject.is_symlink():
        eject.unlink()
        applied.append(f"removed eject symlink {eject}")
    elif eject.is_dir():
        if any(eject.iterdir()):
            raise RuntimeError(f"Legacy path still has contents after move: {eject}")
        eject.rmdir()
        applied.append(f"removed empty eject dir {eject}")
    elif eject.exists():
        eject.unlink()
        applied.append(f"removed eject path {eject}")
    else:
        applied.append(f"no host eject path at {eject}")
    return applied


def _update_host_pointers(host: Path, dest: Path) -> List[str]:
    """After migrate: drop host eject path; optional docs symlink; point .env at store.

    - **Always** remove ``.task-agent/tasks`` (symlink or empty dir). Discovery
      uses moniker/registry; agents must not rely on the eject path.
    - **docs/tasks** / **docks/tasks**: create/repair only when
      ``store_symlink`` preference is on; remove store-pointing symlinks when off.
    - **.env**: ``TA_EJECTED_*_PATH`` points at the data-root store (not a host path).
    """
    applied: List[str] = []
    dest_abs = dest.resolve()

    applied.extend(_remove_host_eject_path(host))

    prefer_docs = store_symlink_preferred(host)
    for rel in (Path("docs") / "tasks", Path("docks") / "tasks"):
        link = host / rel
        if prefer_docs:
            if link.exists() or link.is_symlink():
                if link.is_symlink():
                    link.unlink()
                elif link.is_dir():
                    # Only re-point if empty (content should have been source already)
                    if any(link.iterdir()):
                        applied.append(f"skipped non-empty {link}")
                        continue
                    link.rmdir()
                else:
                    link.unlink()
                _replace_with_symlink(link, dest_abs)
                applied.append(f"symlink {link} → {dest_abs}")
            elif (host / rel.parent).is_dir():
                _replace_with_symlink(link, dest_abs)
                applied.append(f"symlink {link} → {dest_abs}")
        else:
            # store_symlink off: drop convenience links that point at the store
            if link.is_symlink():
                try:
                    points_store = link.resolve() == dest_abs
                except OSError:
                    points_store = True  # broken link → remove
                if points_store:
                    link.unlink()
                    applied.append(f"removed docs symlink {link} (store_symlink off)")

    env_path = host / ".env"
    _update_env_var(env_path, "TA_EJECT_TASKS", "true")
    _update_env_var(env_path, "TA_EJECTED_TASKS_PATH", str(dest_abs))
    _update_env_var(env_path, "TA_EJECTED_ISSUES_PATH", str(dest_abs))
    applied.append(f"update {env_path} eject paths → {dest_abs}")

    return applied


def _remote_default_branch(store: Path, remote_name: str = "origin") -> Optional[str]:
    """Best-effort remote HEAD branch name (requires fetch or network ls-remote)."""
    res = _git_out(
        store, "symbolic-ref", f"refs/remotes/{remote_name}/HEAD", check=False
    )
    if res.returncode == 0:
        ref = (res.stdout or "").strip()
        prefix = f"refs/remotes/{remote_name}/"
        if ref.startswith(prefix):
            return ref[len(prefix) :]

    res = _git_out(store, "ls-remote", "--symref", remote_name, "HEAD", check=False)
    if res.returncode == 0:
        for line in (res.stdout or "").splitlines():
            # ref: refs/heads/main\tHEAD
            if line.startswith("ref:") and "HEAD" in line:
                parts = line.split()
                if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
                    return parts[1][len("refs/heads/") :]

    heads = _remote_heads(store, remote_name)
    for preferred in ("main", "master"):
        if preferred in heads:
            return preferred
    if heads:
        return sorted(heads.keys())[0]
    return None


def _git_is_ancestor(store: Path, maybe_ancestor: str, maybe_descendant: str) -> bool:
    res = _git_out(
        store,
        "merge-base",
        "--is-ancestor",
        maybe_ancestor,
        maybe_descendant,
        check=False,
    )
    return res.returncode == 0


def _working_tree_clean(store: Path) -> bool:
    res = _git_out(store, "status", "--porcelain", check=False)
    return res.returncode == 0 and not (res.stdout or "").strip()


def _only_untracked_changes(store: Path) -> bool:
    """True when porcelain status has only untracked (``??``) lines — safe for branch switch."""
    res = _git_out(store, "status", "--porcelain", check=False)
    if res.returncode != 0:
        return False
    lines = [ln for ln in (res.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return True
    return all(ln.startswith("??") for ln in lines)


def reconcile_adopted_store_git(
    store: Path,
    *,
    remote_name: str = "origin",
    auto_fix: bool = True,
) -> Dict[str, Any]:
    """Fetch + verify branch/upstream for a pre-existing store being adopted.

    Used on the ``already_migrated`` path so we never silently certify a clone
    stuck on a stale/renamed branch (e.g. local ``master`` while remote HEAD is
    ``main``).

    Safe auto-fix (when ``auto_fix`` and the worktree is clean): switch to the
    remote's default branch when the local tip has no unpushed unique commits
    relative to the remote tip. Otherwise fail loudly — do not rewrite
    intentional local-only history.

    Returns a dict with keys: ``ok``, ``applied``, ``problems``, ``skipped``,
    ``local_branch``, ``remote_head``.
    """
    applied: List[str] = []
    problems: List[str] = []
    result: Dict[str, Any] = {
        "ok": True,
        "applied": applied,
        "problems": problems,
        "skipped": False,
        "local_branch": None,
        "remote_head": None,
    }

    store = store.expanduser().resolve()
    if not (store / ".git").exists() and not (store / ".git").is_file():
        # Bare host_tree without git — nothing to reconcile
        result["skipped"] = True
        return result

    remotes = _list_git_remotes(store)
    if remote_name not in remotes:
        result["skipped"] = True
        applied.append(f"no remote {remote_name!r}; skip branch reconcile")
        return result

    fetch = _git_out(store, "fetch", remote_name, "--prune", check=False)
    if fetch.returncode != 0:
        err = ((fetch.stderr or "") + (fetch.stdout or "")).strip()
        problems.append(
            f"git fetch {remote_name} failed: {err or 'unknown error'}. "
            "Cannot verify branch/upstream for the adopted store."
        )
        result["ok"] = False
        return result
    applied.append(f"fetched {remote_name}")

    local = _local_branch(store)
    result["local_branch"] = local
    remote_head = _remote_default_branch(store, remote_name)
    result["remote_head"] = remote_head
    heads = _remote_heads(store, remote_name)

    if not remote_head:
        problems.append(
            f"Could not determine {remote_name}'s default branch after fetch. "
            "Refusing to silently certify the adopted store."
        )
        result["ok"] = False
        return result

    origin_local = f"{remote_name}/{local}"
    origin_head = f"{remote_name}/{remote_head}"
    has_origin_local = local in heads
    has_origin_head = remote_head in heads

    if not has_origin_head:
        problems.append(
            f"Remote default branch {remote_head!r} missing from "
            f"{remote_name} heads after fetch."
        )
        result["ok"] = False
        return result

    # Healthy: on remote default and upstream exists
    if local == remote_head and has_origin_local:
        applied.append(
            f"branch OK: on {local} (matches {remote_name} HEAD), "
            f"{origin_local} present"
        )
        return result

    # Diagnose mismatch
    mismatch_bits = []
    if local != remote_head:
        mismatch_bits.append(
            f"local branch is {local!r} but {remote_name} HEAD is {remote_head!r}"
        )
    if not has_origin_local:
        mismatch_bits.append(
            f"upstream {origin_local} does not exist on the remote "
            f"(stale/renamed branch?)"
        )
    detail = "; ".join(mismatch_bits)

    # Unpushed / unique local commits → never auto-rewrite
    local_tip = _git_out(store, "rev-parse", "HEAD", check=False)
    local_sha = (local_tip.stdout or "").strip()
    remote_sha = heads.get(remote_head, "")
    unpushed_unique = False
    if local_sha and remote_sha:
        if not _git_is_ancestor(store, local_sha, remote_sha):
            # Local has commits not contained in remote HEAD
            unpushed_unique = True

    if unpushed_unique:
        problems.append(
            f"Adopted store branch mismatch: {detail}. "
            f"Local HEAD has commits not on {origin_head}; refusing automatic "
            f"checkout (would rewrite local work). Fix manually in {store}: "
            f"`git fetch {remote_name} && git checkout {remote_head}` "
            f"(or merge/rebase), then re-run migrate."
        )
        result["ok"] = False
        return result

    if not auto_fix:
        problems.append(
            f"Adopted store branch mismatch: {detail}. "
            f"Re-run without dry-run / with auto-fix to switch to {remote_head}."
        )
        result["ok"] = False
        return result

    # Allow untracked-only dirt (common for pre-existing store content not in
    # the clone's first commit). Block modified/staged tracked files.
    if not _working_tree_clean(store) and not _only_untracked_changes(store):
        problems.append(
            f"Adopted store branch mismatch: {detail}. "
            "Working tree has local modifications to tracked files; clean or "
            f"commit, then re-run migrate to switch to {remote_head}."
        )
        result["ok"] = False
        return result

    # Safe switch: track remote default branch
    # Prefer existing local branch of that name, else create from origin
    co = _git_out(
        store,
        "checkout",
        "-B",
        remote_head,
        origin_head,
        check=False,
    )
    if co.returncode != 0:
        err = ((co.stderr or "") + (co.stdout or "")).strip()
        problems.append(
            f"Adopted store branch mismatch: {detail}. "
            f"Automatic checkout of {remote_head} failed: {err}"
        )
        result["ok"] = False
        return result
    applied.append(
        f"reconciled branch: checkout -B {remote_head} {origin_head} "
        f"(was {local}; {detail})"
    )

    # Set upstream tracking
    _git_out(
        store,
        "branch",
        f"--set-upstream-to={origin_head}",
        remote_head,
        check=False,
    )
    applied.append(f"set upstream {remote_head} → {origin_head}")
    result["local_branch"] = remote_head
    return result


def migrate_store(
    host_path: Path,
    *,
    dry_run: bool = False,
    data_root: Optional[Path] = None,
) -> MigrationResult:
    """Migrate a host project's legacy task store into the machine data root.

    Safe defaults:
    - Refuses to overwrite an existing destination store
    - Verifies markers (and nested remotes) before considering success
    - Does **not** invent a git remote for host_tree stores
    - Removes host ``.task-agent/tasks`` eject path; optional docs/tasks symlink
      when store_symlink is on; .env points at the data-root store
    - On already_migrated: fetch + verify/reconcile branch vs remote HEAD
      (never silently certify a stale tracking branch)

    Args:
        host_path: Project root (or any path inside it).
        dry_run: If True, only return the plan; no filesystem changes.
        data_root: Override machine data root (tests).
    """
    plan = plan_migrate(host_path, data_root=data_root)

    if plan.errors:
        return MigrationResult(
            plan=plan,
            dry_run=dry_run,
            success=False,
            message="; ".join(plan.errors),
        )

    if plan.already_migrated:
        host = Path(plan.host_path)
        dest = Path(plan.destination)
        applied: List[str] = []

        # Always inspect git health of the pre-existing store (dry-run too).
        if dest.is_dir() and looks_like_store(dest):
            git_status = reconcile_adopted_store_git(
                dest,
                auto_fix=not dry_run,
            )
            # In dry-run, auto_fix is off: mismatch → fail with guidance
            if dry_run and not git_status["ok"]:
                # Re-run detection-only wording is already in problems
                return MigrationResult(
                    plan=plan,
                    dry_run=True,
                    success=False,
                    message=(
                        "Already migrated, but adopted store git state is bad: "
                        + "; ".join(git_status["problems"])
                    ),
                    applied_steps=list(git_status["applied"]),
                )
            if dry_run:
                return MigrationResult(
                    plan=plan,
                    dry_run=True,
                    success=True,
                    message="Already migrated (dry-run); store git OK",
                    applied_steps=list(git_status["applied"]),
                )

            if not git_status["ok"]:
                return MigrationResult(
                    plan=plan,
                    dry_run=False,
                    success=False,
                    message=(
                        "Already migrated, but refused to certify store: "
                        + "; ".join(git_status["problems"])
                    ),
                    applied_steps=list(git_status["applied"]),
                )
            applied.extend(git_status["applied"])

            applied.extend(_update_host_pointers(host, dest))
            write_host_store_config(host, plan.moniker)
            applied.append(f"wrote {host / '.ta-config.json'} store_moniker")
            # Ensure subject_origin is recorded if missing
            meta = read_store_meta(dest) or {}
            if plan.subject_origin and not meta.get("subject_origin"):
                write_store_meta(
                    dest,
                    moniker=plan.moniker,
                    remote=meta.get("remote") or git_remote_url(dest),
                    extra={**meta, "subject_origin": plan.subject_origin},
                )
                applied.append("recorded subject_origin in store.json")
            reg = MachineRegistry(data_root)
            reg.upsert(
                StoreEntry(
                    moniker=plan.moniker,
                    store_path=str(dest.resolve()),
                    host_paths=[str(host.resolve())],
                    remote=git_remote_url(dest),
                )
            )
            applied.append("registry upsert")
        elif dry_run:
            return MigrationResult(
                plan=plan,
                dry_run=True,
                success=True,
                message="Already migrated (dry-run)",
            )

        return MigrationResult(
            plan=plan,
            dry_run=False,
            success=True,
            message="Already migrated; host pointers refreshed; store git OK",
            applied_steps=applied,
        )

    if plan.kind == "none" or not plan.source:
        return MigrationResult(
            plan=plan,
            dry_run=dry_run,
            success=False,
            message=plan.errors[0] if plan.errors else "Nothing to migrate",
        )

    if dry_run:
        return MigrationResult(
            plan=plan,
            dry_run=True,
            success=True,
            message="Dry-run OK — no changes applied",
            applied_steps=list(plan.steps),
        )

    source = Path(plan.source)
    dest = Path(plan.destination)
    host = Path(plan.host_path)
    applied = []

    # Snapshot for rollback
    source_count = _count_entries(source)
    remotes_expected = dict(plan.remotes_before) if plan.kind == "nested_git" else None

    try:
        # If source is behind a symlink at eject path pointing outside,
        # we move the real directory; eject path becomes a dangling link then.
        eject = host / ".task-agent" / "tasks"
        source_was_eject_dir = (
            eject.exists()
            and not eject.is_symlink()
            and eject.resolve() == source.resolve()
        )

        _move_store_tree(source, dest)
        applied.append(f"moved {source} → {dest}")

        if plan.kind == "host_tree":
            _git_init_and_commit(
                dest,
                "chore: initial task store commit after centralization migrate",
            )
            applied.append("git init + initial commit (no remote)")

        write_store_meta(
            dest,
            moniker=plan.moniker,
            remote=(plan.remotes_before.get("origin") if plan.remotes_before else None),
            extra={
                "migrated_from": str(source),
                "migrated_at": datetime.now(timezone.utc).isoformat(),
                "kind": plan.kind,
                "subject_origin": plan.subject_origin,
                "remotes_before": plan.remotes_before or None,
            },
        )
        applied.append("wrote store.json")
        write_host_store_config(host, plan.moniker)
        applied.append(f"wrote {host / '.ta-config.json'} store_moniker")

        verify_errors = verify_store(dest, remotes_expected=remotes_expected)
        if source_count and _count_entries(dest) < max(1, source_count // 2):
            # Heuristic: catastrophic loss
            verify_errors.append(
                f"Entry count drop suspicious: source had ~{source_count}, "
                f"dest has {_count_entries(dest)}"
            )
        if verify_errors:
            raise RuntimeError("Verification failed: " + "; ".join(verify_errors))
        applied.append("verified store")

        # Host pointers: drop eject path; optional docs/tasks; env → store
        if source_was_eject_dir and not eject.exists():
            pass  # already moved away; _update_host_pointers is a no-op for eject
        applied.extend(_update_host_pointers(host, dest))

        reg = MachineRegistry(data_root)
        reg.ensure_layout()
        reg.upsert(
            StoreEntry(
                moniker=plan.moniker,
                store_path=str(dest.resolve()),
                host_paths=[str(host.resolve())],
                remote=git_remote_url(dest) if plan.kind == "nested_git" else None,
            )
        )
        applied.append(f"registry upsert {plan.moniker}")

    except Exception as e:
        # Best-effort rollback if dest exists and source is gone
        if dest.exists() and not source.exists():
            try:
                # Move back only if source parent still exists
                if source.parent.is_dir():
                    shutil.move(str(dest), str(source))
                    applied.append(f"rollback: moved {dest} → {source}")
            except Exception as rb:
                return MigrationResult(
                    plan=plan,
                    dry_run=False,
                    success=False,
                    message=(
                        f"Migration failed: {e}. Rollback also failed: {rb}. "
                        f"Manual recovery may be needed. Applied: {applied}"
                    ),
                    applied_steps=applied,
                )
        return MigrationResult(
            plan=plan,
            dry_run=False,
            success=False,
            message=f"Migration failed: {e}",
            applied_steps=applied,
        )

    return MigrationResult(
        plan=plan,
        dry_run=False,
        success=True,
        message=f"Migrated {plan.moniker} → {dest}",
        applied_steps=applied,
    )


def inspect_host(host_path: Path, data_root: Optional[Path] = None) -> Dict[str, Any]:
    """Read-only inspection of how a host path maps to moniker / store / legacy.

    Safe: does not create directories, registry entries, or move data.
    """
    host = host_path.expanduser().resolve()
    top = project_host_root(host)
    moniker, origin = resolve_moniker_for_host(top)
    root = data_root if data_root is not None else get_data_root()
    registry = MachineRegistry(root)
    entry = registry.get(moniker) or registry.find_by_host_path(top)
    canonical = store_path_for_moniker(moniker, root)
    legacy = detect_legacy_store(top)

    legacy_kind: Optional[str] = None
    legacy_remote: Optional[str] = None
    if legacy is not None:
        if legacy.resolve() == canonical.resolve():
            # Post-migrate: host path resolves into the data-root store
            legacy_kind = "centralized"
            legacy_remote = (
                git_remote_url(legacy) if is_nested_git_repo(legacy) else None
            )
        elif is_nested_git_repo(legacy):
            legacy_kind = "nested_git"
            legacy_remote = git_remote_url(legacy)
        else:
            legacy_kind = "host_tree"
            legacy_remote = None

    # Post-migrate ideal: no host eject path. Leftover .task-agent/tasks is stale.
    eject = top / ".task-agent" / "tasks"
    eject_present = eject.exists() or eject.is_symlink()

    host_cfg = read_host_store_config(top)
    has_host_moniker = bool(host_cfg.get("store_moniker") or host_cfg.get("moniker"))

    migrated = bool(
        canonical.is_dir()
        and looks_like_store(canonical)
        and (
            (entry and Path(entry.store_path).resolve() == canonical.resolve())
            or has_host_moniker
        )
    )
    # Unmigrated hosts still use .task-agent/tasks as the real store — that is fine.
    # Migrated hosts must not keep a host eject pointer.
    pointers_ok = (not eject_present) if migrated else True

    store_remotes: Dict[str, str] = {}
    subject_origin_meta = None
    if canonical.is_dir():
        store_remotes = _list_git_remotes(canonical)
        meta = read_store_meta(canonical) or {}
        subject_origin_meta = meta.get("subject_origin")

    return {
        "host_path": str(top),
        "moniker": moniker,
        "origin": origin,
        "subject_origin_recorded": subject_origin_meta,
        "data_root": str(root),
        "canonical_store_path": str(canonical),
        "canonical_store_exists": canonical.is_dir(),
        "store_remotes": store_remotes,
        "registry_entry": entry.to_dict() if entry else None,
        "legacy_store_path": str(legacy) if legacy else None,
        "legacy_kind": legacy_kind,
        "legacy_remote": legacy_remote,
        "migrated": migrated,
        "pointers_ok": pointers_ok,
    }


# ---------------------------------------------------------------------------
# Store remotes (Phase 4) — core speaks git URLs only; providers suggest
# ---------------------------------------------------------------------------


def default_remote_providers():
    """Built-in TasksRemoteProvider instances (forges register here).

    Core never hardcodes forge create/suggest logic outside plugins.
    """
    from taskagent.plugins.github import GitHubTasksRemoteProvider

    return [GitHubTasksRemoteProvider()]


def select_remote_provider(
    host_origin_url: str, *, provider_name: Optional[str] = None
):
    """Pick a TasksRemoteProvider for the subject origin (or by name)."""
    providers = default_remote_providers()
    if provider_name:
        for p in providers:
            if p.name == provider_name:
                return p
        raise ValueError(
            f"Unknown remote provider {provider_name!r}. "
            f"Available: {', '.join(p.name for p in providers)}"
        )
    matching = [p for p in providers if p.matches_origin(host_origin_url)]
    if not matching:
        raise ValueError(
            f"No remote provider matches subject origin {host_origin_url!r}. "
            "Install/register a forge plugin (github, future gitlab, …) "
            "or pass an explicit git URL to `ta store remote attach`."
        )
    return matching[0]


def suggest_store_remotes(
    host_path: Path,
    *,
    moniker: Optional[str] = None,
    origin_url: Optional[str] = None,
) -> List[Any]:
    """Collect remote suggestions from all registered providers.

    Returns a list of ``RemoteSuggestion`` (typed loosely to avoid import cycles
    at module load in constrained environments).
    """
    host = host_path.expanduser().resolve()
    top = project_host_root(host)
    if moniker is None or origin_url is None:
        derived_moniker, derived_origin = resolve_moniker_for_host(top)
        moniker = moniker or derived_moniker
        origin_url = origin_url if origin_url is not None else derived_origin

    if not origin_url:
        return []

    suggestions: List[Any] = []
    for provider in default_remote_providers():
        try:
            if not provider.matches_origin(origin_url):
                continue
            suggestions.extend(provider.suggest_remote(origin_url, moniker))
        except Exception:
            continue
    return suggestions


def create_and_attach_store_remote(
    host_path: Path,
    *,
    private: Optional[bool] = None,
    name: Optional[str] = None,
    provider_name: Optional[str] = None,
    attach: bool = True,
    dry_run: bool = False,
    data_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create a tasks remote via the matching forge plugin and optionally attach.

    Visibility:
      - If ``private`` is None, use subject repo visibility from the plugin API
        when available; otherwise default to private.
      - ``private=True/False`` forces visibility.

    Does not use interactive CLIs; providers use forge SDKs (e.g. githubkit).
    """
    host = project_host_root(host_path)
    moniker, origin = resolve_moniker_for_host(host)
    if not origin:
        raise ValueError(
            f"No git origin on subject host {host}; cannot create a tasks remote "
            "from moniker alone. Pass --name owner/repo-tasks and ensure a provider."
        )

    provider = select_remote_provider(origin, provider_name=provider_name)

    # Resolve visibility
    if private is None:
        detected = provider.subject_is_private(origin)
        private = True if detected is None else detected
        visibility_source = "subject" if detected is not None else "default-private"
    else:
        visibility_source = "flag"

    # Resolve store path for attach
    report = inspect_host(host, data_root=data_root)
    store = Path(report["canonical_store_path"])
    if report.get("registry_entry") and report["registry_entry"].get("store_path"):
        cand = Path(report["registry_entry"]["store_path"])
        if cand.is_dir():
            store = cand
    if not store.is_dir() or not looks_like_store(store):
        leg = detect_legacy_store(host)
        if leg is not None and looks_like_store(leg):
            store = leg
        else:
            raise FileNotFoundError(
                f"No task store for {host}. Run `ta store migrate` first."
            )

    suggestions = provider.suggest_remote(origin, moniker)
    planned_url = suggestions[0].url if suggestions else None

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "provider": provider.name,
            "moniker": moniker,
            "subject_origin": origin,
            "private": private,
            "visibility_source": visibility_source,
            "planned_name": name,
            "planned_url": planned_url,
            "store_path": str(store.resolve()),
            "attach": attach,
            "steps": [
                f"create empty tasks repo via {provider.name} API "
                f"({'private' if private else 'public'})",
                "attach + publish local store to that remote"
                if attach
                else "skip attach (--no-attach)",
            ],
        }

    created = provider.create_tasks_remote(origin, moniker, private=private, name=name)
    err = provider.validate_remote(created.url)
    if err:
        raise ValueError(err)

    result: Dict[str, Any] = {
        "ok": True,
        "dry_run": False,
        "provider": provider.name,
        "moniker": moniker,
        "subject_origin": origin,
        "private": created.private,
        "visibility_source": visibility_source,
        "created": created.created,
        "full_name": created.full_name,
        "url": created.url,
        "notes": created.notes,
        "store_path": str(store.resolve()),
        "attach": attach,
    }

    if attach:
        attach_info = attach_store_remote(
            store,
            created.url,
            moniker=moniker,
            data_root=data_root,
        )
        result["attach_result"] = attach_info
        result["status"] = attach_info.get("status")
    else:
        set_info = set_store_remote(
            store, created.url, moniker=moniker, data_root=data_root
        )
        result["set_result"] = set_info
        result["status"] = mission_remote_status(store)

    return result


class RepoNotFoundError(LookupError):
    """No registered store matched the query fragment."""


class AmbiguousRepoMatchError(LookupError):
    """Multiple stores matched the query equally well."""

    def __init__(self, query: str, candidates: List["ResolvedStore"]):
        self.query = query
        self.candidates = candidates
        monikers = ", ".join(c.moniker for c in candidates)
        super().__init__(f"Ambiguous repo query {query!r}: {monikers}")


@dataclass
class ResolvedStore:
    """A registry/store hit from fuzzy moniker or host-path matching."""

    moniker: str
    store_path: Path
    host_paths: List[str] = field(default_factory=list)
    remote: Optional[str] = None
    score: int = 0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "moniker": self.moniker,
            "store_path": str(self.store_path),
            "host_paths": list(self.host_paths),
            "remote": self.remote,
            "score": self.score,
            "reason": self.reason,
        }


def _score_repo_query(
    query: str, moniker: str, host_paths: List[str]
) -> tuple[int, str]:
    """Return (score, reason) for how well ``query`` matches this store.

    Higher is better. Zero means no match.
    """
    q = query.strip().lower()
    if not q:
        return 0, ""

    m = moniker.lower()
    basename = m.rsplit("/", 1)[-1]

    if m == q:
        return 100, "exact moniker"
    if basename == q:
        return 95, "exact moniker basename"
    if m.startswith(q) or basename.startswith(q):
        return 85, "moniker prefix"
    if q in m:
        return 75, "moniker substring"

    for hp in host_paths:
        try:
            p = Path(hp)
            name = p.name.lower()
            full = str(p).lower()
        except Exception:
            continue
        if name == q:
            return 65, f"host basename {p.name}"
        if q in name or q in full:
            return 55, f"host path {hp}"

    return 0, ""


def _effective_store_path(entry: StoreEntry) -> Optional[Path]:
    """Resolve a usable store directory for a registry entry (data-root or legacy)."""
    path = Path(entry.store_path).expanduser()
    try:
        if path.is_dir() and looks_like_store(path):
            return path.resolve()
    except OSError:
        pass

    for hp in entry.host_paths:
        try:
            host = Path(hp).expanduser()
            legacy = detect_legacy_store(host)
            if legacy is not None and looks_like_store(legacy):
                return legacy.resolve()
        except OSError:
            continue
    return None


def fuzzy_match_repos(
    query: str, data_root: Optional[Path] = None
) -> List[ResolvedStore]:
    """Rank registered stores by moniker/host-path fuzzy match (zoxide-style).

    Returns matches sorted by score descending (score > 0 only).
    """
    reg = MachineRegistry(data_root)
    hits: List[ResolvedStore] = []
    for entry in reg.list_entries():
        score, reason = _score_repo_query(query, entry.moniker, entry.host_paths)
        if score <= 0:
            continue
        store_path = _effective_store_path(entry)
        if store_path is None:
            continue
        hits.append(
            ResolvedStore(
                moniker=entry.moniker,
                store_path=store_path,
                host_paths=list(entry.host_paths),
                remote=entry.remote,
                score=score,
                reason=reason,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.moniker))
    return hits


def resolve_repo_query(query: str, data_root: Optional[Path] = None) -> ResolvedStore:
    """Resolve a zoxide-like fragment to exactly one registered store.

    Raises:
        RepoNotFoundError: no matches
        AmbiguousRepoMatchError: multiple top-scoring matches
    """
    q = (query or "").strip()
    if not q:
        raise RepoNotFoundError("Empty repo query")

    hits = fuzzy_match_repos(q, data_root=data_root)
    if not hits:
        raise RepoNotFoundError(f"No registered store matches {q!r}")

    best = hits[0].score
    top = [h for h in hits if h.score == best]
    if len(top) > 1:
        raise AmbiguousRepoMatchError(q, top)
    return top[0]


def manager_for_repo_query(
    query: str, data_root: Optional[Path] = None
) -> tuple[Any, ResolvedStore]:
    """Return ``(TaskAgent, ResolvedStore)`` for a moniker/host fragment."""
    from taskagent.manager import TaskAgent

    resolved = resolve_repo_query(query, data_root=data_root)
    return TaskAgent(config_dir=str(resolved.store_path)), resolved


def get_all_registered_managers(
    data_root: Optional[Path] = None,
) -> list[tuple[str, Any]]:
    """Return a list of ``(moniker, TaskAgent)`` pairs for all registered stores in the machine registry.

    Safely skips any store directory that fails to open or read.
    """

    root = data_root if data_root is not None else get_data_root()
    reg = MachineRegistry(root)
    entries = reg.load()
    results: list[tuple[str, Any]] = []

    for moniker in sorted(entries.keys()):
        try:
            mgr, _ = manager_for_repo_query(moniker, data_root=root)
            results.append((moniker, mgr))
        except Exception:
            continue
    return results


def rebind_store_moniker(
    host_path: Path,
    new_moniker: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Rebind a host project to a (possibly new) moniker after a repo rename.

    Resolution of the *current* store prefers:
    1. ``.ta-config.json`` / pyproject ``store_moniker``
    2. Registry by host path
    3. Moniker derived from current origin

    Then updates store.json, renames the data-root directory if needed,
    rewrites the host pointer, and updates the registry.

    Args:
        host_path: Subject project path.
        new_moniker: Explicit moniker; default = moniker from current host origin.
        data_root: Optional data root override.
    """
    host = host_path.expanduser().resolve()
    top = project_host_root(host)
    root = data_root if data_root is not None else get_data_root()
    reg = MachineRegistry(root)

    # Find existing binding
    derived, subject_origin = resolve_moniker_for_host(top)
    target_moniker = (new_moniker or derived).strip()
    if not target_moniker:
        raise ValueError("Cannot derive moniker; pass new_moniker explicitly")

    entry = reg.find_by_host_path(top) or reg.get(derived)
    # Also try ta-config moniker
    config_moniker = None
    cfg = top / ".ta-config.json"
    if cfg.is_file():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
            config_moniker = raw.get("store_moniker") or raw.get("moniker")
        except (OSError, json.JSONDecodeError):
            pass
    if entry is None and config_moniker:
        entry = reg.get(str(config_moniker))

    if entry is None:
        # Fall back to canonical path for derived moniker
        cand = store_path_for_moniker(derived, root)
        if not (cand.is_dir() and looks_like_store(cand)):
            raise FileNotFoundError(
                f"No registered store for host {top}; run ta store migrate first"
            )
        old_moniker = derived
        old_path = cand
    else:
        old_moniker = entry.moniker
        old_path = Path(entry.store_path).resolve()

    new_path = store_path_for_moniker(target_moniker, root)
    moved = False
    if old_path.resolve() != new_path.resolve():
        if new_path.exists():
            raise FileExistsError(
                f"Destination store already exists: {new_path}. "
                "Remove or choose a different moniker."
            )
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_path), str(new_path))
        moved = True
    else:
        new_path = old_path

    meta = read_store_meta(new_path) or {}
    write_store_meta(
        new_path,
        moniker=target_moniker,
        remote=meta.get("remote") or git_remote_url(new_path),
        extra={
            **{
                k: v
                for k, v in meta.items()
                if k not in ("moniker", "remote", "version")
            },
            "subject_origin": subject_origin or meta.get("subject_origin"),
            "previous_moniker": old_moniker
            if old_moniker != target_moniker
            else meta.get("previous_moniker"),
            "rebound_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    write_host_store_config(top, target_moniker)
    _update_host_pointers(top, new_path)

    # Registry: drop old moniker key if renamed
    entries = reg.load()
    if old_moniker in entries and old_moniker != target_moniker:
        old_entry = entries.pop(old_moniker)
        hosts = list(dict.fromkeys([*old_entry.host_paths, str(top)]))
        entries[target_moniker] = StoreEntry(
            moniker=target_moniker,
            store_path=str(new_path.resolve()),
            host_paths=hosts,
            remote=old_entry.remote or git_remote_url(new_path),
            registered_at=old_entry.registered_at,
        )
        reg.save(entries)
    else:
        reg.upsert(
            StoreEntry(
                moniker=target_moniker,
                store_path=str(new_path.resolve()),
                host_paths=[str(top)],
                remote=git_remote_url(new_path),
            )
        )

    return {
        "old_moniker": old_moniker,
        "new_moniker": target_moniker,
        "store_path": str(new_path.resolve()),
        "moved": moved,
        "subject_origin": subject_origin,
        "host_config": str(top / ".ta-config.json"),
    }


def set_store_remote(
    store_path: Path,
    url: str,
    *,
    remote_name: str = "origin",
    moniker: Optional[str] = None,
    data_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Set (or add) a git remote on a task store and update metadata/registry.

    Does not create the remote repository on a host; only configures local git.
    Does not fetch or push — use :func:`attach_store_remote` for a full reconnect.
    """
    store = store_path.expanduser().resolve()
    if not store.is_dir() or not looks_like_store(store):
        raise ValueError(f"Not a task store: {store}")

    url = url.strip()
    if not url:
        raise ValueError("Empty remote URL")

    # Ensure git repo
    if not (store / ".git").exists() and not is_nested_git_repo(store):
        _git_init_and_commit(
            store,
            "chore: initialize task store git before setting remote",
        )

    existing = _list_git_remotes(store)
    if remote_name in existing:
        subprocess.run(
            ["git", "-C", str(store), "remote", "set-url", remote_name, url],
            check=True,
            capture_output=True,
            shell=(os.name == "nt"),
        )
        action = "set-url"
    else:
        subprocess.run(
            ["git", "-C", str(store), "remote", "add", remote_name, url],
            check=True,
            capture_output=True,
            shell=(os.name == "nt"),
        )
        action = "add"

    # Resolve moniker for metadata
    meta = read_store_meta(store) or {}
    use_moniker = moniker or meta.get("moniker")
    if not use_moniker:
        try:
            use_moniker = moniker_from_remote(url)
        except ValueError:
            use_moniker = store.name.replace("_", "/", 1)

    write_store_meta(
        store,
        moniker=str(use_moniker),
        remote=url if remote_name == "origin" else meta.get("remote"),
        extra={
            "remote_name": remote_name,
            **(
                {"remotes": {**(meta.get("remotes") or {}), remote_name: url}}
                if True
                else {}
            ),
        },
    )

    reg = MachineRegistry(data_root)
    reg.upsert(
        StoreEntry(
            moniker=str(use_moniker),
            store_path=str(store),
            host_paths=[],
            remote=url if remote_name == "origin" else None,
        )
    )

    return {
        "action": action,
        "remote_name": remote_name,
        "url": url,
        "store_path": str(store),
        "moniker": use_moniker,
        "remotes": _list_git_remotes(store),
    }


def _git_out(
    store: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(store), *args],
        capture_output=True,
        text=True,
        check=check,
        shell=(os.name == "nt"),
    )


def _local_branch(store: Path) -> str:
    res = _git_out(store, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    name = (res.stdout or "").strip()
    if res.returncode != 0 or not name or name == "HEAD":
        return "main"
    return name


def _remote_heads(store: Path, remote_name: str = "origin") -> Dict[str, str]:
    """Return {branch: sha} for remote heads after fetch."""
    res = _git_out(store, "ls-remote", "--heads", remote_name, check=False)
    heads: Dict[str, str] = {}
    if res.returncode != 0:
        return heads
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith("refs/heads/"):
            heads[parts[1][len("refs/heads/") :]] = parts[0]
    return heads


def _histories_related(store: Path, local_ref: str, remote_ref: str) -> bool:
    """True if local and remote refs share a merge-base (related history)."""
    res = _git_out(store, "merge-base", local_ref, remote_ref, check=False)
    return res.returncode == 0 and bool((res.stdout or "").strip())


def _gh_repo_edit_default_branch(store: Path, branch: str) -> tuple[bool, str]:
    """Set GitHub default branch via ``gh repo edit`` run in the store cwd.

    Returns (ok, message). Prefers the user's default gh config when agent
    config cannot see private task remotes.
    """
    env = os.environ.copy()
    # Prefer personal gh host config if agent token cannot see private repos
    default_hosts = Path.home() / ".config" / "gh" / "default"
    if default_hosts.is_dir() and "GH_CONFIG_DIR" not in env:
        env["GH_CONFIG_DIR"] = str(default_hosts)

    if not shutil.which("gh"):
        return False, "gh CLI binary not found on PATH"

    attempts = [
        env,
        {k: v for k, v in env.items() if k != "GH_CONFIG_DIR"},
    ]
    last_err = ""
    for attempt_env in attempts:
        try:
            res = subprocess.run(
                ["gh", "repo", "edit", f"--default-branch={branch}"],
                cwd=str(store),
                capture_output=True,
                text=True,
                env=attempt_env,
                shell=(os.name == "nt"),
            )
            if res.returncode == 0:
                return True, (res.stdout or "").strip() or f"default branch → {branch}"
            last_err = ((res.stderr or "") + (res.stdout or "")).strip()
        except FileNotFoundError:
            return False, "gh CLI binary not found on PATH"
    return False, last_err or "gh repo edit failed"


def attach_store_remote(
    store_path: Path,
    url: str,
    *,
    remote_name: str = "origin",
    local_branch: Optional[str] = None,
    default_branch: str = "main",
    moniker: Optional[str] = None,
    data_root: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Connect a task store to an existing remote and publish local history.

    Lessons from the task-agent-tasks recovery:

    1. ``set`` alone only configures the URL — not enough for durability.
    2. After host_tree migrate, local history may be **unrelated** to a stale
       remote seed (e.g. old ``master`` eject commit).
    3. Do **not** force-push over a remote tip without preserving it. If
       histories are unrelated, **rename** each mismatched tip to
       ``mismatched_branch_{name}_{datetime}`` (kept for comparison), push
       local as ``main``, and set the default branch. Do **not** delete the
       mismatched history — tell the user how to compare via git or the web UI.
    4. If histories **are** related, a normal non-force push is enough.

    Steps when unrelated::

        set-url / add remote
        fetch
        rename each unrelated tip → mismatched_branch_<name>_<datetime>
        push local branch → origin/main (or default_branch)
        gh repo edit --default-branch main
        notify user about mismatched branches (no delete)

    Args:
        store_path: Centralized or legacy task store directory.
        url: Git remote URL.
        remote_name: Usually ``origin``.
        local_branch: Branch to publish (default: current HEAD branch).
        default_branch: Canonical remote branch name (default ``main``).
        dry_run: Plan only; no network mutations after set-url.
    """
    steps: List[str] = []
    warnings: List[str] = []

    set_info = set_store_remote(
        store_path,
        url,
        remote_name=remote_name,
        moniker=moniker,
        data_root=data_root,
    )
    store = Path(set_info["store_path"])
    steps.append(f"remote {set_info['action']}: {remote_name} → {url}")

    branch = local_branch or _local_branch(store)
    steps.append(f"local branch: {branch}")

    if dry_run:
        # Fetch-less plan using ls-remote (cannot classify relatedness without objects)
        heads = _remote_heads(store, remote_name)
        return {
            "ok": True,
            "dry_run": True,
            "mode": "plan",
            "store_path": str(store),
            "url": url,
            "local_branch": branch,
            "default_branch": default_branch,
            "remote_heads": heads,
            "steps": steps
            + [
                f"fetch {remote_name}",
                "if unrelated: rename remote tips → mismatched_branch_<name>_<datetime>",
                f"push {branch} → {remote_name}/{default_branch}",
                f"gh repo edit --default-branch {default_branch}",
                "notify user about mismatched branches (no delete)",
            ],
            "warnings": [
                "Dry-run does not fetch; run without --dry-run to publish",
                *warnings,
            ],
            "set": set_info,
        }

    # Fetch
    fetch = _git_out(store, "fetch", remote_name, "--prune", check=False)
    if fetch.returncode != 0:
        raise RuntimeError(
            f"git fetch failed: {(fetch.stderr or fetch.stdout or '').strip()}"
        )
    steps.append(f"fetched {remote_name}")

    heads = _remote_heads(store, remote_name)
    steps.append(f"remote heads: {sorted(heads) or '(none)'}")

    # Classify remote tips relative to local HEAD
    unrelated: List[str] = []
    related: List[str] = []
    local_ref = "HEAD"
    for rb in heads:
        remote_ref = f"{remote_name}/{rb}"
        # ensure remote-tracking ref exists after fetch
        if not _histories_related(store, local_ref, remote_ref):
            unrelated.append(rb)
        else:
            related.append(rb)

    mode = "fast_forward_or_push"
    mismatched: List[Dict[str, str]] = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    if heads and not related and unrelated:
        mode = "unrelated_rename_and_publish"
        warnings.append(
            "No shared history with remote tips. Renaming mismatched remote "
            "branches for comparison, then publishing local as default. "
            "Mismatched tips are kept — not deleted."
        )
        for rb in unrelated:
            # Keep original name in the renamed branch for easy identification
            safe = re.sub(r"[^\w.\-]+", "_", rb).strip("_") or "branch"
            rename_to = f"mismatched_branch_{safe}_{stamp}"
            sha = heads[rb]
            push_ren = _git_out(
                store,
                "push",
                remote_name,
                f"{sha}:refs/heads/{rename_to}",
                check=False,
            )
            if push_ren.returncode != 0:
                raise RuntimeError(
                    f"Failed to rename remote branch {rb!r} → {rename_to}: "
                    f"{(push_ren.stderr or push_ren.stdout or '').strip()}"
                )
            mismatched.append(
                {
                    "original": rb,
                    "renamed_to": rename_to,
                    "sha": sha,
                }
            )
            steps.append(
                f"renamed origin/{rb} → {rename_to} ({sha[:8]}) [kept for compare]"
            )
    elif not heads:
        mode = "empty_remote_publish"
        steps.append("remote has no heads; first publish")
    else:
        steps.append(f"related remote branches: {related}")

    # Publish local branch as default_branch (main)
    # Force only when replacing an unrelated tip on the same branch name
    # (the tip was already copied to mismatched_branch_* above).
    push_args = ["push", "-u", remote_name, f"{branch}:{default_branch}"]
    need_force = mode == "unrelated_rename_and_publish" and default_branch in unrelated
    if need_force:
        push_args.insert(1, "--force")
        steps.append(
            f"force-push {branch} → {remote_name}/{default_branch} "
            f"(prior tip saved as mismatched_branch_*)"
        )
    else:
        steps.append(f"push {branch} → {remote_name}/{default_branch}")

    push = _git_out(store, *push_args, check=False)
    if push.returncode != 0:
        err = (push.stderr or push.stdout or "").strip()
        raise RuntimeError(f"git push failed: {err}")

    steps.append(f"published {default_branch}")

    # Point remote default/HEAD at default_branch
    default_set = False
    gh_ok, gh_msg = _gh_repo_edit_default_branch(store, default_branch)
    if gh_ok:
        steps.append(f"default branch set to {default_branch} (gh)")
        default_set = True
    else:
        remote_url = url
        if remote_url.endswith(".git") or (
            Path(remote_url).exists() and (Path(remote_url) / "HEAD").exists()
        ):
            bare = Path(remote_url)
            if bare.is_dir() and (bare / "HEAD").is_file():
                sym = subprocess.run(
                    [
                        "git",
                        "--git-dir",
                        str(bare),
                        "symbolic-ref",
                        "HEAD",
                        f"refs/heads/{default_branch}",
                    ],
                    capture_output=True,
                    text=True,
                    shell=(os.name == "nt"),
                )
                if sym.returncode == 0:
                    steps.append(f"default branch set to {default_branch} (bare HEAD)")
                    default_set = True
        if not default_set:
            warnings.append(
                f"Could not set default branch via gh ({gh_msg}). "
                f"Run from the store: gh repo edit --default-branch {default_branch}"
            )

    # Notify: mismatched branches kept for user comparison (no deletes)
    if mismatched:
        names = ", ".join(m["renamed_to"] for m in mismatched)
        warnings.append(
            f"Mismatched remote history preserved as: {names}. "
            "Compare with git (e.g. git log main..mismatched_branch_…) "
            "or the host web UI branch list. Original tip SHAs are unchanged."
        )
        for m in mismatched:
            steps.append(
                f"compare tip: git log {default_branch}..{m['renamed_to']} "
                f"(was origin/{m['original']})"
            )

    # Prune local remote-tracking
    _git_out(store, "fetch", remote_name, "--prune", check=False)

    return {
        "ok": True,
        "dry_run": False,
        "mode": mode,
        "store_path": str(store),
        "url": url,
        "local_branch": branch,
        "default_branch": default_branch,
        "related": related,
        "unrelated": unrelated,
        "mismatched": mismatched,
        "steps": steps,
        "warnings": warnings,
        "set": set_info,
        "status": mission_remote_status(store),
    }
