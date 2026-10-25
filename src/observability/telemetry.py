"""
Telemetry Layer (OpenTelemetry)
Collects Traces, Metrics, and Logs from all agent activities:
  - LLM calls (latency, token counts, model)
  - Tool calls (per tool, per step, errors)
  - HTTP API requests (via FastAPI auto-instrumentation)

Exporters:
  - Console  : always active (dev-friendly)
  - OTLP/HTTP: active when OTEL_EXPORTER_OTLP_ENDPOINT is set
                (compatible with Jaeger, Grafana Tempo, etc.)
"""

import logging
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    PeriodicExportingMetricReader,
    ConsoleMetricExporter,
)
from opentelemetry.sdk.resources import Resource
from src.configs.config import OTEL_SERVICE_NAME, OTEL_EXPORTER_OTLP_ENDPOINT

# ── Resource ──────────────────────────────────────────────────────────────────
SERVICE_NAME = OTEL_SERVICE_NAME
OTLP_ENDPOINT = OTEL_EXPORTER_OTLP_ENDPOINT

resource = Resource.create({"service.name": SERVICE_NAME, "service.version": "1.0.0"})

# ── Tracing ───────────────────────────────────────────────────────────────────
_tracer_provider = TracerProvider(resource=resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

if OTLP_ENDPOINT:
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        _tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces"))
        )
        logging.getLogger(__name__).info(f"[Telemetry] OTLP traces -> {OTLP_ENDPOINT}")
    except Exception as _e:
        logging.getLogger(__name__).warning(
            f"[Telemetry] OTLP trace exporter unavailable ({_e}), falling back to console only"
        )

trace.set_tracer_provider(_tracer_provider)
tracer: trace.Tracer = trace.get_tracer(SERVICE_NAME)

# ── Metrics ───────────────────────────────────────────────────────────────────
_metric_readers = [
    PeriodicExportingMetricReader(
        ConsoleMetricExporter(), export_interval_millis=60_000
    )
]

if OTLP_ENDPOINT:
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        _metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{OTLP_ENDPOINT}/v1/metrics"),
                export_interval_millis=30_000,
            )
        )
    except Exception as _e:
        logging.getLogger(__name__).warning(
            f"[Telemetry] OTLP metric exporter unavailable ({_e}), falling back to console only"
        )

_meter_provider = MeterProvider(resource=resource, metric_readers=_metric_readers)
metrics.set_meter_provider(_meter_provider)
meter: metrics.Meter = metrics.get_meter(SERVICE_NAME)

# ── Metric Instruments ────────────────────────────────────────────────────────

# Agent
agent_run_counter = meter.create_counter(
    "agent.runs", description="Total agent.run() invocations"
)

# LLM
llm_call_counter = meter.create_counter(
    "agent.llm.calls", description="Total LLM API calls"
)
llm_input_tokens = meter.create_counter(
    "agent.llm.input_tokens", description="Cumulative input tokens sent to LLM"
)
llm_output_tokens = meter.create_counter(
    "agent.llm.output_tokens", description="Cumulative output tokens from LLM"
)
llm_latency_ms = meter.create_histogram(
    "agent.llm.latency_ms", unit="ms", description="LLM call round-trip latency"
)
llm_cost_usd = meter.create_counter(
    "agent.llm.cost_usd", unit="$", description="Estimated LLM cost in USD (microdollars x1000)"
)

# Tool
tool_call_counter = meter.create_counter(
    "agent.tool.calls", description="Total tool invocations"
)
tool_error_counter = meter.create_counter(
    "agent.tool.errors", description="Total tool invocation errors"
)
security_event_counter = meter.create_counter(
    "agent.security.events",
    description="Security signals: injection flags, blocked tool calls, output redactions",
)
tool_latency_ms = meter.create_histogram(
    "agent.tool.latency_ms", unit="ms", description="Tool execution latency"
)

# ── Convenience helpers ───────────────────────────────────────────────────────

def get_tracer() -> trace.Tracer:
    """Return the shared tracer."""
    return tracer


def get_meter() -> metrics.Meter:
    """Return the shared meter."""
    return meter


def shutdown():
    """Flush and shut down all providers (call on process exit)."""
    _tracer_provider.shutdown()
    _meter_provider.shutdown()
