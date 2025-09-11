import os
import importlib
from fastapi.testclient import TestClient


def build_app_with_env(token: str | None):
    if token is None:
        os.environ.pop("mcp_token", None)
    else:
        os.environ["mcp_token"] = token
    import src.mcp_server as mcp_server
    importlib.reload(mcp_server)
    return mcp_server.app, mcp_server


def test_tools_call_get_deal_happy_path(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    # Return a realistic nested structure that Dealpath would return
    def fake_get_deal_by_id(deal_id: str):
        return {"deal": {"data": {"id": deal_id, "deal_state": "Tracking"}, "next_token": None}}

    monkeypatch.setattr(mod, "client", type("_C", (), {"get_deal_by_id": staticmethod(fake_get_deal_by_id)})())

    payload = {
        "jsonrpc": "2.0",
        "id": "call-deal",
        "method": "tools/call",
        "params": {"name": "get_deal", "arguments": {"deal_id": "12345"}},
    }
    r = client.post("/mcp", json=payload)
    assert r.status_code == 200
    parts = r.json()["result"]["content"]
    assert parts and parts[0]["type"] == "text"
    body = parts[0]["text"]
    assert '"deal"' in body and '"data"' in body
