"""OpenTelemetry provider initialization and configuration."""

import os
from typing import Any

from opentelemetry import trace, metrics
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from loguru import logger


_initialized = False
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def init_telemetry(
    service_name: str = "nanobot",
    service_version: str = "unknown",
    environment: str | None = None,
) -> None:
    """
    Initialize OpenTelemetry with OTLP exporters.

    Reads configuration from standard OTEL environment variables:
    - OTEL_EXPORTER_OTLP_ENDPOINT
    - OTEL_EXPORTER_OTLP_HEADERS
    - OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (optional, overrides endpoint)
    - OTEL_EXPORTER_OTLP_METRICS_ENDPOINT (optional, overrides endpoint)

    For Honeycomb, set:
    - OTEL_EXPORTER_OTLP_ENDPOINT="https://api.honeycomb.io"
    - OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=YOUR_API_KEY"

    Args:
        service_name: Service name for telemetry.
        service_version: Service version.
        environment: Deployment environment (e.g., "production", "development").
    """
    global _initialized, _tracer_provider, _meter_provider

    if _initialized:
        logger.warning("Telemetry already initialized")
        return

    # Check if telemetry is configured
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not otlp_endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set, telemetry disabled")
        _initialized = True
        return

    # Build resource attributes
    resource_attrs = {
        SERVICE_NAME: service_name,
        SERVICE_VERSION: service_version,
    }
    if environment:
        resource_attrs["deployment.environment"] = environment

    resource = Resource.create(resource_attrs)

    # Parse headers from env var (format: "key1=value1,key2=value2")
    headers_str = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    headers = {}
    if headers_str:
        for pair in headers_str.split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                headers[key.strip()] = value.strip()

    # Initialize tracing
    try:
        trace_endpoint = os.getenv(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            f"{otlp_endpoint}/v1/traces"
        )

        trace_exporter = OTLPSpanExporter(
            endpoint=trace_endpoint,
            headers=headers,
        )

        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
        trace.set_tracer_provider(_tracer_provider)

        logger.info(f"Tracing initialized: {trace_endpoint}")
    except Exception as e:
        logger.error(f"Failed to initialize tracing: {e}")

    # Initialize metrics with GenAI-optimized histogram buckets
    try:
        metric_endpoint = os.getenv(
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
            f"{otlp_endpoint}/v1/metrics"
        )

        metric_exporter = OTLPMetricExporter(
            endpoint=metric_endpoint,
            headers=headers,
        )

        metric_reader = PeriodicExportingMetricReader(
            metric_exporter,
            export_interval_millis=30000,  # 30 seconds
        )

        # Note: Custom histogram buckets would be configured here, but
        # OpenTelemetry Python SDK's aggregation API varies by version.
        # Using default buckets for now - they work well for most cases.
        # TODO: Add custom bucket configuration when API stabilizes
        _meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[metric_reader],
        )
        metrics.set_meter_provider(_meter_provider)

        logger.info(f"Metrics initialized: {metric_endpoint}")
    except Exception as e:
        logger.error(f"Failed to initialize metrics: {e}")

    # Auto-instrument httpx and asyncio
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        logger.debug("HTTPX auto-instrumentation enabled")
    except ImportError:
        logger.warning("opentelemetry-instrumentation-httpx not installed")
    except Exception as e:
        logger.warning(f"Failed to instrument HTTPX: {e}")

    try:
        from opentelemetry.instrumentation.asyncio import AsyncioInstrumentor
        AsyncioInstrumentor().instrument()
        logger.debug("Asyncio auto-instrumentation enabled")
    except ImportError:
        logger.warning("opentelemetry-instrumentation-asyncio not installed")
    except Exception as e:
        logger.warning(f"Failed to instrument asyncio: {e}")

    _initialized = True
    logger.info("OpenTelemetry initialization complete")


def shutdown_telemetry() -> None:
    """Shutdown telemetry providers and flush pending data."""
    global _tracer_provider, _meter_provider

    if _tracer_provider:
        try:
            _tracer_provider.shutdown()
            logger.info("Tracer provider shutdown")
        except Exception as e:
            logger.error(f"Error shutting down tracer: {e}")

    if _meter_provider:
        try:
            _meter_provider.shutdown()
            logger.info("Meter provider shutdown")
        except Exception as e:
            logger.error(f"Error shutting down meter: {e}")
