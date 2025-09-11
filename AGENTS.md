# Repository Guidelines

## Project Structure & Module Organization
- Source lives in `src/`:
  - `src/mcp_server.py` — FastAPI routes exposing MCP endpoints backed by Dealpath.
  - `src/dealpath_client.py` — Thin Dealpath HTTP client (auth + headers).
  - `src/main.py` — App entrypoint for local `uvicorn` runs.
- Root: `requirements.txt`, `README.md`, `API.md`. Optional tests in `tests/`.
- Secrets: `.env` (ignored by Git). Required: `dealpath_key`.

## Build, Test, and Development Commands
- Install deps: `pip install -r requirements.txt`.
- Run (dev, autoreload): `uvicorn src.main:app --reload`.
- Run (python): `python src/main.py`.
- Run (background): `uvicorn src.main:app --host 127.0.0.1 --port 8000 &`.
- Docs: open `http://127.0.0.1:8000/docs`.
- MCP endpoint (Claude): `http://127.0.0.1:8000/mcp`.
- Tests (if present): `pytest -q`.

## Coding Style & Naming Conventions
- PEP 8, 4-space indentation, type hints required for public functions.
- Naming: `snake_case` (functions/vars), `PascalCase` (classes), `UPPER_SNAKE_CASE` (constants).
- FastAPI routes stay thin; delegate logic to `DealpathClient`.
- Use `logging` (no prints). Raise `fastapi.HTTPException` for API errors.
- Import order: stdlib, third‑party, local (`src/...`).

## Testing Guidelines
- Place tests in `tests/` with `test_*.py` names.
- Use FastAPI `TestClient` or `httpx.AsyncClient` for routes.
- Mock Dealpath HTTP (`responses` or `pytest-mock`).
- Aim for coverage on touched paths; add regression tests for fixes.

## Commit & Pull Request Guidelines
- Conventional Commits, e.g., `feat: add deals summary endpoint`, `fix: handle missing last_updated`.
- Keep subjects imperative and concise; add context in body when non-trivial.
- PRs include: clear summary, linked issues, testing notes (commands + sample requests), and any README/API updates.

## Security & Configuration Tips
- Set `dealpath_key` in `.env`; never commit `.env` or log secrets.
- All Dealpath calls must include `Authorization: Bearer <key>` and `Accept: application/vnd.dealpath.api.v1+json` (handled by `DealpathClient`).

## Claude Code Integration (MCP)
- Start server: `uvicorn src.main:app --host 127.0.0.1 --port 8000`.
- Configure Claude desktop `claude_desktop_config.json` to point MCP to `http://127.0.0.1:8000/mcp` via `mcp-remote`.
- Restart Claude; available tools include `mcp__dealpath__get_deals`, `mcp__dealpath__get_file_by_id`, and executive analytics tools.
