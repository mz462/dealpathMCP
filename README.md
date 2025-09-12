# Dealpath MCP Server

Python FastAPI server exposing an MCP-compatible HTTP endpoint backed by the Dealpath API.

## Setup

1.  Install dependencies:
    ```
    pip install -r requirements.txt
    ```

2.  Create a `.env` file in the root directory and add your Dealpath API key. The MCP token is optional; if omitted, the MCP endpoint is open (local dev only):
    ```
    dealpath_key=your_api_key
    # mcp_token=change_me_locally   # optional; if set, POST /mcp requires this bearer
    # optional, restrict browser origins (comma-separated)
    allowed_origins=http://127.0.0.1,http://localhost
    ```

## Running the server

To run the server with auto-reload enabled (recommended for development):
```
uvicorn src.main:app --reload
```

Alternatively, you can run it directly:
```
python src/main.py
```

## MCP Endpoint (HTTP)

- Routes:
  - `GET /mcp` — connectivity ping (always 200).
  - `POST /mcp` — MCP JSON-RPC requests.
- Auth: Optional. If `mcp_token` is set, `POST /mcp` requires `Authorization: Bearer <mcp_token>`. If not set, it's open (dev only).
- Origin check: For `POST /mcp` only and only when `mcp_token` is set.

### Example: initialize (open mode)

```
curl -s \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"1","method":"initialize","params":{}}' \
  http://127.0.0.1:8000/mcp | jq
```

### Example: tools/list (with bearer)

```
curl -s \
  -H "Authorization: Bearer $MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"2","method":"tools/list"}' \
  http://127.0.0.1:8000/mcp | jq
```

### Example: tools/call (get_deals)

```
curl -s \
  -H "Authorization: Bearer $MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "jsonrpc":"2.0",
        "id":"3",
        "method":"tools/call",
        "params":{ "name":"get_deals", "arguments": {"status":"Active"} }
      }' \
  http://127.0.0.1:8000/mcp | jq
```

Response shape (abridged):
```
{
  "jsonrpc": "2.0",
  "id": "3",
  "result": {
    "content": [
      { "type": "text", "text": "{\"deals\":{\"data\":[ ... ]}}" }
    ]
  }
}
```

### Example: tools/call (search_deals by name/address)

```
curl -s \
  -H "Authorization: Bearer $MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "jsonrpc":"2.0",
        "id":"s1",
        "method":"tools/call",
        "params":{ "name":"search_deals", "arguments": {"query":"boston"} }
      }' \
  http://127.0.0.1:8000/mcp | jq
```

Returns only deals matched by name/address; no metrics.

### HTTP search endpoint

For quick, non-MCP usage, a simple search route mirrors the MCP search tool but returns JSON directly:

```
GET /mcp/search?query=<text>&updated_after=<iso8601>&limit=<1-200>
```

Notes:
- Filters locally across deal name/address only; no portfolio metrics.
- `updated_after` is optional ISO 8601; `limit` defaults to 50 (max 200).

## Bring Your Own Dealpath Key (BYO)

In production, you can let each client supply their own Dealpath API key instead of configuring a server-wide key. The server never persists or logs these keys; they live only in memory for the session/request.

- MCP initialize: include the key via either header or param
  - Header: `X-Dealpath-Key: <user_api_key>`
  - Body param: `{ "method":"initialize", "params": { "dealpath_key": "<user_api_key>" } }`
  - The key is stored in the MCP session (referenced by `Mcp-Session-Id`) and used for subsequent `tools/call` and `resources/read` in that session.

- MCP REST helpers: include per-request header
  - Example: `GET /mcp/search?query=boston` with `X-Dealpath-Key: <user_api_key>`
  - Works for other `GET /mcp/*` helpers like `/mcp/getPeople`, `/mcp/getLoans`, etc.

Security notes
- Keep `mcp_token` set and serve over HTTPS in production.
- Do not log request headers. This server already avoids logging secrets.
- Keys are cleared automatically with session cleanup; they are never written to disk.

