"""OpenTelemetry → Langfuse instrumentation.

Single ``setup(app_name)`` call per process. Patches LangChain, LangGraph,
OpenAI, httpx, and friends at import time so every LLM call, tool call,
chain step, and outbound HTTP request lands in Langfuse as a span — no
per-call code changes needed.

Design choices:
  * **OTEL-native, not Langfuse-callback.** We export standard OTLP/HTTP
    traces. Langfuse is the current sink; same code works against
    Honeycomb / Jaeger / Datadog by changing one env var.
  * **Safe no-op when unconfigured.** Missing LANGFUSE_* env vars → we
    print one line and return. Nothing breaks; nothing gets traced.
  * **Idempotent.** Calling ``setup`` twice in one process is a no-op
    on the second call (Traceloop guards internally; we add a local
    guard too just in case).
  * **disable_batch=True.** Traces flush immediately rather than every
    5s. Lower throughput, much better UX for a demo where you want to
    see the span appear in the UI right after running the command.

Each process should call ``setup`` with a distinct app_name (e.g.
``orchestrator``, ``data_analysis_agent``) so the Langfuse UI's
"service" filter is useful.
"""

from __future__ import annotations

import base64
import os
import sys

_initialised = False


def _build_otlp_headers() -> str | None:
    """Compose the OTLP Authorization header from the Langfuse keys.

    Returns None if either key is missing — caller treats that as a
    signal to skip instrumentation.
    """
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip().strip('"')
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip().strip('"')
    if not public_key or not secret_key:
        return None
    if "replace-me" in public_key or "replace-me" in secret_key:
        return None
    raw = f"{public_key}:{secret_key}".encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii")
    return f"Authorization=Basic {encoded}"


def setup(app_name: str) -> None:
    """Initialise tracing for this process.

    Parameters
    ----------
    app_name
        Shows up as the service name in Langfuse. Use a stable string
        like ``"orchestrator"`` or ``"sales_intelligence_agent"`` so
        traces from different agents stay distinguishable.
    """
    global _initialised
    if _initialised:
        return

    base_url = os.getenv("LANGFUSE_BASE_URL", "").strip().strip('"')
    headers = _build_otlp_headers()
    if not base_url or not headers:
        # Quietly skip — running without observability is a valid mode.
        print(
            f"[observability] {app_name}: LANGFUSE_* env vars not set; "
            "tracing disabled.",
            file=sys.stderr,
            flush=True,
        )
        _initialised = True
        return

    # Langfuse's OTLP receiver lives at <base>/api/public/otel
    endpoint = base_url.rstrip("/") + "/api/public/otel"

    # Setting these env vars BEFORE importing Traceloop is the most
    # reliable way to point the OTLP exporter at Langfuse — Traceloop's
    # init() also accepts them, but env-first means the underlying
    # OpenTelemetry SDK picks them up unambiguously.
    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    os.environ.setdefault("OTEL_EXPORTER_OTLP_HEADERS", headers)

    # Import inside the function so a missing dependency only blows up
    # if someone actually tries to enable tracing.
    from traceloop.sdk import Traceloop

    Traceloop.init(
        app_name=app_name,
        api_endpoint=endpoint,
        headers={k: v for k, v in (h.split("=", 1) for h in headers.split(","))},
        disable_batch=True,
    )
    print(
        f"[observability] {app_name}: tracing → {endpoint}",
        file=sys.stderr,
        flush=True,
    )
    _initialised = True
