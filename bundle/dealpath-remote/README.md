Dealpath MCP (Remote) Bundle
============================

This bundle connects Claude Desktop to a deployed Dealpath MCP HTTP server using `mcp-remote`. It does not include server code; it only references your running endpoint.

Prerequisites
- Deploy the server over HTTPS, e.g., `https://api.example.com/mcp`.
- Set a server `mcp_token` to guard POST /mcp.

Configure secrets (recommended)
- Export env vars before launching Claude Desktop:
  - `export MCP_TOKEN=<same token configured on the server>`
  - `export DEALPATH_KEY=<your Dealpath API key>`

Update manifest
- Edit `manifest.json` and replace `https://YOUR_HOSTNAME_OR_URL/mcp` with your deployed MCP URL.
- Optionally change `name`, `id`, and `homepage`.

Build the .mcpb

From the `bundle/dealpath-remote` directory:

```
zip -r ../dealpath-remote.mcpb .
```

Install in Claude Desktop
- Open Claude Desktop and drag-drop `dealpath-remote.mcpb` into the Extensions panel, or use “Install from file”.
- Ensure Claude Desktop is launched with `MCP_TOKEN` and `DEALPATH_KEY` in its environment so the headers resolve.

Notes
- This bundle uses `MCP_REMOTE_HEADERS` to attach both the server bearer token and the per-user Dealpath key to all MCP calls.
- Keys are handled client-side by Claude/mcp-remote and never stored in this bundle.

