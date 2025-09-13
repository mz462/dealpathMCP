import json
import logging
import os
import pathlib
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Union, Callable, Tuple
from dataclasses import dataclass

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from uuid6 import uuid7

from .dealpath_client import DealpathClient
from .openapi_tools import load_dealpath_tools_from_yaml, load_get_operations, _jsonschema_from_params
try:
    from prometheus_client import Counter as PCounter, Histogram as PHist, generate_latest, CONTENT_TYPE_LATEST
    HAVE_PROM = True
except Exception:
    HAVE_PROM = False
    PCounter = PHist = None  # type: ignore
    generate_latest = lambda: b""  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"  # type: ignore

# --- App & Security ---------------------------------------------------------

load_dotenv()

# Record start time for uptime calculations
START_TIME = datetime.now(timezone.utc)

logger = logging.getLogger(__name__)

# Configure simple JSON logging by default
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        try:
            if record.exc_info:
                data["exc_info"] = self.formatException(record.exc_info)
        except Exception:
            pass
        return json.dumps(data, ensure_ascii=False)

if not logging.getLogger().handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)

app = FastAPI(title="Dealpath MCP Server (Streamable HTTP)")
try:
    client = DealpathClient()
except Exception:
    logger.info(
        "No default Dealpath API key; running in BYO-key mode (provide X-Dealpath-Key or initialize with dealpath_key)."
    )
    client = None  # type: ignore

MCP_TOKEN = os.getenv("mcp_token")
ALLOWED_ORIGINS = {
    o.strip()
    for o in (
        os.getenv("allowed_origins", "http://127.0.0.1,http://localhost").split(",")
    )
    if o.strip()
}
SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
FILE_STORAGE_DIR = os.getenv(
    "file_storage_dir", os.path.join(os.getcwd(), "local_files")
)

RATE_LIMIT_CALLS_PER_MIN = int(os.getenv("RATE_LIMIT_CALLS_PER_MIN", "60"))

class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float):
        self.capacity = max(1, capacity)
        self.tokens = float(capacity)
        self.refill_per_second = refill_per_second
        self.last = datetime.now(timezone.utc)

    def take(self, n: float = 1.0) -> bool:
        now = datetime.now(timezone.utc)
        elapsed = (now - self.last).total_seconds()
        self.last = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

_buckets: dict[str, TokenBucket] = {}
def _rate_limiter_bucket(key: str) -> TokenBucket:
    b = _buckets.get(key)
    if b is None:
        b = TokenBucket(capacity=RATE_LIMIT_CALLS_PER_MIN, refill_per_second=RATE_LIMIT_CALLS_PER_MIN / 60.0)
        _buckets[key] = b
    return b

# --- Lightweight TTL cache -------------------------------------------------

class TTLCache:
    """Very small in-memory TTL cache for hot items (deals, summaries).

    Not for persistence; just to reduce latency and API calls during a session.
    """

    def __init__(self, default_ttl_seconds: int = 300):
        self.default_ttl = default_ttl_seconds
        self._store: dict[str, tuple[datetime, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        expires_at, value = item
        if datetime.now(timezone.utc) >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        self._store[key] = (datetime.now(timezone.utc) + timedelta(seconds=ttl), value)


# Small caches scoped to process
cache = TTLCache(default_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "180")))
md_cache = TTLCache(default_ttl_seconds=int(os.getenv("MD_CACHE_TTL_SECONDS", "180")))

# Session management for Streamable HTTP transport
sessions: dict[str, dict[str, Any]] = {}

# Tool call metrics (lightweight in-memory counters)
TOOL_METRICS: dict[str, Any] = {
    "calls_total": 0,
    "errors_total": 0,
    "by_name": defaultdict(lambda: {"calls": 0, "errors": 0, "total_latency_ms": 0, "count": 0}),
}

# Prometheus metrics
if HAVE_PROM:
    PROM_TOOL_CALLS = PCounter("mcp_tool_calls_total", "Total tool calls", ["tool", "status"])  # type: ignore
    PROM_TOOL_DURATION = PHist(
        "mcp_tool_call_duration_ms",
        "Tool call duration in milliseconds",
        ["tool"],
        buckets=(50, 100, 200, 500, 1000, 2000, 5000, 10000),
    )  # type: ignore
    PROM_RATE_LIMIT_HITS = PCounter("mcp_rate_limit_hits_total", "Rate limit hits")  # type: ignore
    PROM_LOCAL_FILES_SERVED = PCounter("mcp_local_files_served_total", "Local files served")  # type: ignore
    PROM_LOCAL_BYTES_SERVED = PCounter("mcp_local_bytes_served_total", "Local bytes served")  # type: ignore
    PROM_UPSTREAM_FAILURES = PCounter("mcp_upstream_failures_total", "Upstream failures", ["operation"])  # type: ignore
else:
    PROM_TOOL_CALLS = PROM_TOOL_DURATION = PROM_RATE_LIMIT_HITS = PROM_LOCAL_FILES_SERVED = PROM_LOCAL_BYTES_SERVED = PROM_UPSTREAM_FAILURES = None


def _record_tool_call(name: str, duration_ms: Optional[int] = None, error: bool = False) -> None:
    """Record a tool call result into in-memory metrics."""
    try:
        TOOL_METRICS["calls_total"] += 1
        bucket = TOOL_METRICS["by_name"][name]
        bucket["calls"] += 1
        if duration_ms is not None:
            bucket["total_latency_ms"] += int(duration_ms)
            bucket["count"] += 1
            try:
                PROM_TOOL_DURATION.labels(tool=name).observe(max(0.0, float(duration_ms)))
            except Exception:
                pass
        if error:
            TOOL_METRICS["errors_total"] += 1
            bucket["errors"] += 1
            try:
                PROM_TOOL_CALLS.labels(tool=name, status="error").inc()
            except Exception:
                pass
        else:
            try:
                PROM_TOOL_CALLS.labels(tool=name, status="ok").inc()
            except Exception:
                pass
    except Exception:
        # Never let metrics recording affect request flow
        pass


def create_session() -> str:
    """Create a new MCP session with secure session ID."""
    session_id = str(uuid7())
    sessions[session_id] = {
        "created_at": datetime.now(timezone.utc),
        "last_accessed": datetime.now(timezone.utc),
        "protocol_version": SUPPORTED_PROTOCOL_VERSION,
        "initialized": False,
        # Optionally stores a per-session Dealpath API key (BYO key). Not persisted.
        # Key is accepted at initialize via header X-Dealpath-Key or params.dealpath_key.
        "dealpath_key": None,
    }
    logger.info(f"Created new MCP session: {session_id}")
    return session_id


