# MCP Server Configuration

Last updated: 2026-10-29

The `mcp.json` file defines the MCP (Model Context Protocol) servers that the application can connect to. These servers provide the tools and capabilities available to the LLM.

*   **Location**: The package default is at `atlas/config/mcp.json`. Place your instance-specific configuration in `config/mcp.json`.

## Comprehensive Example

Here is an example of a server configuration that uses all available options.

```json
{
  "MyExampleServer": {
    "enabled": true,
    "description": "A full description of what this server does, which appears in the marketplace.",
    "short_description": "A short description for tooltips.",
    "author": "Your Team Name",
    "help_email": "support@example.com",
    "groups": ["admin", "engineering"],
    "command": ["python", "mcp/MyExampleServer/main.py"],
    "cwd": "backend",
    "env": {
      "API_KEY": "${MY_API_KEY}",
      "DEBUG_MODE": "false",
      "MAX_RETRIES": "3"
    },
    "url": null,
    "transport": "stdio",
    "compliance_level": "Internal",
    "auth_type": "none",
    "require_approval": ["dangerous_tool", "another_risky_tool"],
    "allow_edit": ["dangerous_tool"]
  }
}
```

## Configuration Fields Explained

*   **`enabled`**: (boolean) If `false`, the server is completely disabled and will not be loaded.
*   **`description`**: (string) A detailed description of the server's purpose and capabilities. This is shown to users in the MCP Marketplace.
*   **`short_description`**: (string) A brief, one-line description used for tooltips or other compact UI elements.
*   **`author`**: (string) The name of the team or individual who created the server.
*   **`help_email`**: (string) A contact email for users who need help with the server's tools.
*   **`groups`**: (list of strings) A list of user groups that are allowed to access this server. If a user is not in any of these groups, the server will be hidden from them.
*   **`command`**: (list of strings) For servers using `stdio` transport, this is the command and its arguments used to start the server process.
*   **`cwd`**: (string) The working directory from which to run the `command`.
*   **`env`**: (object) Environment variables to set for `stdio` servers. Keys are variable names, values can be literal strings or use environment variable substitution (e.g., `"${ENV_VAR}"`). This is only applicable to stdio servers and will be ignored for HTTP/SSE servers.
*   **`url`**: (string) For servers using `http` or `sse` transport, this is the URL of the server's endpoint.
*   **`transport`**: (string) The communication protocol to use. Can be `stdio`, `http`, or `sse`. This takes priority over auto-detection.
*   **`auth_token`**: (string) For HTTP/SSE servers, the bearer token used for authentication. Use environment variable substitution (e.g., `"${MCP_SERVER_TOKEN}"`) to avoid storing secrets in config files. Stdio servers ignore this field.
*   **`allowed_data_classifications`**: (list of strings) Every data classification (compliance level) this server is explicitly approved to receive. Its tools are usable only in a conversation whose selected level is in this list, checked on the server for every turn; a server that declares none is unavailable in every classified session. See [Compliance](compliance.md).
*   **`compliance_level`**: (string, deprecated) A single level, read as a one-element `allowed_data_classifications` when that field is not set.
*   **`auth_type`**: (string) The type of per-user authentication required. Options: `none` (default), `api_key`, `jwt`, `bearer`. When set, users must provide their own credentials via the UI.
*   **`auth_header`**: (string) Custom HTTP header name for API key authentication. Defaults to `X-API-Key`. Only used when `auth_type` is `api_key`.
*   **`require_approval`**: (list of strings) A list of tool names (without the server prefix) that will always require user approval before execution.
*   **`allow_edit`**: (list of strings) A list of tool names for which the user is allowed to edit the arguments before approving. (Note: This is a legacy field and may be deprecated; the UI may allow editing for all approval requests).

### Stdio environment isolation

Stdio servers inherit only an allowlisted operating-system environment, not the
backend's complete environment. Backend signing keys, proxy credentials, LLM
keys, and token-encryption keys are not forwarded automatically, even when
`env` is omitted or empty. Atlas retains the Python import path needed by its
bundled servers.

**Migration:** servers that relied on implicit inheritance must declare each
required variable in their `env` block, for example
`"env": {"API_KEY": "${MY_TOOL_API_KEY}"}`. Substitution reads the backend
environment; a missing required variable prevents that server from starting.
Only expose secrets that the particular server needs. This limits accidental
credential exposure, but is not an operating-system sandbox for same-user code.

### Python code execution

