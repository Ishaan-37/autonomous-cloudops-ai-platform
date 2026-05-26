"""
observability/telemetry.py
---------------------------
FULL OPENTELEMETRY OBSERVABILITY FOR THE CLOUDOPS AGENT.

WHAT IS OPENTELEMETRY?
  OpenTelemetry (OTel) is the industry standard for collecting:
    - Traces:  "What happened during this agent run, step by step?"
    - Metrics: "How many alarms/hour? Average LLM latency? Error rate?"
    - Logs:    "Structured logs with trace context attached"

  Think of it like X-ray vision for your running system.

WHERE DATA GOES:
  Traces  → Grafana Tempo  (timeline view of each agent run)
  Metrics → Prometheus     (numbers over time, dashboards, alerts)
  Logs    → Grafana Loki   (searchable logs linked to traces)

  All three visible in Grafana dashboards.

HOW TO USE IN YOUR NODES:
  from observability.telemetry import tracer, meter

  # In any node function:
  with tracer.start_as_current_span("analyze_node") as span:
      span.set_attribute("alarm.name", alarm_name)
      span.set_attribute("llm.model", "gpt-4o")
      result = await do_analysis()
      span.set_attribute("rca.confidence", result["confidence"])

  # Record a metric:
  alarms_counter.add(1, {"severity": "HIGH"})

SETUP REQUIREMENTS:
  The OTel Collector must be running in your EKS cluster.
  We deploy it in the observability stack (see otel_collector.yaml).
  It receives data from your app and forwards to Tempo/Prometheus/Loki.
"""

import logging
import os
from functools import wraps
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.resource import ResourceAttributes

logger = logging.getLogger(__name__)

# OTel Collector endpoint — running as a pod in EKS
# Service name: otel-collector (defined in otel_collector.yaml)
OTEL_ENDPOINT  = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4317")
SERVICE_NAME   = os.environ.get("OTEL_SERVICE_NAME", "cloudops-agent")
ENVIRONMENT    = os.environ.get("CLOUDOPS_ENVIRONMENT", "development")


def setup_telemetry() -> tuple:
    """
    Initialize OpenTelemetry tracing and metrics.
    Call this ONCE at application startup in api/main.py lifespan.

    Returns: (tracer, meter) tuple for use throughout the app.
    """
    # Resource = metadata about YOUR service attached to all telemetry
    resource = Resource.create({
        ResourceAttributes.SERVICE_NAME:       SERVICE_NAME,
        ResourceAttributes.SERVICE_VERSION:    "1.0.0",
        ResourceAttributes.DEPLOYMENT_ENVIRONMENT: ENVIRONMENT,
        "team": "platform-engineering",
    })

    # ── Tracing Setup ─────────────────────────────────────────
    # Traces show the full timeline of an agent run
    trace_provider = TracerProvider(resource=resource)

    # OTLP exporter sends spans to OTel Collector → Grafana Tempo
    otlp_trace_exporter = OTLPSpanExporter(
        endpoint=OTEL_ENDPOINT,
        insecure=True,  # Use TLS in production
    )

    # BatchSpanProcessor: buffers spans and sends in batches (efficient)
    trace_provider.add_span_processor(
        BatchSpanProcessor(
            otlp_trace_exporter,
            max_export_batch_size=512,
            export_timeout_millis=30000,
        )
    )

    trace.set_tracer_provider(trace_provider)
    logger.info(f"Tracing configured → {OTEL_ENDPOINT}")

    # ── Metrics Setup ─────────────────────────────────────────
    # Metrics are numbers over time (counters, histograms, gauges)
    otlp_metric_exporter = OTLPMetricExporter(
        endpoint=OTEL_ENDPOINT,
        insecure=True,
    )

    metric_reader = PeriodicExportingMetricReader(
        exporter=otlp_metric_exporter,
        export_interval_millis=60000,   # Export every 60 seconds
    )

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[metric_reader],
    )

    metrics.set_meter_provider(meter_provider)
    logger.info(f"Metrics configured → {OTEL_ENDPOINT}")

    tracer = trace.get_tracer(SERVICE_NAME)
    meter  = metrics.get_meter(SERVICE_NAME)

    return tracer, meter


