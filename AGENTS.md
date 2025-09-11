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

## Potential Next Steps: MCP File Delivery Strategy

Goal: minimize server-side file persistence; prefer secure, direct downloads.

- Default behavior
  - Prefer returning signed remote URLs as MCP `resource_link` parts.
  - Avoid saving files locally unless explicitly enabled for dev/debug.

- Configurability (env)
  - `FILE_DOWNLOAD_STRATEGY` = `remote_only` | `proxy_stream` | `local_cache` (default: `remote_only`).
  - `FILE_STORAGE_DIR` (existing): local cache root if `local_cache` is used.
  - `FILE_CACHE_TTL_HOURS` (e.g., `72`), `FILE_CACHE_MAX_BYTES` (e.g., `1_000_000_000`), `FILE_CACHE_MAX_FILES`.

- Security hardening
  - Require bearer auth on `GET /local-files/*` when `mcp_token` is set.
  - Send `X-Content-Type-Options: nosniff` and correct `Content-Type` when serving.
  - Maintain strict path sanitization (already implemented) and never log sensitive paths/filenames.

- Streaming alternative (no persistence)
  - Add `GET /mcp/file/{file_id}` that fetches and streams bytes without writing to disk.
  - Set `Content-Disposition` with the filename and `Content-Type` from upstream headers.

- Retention & cleanup (if `local_cache`)
  - Implement TTL-based cleanup job and an admin-only purge endpoint.
  - Track and enforce `FILE_CACHE_MAX_BYTES`/`FILE_CACHE_MAX_FILES`.

- Observability
  - Expose metrics: cache hits/misses, bytes served, evictions, upstream failures.
  - Add structured logs for file fetches (exclude secrets/URLs with tokens).

- Documentation & tests
  - Update README/API with strategy explanation, warnings for public exposure, and env var table.
  - Add tests for: strategy modes, auth guard on `/local-files/*`, and streaming endpoint.