The legacy `code-executor` v1 example is no longer offered by the admin catalog.
Its AST denylist and plain subprocess execution are **not a security sandbox**.
Existing installations are not removed automatically: disable v1 in your
`config/mcp.json` and migrate to the `code-executor-v2` example, which uses
Landlock and network namespace isolation. Verify the required Linux sandbox
support on the deployment host before enabling code execution.

## Server Types

The system can connect to different types of MCP servers:
*   **Standard I/O (`stdio`)**: Servers that are started as a subprocess and communicate over `stdin` and `stdout`.
*   **HTTP (`http`)**: Servers that expose a standard HTTP endpoint.
*   **Server-Sent Events (`sse`)**: Servers that stream responses over an HTTP connection.

For a minimal read-only `stdio` example, see the [Docs Search MCP Example](../developer/docs-search-mcp.md), which searches and reads the repository's `docs/` folder.

## Hot Reload Note

After editing `config/mcp.json`, you do **not** need to restart the backend. Admins can:

- Call `POST /admin/mcp/reload` to reload `mcp.json`, reinitialize MCP clients, and rediscover tools/prompts.
- Use `GET /admin/mcp/status` to see which servers are connected or failing.
- Use `POST /admin/mcp/reconnect` (plus the auto-reconnect feature flag) to retry failed servers with exponential backoff.
- Use `POST /admin/mcp/refresh` with `{"server_name": "<name>"}` to refresh just one server (re-read its config, reconnect, re-discover its tools) without disturbing every other server's connections. The admin panel's server chips each carry a refresh button for this.

## MCP Server Authentication

For MCP servers that require authentication, you can configure bearer token authentication using the `auth_token` field.

### Environment Variable Substitution

**Security Best Practice**: Never store API keys or tokens directly in configuration files. Instead, use environment variable substitution:

```json
{
  "my-external-api": {
    "url": "https://api.example.com/mcp",
    "transport": "http",
    "auth_token": "${MCP_EXTERNAL_API_TOKEN}",
    "groups": ["users"]
  }
}
```

Then set the environment variable:
```bash
export MCP_EXTERNAL_API_TOKEN="your-secret-api-key"
```

### How It Works

1. **HTTP/SSE Servers**: The `auth_token` value is passed as a Bearer token in the `Authorization` header when connecting to the MCP server.
2. **Stdio Servers**: The `auth_token` field is ignored since stdio servers don't use HTTP authentication.
3. **Environment Variables**: If the token contains `${VAR_NAME}` pattern, it's replaced with the value of the environment variable `VAR_NAME`.
4. **Error Handling**: If a required environment variable is missing, the server initialization will fail gracefully with a clear error message.

### Security Considerations

- **Recommended**: Use environment variables for all production tokens
- **Alternative**: For development/testing, you can use direct string values (not recommended for production)
- **Never**: Commit tokens to `atlas/config/mcp.json` or any version-controlled files

## Per-User Authentication

For MCP servers where each user needs their own credentials (e.g., external APIs with individual API keys), use per-user authentication instead of server-level `auth_token`.

### Configuration

```json
{
  "external-api": {
    "url": "https://api.example.com/mcp",
    "transport": "http",
    "auth_type": "api_key",
    "auth_header": "X-API-Key",
    "groups": ["users"]
  }
}
```

**Fields:**
- `auth_type`: Required. Set to `api_key`, `jwt`, or `bearer` to enable per-user authentication.
- `auth_header`: Optional. Custom header name for API keys (default: `X-API-Key`). Only used when `auth_type` is `api_key`.

### How It Works

1. Users see a key icon next to servers requiring authentication in the Tools panel
2. Users click the icon and enter their personal API key or token
3. The token is encrypted and stored securely per-user
4. Future tool calls automatically include the user's token

### Token Storage

Tokens are stored encrypted on disk:
- **Location:** Set `MCP_TOKEN_STORAGE_DIR` env var, or defaults to `config/secure/mcp_tokens.enc`
- **Encryption:** Fernet (AES-128-CBC)
- **Key:** Set `MCP_TOKEN_ENCRYPTION_KEY` environment variable (required)

`MCP_TOKEN_ENCRYPTION_KEY` must be set to a stable secret before starting Atlas. The application refuses to start without it, because a generated ephemeral key would make previously encrypted tokens unreadable after every restart.

### Per-User vs Server-Level Auth