# ── Global tracer and meter ───────────────────────────────────
# These are initialized at startup and used throughout the app
tracer = trace.get_tracer(SERVICE_NAME)
meter  = metrics.get_meter(SERVICE_NAME)


# ── Metrics Definitions ───────────────────────────────────────
# Define all your metrics here, import where needed

# Counter: how many alarms have been processed
alarms_processed = meter.create_counter(
    name="cloudops.alarms.processed",
    description="Total CloudWatch alarms processed by the agent",
    unit="1",
)

# Histogram: how long each agent run takes (in seconds)
agent_run_duration = meter.create_histogram(
    name="cloudops.agent.run_duration_seconds",
    description="Duration of complete agent runs in seconds",
    unit="s",
)

# Counter: LLM API calls made
llm_calls = meter.create_counter(
    name="cloudops.llm.calls",
    description="Total LLM API calls made",
    unit="1",
)

# Histogram: LLM response latency
llm_latency = meter.create_histogram(
    name="cloudops.llm.latency_seconds",
    description="LLM API call latency in seconds",
    unit="s",
)

# Counter: remediations executed
remediations_executed = meter.create_counter(
    name="cloudops.remediations.executed",
    description="Total automated remediations executed",
    unit="1",
)

# Counter: remediations approved vs rejected
approval_decisions = meter.create_counter(
    name="cloudops.approvals",
    description="Human approval decisions (approved/rejected)",
    unit="1",
)

# Counter: errors per node
node_errors = meter.create_counter(
    name="cloudops.node.errors",
    description="Errors per agent node",
    unit="1",
)

# Gauge: monthly AWS cost (updated nightly by FinOps node)
monthly_aws_cost = meter.create_observable_gauge(
    name="cloudops.finops.monthly_cost_usd",
    description="Current month AWS spend in USD",
    unit="USD",
)

# Counter: cost savings identified
cost_savings_identified = meter.create_counter(
    name="cloudops.finops.savings_identified_usd",
    description="Total cost savings identified by FinOps node",
    unit="USD",
)


# ── Tracing Decorator ─────────────────────────────────────────

def traced(span_name: Optional[str] = None):
    """
    Decorator to automatically trace any async function.

    Usage:
      @traced("analyze_node")
      async def analyze_node(state: AgentState) -> dict:
          ...

    Creates a span for the function, records:
      - Function name
      - Exception if raised
      - Duration automatically
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            name = span_name or func.__name__
            with tracer.start_as_current_span(name) as span:
                try:
                    # Add common attributes if state is passed
                    if args and isinstance(args[0], dict):
                        state = args[0]
                        span.set_attribute("run.id",    state.get("run_id", ""))
                        span.set_attribute("alarm.name", state.get("alert", {}).get("AlarmName", ""))

                    result = await func(*args, **kwargs)

                    # Record node-specific attributes from result
                    if isinstance(result, dict):
                        if "root_cause" in result and result["root_cause"]:
                            rca = result["root_cause"]
                            span.set_attribute("rca.severity",   rca.get("severity", ""))
                            span.set_attribute("rca.confidence", rca.get("confidence", 0))
                            span.set_attribute("rca.category",   rca.get("category", ""))

                        if "fix_plan" in result and result["fix_plan"]:
                            plan = result["fix_plan"]
                            span.set_attribute("plan.action_type", plan.get("action_type", ""))
                            span.set_attribute("plan.risk",        plan.get("estimated_risk", ""))

                    return result

                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    node_errors.add(1, {"node": name, "error_type": type(e).__name__})
                    raise

        return wrapper
    return decorator
