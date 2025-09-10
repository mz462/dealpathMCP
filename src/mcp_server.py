from typing import Optional, List, Any, Dict, Callable, Union
from fastapi import FastAPI, HTTPException, Query, Response, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from collections import Counter
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import logging
import base64
import requests
import pathlib
import re

from .dealpath_client import DealpathClient

# --- App & Security ---------------------------------------------------------

load_dotenv()

logger = logging.getLogger(__name__)

app = FastAPI(title="Dealpath MCP Server (Streamable HTTP)")
client = DealpathClient()

MCP_TOKEN = os.getenv("mcp_token")
ALLOWED_ORIGINS = {o.strip() for o in (os.getenv("allowed_origins", "http://127.0.0.1,http://localhost").split(",")) if o.strip()}
SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
FILE_STORAGE_DIR = os.getenv("file_storage_dir", os.path.join(os.getcwd(), "local_files"))

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
                    content={"error": {"code": "forbidden_origin", "message": "Origin not allowed."}},
                )

            authz = request.headers.get("authorization", "")
            scheme, _, token = authz.partition(" ")
            if scheme.lower() != "bearer" or not token or token != MCP_TOKEN:
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"error": {"code": "unauthorized", "message": "Missing or invalid bearer token."}},
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

Json = Dict[str, Any]

def mcp_response_ok(req_id: Any, result: Any) -> Json:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def mcp_response_error(req_id: Any, code: int, message: str, data: Any = None) -> Json:
    err: Json = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def build_tools_list() -> Dict[str, Any]:
    """Declare available tools with minimal schemas for MCP tools/list."""
    return {
        "tools": [
            {
                "name": "get_deals",
                "title": "List Deals",
                "description": "Return deals with optional filters: status, propertyType.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "propertyType": {"type": "string"}
                    },
                },
            },
            {
                "name": "get_deal",
                "title": "Get Deal",
                "description": "Return a single deal by ID.",
                "inputSchema": {
                    "type": "object",
                    "required": ["deal_id"],
                    "properties": {"deal_id": {"type": "string"}},
                },
            },
            {
                "name": "get_deal_files",
                "title": "Deal Files",
                "description": "List files for a deal with optional filters.",
                "inputSchema": {
                    "type": "object",
                    "required": ["deal_id"],
                    "properties": {
                        "deal_id": {"type": "integer"},
                        "parent_folder_ids": {"type": "array", "items": {"type": "integer"}},
                        "file_tag_definition_ids": {"type": "array", "items": {"type": "integer"}},
                        "updated_before": {"type": "integer"},
                        "updated_after": {"type": "integer"},
                        "next_token": {"type": "string"}
                    },
                },
            },
            {
                "name": "get_portfolio_summary",
                "title": "Portfolio Summary",
                "description": "Summarize recent deals by status and property type (last 2 weeks).",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "search",
                "title": "Search",
                "description": "Global Dealpath search.",
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            },
            {
                "name": "get_file_by_id",
                "title": "Get File",
                "description": "Download the file to this server and return a local link.",
                "inputSchema": {
                    "type": "object",
                    "required": ["file_id"],
                    "properties": {"file_id": {"type": "string"}},
                },
            },
        ]
    }


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
    date_str = datetime.utcnow().strftime("%Y%m%d")
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


