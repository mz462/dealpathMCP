import os
import importlib
import json
from fastapi.testclient import TestClient


def build_app_with_env(token: str | None):
    if token is None:
        os.environ.pop("mcp_token", None)
    else:
        os.environ["mcp_token"] = token
    import src.mcp_server as mcp_server
    importlib.reload(mcp_server)
    return mcp_server.app, mcp_server


def test_search_proxies_upstream_search(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    class FakeClient:
        @staticmethod
        def search(**kwargs):
            assert kwargs.get("query") == "boston"
            return {"results": [{"id": 1, "type": "deal"}]}

    monkeypatch.setattr(mod, "client", FakeClient())

    payload = {
        "jsonrpc": "2.0",
        "id": "s",
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "boston"}},
    }
    r = client.post("/mcp", json=payload)
    assert r.status_code == 200
    parts = r.json()["result"]["content"]
    data = json.loads(parts[0]["text"])
    assert data["results"][0]["id"] == 1


def test_resources_read_deal_json_and_md(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    fake_deal_env = {"deal": {"data": {"id": 123, "name": "Test Deal", "deal_state": "Active"}}}

    class FakeClient:
        @staticmethod
        def get_deal_by_id(deal_id: str):
            assert deal_id == "123"
            return fake_deal_env

        @staticmethod
        def get_deals(**kwargs):
            return {"deals": {"data": [], "next_token": None}}

    monkeypatch.setattr(mod, "client", FakeClient())

    # JSON
    r1 = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "rj",
            "method": "resources/read",
            "params": {"uri": "dealpath://deal/123.json"},
        },
    )
    assert r1.status_code == 200
    contents = r1.json()["result"]["contents"]
    assert contents[0]["mimeType"] == "application/json"
    assert json.loads(contents[0]["text"]) == fake_deal_env

    # Markdown
    r2 = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": "rm",
            "method": "resources/read",
            "params": {"uri": "dealpath://deal/123.md"},
        },
    )
    assert r2.status_code == 200
    contents2 = r2.json()["result"]["contents"]
    assert contents2[0]["mimeType"] == "text/markdown"
    assert "Test Deal" in contents2[0]["text"]
