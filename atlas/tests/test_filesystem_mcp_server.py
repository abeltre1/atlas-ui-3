"""Tests for the bundled filesystem MCP server (``atlas/mcp/filesystem``).

The server mounts one local directory, chosen with ``ATLAS_FS_ROOT``, and must
keep every tool inside it. These tests load the server module fresh per case so
each one can pick its own root, read-only flag, and read limit.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_MAIN = Path(__file__).resolve().parents[1] / "mcp" / "filesystem" / "main.py"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(monkeypatch, root=None, read_only=None, max_read=None):
    for name in ("ATLAS_FS_ROOT", "ATLAS_FS_READ_ONLY", "ATLAS_FS_MAX_READ_BYTES"):
        monkeypatch.delenv(name, raising=False)
    if root is not None:
        monkeypatch.setenv("ATLAS_FS_ROOT", str(root))
    if read_only is not None:
        monkeypatch.setenv("ATLAS_FS_READ_ONLY", read_only)
    if max_read is not None:
        monkeypatch.setenv("ATLAS_FS_MAX_READ_BYTES", str(max_read))
    spec = importlib.util.spec_from_file_location("filesystem_main_under_test", _MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fn(tool):
    # FastMCP wraps registered functions; the module keeps the plain callables.
    return getattr(tool, "fn", tool)


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


def test_root_comes_from_env(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    assert module.BASE_PATH == root.resolve()
    info = module.get_mount_info()["results"]
    assert info["root"] == str(root.resolve())
    assert info["read_only"] is False
    assert info["max_read_bytes"] == module.DEFAULT_MAX_READ_BYTES


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


def test_reads_inside_root(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    result = module.read_file("notes.txt")["results"]
    assert result["content"] == "hello\n"
    assert result["path"] == "notes.txt"
    nested = module.read_file(str(root / "sub" / "inner.txt"))["results"]
    assert nested["content"] == "inner\n"
    assert nested["path"] == os.path.join("sub", "inner.txt")


def test_list_directory_root(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    listing = module.list_directory()["results"]
    assert listing["path"] == "."
    assert [(i["name"], i["type"]) for i in listing["items"]] == [("sub", "directory"), ("notes.txt", "file")]


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
    for tool in (
        module.read_file,
        module.list_directory,
        module.file_exists,
        module.create_directory,
        module.delete_file,
    ):
        assert "outside the mounted directory" in tool(path)["results"]["error"], tool.__name__
    assert "outside the mounted directory" in module.write_file(path, "x")["results"]["error"]
    assert (outside / "secret.txt").read_text() == "secret\n"
    assert not Path("/etc/passwd.atlas-test").exists()


def test_symlink_pointing_outside_root_is_rejected(monkeypatch, mount):
    root, outside = mount
    (root / "escape").symlink_to(outside)
    module = _load(monkeypatch, root=root)
    assert "outside the mounted directory" in module.read_file("escape/secret.txt")["results"]["error"]
    assert "outside the mounted directory" in module.list_directory("escape")["results"]["error"]


def test_write_create_delete_inside_root(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root)
    assert module.create_directory("out/deep")["results"]["success"] is True
    assert (root / "out" / "deep").is_dir()
    written = module.write_file("out/deep/report.md", "# r\n")["results"]
    assert written["success"] is True and written["path"] == os.path.join("out", "deep", "report.md")
    assert (root / "out" / "deep" / "report.md").read_text() == "# r\n"
    assert module.delete_file("out/deep/report.md")["results"]["success"] is True
    assert not (root / "out" / "deep" / "report.md").exists()
    assert "not a file" in module.delete_file("out")["results"]["error"]
    assert (root / "out").is_dir()


@pytest.mark.asyncio
async def test_read_only_mode_hides_mutating_tools(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root, read_only="true")
    assert module.READ_ONLY is True
    assert module.get_mount_info()["results"]["read_only"] is True
    assert await _tool_names(module) == ["file_exists", "get_mount_info", "list_directory", "read_file"]


@pytest.mark.asyncio
async def test_writable_mode_registers_all_tools(monkeypatch, mount):
    root, _ = mount
    module = _load(monkeypatch, root=root, read_only="false")
    assert await _tool_names(module) == [
        "create_directory",
        "delete_file",
        "file_exists",
        "get_mount_info",
        "list_directory",
        "read_file",
        "write_file",
    ]


def test_read_limit_refuses_large_files(monkeypatch, mount):
    root, _ = mount
    (root / "big.txt").write_text("x" * 100, encoding="utf-8")
    module = _load(monkeypatch, root=root, max_read=50)
    assert module.MAX_READ_BYTES == 50
    error = module.read_file("big.txt")["results"]["error"]
    assert "larger than the 50 byte read limit" in error
    assert module.read_file("notes.txt")["results"]["content"] == "hello\n"


def test_non_utf8_file_reports_clean_error(monkeypatch, mount):
    root, _ = mount
    (root / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    module = _load(monkeypatch, root=root)
    assert "not UTF-8 text" in module.read_file("blob.bin")["results"]["error"]


def _run_server(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_PROJECT_ROOT),
        "HOME": os.environ.get("HOME", "/"),
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, str(_MAIN)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


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