def tool_call_dispatch(name: str, arguments: Dict[str, Any], *, base_url: Optional[str] = None) -> Any:
    if name == "get_deals":
        status_val = arguments.get("status")
        property_type = arguments.get("propertyType")
        filters = {}
        if status_val:
            filters["status"] = status_val
        if property_type:
            filters["propertyType"] = property_type
        return client.get_deals(**filters)

    if name == "get_deal":
        deal_id = arguments.get("deal_id")
        if not deal_id:
            raise HTTPException(status_code=400, detail="deal_id is required")
        return client.get_deal_by_id(deal_id)

    if name == "get_deal_files":
        deal_id = arguments.get("deal_id")
        if deal_id is None:
            raise HTTPException(status_code=400, detail="deal_id is required")
        params = {k: v for k, v in arguments.items() if k != "deal_id" and v is not None}
        return client.get_deal_files_by_id(deal_id, **params)

    if name == "get_portfolio_summary":
        response = client.get_deals()
        deal_list = response.get("deals", {}).get("data", [])
        two_weeks_ago = datetime.utcnow() - timedelta(weeks=2)
        recent_deals: List[Dict[str, Any]] = []
        for deal in deal_list:
            last_updated_str = deal.get("last_updated")
            if last_updated_str:
                dt = datetime.fromisoformat(last_updated_str.replace("Z", ""))
                if dt > two_weeks_ago:
                    recent_deals.append(deal)

        if not recent_deals:
            return {"totalDeals": 0, "dealsByStatus": {}, "dealsByPropertyType": {}}

        total_deals = len(recent_deals)
        status_counts = Counter(d.get("deal_state") for d in recent_deals)
        property_type_counts = Counter(d.get("deal_type") for d in recent_deals)
        return {
            "totalDeals": total_deals,
            "dealsByStatus": dict(status_counts),
            "dealsByPropertyType": dict(property_type_counts),
        }

    if name == "search":
        query = arguments.get("query")
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        return client.search(query=query)

    if name == "get_file_by_id":
        file_id = arguments.get("file_id")
        if not file_id:
            raise HTTPException(status_code=400, detail="file_id is required")
        parts: List[Dict[str, Any]] = []
        
        # Prefer signed URL; include remote link and also save locally
        try:
            info = client.get_file_download_url(file_id)
            url = info.get("url")
            filename = info.get("filename") or str(file_id)
            if url:
                try:
                    r = requests.get(url, stream=True)
                    r.raise_for_status()
                    rel = _store_stream_locally(file_id, filename, r)
                    local_uri = _absolute_local_url(base_url or "http://127.0.0.1:8000", rel)
                    # Build a summary text part to ensure both links are visible in UIs
                    summary = (
                        f"Links for file '{filename}' (id {file_id}):\n"
                        f"- Local: {local_uri}\n"
                        f"- Remote (expires): {url}"
                    )
                    parts.append({"type": "text", "text": summary})
                    # Add local first, remote last (some clients display only the last part)
                    parts.append({"type": "resource_link", "name": filename, "uri": local_uri})
                    parts.append({"type": "resource_link", "name": filename, "uri": url})
                    return {"__content__": parts}
                except Exception:
                    # If local save fails, still return the remote link (as last part)
                    parts.append({"type": "resource_link", "name": filename, "uri": url})
                    return {"__content__": parts}
        except Exception:
            # proceed to direct download fallback
            pass

        # Fallback: download via files.dealpath.com with Authorization and store locally only
        try:
            data = client.download_file_content(file_id)
            filename = data.get("filename", str(file_id))
            rel = _store_bytes_locally(file_id, filename, data["content"])
            local_uri = _absolute_local_url(base_url or "http://127.0.0.1:8000", rel)
            summary = (
                f"Links for file '{filename}' (id {file_id}):\n"
                f"- Local: {local_uri}"
            )
            parts.append({"type": "text", "text": summary})
            parts.append({"type": "resource_link", "name": filename, "uri": local_uri})
            return {"__content__": parts}
        except requests.HTTPError as http_err:
            resp = http_err.response
            raise HTTPException(status_code=resp.status_code, detail=f"Dealpath error: {resp.text}")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Failed to fetch file: {e}")

    raise HTTPException(status_code=404, detail=f"Unknown tool: {name}")


def to_content_parts(value: Any) -> List[Dict[str, Any]]:
    """Convert a Python value to MCP content parts array.

    For maximum client compatibility, return a single `text` part.
    - dict/list → pretty-printed JSON string
    - str → as-is
    - other scalars → stringified
    """
    import json

    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)

    return [{"type": "text", "text": text}]