def get_session(session_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Get session data if valid, otherwise None."""
    if not session_id or session_id not in sessions:
        return None

    session = sessions[session_id]
    session["last_accessed"] = datetime.now(timezone.utc)
    return session


def cleanup_expired_sessions(max_age_hours: int = 24):
    """Clean up expired sessions."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    expired = [
        sid for sid, session in sessions.items() if session["last_accessed"] < cutoff
    ]

    for session_id in expired:
        del sessions[session_id]
        logger.info(f"Cleaned up expired session: {session_id}")

    return len(expired)


def get_dealpath_client_for_session(session: Optional[dict[str, Any]]) -> Optional[DealpathClient]:
    """Return a DealpathClient using session-specific key if provided; otherwise global client.

    The session key is never logged and only kept in-process for the session lifetime.
    """
    try:
        if session and session.get("dealpath_key"):
            return DealpathClient(api_key=session["dealpath_key"])  # ephemeral client
    except Exception:
        # Fall back to global client on any error constructing per-session client
        pass
    return client


if not MCP_TOKEN:
    logger.info(
        "Environment variable 'mcp_token' not set; POST /mcp is open for local dev. "
        "Set mcp_token to require bearer auth."
    )


@app.middleware("http")
async def auth_and_origin_middleware(request: Request, call_next):
    """Optional bearer auth and Origin check for /mcp endpoints.

    Behavior:
      - If MCP_TOKEN is set and request is POST /mcp, require Authorization: Bearer <token>.
      - GET /mcp is always allowed (connectivity probe / ping).
      - If an Origin header is present, and method is POST /mcp, require it to be in ALLOWED_ORIGINS.
    """
    path = request.url.path
    method = request.method.upper()
    if path == "/mcp" or path.startswith("/mcp/"):
        # Allow GET without auth for connectivity probes
        if method == "GET":
            return await call_next(request)

        # For POST, optionally enforce origin + bearer if token configured
        if method == "POST" and MCP_TOKEN:
            origin = request.headers.get("origin")
            if origin and origin not in ALLOWED_ORIGINS:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={
                        "error": {
                            "code": "forbidden_origin",
                            "message": "Origin not allowed.",
                        }
                    },
                )

            authz = request.headers.get("authorization", "")
            scheme, _, token = authz.partition(" ")
            if scheme.lower() != "bearer" or not token or token != MCP_TOKEN:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": "Missing or invalid bearer token.",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )

    return await call_next(request)


# Restrictive CORS (if a browser client is used in dev). Not required for non-browser clients.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# --- Minimal MCP over HTTP (non-OAuth) -------------------------------------

Json = dict[str, Any]


# Field-thinning helpers removed in lean API; return upstream payloads verbatim


