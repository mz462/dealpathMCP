import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml


def _repo_root_from(file: str) -> Path:
    p = Path(file).resolve()
    for parent in [p] + list(p.parents):
        # Heuristic: stop at folder containing pyproject.toml or .git
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return p.parent


def _jsonschema_from_params(params: List[dict]) -> dict:
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for param in params or []:
        name = param.get("name")
        if not name:
            continue
        schema = param.get("schema") or {}
        # Copy basic fields from OpenAPI schema → JSON Schema subset
        prop: Dict[str, Any] = {}
        for key in ("type", "format", "enum", "minimum", "maximum", "minLength", "maxLength", "items", "pattern", "description", "default"):
            if key in schema:
                prop[key] = schema[key]
        # Fallback description
        if "description" not in prop and param.get("description"):
            prop["description"] = param.get("description")
        properties[name] = prop or {"type": "string"}
        if param.get("required"):
            required.append(name)
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        input_schema["required"] = required
    return input_schema


def build_tools_from_openapi(spec: dict, selection: Dict[str, Tuple[str, str]]) -> List[dict]:
    paths = spec.get("paths", {})
    tools: List[dict] = []
    for tool_name, (method, path) in selection.items():
        p_obj = paths.get(path)
        if not isinstance(p_obj, dict):
            continue
        op = p_obj.get(method.lower())
        if not isinstance(op, dict):
            continue
        # Merge path-level and operation-level parameters
        params: List[dict] = []
        params.extend(p_obj.get("parameters", []) or [])
        params.extend(op.get("parameters", []) or [])
        input_schema = _jsonschema_from_params(params)
        title = op.get("summary") or tool_name.replace("_", " ").title()
        description = op.get("description") or f"Proxy for {method.upper()} {path}"
        tools.append(
            {
                "name": tool_name,
                "title": title,
                "description": description,
                "inputSchema": input_schema,
            }
        )
    return tools


def _to_snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (name[i - 1].islower() or (i + 1 < len(name) and name[i + 1].islower())):
            out.append("_")
        out.append(ch.lower())
    return "".join(out).replace(" ", "_")


def load_get_operations(yaml_filename: str = "dealpath_v1.yaml") -> List[dict]:
    """Return all GET operations from the OpenAPI spec with metadata.

    Each item: {
      name: str (snake_case operationId),
      method: 'get',
      path: '/...',{\n}
      parameters: [...],
      title: summary,
      description: description
    }
    """
    root = _repo_root_from(__file__)
    spec_path = root / yaml_filename
    if not spec_path.exists():
        raise FileNotFoundError(f"OpenAPI spec not found: {spec_path}")
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    out: List[dict] = []
    for path, p_obj in paths.items():
        if not isinstance(p_obj, dict):
            continue
        op = p_obj.get("get")
        if not isinstance(op, dict):
            continue
        op_id = op.get("operationId") or f"get_{path.strip('/').replace('/', '_') or 'root'}"
        name = _to_snake(op_id)
        params = []
        params.extend(p_obj.get("parameters", []) or [])
        params.extend(op.get("parameters", []) or [])
        out.append(
            {
                "name": name,
                "method": "get",
                "path": path,
                "parameters": params,
                "title": op.get("summary") or name.replace("_", " ").title(),
                "description": op.get("description") or f"Proxy for GET {path}",
            }
        )
    return out


def load_dealpath_tools_from_yaml(selection: Dict[str, Tuple[str, str]], yaml_filename: str = "dealpath_v1.yaml") -> List[dict]:
    # Locate YAML near repo root
    root = _repo_root_from(__file__)
    spec_path = root / yaml_filename
    if not spec_path.exists():
        raise FileNotFoundError(f"OpenAPI spec not found: {spec_path}")
    with spec_path.open("r", encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    return build_tools_from_openapi(spec, selection)