@app.post("/mcp")
async def mcp_http_endpoint(request: Request, payload: Union[Dict[str, Any], List[Dict[str, Any]]]):
    """Single HTTP endpoint implementing minimal MCP request handling.

    Supported methods:
      - initialize
      - tools/list
      - tools/call

    This intentionally omits OAuth; a static bearer token is required.
    """

    base_url = str(request.base_url).rstrip("/")

    def handle_one(req: Dict[str, Any]) -> Dict[str, Any]:
        req_id = req.get("id")
        method = req.get("method") or req.get("type")  # tolerate `type` alias
        params: Dict[str, Any] = req.get("params") or {}

        if not method:
            return mcp_response_error(req_id, -32600, "Missing method")

        try:
            if method == "initialize":
                result = {
                    "protocolVersion": SUPPORTED_PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                    "serverInfo": {
                        "name": "dealpath-mcp",
                        "version": "0.1.0",
                    },
                }
                return mcp_response_ok(req_id, result)

            if method in ("tools/list", "tools.list"):
                return mcp_response_ok(req_id, build_tools_list())

            if method in ("tools/call", "tools.call"):
                name = params.get("name")
                arguments = params.get("arguments") or {}
                if not name:
                    return mcp_response_error(req_id, -32602, "Missing tool name")
                result = tool_call_dispatch(name, arguments, base_url=base_url)
                if isinstance(result, dict) and "__content__" in result:
                    parts = result["__content__"]
                else:
                    parts = to_content_parts(result)
                return mcp_response_ok(req_id, {"content": parts})

            if method == "ping":
                return mcp_response_ok(req_id, {"ok": True})

            return mcp_response_error(req_id, -32601, f"Method not found: {method}")

        except HTTPException as http_exc:
            return mcp_response_error(req_id, http_exc.status_code, http_exc.detail)
        except Exception as e:
            logger.exception("Unhandled MCP error")
            return mcp_response_error(req_id, 500, "Internal error", {"message": str(e)})

    if isinstance(payload, list):
        return JSONResponse([handle_one(p) for p in payload])
    return JSONResponse(handle_one(payload))


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


@app.get("/local-files/{date}/{file_id}/{filename}")
async def serve_local_file(date: str, file_id: str, filename: str):
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
    return FileResponse(path)

