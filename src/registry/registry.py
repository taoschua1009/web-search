"""
Tool Registry - 6-step pipeline:
1. Validate Schema
2. Check Authentication
3. Check Scopes
4. Check Rate Limit
5. Audit Log
6. Execute Tool
"""

import json
import time
from collections import deque
from datetime import datetime
from typing import Any
from opentelemetry import trace
from .guardrails import fence, redact
from .models import ToolDefinition
from src.observability import telemetry  # noqa: F401
from src.observability.telemetry import tracer, tool_call_counter, tool_error_counter, tool_latency_ms


# ============================================================
# Step 1: VALIDATE SCHEMA
# ============================================================

TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    # bool is an int subclass in Python: it must not satisfy integer/number.
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _check(value: Any, schema: dict, path: str) -> None:
    """Enforce the JSON-Schema subset the tool manifests use; raises ValueError."""
    expected = schema.get("type")
    if expected in TYPE_CHECKS and not TYPE_CHECKS[expected](value):
        raise ValueError(f"[SCHEMA] Field '{path}' expected {expected}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"[SCHEMA] Field '{path}' must be one of {schema['enum']}, got '{value}'")
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", len(value)):
            raise ValueError(f"[SCHEMA] Field '{path}' is longer than {schema['maxLength']} characters")
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"[SCHEMA] Field '{path}' is shorter than {schema['minLength']} characters")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"[SCHEMA] Field '{path}' must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"[SCHEMA] Field '{path}' must be at most {schema['maximum']}")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"[SCHEMA] Field '{path}' has more than {schema['maxItems']} items")
        for index, item in enumerate(value):
            if "items" in schema:
                _check(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for field in schema.get("required", []):
            if field not in value:
                raise ValueError(f"[SCHEMA] Missing required field: '{path}.{field}'")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise ValueError(f"[SCHEMA] Unknown field(s) not allowed: {unknown}")
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], f"{path}.{key}")


def validate_schema(tool: ToolDefinition, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("[SCHEMA] Tool arguments must be an object")
    for field in tool.input_schema.get("required", []):
        if field not in arguments:
            raise ValueError(f"[SCHEMA] Missing required field: '{field}'")
    _check(arguments, {**tool.input_schema, "required": []}, "arguments")
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


def enforce_risk_policy(tool: ToolDefinition) -> None:
    """Fail closed: a high-risk (or unclassified) tool never runs from a model request.

    Such actions must go through a human approval flow that is bound to the exact
    action (see HumanApprovalAgent); the tool-call path has no approval to present.
    """
    if tool.risk == "high":
        raise PermissionError(
            f"[POLICY] Tool '{tool.name}' is high-risk or unclassified and needs human approval."
        )
    print(f"  [Step 3b] Risk policy OK - risk={tool.risk}")


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

MAX_AUDIT_ENTRIES = 2000
AUDIT_LOG: deque[dict] = deque(maxlen=MAX_AUDIT_ENTRIES)


def audit_log(user: dict, tool_name: str, arguments: dict, result: Any = None, error: str = None, extra: dict | None = None):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "user_id": user["user_id"],
        "role": user["role"],
        "tool": tool_name,
        # Logs hold masked arguments only: secrets, addresses and long text never land here.
        "arguments": redact(arguments),
        "result": str(redact(result))[:200] if result else None,
        "error": redact(error) if error else None,
        "status": "success" if not error else "failed",
        **(extra or {}),
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
        self._audit_log: deque[dict] = deque(maxlen=MAX_AUDIT_ENTRIES)
        self._audit_seq = 0

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

    def call(self, tool_name: str, arguments: dict, api_key: str, principal: str | None = None) -> dict:
        """Run one tool call through the pipeline.

        ``principal`` identifies the signed-in person so rate limits and the audit trail
        are per person even though sessions share a service API key.
        """
        print(f"\n{'='*60}")
        print(f"  TOOL CALL: {tool_name}")
        print(f"  Arguments: {json.dumps(redact(arguments), ensure_ascii=False)}")
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
                    print("  [Step 1] Schema Valid OK")

                # Step 2: Check Authentication
                with tracer.start_as_current_span("step.auth"):
                    user = check_authentication(api_key)
                    span.set_attribute("tool.user_id", user["user_id"])
                    span.set_attribute("tool.role", user["role"])

                # Step 3: Check Scopes
                with tracer.start_as_current_span("step.scopes"):
                    check_scopes(user, tool)
                    enforce_risk_policy(tool)

                # Step 4: Check Rate Limit
                with tracer.start_as_current_span("step.rate_limit"):
                    rate_limiter.check(f"{user['user_id']}:{principal}" if principal else user["user_id"])

                # Step 5 + 6: Execute & Audit
                with tracer.start_as_current_span("step.execute"):
                    result = execute_tool(tool, validated_args)
                    extra = {"risk": tool.risk, "principal": principal}
                    if tool.untrusted_output:
                        # External content reaches the model only as labelled data.
                        result, flags = fence(tool.name, result)
                        extra["security_flags"] = flags
                        if flags:
                            span.set_attribute("tool.injection_flags", ",".join(flags))
                            print(f"  [Guard] Injection-style content in result: {flags}")
                    self._record(audit_log(user, tool_name, arguments, result=result, extra=extra))

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
                    self._record(audit_log(user, tool_name, arguments, error=error_msg, extra={"principal": principal}))
                print(f"  [ERROR] {error_msg}")
                print(f"{'='*60}\n")
                return {"error": error_msg}

    def _record(self, entry: dict) -> None:
        self._audit_seq += 1
        entry["seq"] = self._audit_seq
        self._audit_log.append(entry)

    def risk_of(self, tool_name: str) -> str:
        """Risk tier of a tool; anything unknown counts as high (fail closed)."""
        tool = self._tools.get(tool_name)
        return tool.risk if tool else "high"

    def audit_mark(self) -> int:
        """A position in the audit trail; pass it to ``audit_since`` later."""
        return self._audit_seq

    def audit_since(self, mark: int) -> list[dict]:
        """Entries recorded after ``mark``, robust to the bounded log dropping old ones."""
        return [entry for entry in self._audit_log if entry["seq"] > mark]

    def get_audit_log(self) -> list[dict]:
        return list(self._audit_log)