def mcp_response_ok(req_id: Any, result: Any) -> Json:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def mcp_response_error(req_id: Any, code: int, message: str, data: Any = None) -> Json:
    """Build a JSON-RPC error response consistently.

    Always returns an error envelope; includes optional data when provided.
    """
    err: Json = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def build_tools_list() -> dict[str, Any]:
    """Build tools from OpenAPI spec; fall back to a minimal static list.

    Only includes lean pass-through endpoints plus get_file_by_id (composite).
    """
    try:
        # Build tools for all GET operations from OpenAPI
        ops = load_get_operations()
        tools = []
        # Keep compatibility aliases for test-expected names
        alias_map = {
            "get_deal": "/deal/{deal_id}",  # prefer snake alias for getDealById
        }
        for op in ops:
            input_schema = _jsonschema_from_params(op.get("parameters") or [])
            name = op["name"]
            # Add alias tool names if applicable
            if op["path"] == "/deal/{deal_id}":
                tools.append(
                    {
                        "name": "get_deal",
                        "title": op["title"],
                        "description": op["description"],
                        "inputSchema": input_schema,
                    }
                )
            tools.append(
                {
                    "name": name,
                    "title": op["title"],
                    "description": op["description"],
                    "inputSchema": input_schema,
                }
            )
    except Exception:
        tools = [
            {
                "name": "get_deals",
                "title": "List Deals",
                "description": "Proxy for GET /deals",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "get_deal",
                "title": "Get Deal",
                "description": "Proxy for GET /deal/{deal_id}",
                "inputSchema": {
                    "type": "object",
                    "required": ["deal_id"],
                    "properties": {"deal_id": {"type": "string", "description": "Deal ID"}},
                    "additionalProperties": False,
                },
            },
        ]
    # Always include composite get_file_by_id tool
    tools.append(
        {
            "name": "get_file_by_id",
            "title": "Download File",
            "description": "Download a file by ID via signed URL or proxy; returns resource links.",
            "inputSchema": {
                "type": "object",
                "required": ["file_id"],
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                    "download_locally": {"type": "boolean", "default": True},
                },
                "additionalProperties": False,
            },
        }
    )
    # Keep describe_schema which normalizes field definitions envelope
    tools.append(
        {
            "name": "describe_schema",
            "title": "Describe Schema",
            "description": "Return field definitions normalized to {field_definitions:{data,next_token}}.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        }
    )
    return {"tools": tools}


def _sanitize_filename(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    if not name:
        return "file.bin"
    # allow alnum and a few safe symbols
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return cleaned or "file.bin"


def _sanitize_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(value)) or "id"


def _build_local_relpath(file_id: str, filename: str) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe_id = _sanitize_id(file_id)
    safe_name = _sanitize_filename(filename)
    return f"{date_str}/{safe_id}/{safe_name}"


def _store_bytes_locally(file_id: str, filename: str, data: bytes) -> str:
    relpath = _build_local_relpath(file_id, filename)
    dest_path = os.path.join(FILE_STORAGE_DIR, relpath)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(data)
    return relpath


def _store_stream_locally(file_id: str, filename: str, resp: requests.Response) -> str:
    relpath = _build_local_relpath(file_id, filename)
    dest_path = os.path.join(FILE_STORAGE_DIR, relpath)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return relpath


def _absolute_local_url(base_url: str, relpath: str) -> str:
    base = base_url.rstrip("/")
    rel = relpath.replace("\\", "/")
    return f"{base}/local-files/{rel}"


# --- Tool Registry (incremental refactor) ---------------------------------

@dataclass(frozen=True)
class ToolHandler:
    name: str
    execute: Callable[[dict[str, Any], DealpathClient, Optional[str]], Any]
    title: Optional[str] = None
    description: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = None


TOOL_REGISTRY: dict[str, ToolHandler] = {}


def register_tool(handler: ToolHandler) -> None:
    TOOL_REGISTRY[handler.name] = handler


# Legacy search_deals executor removed; use upstream 'search' tool instead


def _exec_get_deals(args: dict[str, Any], upstream: DealpathClient, base_url: Optional[str]) -> Any:
    status_val = args.get("status")
    property_type = args.get("propertyType")
    next_token = args.get("next_token")
    limit = args.get("limit")
    filters: dict[str, Any] = {}
    if status_val:
        filters["status"] = status_val
    if next_token:
        filters["next_token"] = next_token
    if limit is not None:
        filters["limit"] = limit

    result = upstream.get_deals(**filters)

    # Local property type filter to match prior behavior
    if property_type:
        try:
            deals_container = result.get("deals") or {}
            data = deals_container.get("data") or []
            filtered = [d for d in data if str(d.get("deal_type")) == str(property_type)]
            deals_container = dict(deals_container)
            deals_container["data"] = filtered
            result = dict(result)
            result["deals"] = deals_container
        except Exception:
            pass
    return result


def _exec_get_deal(args: dict[str, Any], upstream: DealpathClient, base_url: Optional[str]) -> Any:
    deal_id = args.get("deal_id")
    if not deal_id:
        raise HTTPException(status_code=400, detail="deal_id is required")
    try:
        return upstream.get_deal_by_id(deal_id)
    except requests.HTTPError as http_err:
        resp = http_err.response
        detail = {
            "url": str(getattr(resp, "url", "")),
            "status": getattr(resp, "status_code", 0),
            "reason": getattr(resp, "reason", ""),
            "body": resp.text[:500] if getattr(resp, "text", None) else None,
        }
        raise HTTPException(status_code=resp.status_code, detail=detail)


# Register a first batch of tools via the registry
# Note: Deliberately omit legacy 'search_deals' (dropped in lean API)
register_tool(
    ToolHandler(
        name="get_deals",
        execute=_exec_get_deals,
        title="List Deals",
        description="Retrieve deals with optional status filter and local propertyType filter.",
    )
)
register_tool(
    ToolHandler(
        name="get_deal",
        execute=_exec_get_deal,
        title="Get Deal Details",
        description="Return a single deal by ID.",
    )
)


def tool_call_dispatch(
    name: str, arguments: dict[str, Any], *, base_url: Optional[str] = None, dp: Optional[DealpathClient] = None
) -> Any:
    upstream = dp or client
    if upstream is None:
        raise HTTPException(
            status_code=400,
            detail="Dealpath API key required. Provide X-Dealpath-Key header or initialize with dealpath_key.",
        )
    # First, try the registry-based handlers to avoid the large conditional chain
    handler = TOOL_REGISTRY.get(name)
    if handler is not None:
        return handler.execute(arguments, upstream, base_url)
    # Generic GET proxy via OpenAPI when possible
    try:
        ops = load_get_operations()
        # Map by name for quick lookup
        op_map = {op["name"]: op for op in ops}
        # Compatibility alias for get_deal
        for op in ops:
            if op["path"] == "/deal/{deal_id}":
                op_map.setdefault("get_deal", op)
        op = op_map.get(name)
        if op:
            # Build URL: substitute path params from arguments; remaining args → query
            path = op["path"]
            params_meta = op.get("parameters") or []
            path_param_names = {p.get("name") for p in params_meta if p.get("in") == "path"}
            url_path = path
            for p in path_param_names:
                token = "{" + p + "}"
                if token in url_path:
                    val = arguments.get(p)
                    if val is None:
                        raise HTTPException(status_code=400, detail=f"Missing path parameter: {p}")
                    url_path = url_path.replace(token, str(val))
            query = {k: v for k, v in arguments.items() if k not in path_param_names and v is not None}
            # Use DealpathClient.session directly
            from .dealpath_client import BASE_URL as DP_BASE
            url = f"{DP_BASE}{url_path}"
            resp = upstream.session.get(url, params=query, timeout=30)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        pass
    # legacy 'search_deals' removed in lean API

    if name == "get_deals":
        status_val = arguments.get("status")
        property_type = arguments.get("propertyType")
        next_token = arguments.get("next_token")
        limit = arguments.get("limit")
        filters = {}
        if status_val:
            filters["status"] = status_val
        if next_token:
            filters["next_token"] = next_token
        if limit is not None:
            filters["limit"] = limit

        result = upstream.get_deals(**filters)

        # If a propertyType filter is provided, apply a safe local filter on the
        # returned payload (deal.deal_type) to ensure the behavior users expect.
        if property_type:
            try:
                deals_container = result.get("deals") or {}
                data = deals_container.get("data") or []
                filtered = [
                    d for d in data if str(d.get("deal_type")) == str(property_type)
                ]
                # Replace data with filtered list; keep other keys intact
                deals_container = dict(deals_container)
                deals_container["data"] = filtered
                # Do not modify next_token since we're client-side filtering
                result = dict(result)
                result["deals"] = deals_container
            except Exception:
                # If structure unexpected, return original result unmodified
                pass
        return result

    if name == "get_deal":
        deal_id = arguments.get("deal_id")
        if not deal_id:
            raise HTTPException(status_code=400, detail="deal_id is required")
        try:
            return upstream.get_deal_by_id(deal_id)
        except requests.HTTPError as http_err:
            resp = http_err.response
            detail = {
                "url": str(getattr(resp, "url", "")),
                "status": getattr(resp, "status_code", 0),
                "reason": getattr(resp, "reason", ""),
                "body": resp.text[:500] if getattr(resp, "text", None) else None,
            }
            raise HTTPException(status_code=resp.status_code, detail=detail)

    if name == "get_fields_by_deal_id":
        deal_id = arguments.get("deal_id")
        if not deal_id:
            raise HTTPException(status_code=400, detail="deal_id is required")
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_fields_by_deal_id(deal_id, **params)

    if name == "describe_schema":
        # Normalize to {field_definitions: {data, next_token}}
        raw = upstream.get_field_definitions()
        container = raw.get("field_definitions") if isinstance(raw, dict) else None
        if not isinstance(container, dict):
            container = {"data": [], "next_token": None}
        return {"field_definitions": container}

    if name == "get_fields_by_investment_id":
        investment_id = arguments.get("investment_id")
        if not investment_id:
            raise HTTPException(status_code=400, detail="investment_id is required")
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_fields_by_investment_id(investment_id, **params)

    if name == "get_fields_by_property_id":
        property_id = arguments.get("property_id")
        if not property_id:
            raise HTTPException(status_code=400, detail="property_id is required")
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_fields_by_property_id(property_id, **params)

    if name == "get_fields_by_asset_id":
        asset_id = arguments.get("asset_id")
        if not asset_id:
            raise HTTPException(status_code=400, detail="asset_id is required")
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_fields_by_asset_id(asset_id, **params)

    if name == "get_fields_by_loan_id":
        loan_id = arguments.get("loan_id")
        if not loan_id:
            raise HTTPException(status_code=400, detail="loan_id is required")
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_fields_by_loan_id(loan_id, **params)

    if name == "get_fields_by_field_definition_id":
        field_definition_id = arguments.get("field_definition_id")
        if not field_definition_id:
            raise HTTPException(
                status_code=400, detail="field_definition_id is required"
            )
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_fields_by_field_definition_id(field_definition_id, **params)

    if name == "get_file_tag_definitions":
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_file_tag_definitions(**params)

    if name == "get_investments":
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_investments(**params)

    if name == "get_loans":
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_loans(**params)

    if name == "get_people":
        params = {}
        if arguments.get("next_token"):
            params["next_token"] = arguments["next_token"]
        return upstream.get_people(**params)

    if name == "get_list_options_by_field_definition_id":
        field_definition_id = arguments.get("field_definition_id")
        if not field_definition_id:
            raise HTTPException(status_code=400, detail="field_definition_id is required")
        return upstream.get_list_options_by_field_definition_id(field_definition_id)

    if name == "get_deal_files":
        deal_id = arguments.get("deal_id")
        if deal_id is None:
            raise HTTPException(status_code=400, detail="deal_id is required")
        params = {
            k: v for k, v in arguments.items() if k != "deal_id" and v is not None
        }
        return upstream.get_deal_files_by_id(deal_id, **params)

    # portfolio summary removed in lean API

    if name == "search":
        query = arguments.get("query")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        return upstream.search(query=query)

    if name == "get_file_by_id":
        file_id = arguments.get("file_id")
        if not file_id:
            raise HTTPException(status_code=400, detail="file_id is required")
        parts: list[dict[str, Any]] = []
        # Download via files.dealpath.com with Authorization and store locally only
        try:
            data = upstream.download_file_content(file_id)
            filename = data.get("filename", str(file_id))
            rel = _store_bytes_locally(file_id, filename, data["content"])
            local_uri = _absolute_local_url(base_url or "http://127.0.0.1:8000", rel)
            summary = f"File saved locally: {filename}\n- Local: {local_uri}"
            parts.append({"type": "text", "text": summary})
            parts.append({"type": "resource_link", "name": filename, "uri": local_uri})
            return {"__content__": parts}
        except requests.HTTPError as http_err:
            resp = http_err.response
            raise HTTPException(status_code=resp.status_code, detail=f"Dealpath error: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch file: {e}")

    # Executive Analytics Tools
    # executive analytics removed in lean API

    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


# _search_deals_impl removed (use upstream 'search')


def to_content_parts(value: Any) -> list[dict[str, Any]]:
    """Convert a Python value to MCP content parts array.

    For maximum client compatibility, return a single `text` part.
    - dict/list → pretty-printed JSON string
    - str → as-is
    - other scalars → stringified
    """

    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)

    return [{"type": "text", "text": text}]