| Feature | `auth_token` (Server-level) | `auth_type` (Per-user) |
|---------|----------------------------|------------------------|
| Who provides token | Admin in config | Each user in UI |
| Token storage | Config file | Encrypted per-user |
| Use case | Shared service accounts | Individual API keys |
| Transport support | HTTP/SSE | HTTP/SSE |

See [MCP Server Authentication](./mcp-server-authentication.md) for full details.

## Environment Variables for Stdio Servers

For stdio servers, you can pass custom environment variables to the server process using the `env` field. This is useful for:
- Configuring server behavior without modifying command arguments
- Passing credentials or API keys securely
- Setting runtime configuration options

### Example Configuration

```json
{
  "my-external-tool": {
    "command": ["wrapper-cli", "my.external.tool@latest", "--allow-write"],
    "cwd": "backend",
    "env": {
      "CLOUD_PROFILE": "my-profile-9",
      "CLOUD_REGION": "us-east-7",
      "API_KEY": "${MY_API_KEY}",
      "DEBUG_MODE": "false"
    },
    "groups": ["users"]
  }
}
```

Then set the environment variable before starting Atlas UI:
```bash
export MY_API_KEY="your-secret-api-key"
```

### Environment Variable Features

- **Literal Values**: Environment variables can contain literal string values (e.g., `"CLOUD_REGION": "us-east-7"`)
- **Variable Substitution**: Use `${VAR_NAME}` syntax to reference system environment variables (e.g., `"API_KEY": "${MY_API_KEY}"`)
- **Empty Values**: An empty object `{}` is valid and will set no environment variables
- **Error Handling**: If a referenced environment variable (e.g., `${MY_API_KEY}`) is not set in the system, the server initialization will fail with a clear error message
- **Stdio Only**: The `env` field only applies to stdio servers; it is ignored for HTTP/SSE servers

### Security Best Practices

- Use environment variable substitution for all sensitive values (API keys, passwords, tokens)
- Never store secrets directly in the `env` object values
- Set environment variables via your deployment system (Docker, Kubernetes, systemd, etc.)
- Use different values for development, staging, and production environments

## Mounting a Local Directory

The bundled `filesystem` server (`atlas/mcp/filesystem/`) mounts **one local
directory** into the chat and confines every tool to it. Relative paths
resolve against that directory, absolute paths are accepted only when they are
already inside it, and symlinks are resolved before the check, so a link that
points outside the directory is rejected like any other escape. Recursive
listings and searches never follow symlinks. The server never advertises a way
to change the root at runtime.

Copy [`mcp-filesystem.json`](../../atlas/config/mcp-example-configs/mcp-filesystem.json)
into `config/mcp.json` and set the root:

```json
{
  "filesystem": {
    "command": ["python", "mcp/filesystem/main.py"],
    "cwd": "atlas",
    "env": {
      "ATLAS_FS_ROOT": "${ATLAS_FS_ROOT}",
      "ATLAS_FS_READ_ONLY": "false"
    },
    "groups": ["users"],
    "require_approval": ["write_file", "edit_file", "delete_file", "create_directory"],
    "compliance_level": "Public"
  }
}
```

```bash
export ATLAS_FS_ROOT=/data/projects/alpha   # or set it in .env
```

### Tools

The server is built for the agent loop: every tool returns a bounded,
structured result, large files are read in pages, and the model can find what
it needs before reading it.

| Tool | Mode | What it does |
|---|---|---|
| `get_mount_info` | read | Reports the mounted path, whether it is writable, the limits, and the tools available. |
| `list_directory` | read | Lists one level, or `recursive=true` down to `max_depth`. Skips `.git`, `node_modules`, caches and dot-entries unless `include_hidden=true`. |
| `search_files` | read | Finds entries by glob (`*.csv`, `src/*.py`) anywhere under a directory. |
| `search_content` | read | Finds lines containing a snippet, with file and line number, optionally limited by a file-name glob. |
| `read_file` | read | Returns a page of a UTF-8 text file from `start_line`, up to `max_lines` lines and the read byte limit; `has_more` and `next_start_line` say where to continue. |
| `file_exists` | read | Reports whether a path exists and whether it is a file or directory. |
| `write_file` | write | Creates or overwrites a file (or appends with `append=true`), creating parents inside the root. |
| `edit_file` | write | Replaces an exact snippet; refuses when the snippet is missing or ambiguous unless `replace_all=true`. |
| `create_directory` | write | Creates a directory and missing parents. |
| `delete_file` | write | Deletes one file; never directories. |

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `ATLAS_FS_ROOT` | the entry's `cwd` | Directory to mount. Must exist and be a directory, or the server refuses to start and the admin panel shows the reason. |
| `ATLAS_FS_READ_ONLY` | `false` | `true` registers only the read tools. The write, edit, create and delete tools are not advertised to the model at all. |
| `ATLAS_FS_MAX_READ_BYTES` | `131072` | Most text one `read_file` call returns. Longer files are paged, never loaded whole into the model's context. 128 KiB is roughly 32k tokens. |
| `ATLAS_FS_MAX_LIST_ENTRIES` | `500` | Most entries one listing or search returns; results report `truncated: true` past it. |

