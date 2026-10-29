"""Mount configuration and path containment for the filesystem MCP server.

Everything the server is allowed to touch is decided here: which directory is
mounted, whether it is writable, how much of a file one read may return, and
how a caller-supplied path is resolved and checked against the mount root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ROOT_ENV = "ATLAS_FS_ROOT"
READ_ONLY_ENV = "ATLAS_FS_READ_ONLY"
MAX_READ_BYTES_ENV = "ATLAS_FS_MAX_READ_BYTES"
MAX_LIST_ENTRIES_ENV = "ATLAS_FS_MAX_LIST_ENTRIES"

# One read returns at most this many bytes of text. 128 KiB is roughly 32k
# tokens, which leaves room in a 128k-token context for the rest of the turn.
# Larger files are read in pages with read_file's start_line parameter.
DEFAULT_MAX_READ_BYTES = 128 * 1024
# One listing or search returns at most this many entries.
DEFAULT_MAX_LIST_ENTRIES = 500
# Files larger than this are skipped by search_content and refused by edit_file.
MAX_SCAN_BYTES = 10 * 1024 * 1024
# search_content stops looking after this many files.
MAX_SEARCH_FILES = 5000

# Directories that are almost never what a user wants an agent to walk and
# that are high-noise or high-risk (secrets, huge vendored trees). Never
# descended into by recursive listing or search.
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        ".next",
        ".cache",
    }
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class MountConfig:
    root: Path
    read_only: bool
    max_read_bytes: int
    max_list_entries: int


def env_flag(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def resolve_root(raw: Optional[str]) -> Path:
    """Turn the configured root into an existing, resolved directory path.

    Raises ValueError with an operator-readable message when the root cannot
    be used, so the server fails at startup rather than on the first tool call.
    """
    candidate = Path(raw).expanduser() if raw and raw.strip() else Path(".")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f"{ROOT_ENV} points to a path that does not exist: {candidate}")
    except OSError as exc:
        raise ValueError(f"{ROOT_ENV} could not be resolved ({candidate}): {exc}")
    if not resolved.is_dir():
        raise ValueError(f"{ROOT_ENV} must be a directory, not a file: {resolved}")
    return resolved


def resolve_positive_int(name: str, raw: Optional[str], default: int) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def load_mount_config(environ: Optional[Mapping[str, str]] = None) -> MountConfig:
    """Build the mount configuration from the environment. Raises ValueError."""
    env = os.environ if environ is None else environ
    return MountConfig(
        root=resolve_root(env.get(ROOT_ENV)),
        read_only=env_flag(env, READ_ONLY_ENV),
        max_read_bytes=resolve_positive_int(MAX_READ_BYTES_ENV, env.get(MAX_READ_BYTES_ENV), DEFAULT_MAX_READ_BYTES),
        max_list_entries=resolve_positive_int(
            MAX_LIST_ENTRIES_ENV, env.get(MAX_LIST_ENTRIES_ENV), DEFAULT_MAX_LIST_ENTRIES
        ),
    )


def safe_path(config: MountConfig, path: str) -> Path:
    """Resolve ``path`` and ensure it stays inside the mounted root.

    Relative paths resolve against the root. Absolute paths are accepted only
    when they are already inside the root. Symlinks are resolved before the
    containment check so a link pointing outside the root is rejected.
    """
    requested = Path(path)
    full_path = requested if requested.is_absolute() else config.root / requested
    resolved = full_path.resolve()
    try:
        resolved.relative_to(config.root)
    except ValueError:
        raise PermissionError("Access denied: path outside the mounted directory")
    return resolved


def relative(config: MountConfig, path: Path) -> str:
    rel = path.relative_to(config.root).as_posix()
    return "." if rel == "" else rel


def error(message: str) -> Dict[str, Any]:
    return {"results": {"error": message}}


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def should_descend(name: str, include_hidden: bool) -> bool:
    if name in IGNORED_DIRS:
        return False
    return include_hidden or not is_hidden(name)