## Desktop Extensions (.mcpb)

You can use this server with Claude Desktop via a remote-only MCP bundle (no server code embedded).

- Bundle provided: `bundle/dealpath-remote` (remote connector using `mcp-remote`).
- Update `bundle/dealpath-remote/manifest.json` and set your deployed URL (HTTPS), e.g. `https://api.example.com/mcp`.
- Provide secrets via environment variables so they are not stored in the bundle:
  - `export MCP_TOKEN=<server mcp_token>`
  - `export DEALPATH_KEY=<your Dealpath API key>`
- The manifest injects headers for every call via `MCP_REMOTE_HEADERS`:
  - `Authorization: Bearer ${env:MCP_TOKEN}`
  - `X-Dealpath-Key: ${env:DEALPATH_KEY}`

Build and install
- Build the bundle file: `./scripts/make_mcpb.sh` → outputs `dealpath-remote.mcpb` at repo root.
- Install: drag-drop the `.mcpb` into Claude Desktop Extensions (or “Install from file”).
- Ensure Claude Desktop is launched with the env vars above so the headers resolve.

Notes
- This approach keeps the server deployable independently; the `.mcpb` only points to it.
- Always deploy behind TLS and keep `mcp_token` enabled in production.

### MCP Resources

Supports MCP resource templates for direct reads:
- `dealpath://deal/{deal_id}.json` (application/json)
- `dealpath://deal/{deal_id}.md` (text/markdown)
- `dealpath://search/{query}.json` (application/json)

List templates:
```
curl -s -H "Authorization: Bearer $MCP_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"r1","method":"resources/list"}' \
  http://127.0.0.1:8000/mcp | jq
```

Read a resource:
```
curl -s -H "Authorization: Bearer $MCP_TOKEN" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":"r2","method":"resources/read","params":{"uri":"dealpath://deal/12345.md"}}' \
  http://127.0.0.1:8000/mcp | jq -r .result.contents[0].text
```

## Key Learnings & API Details

During the development of this server, the following key details about the Dealpath API were discovered:

*   **API Base URL**: The correct base URL for API requests is `https://api.dealpath.com`.
*   **Authentication**: The API uses a Bearer Token for authentication, which must be included in the `Authorization` header of each request.
*   **Required Header**: A specific `Accept` header is required for all requests: `Accept: application/vnd.dealpath.api.v1+json`.
*   **API Documentation**: The official OpenAPI (Swagger) specification is available at `https://platform.dealpath.com/dealpath_v1.yaml`. This file provides the most accurate and detailed information about available endpoints, request parameters, and response formats.
### Example: tools/call (get_file_by_id → resource_link)

```
curl -s \
  -H "Content-Type: application/json" \
  -d '{
        "jsonrpc":"2.0",
        "id":"4",
        "method":"tools/call",
        "params":{ "name":"get_file_by_id", "arguments": {"file_id":"<FILE_ID>"} }
      }' \
  http://127.0.0.1:8000/mcp | jq
```

Response (abridged):
```
{
  "result": {
    "content": [
      { "type": "text", "text": "Links for file 'doc.pdf' (id 2344739):\n- Local: http://127.0.0.1:8000/local-files/20250910/2344739/doc.pdf\n- Remote (expires): https://files.dealpath.com/..." },
      { "type": "resource_link", "name": "doc.pdf", "uri": "https://files.dealpath.com/...signed..." },
      { "type": "resource_link", "name": "doc.pdf", "uri": "http://127.0.0.1:8000/local-files/YYYYMMDD/<file_id>/doc.pdf" }
    ]
  }
}
```
Local file serving
- Files downloaded via tools are stored under `file_storage_dir` (default: `<repo>/local_files/YYYYMMDD/<file_id>/<filename>`).
- They are served at `GET /local-files/{date}/{file_id}/{filename}`.
- For dev only. Do not expose this publicly without auth/cleanup.
