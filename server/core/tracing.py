"""OpenTelemetry tracing bootstrap.

Single initialization point for the OTel TracerProvider. Modules that
want to emit spans do::

    from opentelemetry import trace
    tracer = trace.get_tracer(__name__)

    async def something():
        with tracer.start_as_current_span("namespace.operation"):
            ...

Span export to the console is **opt-in**. Spans are always recorded (so
an OTLP exporter can be attached without touching call sites), but the
``ConsoleSpanExporter`` is only wired when ``console_spans=True`` — set
by ``LOG_LEVEL=DEBUG`` or ``TRACING_CONSOLE_SPANS=true``. At INFO the
Temporal SDK's ``TracingInterceptor`` alone emits a span per
Signal/Query/Update/Activity on both the caller and handler side, which
buries the operator log in per-RPC lines that carry no operational
signal.

When enabled, a compact one-line ``formatter`` renders each completed
span as::

    [span] broadcaster.refresh_telegram 4012ms path=auto_reconnect connected=False

(The default OTel formatter emits a 30-line JSON document per span,
which makes interactive logs unreadable.)

In production, swap ``ConsoleSpanExporter`` for an OTLP exporter
pointed at a collector (Jaeger / Tempo / Honeycomb / etc.) — purely
config, no code change at the call sites.

This file owns the only place in the codebase where the OTel SDK is
configured. Every other module uses ``trace.get_tracer(__name__)``
which is the standard library API; no project-specific abstraction.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)


_INITIALIZED = False


def _compact_span_formatter(span: ReadableSpan) -> str:
    """Render a finished span as a single ``[span] name Xms key=value ...`` line.

    Hooked into ``ConsoleSpanExporter`` via its standard ``formatter``
    parameter (the documented OTel extension point for changing output
    format without subclassing the exporter).
    """
    duration_ms = (span.end_time - span.start_time) // 1_000_000  # ns -> ms
    parts = [f"[span] {span.name} {duration_ms}ms"]
    if span.attributes:
        for key, value in span.attributes.items():
            parts.append(f"{key}={value}")
    if span.status.status_code.name != "UNSET":
        parts.append(f"status={span.status.status_code.name}")
    return " ".join(parts) + "\n"


def init_tracing(
    service_name: str = "opencompany-backend",
    *,
    console_spans: bool = False,
) -> None:
    """Configure the global TracerProvider.

    Args:
        service_name: ``service.name`` resource attribute.
        console_spans: Attach the console exporter. Off by default —
            span-per-RPC output is a debugging tool, not operator
            signal. The provider is still installed either way, so
            ``trace.get_tracer(__name__)`` call sites keep working and
            an OTLP exporter can be added without code changes.

    Idempotent — safe to call multiple times. Subsequent calls are
    no-ops so tests / reloads do not accumulate processors.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    if console_spans:
        # Compact one-line console output instead of the default 30-line
        # JSON dump per span — keeps interactive logs readable.
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter(formatter=_compact_span_formatter)))
    trace.set_tracer_provider(provider)
    _INITIALIZED = True