# --- MCP Resources & Prompts ----------------------------------------------

def build_resource_templates() -> list[dict[str, Any]]:
    return [
        {
            "name": "Deal JSON",
            "uriTemplate": "dealpath://deal/{deal_id}.json",
            "mimeType": "application/json",
            "description": "Canonical JSON for a single deal",
        },
        {
            "name": "Deal Summary",
            "uriTemplate": "dealpath://deal/{deal_id}.md",
            "mimeType": "text/markdown",
            "description": "Compact markdown summary of a deal",
        },
    ]


def _parse_dealpath_uri(uri: str) -> Tuple[str, str]:
    """Parse a dealpath:// URI and return (kind, value).

    kind: 'deal_json' | 'deal_md'
    value: id or query
    """
    if not uri.startswith("dealpath://"):
        raise HTTPException(status_code=400, detail="Unsupported URI scheme")
    body = uri[len("dealpath://") :]
    if body.startswith("deal/") and body.endswith(".json"):
        return ("deal_json", body[len("deal/") : -len(".json")])
    if body.startswith("deal/") and body.endswith(".md"):
        return ("deal_md", body[len("deal/") : -len(".md")])
    raise HTTPException(status_code=404, detail="Resource not found")


def _deal_markdown(deal: dict[str, Any]) -> str:
    name = deal.get("name") or deal.get("title") or f"Deal {deal.get('id','?')}"
    deal_id = deal.get("id") or deal.get("deal_id")
    state = deal.get("deal_state") or deal.get("status")
    dtype = deal.get("deal_type") or deal.get("type")
    last_updated = deal.get("last_updated") or deal.get("updated_at")
    addr = deal.get("address") or {}
    addr_str = ", ".join(
        [
            s
            for s in [addr.get("line1"), addr.get("city"), addr.get("state"), addr.get("country")]
            if s
        ]
    )
    lines = [
        f"# {name}",
        "",
        f"- ID: {deal_id}",
        f"- Stage: {state}",
        f"- Type: {dtype}",
        f"- Address: {addr_str}" if addr_str else "- Address: (none)",
        f"- Last Updated: {last_updated}" if last_updated else "- Last Updated: (unknown)",
    ]
    # Key dates if present
    for k in ("loi_date", "ic_date", "close_date"):
        if deal.get(k):
            lines.append(f"- {k.replace('_',' ').title()}: {deal[k]}")
    return "\n".join(lines) + "\n"


