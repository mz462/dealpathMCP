import os
import importlib
from fastapi.testclient import TestClient


def build_app_with_env(token: str | None):
    # Ensure we reflect desired mode: if token is None, remove it from env
    if token is None:
        os.environ.pop("mcp_token", None)
    else:
        os.environ["mcp_token"] = token
    import src.mcp_server as mcp_server
    importlib.reload(mcp_server)
    return mcp_server.app, mcp_server


def test_mcp_ping_open_mode():
    app, mod = build_app_with_env(None)
    client = TestClient(app)
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["protocolVersion"] == mod.SUPPORTED_PROTOCOL_VERSION


def test_initialize_and_tools_list_open_mode():
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    r1 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert r1.status_code == 200
    body = r1.json()
    assert body["result"]["protocolVersion"] == mod.SUPPORTED_PROTOCOL_VERSION

    r2 = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r2.status_code == 200
    tools = r2.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"get_deals", "get_deal", "get_portfolio_summary"}.issubset(names)


def test_tools_call_get_deals_monkeypatched_open_mode(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    # Monkeypatch underlying client call to avoid real HTTP
    def fake_get_deals(**filters):
        return {"deals": {"data": [{"id": "d1", "deal_state": "Active", "deal_type": "Office", "last_updated": "2025-09-01T00:00:00Z"}]}}

    monkeypatch.setattr(mod, "client", type("_C", (), {"get_deals": staticmethod(fake_get_deals)})())

    payload = {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {"name": "get_deals", "arguments": {"status": "Active"}},
    }
    r = client.post("/mcp", json=payload)
    assert r.status_code == 200
    parts = r.json()["result"]["content"]
    assert isinstance(parts, list) and parts, "content should be a non-empty array"
    first = parts[0]
    assert first.get("type") in {"json", "text"}
    if first.get("type") == "json":
        assert "deals" in first.get("json", {})


def test_get_file_by_id_returns_resource_link(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    def fake_get_file_download_url(file_id: str):
        return {"url": "https://example.com/tmp/file123", "filename": "doc.pdf"}

    class FakeClient:
        @staticmethod
        def get_file_download_url(file_id: str):
            return {"url": "https://example.com/tmp/file123", "filename": "doc.pdf"}

        @staticmethod
        def download_file_content(file_id: str):
            # not used in this path
            return {"content": b"", "filename": "doc.pdf", "mime_type": "application/pdf"}

    monkeypatch.setattr(mod, "client", FakeClient())

    payload = {
        "jsonrpc": "2.0",
        "id": "call-file",
        "method": "tools/call",
        "params": {"name": "get_file_by_id", "arguments": {"file_id": "abc123"}},
    }
    r = client.post("/mcp", json=payload)
    assert r.status_code == 200
    parts = r.json()["result"]["content"]
    # Expect at least 3 parts: a text summary, a local resource_link, and a remote resource_link last
    assert len(parts) >= 2
    assert parts[-1]["type"] == "resource_link" and parts[-1]["uri"].startswith("https://example.com/")
    # Ensure a local link is also present somewhere
    assert any(p.get("type") == "resource_link" and "/local-files/" in p.get("uri", "") for p in parts)


def test_get_file_by_id_fallbacks_to_local_save(monkeypatch, tmp_path):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    class FakeClient:
        @staticmethod
        def get_file_download_url(file_id: str):
            raise RuntimeError("no download url available")

        @staticmethod
        def download_file_content(file_id: str):
            return {"content": b"hello", "filename": "hello.txt", "mime_type": "text/plain"}

    monkeypatch.setattr(mod, "client", FakeClient())
    monkeypatch.setattr(mod, "FILE_STORAGE_DIR", str(tmp_path))

    payload = {
        "jsonrpc": "2.0",
        "id": "call-file2",
        "method": "tools/call",
        "params": {"name": "get_file_by_id", "arguments": {"file_id": "abc123"}},
    }
    r = client.post("/mcp", json=payload)
    assert r.status_code == 200
    parts = r.json()["result"]["content"]
    # Last part should be a resource_link (no remote here, so local)
    assert parts and parts[-1]["type"] == "resource_link"
    assert parts[-1]["name"] == "hello.txt"
    assert parts[-1]["uri"].startswith("http://testserver/local-files/")
