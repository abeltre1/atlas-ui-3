"""File operations for the filesystem MCP server.

Pure functions over already-contained paths. Every caller resolves and checks
the path with ``mount_policy.safe_path`` first; nothing here widens access.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

from mount_policy import (
    MAX_SCAN_BYTES,
    MAX_SEARCH_FILES,
    MountConfig,
    is_hidden,
    relative,
    should_descend,
)


def read_window(file_path: Path, start_line: int, max_lines: int, max_bytes: int) -> Dict[str, Any]:
    """Return a window of a UTF-8 text file starting at ``start_line`` (1-based).

    Stops after ``max_lines`` lines (0 means no line limit) or when the next
    line would push the window past ``max_bytes``. ``has_more`` tells the
    caller whether to come back with ``next_start_line``.
    """
    lines: List[str] = []
    nbytes = 0
    end_line = start_line - 1
    has_more = False
    truncated_by_bytes = False
    with open(file_path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if lineno < start_line:
                continue
            if max_lines and len(lines) >= max_lines:
                has_more = True
                break
            encoded = len(line.encode("utf-8"))
            if nbytes + encoded > max_bytes:
                if lines:
                    has_more = True
                    truncated_by_bytes = True
                    break
                # A single line longer than the whole budget: return its head.
                line = line.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
                encoded = len(line.encode("utf-8"))
                truncated_by_bytes = True
                has_more = True
            lines.append(line)
            nbytes += encoded
            end_line = lineno
            if truncated_by_bytes:
                break
    return {
        "content": "".join(lines),
        "start_line": start_line,
        "end_line": end_line,
        "has_more": has_more,
        "next_start_line": end_line + 1 if has_more else None,
        "truncated_by_bytes": truncated_by_bytes,
    }


def _entry(config: MountConfig, path: Path, depth: int = 0) -> Dict[str, Any]:
    is_dir = path.is_dir()
    size = None
    if not is_dir:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
    return {
        "name": path.name,
        "path": relative(config, path),
        "type": "directory" if is_dir else "file",
        "size": size,
        "depth": depth,
    }


def _walk(start: Path, max_depth: int, include_hidden: bool):
    """Yield (dirpath, dirnames, filenames) with noise and hidden dirs pruned.

    Symlinked directories are listed but never descended, so a link that
    points outside the mount cannot pull its contents into a listing.
    """
    start_depth = len(start.parts)
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        depth = len(Path(dirpath).parts) - start_depth
        dirnames.sort()
        filenames.sort()
        if depth + 1 >= max_depth:
            # Entries at this level are reported by the caller; do not go deeper.
            kept = []
        else:
            kept = [d for d in dirnames if should_descend(d, include_hidden) and not os.path.islink(os.path.join(dirpath, d))]
        yield Path(dirpath), dirnames, filenames, depth
        dirnames[:] = kept


def list_entries(
    config: MountConfig, dir_path: Path, recursive: bool, max_depth: int, include_hidden: bool
) -> Tuple[List[Dict[str, Any]], bool]:
    items: List[Dict[str, Any]] = []
    limit = config.max_list_entries
    if not recursive:
        children = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        for child in children:
            if not include_hidden and is_hidden(child.name):
                continue
            if len(items) >= limit:
                return items, True
            items.append(_entry(config, child))
        return items, False

    for dirpath, dirnames, filenames, depth in _walk(dir_path, max_depth, include_hidden):
        names = [(d, True) for d in dirnames] + [(f, False) for f in filenames]
        for name, _is_dir in names:
            if not include_hidden and is_hidden(name):
                continue
            if len(items) >= limit:
                return items, True
            items.append(_entry(config, dirpath / name, depth + 1))
    return items, False


def _matches(pattern: str, name: str, rel_path: str) -> bool:
    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_path, pattern)


def glob_search(
    config: MountConfig, start: Path, pattern: str, max_results: int, include_hidden: bool
) -> Tuple[List[Dict[str, Any]], bool]:
    matches: List[Dict[str, Any]] = []
    limit = min(max_results, config.max_list_entries)
    for dirpath, dirnames, filenames, depth in _walk(start, max_depth=10_000, include_hidden=include_hidden):
        for name in [*dirnames, *filenames]:
            if not include_hidden and is_hidden(name):
                continue
            full = dirpath / name
            rel = relative(config, full)
            if not _matches(pattern, name, rel):
                continue
            if len(matches) >= limit:
                return matches, True
            matches.append(_entry(config, full, depth + 1))
    return matches, False


def content_search(
    config: MountConfig,
    start: Path,
    query: str,
    glob: str,
    max_results: int,
    case_sensitive: bool,
) -> Dict[str, Any]:
    needle = query if case_sensitive else query.casefold()
    matches: List[Dict[str, Any]] = []
    limit = min(max_results, config.max_list_entries)
    files_scanned = 0
    files_skipped = 0
    truncated = False
    for dirpath, _dirnames, filenames, _depth in _walk(start, max_depth=10_000, include_hidden=False):
        for name in filenames:
            if is_hidden(name) or not fnmatch.fnmatch(name, glob):
                continue
            full = dirpath / name
            if full.is_symlink():
                continue
            if files_scanned >= MAX_SEARCH_FILES:
                truncated = True
                break
            try:
                if full.stat().st_size > MAX_SCAN_BYTES:
                    files_skipped += 1
                    continue
                with open(full, "r", encoding="utf-8") as f:
                    files_scanned += 1
                    for lineno, line in enumerate(f, 1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle in haystack:
                            if len(matches) >= limit:
                                truncated = True
                                break
                            matches.append(
                                {"path": relative(config, full), "line": lineno, "text": line.rstrip("\r\n")[:200]}
                            )
            except (UnicodeDecodeError, OSError):
                files_skipped += 1
                continue
            if truncated:
                break
        if truncated:
            break
    return {
        "matches": matches,
        "truncated": truncated,
        "files_scanned": files_scanned,
        "files_skipped": files_skipped,
    }


def apply_edit(file_path: Path, old_text: str, new_text: str, replace_all: bool) -> int:
    """Replace ``old_text`` in the file. Returns the number of replacements.

    Raises ValueError when the text is missing, or ambiguous without
    ``replace_all``, so the model gets a reason instead of a silent no-op.
    """
    if not old_text:
        raise ValueError("old_text must not be empty")
    if file_path.stat().st_size > MAX_SCAN_BYTES:
        raise ValueError(f"File is larger than {MAX_SCAN_BYTES} bytes; edit_file refuses files that large")
    original = file_path.read_text(encoding="utf-8")
    count = original.count(old_text)
    if count == 0:
        raise ValueError("old_text was not found in the file; read it again and copy the text exactly")
    if count > 1 and not replace_all:
        raise ValueError(
            f"old_text occurs {count} times; include more surrounding text to make it unique, "
            "or set replace_all=true"
        )
    updated = original.replace(old_text, new_text) if replace_all else original.replace(old_text, new_text, 1)
    file_path.write_text(updated, encoding="utf-8")
    return count if replace_all else 1
