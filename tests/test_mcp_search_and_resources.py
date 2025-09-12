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


def test_search_deals_filters_by_name_and_address(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    fake_deals = {
        "deals": {
            "data": [
                {"id": 1, "name": "Boston Office Tower", "address": {"city": "Boston"}},
                {"id": 2, "name": "Chicago Retail", "address": {"city": "Chicago"}},
                {"id": 3, "name": "Warehouse", "address": {"city": "Houston"}},
            ],
            "next_token": None,
        }
    }

    class FakeClient:
        @staticmethod
        def get_deals(**kwargs):
            return fake_deals

    monkeypatch.setattr(mod, "client", FakeClient())

    payload = {
        "jsonrpc": "2.0",
        "id": "s",
        "method": "tools/call",
        "params": {"name": "search_deals", "arguments": {"query": "boston"}},
    }
    r = client.post("/mcp", json=payload)
    assert r.status_code == 200
    parts = r.json()["result"]["content"]
    data = json.loads(parts[0]["text"])
    items = data["deals"]["data"]
    assert [d["id"] for d in items] == [1]


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

