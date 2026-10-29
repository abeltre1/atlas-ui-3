#!/bin/bash
# Test script for PR #1: mount one local directory with the filesystem MCP server.
# Covers: root selection via ATLAS_FS_ROOT, containment (traversal, absolute,
# symlink), read-only mode hiding the mutating tools, fail-fast startup on a
# missing root, and the backend advertising the right tools via /api/config.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FIXTURES="$SCRIPT_DIR/fixtures/pr1"

PASSED=0
FAILED=0

print_result() {
    if [ "$1" -eq 0 ]; then
        echo -e "\033[0;32mPASSED\033[0m: $2"
        PASSED=$((PASSED + 1))
    else
        echo -e "\033[0;31mFAILED\033[0m: $2"
        FAILED=$((FAILED + 1))
    fi
}

cd "$PROJECT_ROOT"
source .venv/bin/activate

if pgrep -f '[u]vicorn main:app' > /dev/null; then
    echo "Refusing to start a second backend; stop the running one before this validation."
    exit 1
fi

WORK="$(mktemp -d)"
BACKEND_PID=""
cleanup() {
    stop_backend
    rm -rf "$WORK"
}
stop_backend() {
    if [ -n "$BACKEND_PID" ]; then
        pkill -P "$BACKEND_PID" 2>/dev/null || true
        kill "$BACKEND_PID" 2>/dev/null || true
        wait "$BACKEND_PID" 2>/dev/null || true
        BACKEND_PID=""
    fi
}
trap cleanup EXIT

MOUNT="$WORK/mount"
OUTSIDE="$WORK/outside"
mkdir -p "$MOUNT/sub" "$OUTSIDE"
echo "hello from mount" > "$MOUNT/notes.txt"
echo "secret" > "$OUTSIDE/secret.txt"
ln -s "$OUTSIDE" "$MOUNT/escape_link"

# --- 1. Real stdio transport, writable mount ---------------------------------
python "$FIXTURES/transport_check.py" "$MOUNT" "$OUTSIDE" writable
print_result $? "Writable mount: tools listed, read/list/search/write/edit inside root, every escape denied"

# --- 2. Real stdio transport, read-only mount --------------------------------
python "$FIXTURES/transport_check.py" "$MOUNT" "$OUTSIDE" readonly
print_result $? "Read-only mount: only read tools advertised, escapes denied"

# --- 3. Symlink that points outside the root is rejected ---------------------
SYMLINK_OUT="$(ATLAS_FS_ROOT="$MOUNT" PYTHONPATH="$PROJECT_ROOT" python - <<'PY'
import importlib.util, pathlib
main = pathlib.Path("atlas/mcp/filesystem/main.py")
spec = importlib.util.spec_from_file_location("fs_main", main)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.read_file("escape_link/secret.txt")["results"].get("error", ""))
PY
)"
echo "$SYMLINK_OUT" | grep -q "outside the mounted directory"
print_result $? "Symlink pointing outside the root is rejected"

# --- 4. Missing root fails at startup ----------------------------------------
STARTUP_ERR="$(ATLAS_FS_ROOT="$WORK/does-not-exist" PYTHONPATH="$PROJECT_ROOT" python atlas/mcp/filesystem/main.py 2>&1 >/dev/null)"
STARTUP_RC=$?
[ "$STARTUP_RC" -eq 2 ] && echo "$STARTUP_ERR" | grep -q "does not exist"
print_result $? "Server refuses to start when ATLAS_FS_ROOT is missing (exit 2 with a clear message)"

# --- 5. Backend advertises the mount through /api/config ---------------------
free_port() {
    python - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}
wait_for_health() {
    for _ in $(seq 1 60); do
        curl -sf --noproxy "*" "$1/api/health" > /dev/null && return 0
        sleep 1
    done
    return 1
}
MCP_TOKEN_ENCRYPTION_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
start_backend() {
    local read_only="$1"
    export ATLAS_PORT="$(free_port)"
    mkdir -p "$WORK/config" "$WORK/data" "$WORK/logs"
    cp "$FIXTURES/mcp.json" "$FIXTURES/llmconfig.yml" "$WORK/config/"
    cat > "$WORK/test.env" <<ENV
PORT=$ATLAS_PORT
ENVIRONMENT=development
DEBUG_MODE=true
MCP_TOKEN_ENCRYPTION_KEY=$MCP_TOKEN_ENCRYPTION_KEY
FEATURE_TOOLS_ENABLED=true
FEATURE_RAG_ENABLED=false
USE_MOCK_S3=true
CHAT_HISTORY_DB_URL=duckdb:///$WORK/data/chat_history_$read_only.db
APP_LOG_DIR=$WORK/logs
APP_CONFIG_DIR=$WORK/config
ATLAS_FS_ROOT=$MOUNT
ATLAS_FS_READ_ONLY=$read_only
ENV
    bash "$PROJECT_ROOT/agent_start.sh" --backend-only --env-file "$WORK/test.env" > "$WORK/backend_$read_only.log" 2>&1 &
    BACKEND_PID=$!
    wait_for_health "http://127.0.0.1:$ATLAS_PORT"
}
config_tools() {
    curl -sf --noproxy "*" -H "X-User-Email: validator@example.com" "http://127.0.0.1:$ATLAS_PORT/api/config" \
        | python -c 'import json,sys; d=json.load(sys.stdin); print("\n".join(sorted(t for s in d["tools"] if s["server"]=="filesystem" for t in s["tools"])))'
}

start_backend false
print_result $? "Backend starts with the filesystem server mounted at ATLAS_FS_ROOT"
TOOLS="$(config_tools)"
echo "$TOOLS" | grep -qx "write_file" && echo "$TOOLS" | grep -qx "edit_file" && echo "$TOOLS" | grep -qx "get_mount_info"
print_result $? "/api/config lists write_file and get_mount_info for the writable mount"
stop_backend

start_backend true
print_result $? "Backend starts with the filesystem server mounted read-only"
TOOLS="$(config_tools)"
echo "$TOOLS" | grep -qx "search_content" && ! echo "$TOOLS" | grep -q "write_file\|edit_file\|delete_file\|create_directory"
print_result $? "/api/config lists only read tools for the read-only mount"
stop_backend

# --- 6. Unit tests for the server --------------------------------------------
python -m pytest atlas/tests/test_filesystem_mcp_server.py -q > /dev/null 2>&1
print_result $? "Filesystem server unit tests"

# Final: run backend unit tests
./test/run_tests.sh backend > "$WORK/backend_tests.log" 2>&1
BACKEND_RC=$?
tail -3 "$WORK/backend_tests.log"
print_result $BACKEND_RC "Backend unit tests"

echo ""
echo "Passed: $PASSED | Failed: $FAILED"
[ $FAILED -eq 0 ] && exit 0 || exit 1
