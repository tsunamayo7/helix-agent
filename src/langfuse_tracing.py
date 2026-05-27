"""Langfuse OTLP tracing integration (optional).

Sends MCP tool call traces to Langfuse via OpenTelemetry OTLP.
All dependencies are optional — when opentelemetry or Langfuse env vars
are missing, every function is a silent no-op.
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger("helix.langfuse")
_tracer = None
_provider = None


def init_tracing() -> None:
    """Initialize the OTLP tracer for Langfuse. No-op when env vars are missing."""
    global _tracer, _provider

    sk = os.environ.get("LANGFUSE_SECRET_KEY")
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
    base = os.environ.get("LANGFUSE_BASE_URL", "http://localhost:3000")

    if not sk or not pk:
        _logger.debug("Langfuse tracing disabled (LANGFUSE_SECRET_KEY/PUBLIC_KEY not set)")
        return

    try:
        import base64

        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()

        resource = Resource.create({"service.name": "helix-agent"})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=f"{base}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        _provider = provider
        _tracer = trace.get_tracer("helix-agent")
        _logger.info("Langfuse OTLP tracing enabled (endpoint=%s)", base)

    except ImportError:
        _logger.debug("Langfuse tracing disabled (opentelemetry not installed)")
    except Exception:
        _logger.warning("Langfuse tracing init failed", exc_info=True)


def get_tracer():
    """Return the OpenTelemetry tracer, or None if tracing is disabled."""
    return _tracer


def trace_tool_call(
    tool_name: str,
    params: dict,
    result: dict,
    duration_ms: float,
) -> None:
    """Record an MCP tool call as an OpenTelemetry span for Langfuse.

    No-op when tracing is not initialized.
    Parameters and results are truncated to 500 chars to prevent trace bloat.
    """
    tracer = get_tracer()
    if not tracer:
        return

    try:
        with tracer.start_as_current_span(f"mcp.tool.{tool_name}") as span:
            span.set_attribute("mcp.tool.name", tool_name)
            span.set_attribute("mcp.tool.duration_ms", duration_ms)
            span.set_attribute("mcp.tool.params", str(params)[:500])
            span.set_attribute("mcp.tool.result_preview", str(result)[:500])
    except Exception:
        _logger.debug("Failed to record trace for %s", tool_name, exc_info=True)


def shutdown_tracing() -> None:
    """Flush and shut down the tracer provider. Safe to call when tracing is disabled."""
    global _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            _logger.debug("Tracer shutdown error", exc_info=True)
        _provider = None
