"""
Pydantic AI Agent with OpenTelemetry instrumentation for CloudWatch.

This agent uses BedrockConverseModel (Claude Sonnet on Bedrock) with three tools,
instrumented via Pydantic AI's built-in OTEL support. SigV4 authentication
for CloudWatch OTLP endpoints is handled by requests-auth-aws-sigv4, with
no dependency on ADOT or the opentelemetry-instrument wrapper.

    python main.py
"""

import asyncio
import math
import os
from datetime import datetime, timezone, timedelta

import requests
from requests_auth_aws_sigv4 import AWSSigV4
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from pydantic_ai import Agent, InstrumentationSettings


def setup_otel():
    """Configure OpenTelemetry to export to CloudWatch OTLP endpoints with SigV4 auth."""
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    service_name = os.environ.get("OTEL_SERVICE_NAME", "pydantic-ai-agent-demo")

    resource = Resource.create({"service.name": service_name})

    # Traces: SigV4-signed session for the X-Ray OTLP endpoint
    trace_session = requests.Session()
    trace_session.auth = AWSSigV4("xray", region=region)

    trace_exporter = OTLPSpanExporter(
        endpoint=f"https://xray.{region}.amazonaws.com/v1/traces",
        session=trace_session,
    )
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Metrics: SigV4-signed session for the CloudWatch Metrics OTLP endpoint
    metrics_session = requests.Session()
    metrics_session.auth = AWSSigV4("monitoring", region=region)

    metric_exporter = OTLPMetricExporter(
        endpoint=f"https://monitoring.{region}.amazonaws.com/v1/metrics",
        session=metrics_session,
    )
    reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(metric_readers=[reader], resource=resource)
    metrics.set_meter_provider(meter_provider)

    return tracer_provider, meter_provider


# 1. Set up OTEL providers with SigV4 auth
tracer_provider, meter_provider = setup_otel()

# 2. Enable Pydantic AI instrumentation (must come after providers are set)
Agent.instrument_all(InstrumentationSettings(version=5))

# 3. Define the agent
agent = Agent(
    "bedrock:us.anthropic.claude-sonnet-4-6",
    system_prompt=(
        "You are a helpful assistant with access to weather, math, and time tools. "
        "Use the appropriate tool to answer the user's question. Be concise."
    ),
)


@agent.tool_plain
def get_weather(city: str) -> str:
    """Get the current weather for a city.

    Args:
        city: The city name to get weather for.
    """
    # Hardcoded responses to keep the sample self-contained (no external API).
    weather_data = {
        "seattle": "Cloudy, 58°F (14°C), 72% humidity, light rain expected",
        "new york": "Sunny, 75°F (24°C), 45% humidity, clear skies",
        "london": "Overcast, 62°F (17°C), 80% humidity, chance of showers",
        "tokyo": "Partly cloudy, 70°F (21°C), 55% humidity",
        "sydney": "Clear, 68°F (20°C), 50% humidity, mild breeze",
    }
    key = city.lower().strip()
    if key in weather_data:
        return f"Weather in {city}: {weather_data[key]}"
    return f"Weather in {city}: Partly cloudy, 65°F (18°C), 60% humidity (default forecast)"


@agent.tool_plain
def calculate(expression: str) -> str:
    """Evaluate a mathematical expression.

    Args:
        expression: A math expression to evaluate, e.g. '2 + 3 * 4' or 'sqrt(144)'.
    """
    # Restricted eval with math functions only.
    allowed_names = {k: v for k, v in math.__dict__.items() if not k.startswith("_")}
    allowed_names["abs"] = abs
    allowed_names["round"] = round
    try:
        result = eval(expression, {"__builtins__": {}}, allowed_names)  # noqa: S307
        return f"{expression} = {result}"
    except Exception as e:
        return f"Could not evaluate '{expression}': {e}"


@agent.tool_plain
def get_current_time(timezone_name: str) -> str:
    """Get the current time in a given timezone.

    Args:
        timezone_name: Timezone as UTC offset, e.g. 'UTC', 'UTC+5', 'UTC-8'.
    """
    tz_name = timezone_name.strip().upper()
    if tz_name == "UTC":
        tz = timezone.utc
    elif tz_name.startswith("UTC") and (
        len(tz_name) > 3 and tz_name[3] in ("+", "-")
    ):
        try:
            offset_hours = int(tz_name[3:])
            tz = timezone(timedelta(hours=offset_hours))
        except ValueError:
            return f"Invalid timezone format: {timezone_name}. Use UTC, UTC+5, UTC-8, etc."
    else:
        return f"Invalid timezone format: {timezone_name}. Use UTC, UTC+5, UTC-8, etc."

    now = datetime.now(tz)
    return f"Current time in {timezone_name}: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"


async def main():
    prompts = [
        "What's the weather like in Seattle and what's 15% of 340?",
        "What time is it in UTC-8 right now?",
    ]

    for prompt in prompts:
        print(f"\n{'='*60}")
        print(f"Prompt: {prompt}")
        print(f"{'='*60}")
        result = await agent.run(prompt)
        print(f"\nResponse: {result.output}")
        print(f"Usage: {result.usage()}")

    # Flush remaining telemetry before exit
    tracer_provider.force_flush()
    meter_provider.force_flush()


if __name__ == "__main__":
    asyncio.run(main())
