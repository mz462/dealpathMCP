import os
import importlib
from fastapi.testclient import TestClient


def build_app_with_env(token: str | None, allowed_origins: str | None = None):
    # Configure environment before module import
    if token is None:
        os.environ.pop("mcp_token", None)
    else:
        os.environ["mcp_token"] = token
    if allowed_origins is None:
        os.environ.pop("allowed_origins", None)
    else:
        os.environ["allowed_origins"] = allowed_origins
    import src.mcp_server as mcp_server
    importlib.reload(mcp_server)
    return mcp_server.app


def test_post_mcp_requires_bearer_when_token_set():
    app = build_app_with_env("secret-token", allowed_origins="http://127.0.0.1")
    client = TestClient(app)

    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_post_mcp_forbidden_origin_when_token_set():
    app = build_app_with_env("secret-token", allowed_origins="http://localhost")
    client = TestClient(app)
    headers = {"Origin": "https://evil.example"}
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers=headers,
    )
    assert r.status_code == 403


def test_initialize_sets_session_header_and_tools_list_with_session():
    app = build_app_with_env(None)
    client = TestClient(app)

    r1 = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert r1.status_code == 200
    session_id = r1.headers.get("Mcp-Session-Id")
    assert session_id and len(session_id) > 10

    r2 = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        headers={"Mcp-Session-Id": session_id},
    )
    assert r2.status_code == 200
