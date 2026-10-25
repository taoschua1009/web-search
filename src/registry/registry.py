"""
Tool Registry - 6-step pipeline:
1. Validate Schema
2. Check Authentication
3. Check Scopes
4. Check Rate Limit
5. Audit Log
6. Execute Tool
"""

import time
import json
from datetime import datetime
from typing import Any
from opentelemetry import trace
from .models import ToolDefinition
from src.observability import telemetry  # noqa: F401
from src.observability.telemetry import tracer, tool_call_counter, tool_error_counter, tool_latency_ms


# ============================================================
# Step 1: VALIDATE SCHEMA
# ============================================================

def validate_schema(tool: ToolDefinition, arguments: dict) -> dict:
    schema = tool.input_schema
    required_fields = schema.get("required", [])
    properties = schema.get("properties", {})

    for field in required_fields:
        if field not in arguments:
            raise ValueError(f"[SCHEMA] Missing required field: '{field}'")

    type_map = {"string": str, "integer": int, "number": (int, float), "boolean": bool}
    for key, value in arguments.items():
        if key in properties:
            expected_type = properties[key].get("type")
            if expected_type and expected_type in type_map:
                if not isinstance(value, type_map[expected_type]):
                    raise ValueError(
                        f"[SCHEMA] Field '{key}' expected {expected_type}, got {type(value).__name__}"
                    )

    for key, value in arguments.items():
        if key in properties:
            enum_values = properties[key].get("enum")
            if enum_values and value not in enum_values:
                raise ValueError(
                    f"[SCHEMA] Field '{key}' must be one of {enum_values}, got '{value}'"
                )

    return arguments


# ============================================================
# Step 2: CHECK AUTHENTICATION
# ============================================================

USER_DB: dict[str, dict] = {
    "sk-admin-001": {
        "user_id": "user_admin",
        "role": "admin",
        "scopes": [
            "drive:read", "calendar:read", "gmail:read",
            "memory:read", "memory:write",
        ],
    },
    "sk-user-002": {
        "user_id": "user_standard",
        "role": "user",
        "scopes": [
            "drive:read", "calendar:read", "gmail:read",
            "memory:read", "memory:write",
        ],
    },
    "sk-guest-003": {
        "user_id": "user_guest",
        "role": "guest",
        "scopes": ["drive:read", "memory:read"],
    },
}


def check_authentication(api_key: str) -> dict:
    if not api_key:
        raise PermissionError("[AUTH] No API key provided")

    user = USER_DB.get(api_key)
    if not user:
        raise PermissionError(f"[AUTH] Invalid API key: '{api_key[:8]}...'")

    print(f"  [Step 2] Auth OK - user={user['user_id']}, role={user['role']}")
    return user


# ============================================================
# Step 3: CHECK SCOPES
# ============================================================

def check_scopes(user: dict, tool: ToolDefinition) -> bool:
    user_scopes = set(user.get("scopes", []))
    required = set(tool.required_scopes)
    missing = required - user_scopes

    if missing:
        raise PermissionError(
            f"[SCOPES] User '{user['user_id']}' missing scopes: {missing}. "
            f"Has: {user_scopes}"
        )

    print(f"  [Step 3] Scopes OK - required={required}")
    return True


# ============================================================
# Step 4: RATE LIMIT
# ============================================================

class RateLimiter:
    def __init__(self, max_calls: int = 10, window_seconds: int = 60):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def check(self, user_id: str) -> bool:
        now = time.time()
        if user_id not in self._buckets:
            self._buckets[user_id] = []

        self._buckets[user_id] = [
            t for t in self._buckets[user_id]
            if now - t < self.window_seconds
        ]

        if len(self._buckets[user_id]) >= self.max_calls:
            remaining = self.window_seconds - (now - self._buckets[user_id][0])
            raise RuntimeError(
                f"[RATE LIMIT] User '{user_id}' exceeded {self.max_calls} calls "
                f"per {self.window_seconds}s. Retry in {remaining:.0f}s"
            )

        self._buckets[user_id].append(now)
        remaining = self.max_calls - len(self._buckets[user_id])
        print(f"  [Step 4] Rate Limit OK - {remaining}/{self.max_calls} remaining")
        return True


rate_limiter = RateLimiter(max_calls=20, window_seconds=60)


# ============================================================
# Step 5: AUDIT LOG
# ============================================================

