"""Tests for the bundled filesystem MCP server (``atlas/mcp/filesystem``).

The server mounts one local directory, chosen with ``ATLAS_FS_ROOT``, and must
keep every tool inside it. These tests load the server module fresh per case so
each one can pick its own root, read-only flag, and limits.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[1] / "mcp" / "filesystem"
_MAIN = _SERVER_DIR / "main.py"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

READ_TOOL_NAMES = ["file_exists", "get_mount_info", "list_directory", "read_file", "search_content", "search_files"]
WRITE_TOOL_NAMES = ["create_directory", "delete_file", "edit_file", "write_file"]


def _load(monkeypatch, root=None, read_only=None, max_read=None, max_entries=None):
    for name in ("ATLAS_FS_ROOT", "ATLAS_FS_READ_ONLY", "ATLAS_FS_MAX_READ_BYTES", "ATLAS_FS_MAX_LIST_ENTRIES"):
        monkeypatch.delenv(name, raising=False)
    if root is not None:
        monkeypatch.setenv("ATLAS_FS_ROOT", str(root))
    if read_only is not None:
        monkeypatch.setenv("ATLAS_FS_READ_ONLY", read_only)
    if max_read is not None:
        monkeypatch.setenv("ATLAS_FS_MAX_READ_BYTES", str(max_read))
    if max_entries is not None:
        monkeypatch.setenv("ATLAS_FS_MAX_LIST_ENTRIES", str(max_entries))
    spec = importlib.util.spec_from_file_location("filesystem_main_under_test", _MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy():
    return sys.modules["mount_policy"]


async def _tool_names(module):
    return sorted(t.name for t in await module.mcp.list_tools())


@pytest.fixture
def mount(tmp_path):
    root = tmp_path / "mount"
    root.mkdir()
    (root / "notes.txt").write_text("hello\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "inner.txt").write_text("inner\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    return root, outside


# --- configuration ----------------------------------------------------------


def test_root_comes_from_env(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    assert module.BASE_PATH == root.resolve()
    info = module.get_mount_info()["results"]
    assert info["root"] == str(root.resolve())
    assert info["read_only"] is False
    assert info["max_read_bytes"] == _policy().DEFAULT_MAX_READ_BYTES
    assert info["max_list_entries"] == _policy().DEFAULT_MAX_LIST_ENTRIES
    assert sorted(info["tools"]) == sorted(READ_TOOL_NAMES + WRITE_TOOL_NAMES)


def test_root_defaults_to_cwd(monkeypatch, mount):
    root, _ = mount
    monkeypatch.chdir(root)
    module = _load(monkeypatch)
    assert module.BASE_PATH == root.resolve()


def test_root_expands_user_home(monkeypatch, mount):
    root, _ = mount
    monkeypatch.setenv("HOME", str(root.parent))
    module = _load(monkeypatch, root="~/mount")
    assert module.BASE_PATH == root.resolve()


@pytest.mark.asyncio
async def test_read_only_mode_hides_mutating_tools(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root, read_only="true")
    assert module.READ_ONLY is True
    info = module.get_mount_info()["results"]
    assert info["read_only"] is True
    assert sorted(info["tools"]) == READ_TOOL_NAMES
    assert await _tool_names(module) == READ_TOOL_NAMES


@pytest.mark.asyncio
async def test_writable_mode_registers_all_tools(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root, read_only="false")
    assert await _tool_names(module) == sorted(READ_TOOL_NAMES + WRITE_TOOL_NAMES)


# --- containment --------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    [
        "../outside/secret.txt",
        "sub/../../outside/secret.txt",
        "{outside}/secret.txt",
        "/etc/passwd",
    ],
)
def test_every_tool_rejects_paths_outside_root(monkeypatch, mount, escape):
    root, outside = mount
    module = _load(monkeypatch, root=root)
    path = escape.format(outside=outside)
    denied = "outside the mounted directory"
    assert denied in module.read_file(path)["results"]["error"]
    assert denied in module.list_directory(path)["results"]["error"]
    assert denied in module.search_files("*", path)["results"]["error"]
    assert denied in module.search_content("secret", path)["results"]["error"]
    assert denied in module.file_exists(path)["results"]["error"]
    assert denied in module.create_directory(path)["results"]["error"]
    assert denied in module.delete_file(path)["results"]["error"]
    assert denied in module.write_file(path, "x")["results"]["error"]
    assert denied in module.edit_file(path, "secret", "x")["results"]["error"]
    assert (outside / "secret.txt").read_text() == "secret\n"


def test_symlink_pointing_outside_root_is_rejected(monkeypatch, mount):
    root, outside = mount
    (root / "escape").symlink_to(outside)
    module = _load(monkeypatch, root=root)
    assert "outside the mounted directory" in module.read_file("escape/secret.txt")["results"]["error"]
    assert "outside the mounted directory" in module.list_directory("escape")["results"]["error"]
    # Recursive listing and search never descend into the link either.
    listed = module.list_directory(recursive=True)["results"]["items"]
    assert all(not i["path"].startswith("escape/") for i in listed)
    assert module.search_content("secret")["results"]["matches"] == []
    assert module.search_files("secret.txt")["results"]["matches"] == []


# --- reading ------------------------------------------------------------------


def test_reads_inside_root(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    result = module.read_file("notes.txt")["results"]
    assert result["content"] == "hello\n"
    assert result["path"] == "notes.txt"
    assert (result["start_line"], result["end_line"], result["has_more"]) == (1, 1, False)
    assert result["next_start_line"] is None
    nested = module.read_file(str(root / "sub" / "inner.txt"))["results"]
    assert nested["content"] == "inner\n"
    assert nested["path"] == "sub/inner.txt"


def test_read_file_pages_by_lines(monkeypatch, mount):
    root, _ = mount
    (root / "log.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)), encoding="utf-8")
    module = _load(monkeypatch, root=root)
    first = module.read_file("log.txt", max_lines=4)["results"]
    assert first["content"] == "line 1\nline 2\nline 3\nline 4\n"
    assert (first["start_line"], first["end_line"], first["has_more"], first["next_start_line"]) == (1, 4, True, 5)
    second = module.read_file("log.txt", start_line=first["next_start_line"], max_lines=4)["results"]
    assert second["content"].startswith("line 5\n") and second["end_line"] == 8
    last = module.read_file("log.txt", start_line=9)["results"]
    assert last["content"] == "line 9\nline 10\n"
    assert last["has_more"] is False
    beyond = module.read_file("log.txt", start_line=50)["results"]
    assert beyond["content"] == "" and beyond["has_more"] is False
    assert "1 or greater" in module.read_file("log.txt", start_line=0)["results"]["error"]


def test_read_limit_pages_large_files_instead_of_refusing(monkeypatch, mount):
    root, _ = mount
    (root / "big.txt").write_text("".join(f"{i:03d} abcdefghij\n" for i in range(100)), encoding="utf-8")
    module = _load(monkeypatch, root=root, max_read=150)
    assert module.MAX_READ_BYTES == 150
    page = module.read_file("big.txt")["results"]
    assert len(page["content"].encode()) <= 150
    assert page["truncated_by_bytes"] is True and page["has_more"] is True
    assert page["end_line"] == 10 and page["next_start_line"] == 11
    assert page["size"] == (root / "big.txt").stat().st_size


def test_single_oversized_line_is_cut_not_refused(monkeypatch, mount):
    root, _ = mount
    (root / "wide.txt").write_text("x" * 500 + "\nshort\n", encoding="utf-8")
    module = _load(monkeypatch, root=root, max_read=100)
    page = module.read_file("wide.txt")["results"]
    assert page["content"] == "x" * 100
    assert page["truncated_by_bytes"] is True and page["has_more"] is True


def test_non_utf8_file_reports_clean_error(monkeypatch, mount):
    root, _ = mount
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    module = _load(monkeypatch, root=root)
    assert "not UTF-8 text" in module.read_file("blob.bin")["results"]["error"]


# --- listing and search ---------------------------------------------------------


def test_list_directory_root(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    listing = module.list_directory()["results"]
    assert listing["path"] == "." and listing["truncated"] is False
    assert [(i["name"], i["type"], i["path"]) for i in listing["items"]] == [
        ("sub", "directory", "sub"),
        ("notes.txt", "file", "notes.txt"),
    ]


def test_list_directory_recursive_skips_noise_and_hidden(monkeypatch, mount):
    root, _ = mount
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("", encoding="utf-8")
    (root / ".secret").write_text("", encoding="utf-8")
    (root / "sub" / "deep" / "deeper").mkdir(parents=True)
    (root / "sub" / "deep" / "deeper" / "leaf.txt").write_text("", encoding="utf-8")
    module = _load(monkeypatch, root=root)
    paths = [i["path"] for i in module.list_directory(recursive=True)["results"]["items"]]
    assert "node_modules" in paths and "node_modules/pkg" not in paths
    assert not any(p.startswith(".git") or p == ".secret" for p in paths)
    assert "sub/inner.txt" in paths and "sub/deep/deeper" in paths
    assert "sub/deep/deeper/leaf.txt" not in paths  # beyond max_depth=3
    deeper = [i["path"] for i in module.list_directory(recursive=True, max_depth=4)["results"]["items"]]
    assert "sub/deep/deeper/leaf.txt" in deeper
    hidden = [i["path"] for i in module.list_directory(include_hidden=True)["results"]["items"]]
    assert ".secret" in hidden and ".git" in hidden


def test_list_directory_entry_cap(monkeypatch, mount):
    root, _ = mount
    for i in range(10):
        (root / f"f{i}.txt").write_text("", encoding="utf-8")
    module = _load(monkeypatch, root=root, max_entries=4)
    listing = module.list_directory()["results"]
    assert len(listing["items"]) == 4 and listing["truncated"] is True


def test_search_files_by_glob(monkeypatch, mount):
    root, _ = mount
    (root / "sub" / "report.csv").write_text("a,b\n", encoding="utf-8")
    (root / "data.csv").write_text("c,d\n", encoding="utf-8")
    module = _load(monkeypatch, root=root)
    found = module.search_files("*.csv")["results"]
    assert [m["path"] for m in found["matches"]] == ["data.csv", "sub/report.csv"]
    assert found["truncated"] is False
    scoped = module.search_files("*.csv", path="sub")["results"]
    assert [m["path"] for m in scoped["matches"]] == ["sub/report.csv"]
    capped = module.search_files("*", max_results=1)["results"]
    assert len(capped["matches"]) == 1 and capped["truncated"] is True
    assert "must not be empty" in module.search_files("")["results"]["error"]


def test_search_content_reports_file_and_line(monkeypatch, mount):
    root, _ = mount
    (root / "app.log").write_text("ok\nERROR disk full\nok\nerror again\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"\xff\xfeERROR")
    module = _load(monkeypatch, root=root)
    found = module.search_content("error")["results"]
    assert [(m["path"], m["line"], m["text"]) for m in found["matches"]] == [
        ("app.log", 2, "ERROR disk full"),
        ("app.log", 4, "error again"),
    ]
    assert found["files_skipped"] >= 1  # the binary file
    exact = module.search_content("error", case_sensitive=True)["results"]["matches"]
    assert [m["line"] for m in exact] == [4]
    only_txt = module.search_content("error", glob="*.txt")["results"]["matches"]
    assert only_txt == []
    capped = module.search_content("error", max_results=1)["results"]
    assert len(capped["matches"]) == 1 and capped["truncated"] is True


# --- writing ------------------------------------------------------------------


def test_write_create_delete_inside_root(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    assert module.create_directory("out/deep")["results"]["success"] is True
    assert (root / "out" / "deep").is_dir()
    written = module.write_file("out/deep/report.md", "# r\n")["results"]
    assert written["success"] is True and written["path"] == "out/deep/report.md"
    assert (root / "out" / "deep" / "report.md").read_text() == "# r\n"
    appended = module.write_file("out/deep/report.md", "more\n", append=True)["results"]
    assert appended["appended"] is True
    assert (root / "out" / "deep" / "report.md").read_text() == "# r\nmore\n"
    assert module.delete_file("out/deep/report.md")["results"]["success"] is True
    assert not (root / "out" / "deep" / "report.md").exists()
    assert "not a file" in module.delete_file("out")["results"]["error"]
    assert (root / "out").is_dir()


def test_edit_file_replaces_unique_snippet(monkeypatch, mount):
    root, _ = mount
    (root / "cfg.ini").write_text("[a]\nkey = 1\n[b]\nkey = 1\n", encoding="utf-8")
    module = _load(monkeypatch, root=root)
    ambiguous = module.edit_file("cfg.ini", "key = 1", "key = 2")["results"]
    assert "occurs 2 times" in ambiguous["error"]
    assert (root / "cfg.ini").read_text() == "[a]\nkey = 1\n[b]\nkey = 1\n"
    unique = module.edit_file("cfg.ini", "[a]\nkey = 1", "[a]\nkey = 2")["results"]
    assert unique["success"] is True and unique["replacements"] == 1
    assert (root / "cfg.ini").read_text() == "[a]\nkey = 2\n[b]\nkey = 1\n"
    everywhere = module.edit_file("cfg.ini", "key", "k", replace_all=True)["results"]
    assert everywhere["replacements"] == 2
    missing = module.edit_file("cfg.ini", "nope", "x")["results"]
    assert "was not found" in missing["error"]
    assert "File not found" in module.edit_file("absent.txt", "a", "b")["results"]["error"]


# --- startup --------------------------------------------------------------------


def _run_server(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_PROJECT_ROOT),
        "HOME": os.environ.get("HOME", "/"),
        **env_overrides,
    }
    return subprocess.run([sys.executable, str(_MAIN)], env=env, capture_output=True, text=True, timeout=30)


def test_missing_root_fails_at_startup(tmp_path):
    proc = _run_server({"ATLAS_FS_ROOT": str(tmp_path / "does-not-exist")})
    assert proc.returncode == 2
    assert "ATLAS_FS_ROOT points to a path that does not exist" in proc.stderr


def test_file_root_fails_at_startup(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("x")
    proc = _run_server({"ATLAS_FS_ROOT": str(target)})
    assert proc.returncode == 2
    assert "must be a directory" in proc.stderr


def test_bad_read_limit_fails_at_startup(tmp_path):
    proc = _run_server({"ATLAS_FS_ROOT": str(tmp_path), "ATLAS_FS_MAX_READ_BYTES": "lots"})
    assert proc.returncode == 2
    assert "ATLAS_FS_MAX_READ_BYTES must be an integer" in proc.stderr
