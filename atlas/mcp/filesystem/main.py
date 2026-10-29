#!/usr/bin/env python3
"""
Filesystem MCP Server using FastMCP.

Mounts one local directory into the Atlas chat and exposes file operations
on it through MCP. Every tool is confined to that directory: relative paths
resolve against it, absolute paths must already be inside it, and symlinks
are followed before the check so they cannot point outside.

Configuration (environment variables, set via the ``env`` block of the
server's ``mcp.json`` entry):

- ``ATLAS_FS_ROOT``: absolute path of the directory to mount. Defaults to the
  process working directory (the ``cwd`` of the ``mcp.json`` entry). The server
  refuses to start when the path is missing or is not a directory.
- ``ATLAS_FS_READ_ONLY``: ``true`` registers only the read-only tools. The
  mutating tools are never advertised to the model. Defaults to ``false``.
- ``ATLAS_FS_MAX_READ_BYTES``: most text one ``read_file`` call returns, in
  bytes. Defaults to 128 KiB; larger files are read in pages.
- ``ATLAS_FS_MAX_LIST_ENTRIES``: most entries one listing or search returns.
  Defaults to 500.

This is a stdio server, so one process serves every user of the backend
replica that started it and the directory is that replica's local disk. It is
meant for a single-user or single-host deployment; see docs/admin/mcp-servers.md.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mount_operations import (  # noqa: E402
    apply_edit,
    content_search,
    glob_search,
    list_entries,
    read_window,
)
from mount_policy import (  # noqa: E402
    MountConfig,
    error,
    load_mount_config,
    relative,
    safe_path,
)

from atlas.mcp_shared.server_factory import create_stdio_server  # noqa: E402

try:
    CONFIG: MountConfig = load_mount_config()
except ValueError as exc:
    print(f"filesystem MCP server: {exc}", file=sys.stderr)
    sys.exit(2)

BASE_PATH = CONFIG.root
READ_ONLY = CONFIG.read_only
MAX_READ_BYTES = CONFIG.max_read_bytes
MAX_LIST_ENTRIES = CONFIG.max_list_entries

mcp = create_stdio_server("Filesystem")


def _safe_path(path: str) -> Path:
    return safe_path(CONFIG, path)


def _relative(path: Path) -> str:
    return relative(CONFIG, path)


def get_mount_info() -> Dict[str, Any]:
    """
    Report which local directory this server exposes and whether it is writable.

    Call this first when you need to know where files live or whether you can
    create, modify, or delete them. All other tools in this server operate
    relative to the reported root and cannot reach outside it.

    Returns:
        Dictionary containing:
        - root: absolute path of the mounted directory
        - read_only: true when only read, list, search, and exists tools are available
        - max_read_bytes: most text one read_file call returns; longer files are paged
        - max_list_entries: most entries one listing or search returns
        - tools: names of the tools this server currently exposes
    """
    return {
        "results": {
            "root": str(BASE_PATH),
            "read_only": READ_ONLY,
            "max_read_bytes": MAX_READ_BYTES,
            "max_list_entries": MAX_LIST_ENTRIES,
            "tools": [t.__name__ for t in (READ_TOOLS if READ_ONLY else READ_TOOLS + WRITE_TOOLS)],
        }
    }


def read_file(path: str, start_line: int = 1, max_lines: int = 0) -> Dict[str, Any]:
    """
    Read a UTF-8 text file inside the mounted directory, one page at a time.

    - Returns lines from start_line onward, up to max_lines lines (0 = no line
      limit) and never more than the server's read byte limit
    - When has_more is true, call again with start_line = next_start_line to
      continue; large files are meant to be read in pages, not all at once
    - Use search_content first to find the lines you need in a large file

    Args:
        path: File path to read, relative to the mounted root or absolute within it.
        start_line: First line to return (1-based). Defaults to 1.
        max_lines: Maximum number of lines to return. 0 means as many as fit.

    Returns:
        Dictionary containing:
        - content: the text of the returned lines
        - path: path relative to the mounted root
        - size: whole file size in bytes
        - start_line, end_line: the range returned (end_line 0 for an empty file)
        - has_more: whether lines remain after end_line
        - next_start_line: where to continue when has_more is true
        - truncated_by_bytes: true when the byte limit cut the page short
        Or an error message if the file is missing, not text, or outside the root
    """
    try:
        if start_line < 1:
            return error("start_line must be 1 or greater")
        if max_lines < 0:
            return error("max_lines must be 0 or greater")
        file_path = _safe_path(path)
        if not file_path.exists():
            return error(f"File not found: {path}")
        if file_path.is_dir():
            return error(f"Path is a directory: {path}")
        window = read_window(file_path, start_line, max_lines, MAX_READ_BYTES)
        window.update({"path": _relative(file_path), "size": file_path.stat().st_size})
        return {"results": window}
    except PermissionError as e:
        return error(str(e))
    except UnicodeDecodeError:
        return error(f"File is not UTF-8 text: {path}")
    except Exception as e:
        return error(f"Error reading file: {str(e)}")


def list_directory(
    path: str = ".", recursive: bool = False, max_depth: int = 3, include_hidden: bool = False
) -> Dict[str, Any]:
    """
    List the files and subdirectories inside the mounted directory.

    - Defaults to one level of the mounted root; set recursive=true to walk
      subdirectories down to max_depth levels
    - Recursive walks skip version-control, dependency, and cache directories
      such as .git, node_modules, and __pycache__, and never follow symlinks
    - Dot-files and dot-directories are omitted unless include_hidden=true
    - Returns at most the server's entry limit; truncated=true means there
      were more, so narrow the path or use search_files

    Args:
        path: Directory to list, relative to the mounted root. Defaults to the root.
        recursive: Walk subdirectories as well. Defaults to false.
        max_depth: How many levels to descend when recursive. Defaults to 3.
        include_hidden: Include dot-prefixed entries. Defaults to false.

    Returns:
        Dictionary containing:
        - path: listed directory relative to the mounted root
        - items: entries with name, path (relative to the root), type, size (files), depth
        - truncated: true when the entry limit cut the listing short
        Or an error message if the directory is missing or outside the root
    """
    try:
        if max_depth < 1:
            return error("max_depth must be 1 or greater")
        dir_path = _safe_path(path)
        if not dir_path.exists():
            return error(f"Directory not found: {path}")
        if not dir_path.is_dir():
            return error(f"Path is not a directory: {path}")
        items, truncated = list_entries(CONFIG, dir_path, recursive, max_depth, include_hidden)
        return {"results": {"path": _relative(dir_path), "items": items, "truncated": truncated}}
    except PermissionError as e:
        return error(str(e))
    except Exception as e:
        return error(f"Error listing directory: {str(e)}")


def search_files(pattern: str, path: str = ".", max_results: int = 200, include_hidden: bool = False) -> Dict[str, Any]:
    """
    Find files and directories by name pattern anywhere under a directory in the mount.

    - pattern is a shell-style glob matched against each entry's name and its
      path relative to the mounted root, e.g. "*.csv", "report_*", "src/*.py"
    - Walks every level below path, skipping version-control, dependency, and
      cache directories, and never follows symlinks

    Args:
        pattern: Glob pattern to match, e.g. "*.md".
        path: Directory to search under, relative to the mounted root. Defaults to the root.
        max_results: Most matches to return. Defaults to 200.
        include_hidden: Include dot-prefixed entries. Defaults to false.

    Returns:
        Dictionary containing:
        - matches: entries with name, path, type, size, depth
        - truncated: true when max_results or the server's entry limit cut the search short
        Or an error message if the directory is missing or outside the root
    """
    try:
        if not pattern:
            return error("pattern must not be empty")
        if max_results < 1:
            return error("max_results must be 1 or greater")
        start = _safe_path(path)
        if not start.is_dir():
            return error(f"Directory not found: {path}")
        matches, truncated = glob_search(CONFIG, start, pattern, max_results, include_hidden)
        return {"results": {"path": _relative(start), "pattern": pattern, "matches": matches, "truncated": truncated}}
    except PermissionError as e:
        return error(str(e))
    except Exception as e:
        return error(f"Error searching files: {str(e)}")


def search_content(
    query: str, path: str = ".", glob: str = "*", max_results: int = 100, case_sensitive: bool = False
) -> Dict[str, Any]:
    """
    Find lines containing a text snippet in files under a directory in the mount.

    - Plain substring search, case-insensitive unless case_sensitive=true
    - glob limits which file names are scanned, e.g. "*.py" or "*.log"
    - Binary files, hidden files, and files larger than 10 MiB are skipped
    - Each match reports the file, line number, and the line's text, so you can
      follow up with read_file at that start_line

    Args:
        query: Text to look for.
        path: Directory to search under, relative to the mounted root. Defaults to the root.
        glob: File name pattern to scan. Defaults to every file.
        max_results: Most matching lines to return. Defaults to 100.
        case_sensitive: Match case exactly. Defaults to false.

    Returns:
        Dictionary containing:
        - matches: list of {path, line, text}
        - truncated: true when the result or file limit cut the search short
        - files_scanned, files_skipped: how much of the tree was examined
        Or an error message if the directory is missing or outside the root
    """
    try:
        if not query:
            return error("query must not be empty")
        if max_results < 1:
            return error("max_results must be 1 or greater")
        start = _safe_path(path)
        if not start.is_dir():
            return error(f"Directory not found: {path}")
        found = content_search(CONFIG, start, query, glob, max_results, case_sensitive)
        found.update({"path": _relative(start), "query": query})
        return {"results": found}
    except PermissionError as e:
        return error(str(e))
    except Exception as e:
        return error(f"Error searching content: {str(e)}")


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
        return error(str(e))
    except Exception as e:
        return error(f"Error checking path: {str(e)}")


def write_file(path: str, content: str, append: bool = False) -> Dict[str, Any]:
    """
    Write UTF-8 text to a file inside the mounted directory, creating parent directories as needed.

    - Overwrites an existing file completely unless append=true
    - Parent directories are created, but only inside the mounted root
    - To change part of an existing file, prefer edit_file
    - Not available when the server is mounted read-only

    Args:
        path: File path to write, relative to the mounted root or absolute within it.
        content: Text content to write (UTF-8).
        append: Add to the end of the file instead of replacing it. Defaults to false.

    Returns:
        Dictionary containing:
        - success: true on success
        - path: path relative to the mounted root
        - size: number of characters written
        - appended: whether the content was appended
        Or an error message if the path is outside the root or cannot be written
    """
    try:
        file_path = _safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return {"results": {"success": True, "path": _relative(file_path), "size": len(content), "appended": append}}
    except PermissionError as e:
        return error(str(e))
    except Exception as e:
        return error(f"Error writing file: {str(e)}")


def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> Dict[str, Any]:
    """
    Replace an exact snippet of text in a file inside the mounted directory.

    - old_text must match the file exactly, including whitespace; read the file
      first and copy the snippet
    - Fails when old_text is missing, or appears more than once unless
      replace_all=true, so nothing changes by accident
    - Keeps the rest of the file untouched; use write_file to replace a whole file
    - Not available when the server is mounted read-only

    Args:
        path: File path to edit, relative to the mounted root or absolute within it.
        old_text: Exact text to replace.
        new_text: Replacement text (may be empty to delete old_text).
        replace_all: Replace every occurrence instead of requiring a unique match. Defaults to false.

    Returns:
        Dictionary containing:
        - success: true on success
        - path: path relative to the mounted root
        - replacements: how many occurrences were replaced
        Or an error message explaining why nothing was changed
    """
    try:
        file_path = _safe_path(path)
        if not file_path.is_file():
            return error(f"File not found: {path}")
        replacements = apply_edit(file_path, old_text, new_text, replace_all)
        return {"results": {"success": True, "path": _relative(file_path), "replacements": replacements}}
    except PermissionError as e:
        return error(str(e))
    except UnicodeDecodeError:
        return error(f"File is not UTF-8 text: {path}")
    except ValueError as e:
        return error(str(e))
    except Exception as e:
        return error(f"Error editing file: {str(e)}")


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
        return error(str(e))
    except Exception as e:
        return error(f"Error creating directory: {str(e)}")


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
            return error(f"File not found: {path}")
        if file_path.is_dir():
            return error(f"Path is a directory, not a file: {path}")
        file_path.unlink()
        return {"results": {"success": True, "path": _relative(file_path)}}
    except PermissionError as e:
        return error(str(e))
    except Exception as e:
        return error(f"Error deleting file: {str(e)}")


READ_TOOLS = (get_mount_info, read_file, list_directory, search_files, search_content, file_exists)
WRITE_TOOLS = (write_file, edit_file, create_directory, delete_file)

for _tool in READ_TOOLS:
    mcp.tool(_tool)

if not READ_ONLY:
    for _tool in WRITE_TOOLS:
        mcp.tool(_tool)


if __name__ == "__main__":
    mcp.run(show_banner=False)