@app.get("/mcp/getDeals")
def get_deals_endpoint(status: Optional[str] = None, propertyType: Optional[str] = None):
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
            filters['status'] = status
        if propertyType:
            filters['propertyType'] = propertyType

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
    parent_folder_ids: Optional[List[int]] = Query(None),
    file_tag_definition_ids: Optional[List[int]] = Query(None),
    updated_before: Optional[int] = Query(None),
    updated_after: Optional[int] = Query(None),
    next_token: Optional[str] = Query(None)
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
            params['parent_folder_ids'] = parent_folder_ids
        if file_tag_definition_ids:
            params['file_tag_definition_ids'] = file_tag_definition_ids
        if updated_before:
            params['updated_before'] = updated_before
        if updated_after:
            params['updated_after'] = updated_after
        if next_token:
            params['next_token'] = next_token

        files = client.get_deal_files_by_id(deal_id, **params)
        return files
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getPortfolioSummary")
def get_portfolio_summary_endpoint():
    """
    Provides a summary of the deal portfolio, including total deals and counts by status and property type for deals updated in the last two weeks.

    Returns:
        A JSON object with portfolio summary.
    """
    try:
        response = client.get_deals()
        deal_list = response.get('deals', {}).get('data', [])

        # Filter for deals updated in the last two weeks
        two_weeks_ago = datetime.utcnow() - timedelta(weeks=2)
        recent_deals = []
        for deal in deal_list:
            last_updated_str = deal.get('last_updated')
            if last_updated_str:
                # Parse the date, assuming UTC (Z suffix)
                last_updated_date = datetime.fromisoformat(last_updated_str.replace('Z', ''))
                if last_updated_date > two_weeks_ago:
                    recent_deals.append(deal)
        
        deal_list = recent_deals # Continue with the filtered list

        if not deal_list:
            return {
                "totalDeals": 0,
                "dealsByStatus": {},
                "dealsByPropertyType": {}
            }

        total_deals = len(deal_list)

        # Use 'deal_state' for status and 'deal_type' for property type
        status_counts = Counter(d.get('deal_state') for d in deal_list)
        property_type_counts = Counter(d.get('deal_type') for d in deal_list)

        return {
            "totalDeals": total_deals,
            "dealsByStatus": dict(status_counts),
            "dealsByPropertyType": dict(property_type_counts)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getAssets")
def get_assets_endpoint(property_type: Optional[str] = None, status: Optional[str] = None):
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
            filters['propertyType'] = property_type
        if status:
            filters['status'] = status
        assets = client.get_assets(**filters)
        return assets
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFieldDefinitions")
def get_field_definitions_endpoint(page: Optional[int] = None, per_page: Optional[int] = None):
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
            params['page'] = page
        if per_page:
            params['per_page'] = per_page
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
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getFileTagDefinitions")
def get_file_tag_definitions_endpoint(page: Optional[int] = None, per_page: Optional[int] = None):
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
            params['page'] = page
        if per_page:
            params['per_page'] = per_page
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
def get_folders_by_asset_id_endpoint(asset_id: int):
    """
    Retrieves a list of folders for a specific asset.

    Args:
        asset_id: The unique identifier for the asset.

    Returns:
        A JSON object containing a list of folders.
    """
    try:
        folders = client.get_folders_by_asset_id(asset_id)
        return folders
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getInvestments")
def get_investments_endpoint(page: Optional[int] = None, per_page: Optional[int] = None):
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
            params['page'] = page
        if per_page:
            params['per_page'] = per_page
        investments = client.get_investments(**params)
        return investments
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getListOptionsByFieldDefinitionId/{field_definition_id}")
def get_list_options_by_field_definition_id_endpoint(field_definition_id: str):
    """
    Retrieves the available options for a list-based custom field.

    Args:
        field_definition_id: The unique identifier for the field definition.

    Returns:
        A JSON object containing the list options.
    """
    try:
        list_options = client.get_list_options_by_field_definition_id(field_definition_id)
        return list_options
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getLoans")
def get_loans_endpoint(page: Optional[int] = None, per_page: Optional[int] = None):
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
            params['page'] = page
        if per_page:
            params['per_page'] = per_page
        loans = client.get_loans(**params)
        return loans
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getPeople")
def get_people_endpoint(page: Optional[int] = None, per_page: Optional[int] = None):
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
            params['page'] = page
        if per_page:
            params['per_page'] = per_page
        people = client.get_people(**params)
        return people
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getPropertyById/{property_id}")
def get_property_by_id_endpoint(property_id: str):
    """
    Retrieves a single property by its unique ID.

    Args:
        property_id: The unique identifier for the property.

    Returns:
        A JSON object representing the property.
    """
    try:
        property_data = client.get_property_by_id(property_id)
        return property_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getProperties")
def get_properties_endpoint(page: Optional[int] = None, per_page: Optional[int] = None):
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
            params['page'] = page
        if per_page:
            params['per_page'] = per_page
        properties = client.get_properties(**params)
        return properties
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getRolesByDealId/{deal_id}")
def get_roles_by_deal_id_endpoint(deal_id: str):
    """
    Retrieves the roles associated with a specific deal.

    Args:
        deal_id: The unique identifier for the deal.

    Returns:
        A JSON object containing a list of roles.
    """
    try:
        roles = client.get_roles_by_deal_id(deal_id)
        return roles
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/getRolesByAssetId/{asset_id}")
def get_roles_by_asset_id_endpoint(asset_id: str):
    """
    Retrieves the roles associated with a specific asset.

    Args:
        asset_id: The unique identifier for the asset.

    Returns:
        A JSON object containing a list of roles.
    """
    try:
        roles = client.get_roles_by_asset_id(asset_id)
        return roles
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/mcp/search")
def search_endpoint(query: str):
    """
    Performs a global search across the Dealpath environment.

    Args:
        query: The search term.

    Returns:
        A JSON object containing the search results.
    """
    try:
        search_results = client.search(query=query)
        return search_results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
