"""Model Context Protocol (MCP) JSON-RPC 2.0 Stdio Server for ToolRegistry.

Exposes deterministic tools registered in `ToolRegistry` (including RSI-evolved
persistent tools) over standard MCP stdio transport so native CLI agents
(`agy`, `claude`, `pi`, `omp`) can invoke tools natively without fragile regex tags.
"""
import inspect
import json
import sys
from typing import Any, Callable, Dict, List, Optional

from tools.tool_registry import ToolRegistry

MCP_PROTOCOL_VERSION = "2024-11-05"


def _python_type_to_json_type(annotation: Any) -> str:
    if annotation in (int,):
        return "integer"
    if annotation in (float,):
        return "number"
    if annotation in (bool,):
        return "boolean"
    if annotation in (list, List) or getattr(annotation, "__origin__", None) is list:
        return "array"
    if annotation in (dict, Dict) or getattr(annotation, "__origin__", None) is dict:
        return "object"
    return "string"


def build_tool_schema(name: str, func: Callable) -> Dict[str, Any]:
    """Inspect a Python tool function and build an MCP-compliant JSON Schema descriptor."""
    properties: Dict[str, Any] = {}
    required: List[str] = []
    try:
        sig = inspect.signature(func)
        for param_name, param in sig.parameters.items():
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            jtype = _python_type_to_json_type(param.annotation)
            prop: Dict[str, Any] = {"type": jtype}
            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            else:
                prop["default"] = param.default
            properties[param_name] = prop
    except (TypeError, ValueError):
        properties = {}

    return {
        "name": name,
        "description": (func.__doc__ or f"Deterministic tool {name}").strip(),
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


class MCPToolServer:
    """Stateless JSON-RPC 2.0 request handler implementing MCP tools capability."""

    @staticmethod
    def handle_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params") or {}

        if method.startswith("notifications/"):
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "agywechat-deterministic-tools", "version": "3.0.0"},
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            ToolRegistry.load_persisted_tools()
            tools_list = []
            for tname in ToolRegistry.list_tools():
                fn = ToolRegistry.get_tool(tname)
                if fn is not None:
                    tools_list.append(build_tool_schema(tname, fn))
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Invalid tool arguments: expected object"},
                }
            try:
                output = ToolRegistry.execute(tool_name, **arguments)
                text = json.dumps(output, ensure_ascii=False) if not isinstance(output, str) else output
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Tool execution error: {exc}"}],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def run_stdio_server() -> None:
    """Serve MCP requests line-by-line over stdin/stdout."""
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            resp: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            resp = MCPToolServer.handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run_stdio_server()
