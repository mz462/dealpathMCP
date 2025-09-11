import os
import importlib
import json
from fastapi.testclient import TestClient


def build_app_with_env(token: str | None):
    # Mirror helper from existing tests without cross-imports
    if token is None:
        os.environ.pop("mcp_token", None)
    else:
        os.environ["mcp_token"] = token
    import src.mcp_server as mcp_server
    importlib.reload(mcp_server)
    return mcp_server.app, mcp_server


def test_basic_get_tools_monkeypatched(monkeypatch):
    app, mod = build_app_with_env(None)
    client = TestClient(app)

    # Fake client that returns minimal, schema-correct payloads for each tool
    class FakeClient:
        @staticmethod
        def get_fields_by_deal_id(deal_id: str, **params):
            return {"fields": {"data": [], "next_token": None}}

        @staticmethod
        def get_fields_by_investment_id(investment_id: str, **params):
            return {"fields": {"data": [], "next_token": None}}

        @staticmethod
        def get_fields_by_property_id(property_id: str, **params):
            return {"fields": {"data": [], "next_token": None}}

        @staticmethod
        def get_fields_by_asset_id(asset_id: str, **params):
            return {"fields": {"data": [], "next_token": None}}

        @staticmethod
        def get_fields_by_loan_id(loan_id: str, **params):
            return {"fields": {"data": [], "next_token": None}}

        @staticmethod
        def get_fields_by_field_definition_id(field_definition_id: str, **params):
            return {"fields": {"data": [], "next_token": None}}

        @staticmethod
        def get_file_tag_definitions(**params):
            return {"file_tag_definitions": {"data": [], "next_token": None}}

        @staticmethod
        def get_investments(**params):
            return {"investments": {"data": [], "next_token": None}}

        @staticmethod
        def get_loans(**params):
            return {"loans": {"data": [], "next_token": None}}

        @staticmethod
        def get_people(**params):
            return {"people": {"data": [], "next_token": None}}

    monkeypatch.setattr(mod, "client", FakeClient())

    tool_calls = [
        ("get_fields_by_deal_id", {"deal_id": "123"}, "fields"),
        ("get_fields_by_investment_id", {"investment_id": "456"}, "fields"),
        ("get_fields_by_property_id", {"property_id": "789"}, "fields"),
        ("get_fields_by_asset_id", {"asset_id": "234"}, "fields"),
        ("get_fields_by_loan_id", {"loan_id": "345"}, "fields"),
        (
            "get_fields_by_field_definition_id",
            {"field_definition_id": "567"},
            "fields",
        ),
        ("get_file_tag_definitions", {}, "file_tag_definitions"),
        ("get_investments", {}, "investments"),
        ("get_loans", {}, "loans"),
        ("get_people", {}, "people"),
    ]

    for name, args, top_key in tool_calls:
        payload = {
            "jsonrpc": "2.0",
            "id": f"call-{name}",
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        r = client.post("/mcp", json=payload)
        assert r.status_code == 200, f"{name} returned {r.status_code}"
        parts = r.json()["result"]["content"]
        assert parts and parts[0]["type"] == "text"
        data = json.loads(parts[0]["text"])  # server serializes dict to JSON string
        assert top_key in data, f"missing key {top_key} in response for {name}"
        inner = data[top_key]
        assert set(inner.keys()) == {"data", "next_token"}
        assert isinstance(inner["data"], list)