AUDIT_LOG: list[dict] = []


def audit_log(user: dict, tool_name: str, arguments: dict, result: Any = None, error: str = None):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user["user_id"],
        "role": user["role"],
        "tool": tool_name,
        "arguments": arguments,
        "result": str(result)[:200] if result else None,
        "error": error,
        "status": "success" if not error else "failed",
    }
    AUDIT_LOG.append(entry)
    status = "OK" if not error else f"FAILED: {error[:80]}"
    print(f"  [Step 5] Audit Log - {status}")
    return entry


# ============================================================
# Step 6: EXECUTE TOOL
# ============================================================

def execute_tool(tool: ToolDefinition, arguments: dict) -> Any:
    result = tool.handler(**arguments)
    print(f"  [Step 6] Execute OK - result: {json.dumps(result, ensure_ascii=False)[:100]}")
    return result


# ============================================================
# TOOL REGISTRY
# ============================================================

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._audit_log: list[dict] = []

    def register(self, tool: ToolDefinition):
        self._tools[tool.name] = tool
        # Pre-create counter series at 0 so Prometheus rate()/increase() sees the first real call
        for status in ("success", "error"):
            tool_call_counter.add(0, {"tool": tool.name, "status": status})
        tool_error_counter.add(0, {"tool": tool.name})
        print(f"[Registry] Registered tool: '{tool.name}'")

    def get_tool(self, name: str) -> ToolDefinition:
        tool = self._tools.get(name)
        if not tool:
            raise KeyError(f"[Registry] Tool '{name}' not found. Available: {list(self._tools.keys())}")
        return tool

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

    def call(self, tool_name: str, arguments: dict, api_key: str) -> dict:
        print(f"\n{'='*60}")
        print(f"  TOOL CALL: {tool_name}")
        print(f"  Arguments: {json.dumps(arguments, ensure_ascii=False)}")
        print(f"{'='*60}")

        user = None
        t0 = time.perf_counter()

        with tracer.start_as_current_span("tool.pipeline") as span:
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.api_key_prefix", api_key[:8])

            try:
                # Step 1: Validate Schema
                with tracer.start_as_current_span("step.validate_schema"):
                    tool = self.get_tool(tool_name)
                    validated_args = validate_schema(tool, arguments)
                    print(f"  [Step 1] Schema Valid OK")

                # Step 2: Check Authentication
                with tracer.start_as_current_span("step.auth"):
                    user = check_authentication(api_key)
                    span.set_attribute("tool.user_id", user["user_id"])
                    span.set_attribute("tool.role", user["role"])

                # Step 3: Check Scopes
                with tracer.start_as_current_span("step.scopes"):
                    check_scopes(user, tool)

                # Step 4: Check Rate Limit
                with tracer.start_as_current_span("step.rate_limit"):
                    rate_limiter.check(user["user_id"])

                # Step 5 + 6: Execute & Audit
                with tracer.start_as_current_span("step.execute"):
                    result = execute_tool(tool, validated_args)
                    self._audit_log.append(audit_log(user, tool_name, arguments, result=result))

                elapsed_ms = (time.perf_counter() - t0) * 1000
                span.set_attribute("tool.status", "success")
                span.set_attribute("tool.latency_ms", round(elapsed_ms))

                attrs = {"tool": tool_name, "status": "success"}
                tool_call_counter.add(1, attrs)
                tool_latency_ms.record(elapsed_ms, attrs)

                print(f"{'='*60}\n")
                return {"result": result}

            except Exception as e:
                error_msg = str(e)
                elapsed_ms = (time.perf_counter() - t0) * 1000

                span.set_attribute("tool.status", "error")
                span.set_attribute("tool.error", error_msg[:200])
                span.record_exception(e)
                span.set_status(trace.StatusCode.ERROR, error_msg)

                attrs = {"tool": tool_name, "status": "error"}
                tool_call_counter.add(1, attrs)
                tool_error_counter.add(1, {"tool": tool_name})
                tool_latency_ms.record(elapsed_ms, attrs)

                if user:
                    self._audit_log.append(audit_log(user, tool_name, arguments, error=error_msg))
                print(f"  [ERROR] {error_msg}")
                print(f"{'='*60}\n")
                return {"error": error_msg}

    def get_audit_log(self) -> list[dict]:
        return self._audit_log