@app.post("/mcp")
async def mcp_http_endpoint(
    request: Request,
    payload: Union[dict[str, Any], list[dict[str, Any]]],
    mcp_session_id: Optional[str] = Header(None, alias="Mcp-Session-Id"),
    accept: Optional[str] = Header(None),
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """Streamable HTTP MCP endpoint (2025-03-26 spec) with session management.

    Supported methods:
      - initialize (creates session)
      - tools/list
      - tools/call
      - ping

    Features:
      - Session management with Mcp-Session-Id headers
      - Backward compatibility with legacy clients
      - Enhanced error handling and logging
    """

    base_url = str(request.base_url).rstrip("/")

    # Clean up expired sessions periodically
    if len(sessions) > 100:  # arbitrary threshold
        cleanup_expired_sessions()

    def handle_one(req: dict[str, Any]) -> dict[str, Any]:
        req_id = req.get("id")
        method = req.get("method") or req.get("type")  # tolerate `type` alias
        params: dict[str, Any] = req.get("params") or {}

        if not method:
            return mcp_response_error(req_id, -32600, "Missing method")

        try:
            if method == "initialize":
                # Create new session for Streamable HTTP transport
                session_id = create_session()
                session = sessions[session_id]
                session["initialized"] = True

                # BYO Dealpath key: accept from header or params.dealpath_key
                # Only store in-memory, never log or persist.
                try:
                    dp_key_param = (params.get("dealpath_key") if isinstance(params, dict) else None)
                except Exception:
                    dp_key_param = None
                dp_key = x_dealpath_key or dp_key_param
                if isinstance(dp_key, str) and dp_key.strip():
                    session["dealpath_key"] = dp_key.strip()

                result = {
                    "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False},
                        "prompts": {"listChanged": False},
                        "logging": {},
                    },
                    "serverInfo": {
                        "name": "dealpath-mcp",
                        "version": "0.2.0",
                    },
                    "instructions": "Dealpath MCP server provides access to real estate deal data, file management, and portfolio analytics.",
                }

                # Return with session ID header for Streamable HTTP transport
                response = mcp_response_ok(req_id, result)
                # Note: We'll handle headers in the outer scope
                response["_session_id"] = session_id
                return response

            # Validate session for non-initialize requests
            session = get_session(mcp_session_id)
            if session and not session.get("initialized"):
                return mcp_response_error(req_id, -32002, "Session not initialized")

            if method in ("tools/list", "tools.list"):
                return mcp_response_ok(req_id, build_tools_list())

            if method in ("tools/call", "tools.call"):
                # Rate limit per session or IP
                client_ip = request.client.host if request.client else "unknown"
                key_for_bucket = mcp_session_id or client_ip
                bucket = _rate_limiter_bucket(f"tools:{key_for_bucket}")
                if not bucket.take(1):
                    try:
                        PROM_RATE_LIMIT_HITS.inc()
                    except Exception:
                        pass
                    return mcp_response_error(
                        req_id,
                        429,
                        "Rate limit exceeded for tools/call (per minute)",
                        {"retry_after_seconds": 60},
                    )
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not name:
                    return mcp_response_error(req_id, -32602, "Missing tool name")

                # Enhanced logging for tool calls
                try:
                    logger.info(
                        f"Tool call: {name} with args: {list(arguments.keys())} [session: {mcp_session_id}]"
                    )
                except Exception:
                    pass

                # Metrics instrumentation around tool call
                import time as _time
                _start = _time.time()
                dp_client = get_dealpath_client_for_session(session)
                # Allow per-request header for BYO key even if no session/initialize
                if dp_client is None and x_dealpath_key and x_dealpath_key.strip():
                    try:
                        dp_client = DealpathClient(api_key=x_dealpath_key.strip())
                    except Exception:
                        dp_client = None
                try:
                    result = tool_call_dispatch(name, arguments, base_url=base_url, dp=dp_client)
                except HTTPException as http_exc:
                    _record_tool_call(name, duration_ms=int((_time.time() - _start) * 1000), error=True)
                    raise http_exc
                except Exception:
                    _record_tool_call(name, duration_ms=int((_time.time() - _start) * 1000), error=True)
                    raise
                else:
                    _record_tool_call(name, duration_ms=int((_time.time() - _start) * 1000), error=False)
                if isinstance(result, dict) and "__content__" in result:
                    parts = result["__content__"]
                else:
                    parts = to_content_parts(result)
                return mcp_response_ok(req_id, {"content": parts})

            if method in ("resources/list", "resources.list"):
                return mcp_response_ok(
                    req_id, {"resources": [], "resourceTemplates": build_resource_templates()}
                )

            if method in ("resources/read", "resources.read"):
                uri = params.get("uri")
                if not uri:
                    return mcp_response_error(req_id, -32602, "Missing uri")
                kind, value = _parse_dealpath_uri(uri)
                dp_client = get_dealpath_client_for_session(session)
                if dp_client is None and x_dealpath_key and x_dealpath_key.strip():
                    try:
                        dp_client = DealpathClient(api_key=x_dealpath_key.strip())
                    except Exception:
                        dp_client = None
                if dp_client is None:
                    return mcp_response_error(
                        req_id,
                        401,
                        "Dealpath API key required. Provide X-Dealpath-Key header or initialize with dealpath_key.",
                    )
                if kind == "deal_json":
                    cache_key = f"deal_json:{value}"
                    data = cache.get(cache_key)
                    if data is None:
                        data = dp_client.get_deal_by_id(value)
                        cache.set(cache_key, data)
                    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                    return mcp_response_ok(
                        req_id,
                        {
                            "contents": [
                                {
                                    "uri": uri,
                                    "mimeType": "application/json",
                                    "text": text,
                                }
                            ]
                        },
                    )
                if kind == "deal_md":
                    cache_key = f"deal_md:{value}"
                    md = md_cache.get(cache_key)
                    if md is None:
                        deal_obj = cache.get(f"deal_json:{value}") or dp_client.get_deal_by_id(
                            value
                        )
                        # normalize deal dict from nested envelope if needed
                        deal = (
                            deal_obj.get("deal", {}).get("data")
                            if isinstance(deal_obj, dict)
                            else None
                        ) or deal_obj
                        md = _deal_markdown(deal)
                        md_cache.set(cache_key, md)
                    return mcp_response_ok(
                        req_id,
                        {
                            "contents": [
                                {
                                    "uri": uri,
                                    "mimeType": "text/markdown",
                                    "text": md,
                                }
                            ]
                        },
                    )
                # no other resource kinds supported in lean mode

            if method in ("prompts/list", "prompts.list"):
                return mcp_response_ok(
                    req_id,
                    {
                        "prompts": [
                            {
                                "name": "ask_about_deal",
                                "description": "Prefer get_deal and get_fields_by_deal_id; never invent missing fields.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"deal_id": {"type": "string"}},
                                },
                            },
                            {
                                "name": "summarize_pipeline",
                                "description": "Summarize deals grouped by stage/market/owner.",
                                "inputSchema": {"type": "object", "properties": {}},
                            },
                            # lean mode: omit advanced field-thinning guidance
                        ]
                    },
                )

            if method in ("prompts/get", "prompts.get"):
                name = params.get("name")
                if not name:
                    return mcp_response_error(req_id, -32602, "Missing prompt name")
                if name == "ask_about_deal":
                    return mcp_response_ok(
                        req_id,
                        {
                            "messages": [
                                {
                                    "role": "system",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": (
                                                "Use structured tools first: get_deal (core) and get_fields_by_deal_id (custom fields). "
                                                "Warning: get_fields_by_* can return many items (including long text, HTML snippets, lists, and linked IDs). "
                                                "Start with filters to control size: non_null:true, names_only:true, name_contains:[""risk"", ""milestone"", ...], limit:25. "
                                                "If more is needed, paginate with next_token. Do not request all fields without filters. "
                                                "If tools are insufficient for summarization, you may read dealpath://deal/{deal_id}.md."
                                            ),
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                if name == "summarize_pipeline":
                    return mcp_response_ok(
                        req_id,
                        {
                            "messages": [
                                {
                                    "role": "system",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": (
                                                "Group by stage, market, and owner. Prefer structured fields."
                                            ),
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                # lean mode: omit 'inspect_fields' prompt
                return mcp_response_error(req_id, 404, f"Unknown prompt: {name}")

            if method == "ping":
                return mcp_response_ok(
                    req_id, {"ok": True, "session": mcp_session_id is not None}
                )

            return mcp_response_error(req_id, -32601, f"Method not found: {method}")

        except HTTPException as http_exc:
            logger.error(f"HTTP error in MCP call {method}: {http_exc.detail}")
            return mcp_response_error(req_id, http_exc.status_code, http_exc.detail)
        except Exception as e:
            logger.exception(f"Unhandled MCP error in {method}")
            return mcp_response_error(
                req_id, 500, "Internal error", {"message": str(e)}
            )

    # Handle batched requests (JSON-RPC 2.0 batch)
    if isinstance(payload, list):
        responses = []
        session_id_to_set = None
        for req in payload:
            response = handle_one(req)
            if isinstance(response, dict) and "_session_id" in response:
                session_id_to_set = response.pop("_session_id")
            responses.append(response)

        json_response = JSONResponse(responses)
        if session_id_to_set:
            json_response.headers["Mcp-Session-Id"] = session_id_to_set
        return json_response
    else:
        response = handle_one(payload)

        # Handle session ID header for initialize
        session_id_to_set = None
        if isinstance(response, dict) and "_session_id" in response:
            session_id_to_set = response.pop("_session_id")

        json_response = JSONResponse(response)
        if session_id_to_set:
            json_response.headers["Mcp-Session-Id"] = session_id_to_set

        return json_response


@app.get("/mcp")
async def mcp_http_ping():
    """Simple connectivity check for clients that probe with GET.

    Always returns 200. Does not require auth, even if `mcp_token` is set.
    """
    return {
        "ok": True,
        "message": "Dealpath MCP HTTP endpoint",
        "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
    }


# --- OAuth stubs for remote transports that probe OAuth flows --------------

@app.get("/oauth/authorize")
async def oauth_authorize_stub():
    # We do not support OAuth; return a well-formed OAuth error body
    return JSONResponse(
        status_code=400,
        content={
            "error": "unsupported_response_type",
            "error_description": "This server does not implement OAuth authorization. Use direct headers.",
        },
    )


@app.post("/oauth/token")
async def oauth_token_stub():
    return JSONResponse(
        status_code=400,
        content={
            "error": "unsupported_grant_type",
            "error_description": "This server does not implement OAuth token exchange.",
        },
    )


@app.get("/local-files/{date}/{file_id}/{filename}")
async def serve_local_file(request: Request, date: str, file_id: str, filename: str):
    base = pathlib.Path(FILE_STORAGE_DIR).resolve()
    # Normalize and sanitize path components
    safe_date = re.sub(r"[^0-9]", "", date)[:8]
    safe_id = _sanitize_id(file_id)
    safe_name = _sanitize_filename(filename)
    path = (base / safe_date / safe_id / safe_name).resolve()
    # Prevent path traversal
    if not str(path).startswith(str(base)) or not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    # Stream file back
    from fastapi.responses import FileResponse
    # Optional bearer guard when MCP_TOKEN is set
    if MCP_TOKEN:
        authz = request.headers.get("authorization", "")
        scheme, _, token = authz.partition(" ")
        if scheme.lower() != "bearer" or not token or token != MCP_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")
    resp = FileResponse(path)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    try:
        PROM_LOCAL_FILES_SERVED.inc()
        size = os.path.getsize(path)
        PROM_LOCAL_BYTES_SERVED.inc(size)
    except Exception:
        pass
    return resp


@app.get("/mcp/getDeals")
def get_deals_endpoint(
    status: Optional[str] = None, propertyType: Optional[str] = None
):
    """
    Retrieves a list of deals, with optional filtering by status and property type.

    Args:
        status: Filter deals by status (e.g., "Active", "Closed").
        propertyType: Filter deals by property type (e.g., "Office", "Retail").

    Returns:
        A JSON object containing a list of deals.
    """
    try:
        filters = {}
        if status:
            filters["status"] = status
        if propertyType:
            filters["propertyType"] = propertyType

        deals = client.get_deals(**filters)
        return deals
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getDeal/{deal_id}")
def get_deal_by_id_endpoint(deal_id: str):
    """
    Retrieves a single deal by its unique ID.

    Args:
        deal_id: The unique identifier for the deal.

    Returns:
        A JSON object representing the deal.
    """
    try:
        deal = client.get_deal_by_id(deal_id)
        return deal
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getDealFiles/{deal_id}")
def get_deal_files_by_id_endpoint(
    deal_id: int,
    parent_folder_ids: Optional[list[int]] = Query(None),
    file_tag_definition_ids: Optional[list[int]] = Query(None),
    updated_before: Optional[int] = Query(None),
    updated_after: Optional[int] = Query(None),
    next_token: Optional[str] = Query(None),
):
    """
    Retrieves a list of files for a specific deal, with optional filtering.

    Args:
        deal_id: The unique identifier for the deal.
        parent_folder_ids: List of parent folder IDs to filter files.
        file_tag_definition_ids: List of file tag definition IDs to filter files.
        updated_before: Unix timestamp to filter files updated before this time.
        updated_after: Unix timestamp to filter files updated after this time.
        next_token: Token for pagination to retrieve the next set of results.

    Returns:
        A JSON object containing a list of files.
    """
    try:
        params = {}
        if parent_folder_ids:
            params["parent_folder_ids"] = parent_folder_ids
        if file_tag_definition_ids:
            params["file_tag_definition_ids"] = file_tag_definition_ids
        if updated_before:
            params["updated_before"] = updated_before
        if updated_after:
            params["updated_after"] = updated_after
        if next_token:
            params["next_token"] = next_token

        files = client.get_deal_files_by_id(deal_id, **params)
        return files
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


## Removed: /mcp/getPortfolioSummary (not part of lean API)


@app.get("/mcp/getAssets")
def get_assets_endpoint(
    property_type: Optional[str] = None, status: Optional[str] = None
):
    """
    Retrieves a list of assets, with optional filtering by property type and status.

    Args:
        property_type: Filter assets by property type.
        status: Filter assets by status.

    Returns:
        A JSON object containing a list of assets.
    """
    try:
        filters = {}
        if property_type:
            filters["propertyType"] = property_type
        if status:
            filters["status"] = status
        assets = client.get_assets(**filters)
        return assets
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldDefinitions")
def get_field_definitions_endpoint(
    page: Optional[int] = None, per_page: Optional[int] = None
):
    """
    Retrieves a list of field definitions, with optional pagination.

    Args:
        page: The page number for pagination.
        per_page: The number of items per page for pagination.

    Returns:
        A JSON object containing a list of field definitions.
    """
    try:
        params = {}
        if page:
            params["page"] = page
        if per_page:
            params["per_page"] = per_page
        field_definitions = client.get_field_definitions(**params)
        return field_definitions
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldsByDealId/{deal_id}")
def get_fields_by_deal_id_endpoint(deal_id: str):
    """
    Retrieves custom field data for a specific deal.

    Args:
        deal_id: The unique identifier for the deal.

    Returns:
        A JSON object containing the custom fields for the deal.
    """
    try:
        fields = client.get_fields_by_deal_id(deal_id)
        return fields
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldsByInvestmentId/{investment_id}")
def get_fields_by_investment_id_endpoint(investment_id: str):
    """
    Retrieves custom field data for a specific investment.

    Args:
        investment_id: The unique identifier for the investment.

    Returns:
        A JSON object containing the custom fields for the investment.
    """
    try:
        fields = client.get_fields_by_investment_id(investment_id)
        return fields
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldsByPropertyId/{property_id}")
def get_fields_by_property_id_endpoint(property_id: str):
    """
    Retrieves custom field data for a specific property.

    Args:
        property_id: The unique identifier for the property.

    Returns:
        A JSON object containing the custom fields for the property.
    """
    try:
        fields = client.get_fields_by_property_id(property_id)
        return fields
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldsByAssetId/{asset_id}")
def get_fields_by_asset_id_endpoint(asset_id: str):
    """
    Retrieves custom field data for a specific asset.

    Args:
        asset_id: The unique identifier for the asset.

    Returns:
        A JSON object containing the custom fields for the asset.
    """
    try:
        fields = client.get_fields_by_asset_id(asset_id)
        return fields
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldsByLoanId/{loan_id}")
def get_fields_by_loan_id_endpoint(loan_id: str):
    """
    Retrieves custom field data for a specific loan.

    Args:
        loan_id: The unique identifier for the loan.

    Returns:
        A JSON object containing the custom fields for the loan.
    """
    try:
        fields = client.get_fields_by_loan_id(loan_id)
        return fields
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldsByFieldDefinitionId/{field_definition_id}")
def get_fields_by_field_definition_id_endpoint(field_definition_id: str):
    """
    Retrieves all field values for a specific field definition.

    Args:
        field_definition_id: The unique identifier for the field definition.

    Returns:
        A JSON object containing a list of field values.
    """
    try:
        fields = client.get_fields_by_field_definition_id(field_definition_id)
        return fields
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getAssetFilesById/{asset_id}")
def get_asset_files_by_id_endpoint(asset_id: int):
    """
    Retrieves a list of files for a specific asset.

    Args:
        asset_id: The unique identifier for the asset.

    Returns:
        A JSON object containing a list of files.
    """
    try:
        files = client.get_asset_files_by_id(asset_id)
        return files
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFileById/{file_id}")
def get_file_by_id_endpoint(file_id: str):
    """
    Downloads a single file by its unique ID.

    Args:
        file_id: The unique identifier for the file.

    Returns:
        A file download response.
    """
    try:
        file_data = client.get_file_by_id(file_id)
        return Response(
            content=file_data["content"],
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename={file_data['filename']}"
            },
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFileTagDefinitions")
def get_file_tag_definitions_endpoint(
    page: Optional[int] = None, per_page: Optional[int] = None
):
    """
    Retrieves a list of file tag definitions, with optional pagination.

    Args:
        page: The page number for pagination.
        per_page: The number of items per page for pagination.

    Returns:
        A JSON object containing a list of file tag definitions.
    """
    try:
        params = {}
        if page:
            params["page"] = page
        if per_page:
            params["per_page"] = per_page
        tag_definitions = client.get_file_tag_definitions(**params)
        return tag_definitions
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFoldersByDealId/{deal_id}")
def get_folders_by_deal_id_endpoint(deal_id: int):
    """
    Retrieves a list of folders for a specific deal.

    Args:
        deal_id: The unique identifier for the deal.

    Returns:
        A JSON object containing a list of folders.
    """
    try:
        folders = client.get_folders_by_deal_id(deal_id)
        return folders
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFoldersByAssetId/{asset_id}")
def get_folders_by_asset_id_endpoint(
    asset_id: int,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves a list of folders for a specific asset.

    Args:
        asset_id: The unique identifier for the asset.

    Returns:
        A JSON object containing a list of folders.
    """
    try:
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        folders = dp_client.get_folders_by_asset_id(asset_id)
        return folders
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getInvestments")
def get_investments_endpoint(
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves a list of investments, with optional pagination.

    Args:
        page: The page number for pagination.
        per_page: The number of items per page for pagination.

    Returns:
        A JSON object containing a list of investments.
    """
    try:
        params = {}
        if page:
            params["page"] = page
        if per_page:
            params["per_page"] = per_page
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        investments = dp_client.get_investments(**params)
        return investments
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getListOptionsByFieldDefinitionId/{field_definition_id}")
def get_list_options_by_field_definition_id_endpoint(
    field_definition_id: str,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves the available options for a list-based custom field.

    Args:
        field_definition_id: The unique identifier for the field definition.

    Returns:
        A JSON object containing the list options.
    """
    try:
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        list_options = dp_client.get_list_options_by_field_definition_id(
            field_definition_id
        )
        return list_options
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getLoans")
def get_loans_endpoint(
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves a list of loans, with optional pagination.

    Args:
        page: The page number for pagination.
        per_page: The number of items per page for pagination.

    Returns:
        A JSON object containing a list of loans.
    """
    try:
        params = {}
        if page:
            params["page"] = page
        if per_page:
            params["per_page"] = per_page
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        loans = dp_client.get_loans(**params)
        return loans
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getPeople")
def get_people_endpoint(
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves a list of people, with optional pagination.

    Args:
        page: The page number for pagination.
        per_page: The number of items per page for pagination.

    Returns:
        A JSON object containing a list of people.
    """
    try:
        params = {}
        if page:
            params["page"] = page
        if per_page:
            params["per_page"] = per_page
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        people = dp_client.get_people(**params)
        return people
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getPropertyById/{property_id}")
def get_property_by_id_endpoint(
    property_id: str,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves a single property by its unique ID.

    Args:
        property_id: The unique identifier for the property.

    Returns:
        A JSON object representing the property.
    """
    try:
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        property_data = dp_client.get_property_by_id(property_id)
        return property_data
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getProperties")
def get_properties_endpoint(
    page: Optional[int] = None,
    per_page: Optional[int] = None,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves a list of properties, with optional pagination.

    Args:
        page: The page number for pagination.
        per_page: The number of items per page for pagination.

    Returns:
        A JSON object containing a list of properties.
    """
    try:
        params = {}
        if page:
            params["page"] = page
        if per_page:
            params["per_page"] = per_page
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        properties = dp_client.get_properties(**params)
        return properties
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getRolesByDealId/{deal_id}")
def get_roles_by_deal_id_endpoint(
    deal_id: str,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves the roles associated with a specific deal.

    Args:
        deal_id: The unique identifier for the deal.

    Returns:
        A JSON object containing a list of roles.
    """
    try:
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        roles = dp_client.get_roles_by_deal_id(deal_id)
        return roles
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/getRolesByAssetId/{asset_id}")
def get_roles_by_asset_id_endpoint(
    asset_id: str,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """
    Retrieves the roles associated with a specific asset.

    Args:
        asset_id: The unique identifier for the asset.

    Returns:
        A JSON object containing a list of roles.
    """
    try:
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        roles = dp_client.get_roles_by_asset_id(asset_id)
        return roles
    except Exception as e:
        raise HTTPException(
            status_code=500, detail={"code": "upstream_error", "message": str(e)}
        )


@app.get("/mcp/search")
def search_endpoint(
    query: str,
    x_dealpath_key: Optional[str] = Header(None, alias="X-Dealpath-Key"),
):
    """Proxy to Dealpath global search (lean mode)."""
    try:
        dp_client = DealpathClient(api_key=x_dealpath_key) if x_dealpath_key else client
        return dp_client.search(query=query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Health Check and Monitoring Endpoints (2025 standards) ---


@app.get("/health")
def health_check():
    """Health check endpoint for load balancers and monitoring systems."""
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "0.2.0",
        "protocol_version": SUPPORTED_PROTOCOL_VERSION,
    }


@app.get("/health/ready")
def readiness_check():
    """Readiness probe - checks if server can handle requests."""
    # In BYO-only mode there may be no default client; don't fail readiness for that.
    if client is None:
        return {
            "status": "ready",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {"dealpath_api": "skipped_no_default_key", "session_store": "ok"},
        }
    try:
        # Test Dealpath API connectivity with default key if present
        client.get_deals(limit=1)
        return {
            "status": "ready",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks": {"dealpath_api": "ok", "session_store": "ok"},
        }
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "checks": {"dealpath_api": "failed", "error": str(e)},
            },
        )


@app.get("/health/live")
def liveness_check():
    """Liveness probe - basic server responsiveness."""
    return {
        "status": "alive",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": (datetime.now(timezone.utc) - START_TIME).total_seconds(),
    }


@app.get("/metrics")
def metrics_endpoint():
    """Metrics endpoint.

    - If Prometheus client available → return text exposition format
    - Else → return JSON snapshot of internal counters (backward-compat)
    """
    if HAVE_PROM:
        try:
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
        except Exception:
            return Response(b"", media_type=CONTENT_TYPE_LATEST)

    # JSON fallback
    cleaned_sessions = cleanup_expired_sessions()
    by_name: dict[str, Any] = {}
    try:
        for k, v in TOOL_METRICS["by_name"].items():
            avg_latency_ms = (v["total_latency_ms"] / v["count"]) if v["count"] else None
            by_name[k] = {
                "calls": v["calls"],
                "errors": v["errors"],
                "avg_latency_ms": avg_latency_ms,
            }
    except Exception:
        by_name = {}
    return {
        "mcp_server": {
            "version": "0.2.0",
            "protocol_version": SUPPORTED_PROTOCOL_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "sessions": {
            "active_sessions": len(sessions),
            "cleaned_sessions": cleaned_sessions,
        },
        "system": {
            "file_storage_dir": FILE_STORAGE_DIR,
            "auth_enabled": MCP_TOKEN is not None,
        },
        "tools": {
            "calls_total": TOOL_METRICS.get("calls_total", 0),
            "errors_total": TOOL_METRICS.get("errors_total", 0),
            "by_name": by_name,
        },
    }


@app.get("/version")
def version_info():
    """Version and build information."""
    return {
        "name": "dealpath-mcp",
        "version": "0.2.0",
        "protocol_version": SUPPORTED_PROTOCOL_VERSION,
        "features": [
            "streamable_http_transport",
            "session_management",
            "enhanced_tool_schemas",
            "health_monitoring",
            "file_download_with_resource_links",
        ],
        "endpoints": {"mcp": "/mcp", "health": "/health", "metrics": "/metrics"},
    }