### Keeping the mount safe

- **Do not leave `ATLAS_FS_ROOT` unset with `cwd: "atlas"`.** Without a root the
  server falls back to its working directory, which in that configuration is the
  Atlas package itself, and a writable mount there lets the model edit the
  application's own code and package defaults. Point the root at a dedicated
  folder.
- **Keep `require_approval` on the mutating tools** unless the directory is
  disposable. Tools listed there are admin-required: a user cannot auto-approve
  them, and a background run launched with `atlas_launch` pauses on them until
  the user opens that conversation.
- **Classify the mount like its contents.** The example ships as `Public`. If
  the folder holds internal data, set `allowed_data_classifications` so the
  server is only usable in conversations at a matching level (see
  [Compliance](compliance.md)).
- **Add a policy hook** for anything the approval gate cannot express. The
  bundled `pre_tool_use.sh` example blocks writes outside a workspace path; its
  matcher is `filesystem_.*` (the qualified tool name uses a single underscore).
  See [Hooks](hooks.md).
- **Logged paths.** `get_mount_info` and every result report the absolute
  mount path and root-relative file paths, and those land in the saved
  conversation.

### Running in a container

The server reads the directory from the backend's filesystem, so in a container
the directory has to be mounted in and the variable has to point at the
in-container path:

```bash
podman run -p 8000:8000 \
  -v /data/projects/alpha:/mnt/alpha:Z \
  -e ATLAS_FS_ROOT=/mnt/alpha \
  atlas-ui-3
```

With Compose, add the bind mount to the `atlas-ui` service's `volumes` and
`ATLAS_FS_ROOT=/mnt/alpha` to its environment. Use `:ro` on the bind mount for a
read-only mount in addition to `ATLAS_FS_READ_ONLY=true`.

Scaling note: this is a stdio server, so the mounted directory is the local
disk of the backend replica that started it. On a multi-replica deployment each
replica sees its own copy unless the path is a shared mount. It is intended for
single-user and single-host installs; for shared, durable files use the S3
file storage described in [File Storage](file-storage.md).

## Access Control with Groups

You can restrict access to MCP servers based on user groups. This is a critical feature for controlling which users can access powerful or sensitive tools. Group checks run at both request time (when tools are listed or selected) and at the single authorization choke point inside `MCPToolManager`, keyed on the authenticated user's identity from the request context. A missing user context, a disabled server, an unreachable authorization service, or membership that does not satisfy the `groups` list will all fail closed and deny access. If a user is not in the required group, the server will be completely invisible to them in the UI, and any attempt to call its functions will be blocked.

The same central policy covers **tools, prompts, and resources**. Prompt text and resource contents are injected into model context, so they are never fetched from a server the requesting user may not use. Prompt and resource operations require an authenticated identity even on a server with no `groups`; when an MCP operation selects a model, the user's per-model `groups` ACL is enforced as well. An active data classification must be declared by the server (`allowed_data_classifications`) before any of the three capabilities can run under that level. MCP sampling (server-initiated LLM calls) is not supported.

## A Note on the `_atlas_user` Argument

As a security measure, if a tool is designed to accept an `_atlas_user` argument, the Atlas UI backend will **always** overwrite this argument with the authenticated user's identity before calling the tool. Ordinary `username` arguments are left under LLM control. This ensures tools that opt in to Atlas user context run with the correct user and prevents the LLM from impersonating another user through the injected field.


## Advanced MCP Features

### User Elicitation

MCP tools can request structured input from users during tool execution. See the [Elicitation Documentation](../developer/elicitation.md) for details.

### Progress Updates

MCP tools can send real-time progress updates during long-running operations. See the [Progress Updates Documentation](../developer/progress-updates.md) for details.
