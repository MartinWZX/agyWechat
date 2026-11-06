"""Deterministic Tool Registry with Execution Trace Audit Log."""
import os
import json
import time
import shutil
from typing import Callable, Dict, Any, Optional

class ToolRegistry:
    _instance = None
    _tools: Dict[str, Callable] = {}
    _trace_path = "tools/tool_execution_trace.jsonl"

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(ToolRegistry, cls).__new__(cls)
        return cls._instance

    @classmethod
    def register(cls, name: str):
        def decorator(func: Callable):
            cls._tools[name] = func
            return func
        return decorator

    @classmethod
    def set_trace_path(cls, path: str):
        cls._trace_path = path

    @classmethod
    def get_tool(cls, name: str) -> Optional[Callable]:
        return cls._tools.get(name)

    @classmethod
    def execute(cls, name: str, **kwargs) -> Any:
        func = cls._tools.get(name)
        if not func:
            raise ValueError(f"Tool {name} not found in registry")
        start_time = time.time()
        try:
            result = func(**kwargs)
            duration = time.time() - start_time
            cls._record_trace(name, kwargs, result, duration, success=True)
            return result
        except Exception as e:
            duration = time.time() - start_time
            cls._record_trace(name, kwargs, str(e), duration, success=False)
            raise

    @classmethod
    def _record_trace(cls, tool_name: str, args: Dict[str, Any], result: Any, duration: float, success: bool):
        entry = {
            "timestamp": time.time(),
            "tool_name": tool_name,
            "args": args,
            "success": success,
            "duration": round(duration, 4),
            "result_preview": str(result)[:300]
        }
        os.makedirs(os.path.dirname(cls._trace_path), exist_ok=True)
        with open(cls._trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @classmethod
    def get_trace_count(cls) -> int:
        if not os.path.exists(cls._trace_path):
            return 0
        with open(cls._trace_path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

# Standard library default tools
@ToolRegistry.register("read_file")
def tool_read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

@ToolRegistry.register("write_file")
def tool_write_file(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully wrote {len(content)} bytes to {path}"

@ToolRegistry.register("system_metrics")
def tool_system_metrics() -> Dict[str, Any]:
    load1, _, _ = os.getloadavg()
    total_b, used_b, _ = shutil.disk_usage("/")
    return {
        "load_1m": load1,
        "disk_used_percent": round((used_b / total_b) * 100, 2)
    }
