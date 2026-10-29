"""Drive the filesystem MCP server over real stdio transport.

Usage: python transport_check.py <mount_root> <outside_dir> <writable|readonly>
Exits non-zero on the first failed expectation.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

PROJECT_ROOT = Path(__file__).resolve().parents[4]
EXAMPLE = PROJECT_ROOT / "atlas" / "config" / "mcp-example-configs" / "mcp-filesystem.json"


def _data(result):
    return (result.structured_content or result.data)["results"]


async def main(root: str, outside: str, mode: str) -> None:
    cfg = json.load(open(EXAMPLE))["filesystem"]
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(PROJECT_ROOT),
        "ATLAS_FS_ROOT": root,
        "ATLAS_FS_READ_ONLY": "true" if mode == "readonly" else "false",
    }
    transport = StdioTransport(
        command=sys.executable,
        args=cfg["command"][1:],
        cwd=str(PROJECT_ROOT / cfg["cwd"]),
        env=env,
    )
    async with Client(transport) as client:
        names = sorted(t.name for t in await client.list_tools())
        read_only_names = ["file_exists", "get_mount_info", "list_directory", "read_file"]
        if mode == "readonly":
            assert names == read_only_names, names
        else:
            assert names == sorted(read_only_names + ["write_file", "create_directory", "delete_file"]), names

        info = _data(await client.call_tool("get_mount_info", {}))
        assert info["root"] == str(Path(root).resolve()), info
        assert info["read_only"] is (mode == "readonly"), info

        listing = _data(await client.call_tool("list_directory", {}))
        assert "notes.txt" in [i["name"] for i in listing["items"]], listing

        content = _data(await client.call_tool("read_file", {"path": "notes.txt"}))
        assert content["content"] == "hello from mount\n", content

        for escape in ("../secret.txt", os.path.join(outside, "secret.txt"), "/etc/passwd"):
            denied = _data(await client.call_tool("read_file", {"path": escape}))
            assert "outside the mounted directory" in denied.get("error", ""), (escape, denied)

        if mode == "writable":
            written = _data(await client.call_tool("write_file", {"path": "out/new.txt", "content": "x"}))
            assert written.get("success") is True, written
            assert (Path(root) / "out" / "new.txt").read_text() == "x"
            denied = _data(await client.call_tool("write_file", {"path": "../pwned.txt", "content": "x"}))
            assert "outside the mounted directory" in denied.get("error", ""), denied
            assert not (Path(outside) / "pwned.txt").exists()
            assert not (Path(root).parent / "pwned.txt").exists()
    print(f"transport check passed ({mode})")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3]))
