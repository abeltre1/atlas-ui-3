#!/usr/bin/env python3
"""
Filesystem MCP Server using FastMCP.

Mounts one local directory into the Atlas chat and exposes read/write
operations on it through MCP. Every tool is confined to that directory:
relative paths resolve against it, absolute paths must already be inside it,
and symlinks are followed before the check so they cannot point outside.

Configuration (environment variables, set via the ``env`` block of the
server's ``mcp.json`` entry):

- ``ATLAS_FS_ROOT``: absolute path of the directory to mount. Defaults to the
  process working directory (the ``cwd`` of the ``mcp.json`` entry). The server
  refuses to start when the path is missing or is not a directory.
- ``ATLAS_FS_READ_ONLY``: ``true`` registers only the read-only tools
  (``read_file``, ``list_directory``, ``file_exists``, ``get_mount_info``).
  The mutating tools are never advertised to the model. Defaults to ``false``.
- ``ATLAS_FS_MAX_READ_BYTES``: largest file ``read_file`` returns, in bytes.
  Defaults to 1 MiB. Larger files return an error instead of being read into
  the model's context.

This is a stdio server, so one process serves every user of the backend
replica that started it and the directory is that replica's local disk. It is
meant for a single-user or single-host deployment; see docs/admin/mcp-servers.md.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

from atlas.mcp_shared.server_factory import create_stdio_server

ROOT_ENV = "ATLAS_FS_ROOT"
READ_ONLY_ENV = "ATLAS_FS_READ_ONLY"
MAX_READ_BYTES_ENV = "ATLAS_FS_MAX_READ_BYTES"
DEFAULT_MAX_READ_BYTES = 1024 * 1024

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _resolve_root(raw: str | None) -> Path:
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


def _resolve_max_read_bytes(raw: str | None) -> int:
    if raw is None or raw.strip() == "":
        return DEFAULT_MAX_READ_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{MAX_READ_BYTES_ENV} must be an integer number of bytes, got {raw!r}")
    if value <= 0:
        raise ValueError(f"{MAX_READ_BYTES_ENV} must be positive, got {value}")
    return value


try:
    BASE_PATH = _resolve_root(os.environ.get(ROOT_ENV))
    MAX_READ_BYTES = _resolve_max_read_bytes(os.environ.get(MAX_READ_BYTES_ENV))
except ValueError as exc:
    print(f"filesystem MCP server: {exc}", file=sys.stderr)
    sys.exit(2)

READ_ONLY = _env_flag(READ_ONLY_ENV)

mcp = create_stdio_server("Filesystem")


def _safe_path(path: str) -> Path:
    """Resolve ``path`` and ensure it stays inside the mounted root.

    Relative paths resolve against the root. Absolute paths are accepted only
    when they are already inside the root. Symlinks are resolved before the
    containment check so a link pointing outside the root is rejected.
    """
    requested_path = Path(path)
    if requested_path.is_absolute():
        full_path = requested_path
    else:
        full_path = BASE_PATH / requested_path

    resolved_path = full_path.resolve()

    try:
        resolved_path.relative_to(BASE_PATH)
    except ValueError:
        raise PermissionError("Access denied: path outside the mounted directory")

    return resolved_path


def _relative(path: Path) -> str:
    rel = str(path.relative_to(BASE_PATH))
    return "." if rel == "" else rel


def _error(message: str) -> Dict[str, Any]:
    return {"results": {"error": message}}


def get_mount_info() -> Dict[str, Any]:
    """
    Report which local directory this server exposes and whether it is writable.

    Call this first when you need to know where files live or whether you can
    create, modify, or delete them. All other tools in this server operate
    relative to the reported root and cannot reach outside it.

    Returns:
        Dictionary containing:
        - root: absolute path of the mounted directory
        - read_only: true when only read, list, and exists tools are available
        - max_read_bytes: largest file read_file will return
    """
    return {
        "results": {
            "root": str(BASE_PATH),
            "read_only": READ_ONLY,
            "max_read_bytes": MAX_READ_BYTES,
        }
    }


def read_file(path: str) -> Dict[str, Any]:
    """
    Read the complete contents of a UTF-8 text file inside the mounted directory.

    - Relative paths resolve from the mounted root; absolute paths must be inside it
    - Files larger than the configured read limit are refused (see get_mount_info)
    - Returns the content together with its size and root-relative path

    Args:
        path: File path to read, relative to the mounted root or absolute within it.

    Returns:
        Dictionary containing:
        - content: complete file contents (UTF-8)
        - size: file size in bytes
        - path: path relative to the mounted root
        Or an error message if the file is missing, too large, or outside the root
    """
    try:
        file_path = _safe_path(path)
        if not file_path.exists():
            return _error(f"File not found: {path}")

        if file_path.is_dir():
            return _error(f"Path is a directory: {path}")

        size = file_path.stat().st_size
        if size > MAX_READ_BYTES:
            return _error(
                f"File is {size} bytes, larger than the {MAX_READ_BYTES} byte read limit: {path}"
            )

        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        return {
            "results": {
                "content": content,
                "size": size,
                "path": _relative(file_path),
            }
        }
    except PermissionError as e:
        return _error(str(e))
    except UnicodeDecodeError:
        return _error(f"File is not UTF-8 text: {path}")
    except Exception as e:
        return _error(f"Error reading file: {str(e)}")


def write_file(path: str, content: str) -> Dict[str, Any]:
    """
    Write UTF-8 text to a file inside the mounted directory, creating parent directories as needed.

    - Overwrites an existing file completely
    - Parent directories are created, but only inside the mounted root
    - Not available when the server is mounted read-only

    Args:
        path: File path to write, relative to the mounted root or absolute within it.
        content: Text content to write (UTF-8).

    Returns:
        Dictionary containing:
        - success: true on success
        - path: path relative to the mounted root
        - size: number of characters written
        Or an error message if the path is outside the root or cannot be written
    """
    try:
        file_path = _safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "results": {
                "success": True,
                "path": _relative(file_path),
                "size": len(content),
            }
        }
    except PermissionError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Error writing file: {str(e)}")


def list_directory(path: str = ".") -> Dict[str, Any]:
    """
    List the files and subdirectories of a directory inside the mounted directory.

    - Defaults to the mounted root when no path is given
    - Each entry reports its name, type (file or directory), and size for files
    - Entries are sorted with directories first, then by name

    Args:
        path: Directory path to list, relative to the mounted root. Defaults to the root.

    Returns:
        Dictionary containing:
        - path: path relative to the mounted root
        - items: list of entries with name, type, and size (files only)
        Or an error message if the directory is missing or outside the root
    """
    try:
        dir_path = _safe_path(path)
        if not dir_path.exists():
            return _error(f"Directory not found: {path}")

        if not dir_path.is_dir():
            return _error(f"Path is not a directory: {path}")

        items = []
        for item in dir_path.iterdir():
            items.append(
                {
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )

        return {
            "results": {
                "path": _relative(dir_path),
                "items": sorted(items, key=lambda x: (x["type"], x["name"])),
            }
        }
    except PermissionError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Error listing directory: {str(e)}")


def create_directory(path: str) -> Dict[str, Any]:
    """
    Create a directory inside the mounted directory, including missing parents.

    - Succeeds silently if the directory already exists
    - Not available when the server is mounted read-only

    Args:
        path: Directory path to create, relative to the mounted root or absolute within it.

    Returns:
        Dictionary containing:
        - success: true on success
        - path: path relative to the mounted root
        Or an error message if the path is outside the root or cannot be created
    """
    try:
        dir_path = _safe_path(path)
        dir_path.mkdir(parents=True, exist_ok=True)

        return {"results": {"success": True, "path": _relative(dir_path)}}
    except PermissionError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Error creating directory: {str(e)}")


def delete_file(path: str) -> Dict[str, Any]:
    """
    Permanently delete a single file inside the mounted directory.

    - Deletes files only; directories are refused
    - This cannot be undone
    - Not available when the server is mounted read-only

    Args:
        path: File path to delete, relative to the mounted root or absolute within it.

    Returns:
        Dictionary containing:
        - success: true on success
        - path: path relative to the mounted root
        Or an error message if the file is missing, is a directory, or is outside the root
    """
    try:
        file_path = _safe_path(path)
        if not file_path.exists():
            return _error(f"File not found: {path}")

        if file_path.is_dir():
            return _error(f"Path is a directory, not a file: {path}")

        file_path.unlink()
        return {"results": {"success": True, "path": _relative(file_path)}}
    except PermissionError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Error deleting file: {str(e)}")


def file_exists(path: str) -> Dict[str, Any]:
    """
    Check whether a path exists inside the mounted directory and what kind of entry it is.

    Args:
        path: Path to check, relative to the mounted root or absolute within it.

    Returns:
        Dictionary containing:
        - exists: whether the path exists
        - is_file: whether it is a regular file
        - is_directory: whether it is a directory
        - path: path relative to the mounted root
        Or an error message if the path is outside the root
    """
    try:
        file_path = _safe_path(path)
        return {
            "results": {
                "exists": file_path.exists(),
                "is_file": file_path.is_file(),
                "is_directory": file_path.is_dir(),
                "path": _relative(file_path),
            }
        }
    except PermissionError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Error checking path: {str(e)}")


READ_TOOLS = (get_mount_info, read_file, list_directory, file_exists)
WRITE_TOOLS = (write_file, create_directory, delete_file)

for _tool in READ_TOOLS:
    mcp.tool(_tool)

if not READ_ONLY:
    for _tool in WRITE_TOOLS:
        mcp.tool(_tool)


if __name__ == "__main__":
    mcp.run(show_banner=False)
