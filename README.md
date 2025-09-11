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
