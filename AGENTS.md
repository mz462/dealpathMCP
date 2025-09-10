# Repository Guidelines

## Project Structure & Module Organization
- `src/mcp_server.py`: FastAPI routes exposing MCP endpoints backed by Dealpath.
- `src/dealpath_client.py`: Thin HTTP client for Dealpath API (auth + headers).
- `src/main.py`: App entrypoint for local `uvicorn` runs.
- `requirements.txt`: Runtime dependencies.
- `README.md`, `API.md`: Setup notes and endpoint references.
- `.env`: Local secrets (e.g., `dealpath_key`). Do not commit.

## Build, Test, and Development Commands
- Install: `pip install -r requirements.txt`
- Run (dev, auto-reload): `uvicorn src.main:app --reload`
- Run (python): `python src/main.py`
- Run (background): `uvicorn src.main:app --host 127.0.0.1 --port 8000 &`
- Docs: open `http://127.0.0.1:8000/docs` to exercise endpoints.
- MCP endpoint: `http://127.0.0.1:8000/mcp` (for Claude Code integration)
- Optional tests (if added): `pytest -q` (suggested stack: `pytest`, `httpx`, `pytest-cov`).

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation and type hints.
- Names: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep FastAPI route handlers small; delegate to `DealpathClient`.
- Prefer `logging` over `print`; raise `HTTPException` for API errors.
- Import order: stdlib, third‑party, local (`src/...`).

## Testing Guidelines
- Place tests in `tests/` using `test_*.py` naming.
- Use FastAPI `TestClient` or `httpx.AsyncClient` to validate routes.
- Mock Dealpath HTTP calls (e.g., `responses` or `pytest-mock`).
- Target coverage on touched code paths; add regression tests for fixes.

## Commit & Pull Request Guidelines
- Commits: Conventional Commits style, e.g., `feat: add deals summary endpoint`, `fix: handle missing last_updated`.
- Keep subjects imperative and concise; include rationale in body if non-trivial.
- PRs must include: clear summary, linked issues, testing notes (commands and sample requests), and any API/README updates.

## Security & Configuration Tips
- Required env var: `dealpath_key` in `.env`.
- Never log secrets or commit `.env`; `.gitignore` already excludes it.
- All Dealpath calls require `Authorization: Bearer <key>` and `Accept: application/vnd.dealpath.api.v1+json` (enforced in `DealpathClient`).

## Claude Code Integration
This server provides an HTTP-based MCP endpoint for Claude Code integration:

### Setup Steps:
1. **Start the server**: `uvicorn src.main:app --host 127.0.0.1 --port 8000`
2. **Configure Claude desktop config** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
   ```json
   {
     "mcpServers": {
       "dealpath": {
         "command": "npx",
         "args": ["-y", "mcp-remote@latest", "http://127.0.0.1:8000/mcp"]
       }
     }
   }
   ```
3. **Restart Claude Code** to load the MCP server
4. **Available tools**: `mcp__dealpath__get_deals`, `mcp__dealpath__get_file_by_id`, etc.

### Prerequisites:
- npm permissions must be correct (run `sudo chown -R $(id -u):$(id -g) ~/.npm` if needed)
- HTTP server must be running on port 8000 before starting Claude Code
