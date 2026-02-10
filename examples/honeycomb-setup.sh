#!/bin/bash
# Honeycomb OpenTelemetry Configuration for nanobot
#
# Set these environment variables before running nanobot gateway:

# Required: Honeycomb API endpoint
export OTEL_EXPORTER_OTLP_ENDPOINT="https://api.honeycomb.io"

# Required: Honeycomb authentication (replace with your API key)
export OTEL_EXPORTER_OTLP_HEADERS="x-honeycomb-team=YOUR_HONEYCOMB_API_KEY_HERE"

# Optional: Environment name (shown in Honeycomb)
export ENVIRONMENT="production"

# Optional: Explicit trace/metric endpoints (if not using combined endpoint)
# export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="https://api.honeycomb.io/v1/traces"
# export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT="https://api.honeycomb.io/v1/metrics"

echo "✓ Honeycomb telemetry configured"
echo "  Service: nanobot"
echo "  Endpoint: $OTEL_EXPORTER_OTLP_ENDPOINT"
echo ""
echo "Run nanobot gateway to start sending telemetry to Honeycomb"
echo ""
echo "View your data at: https://ui.honeycomb.io/"
