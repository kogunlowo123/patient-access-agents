"""Process entry point: ``python -m patient_access_agents``."""

from __future__ import annotations

import logging
import os

import uvicorn
from agentic_core.observability import configure_logging, configure_telemetry
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from patient_access_agents import __version__
from patient_access_agents.api.app import create_app
from patient_access_agents.config import Settings
from patient_access_agents.runtime.container import build_container


def main() -> None:
    settings = Settings()  # populated from PAA_* environment variables
    configure_logging(
        service="patient-access-agents", environment=settings.environment, level=settings.log_level
    )
    # Request URLs carry FHIR search parameters such as patient references;
    # keep them out of logs (HIPAA minimum necessary).
    for noisy in ("httpx", "httpcore", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    exporter = None
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    configure_telemetry(
        service_name="patient-access-agents",
        service_version=__version__,
        environment=settings.environment,
        span_exporter=exporter,
    )
    app = create_app(lambda: build_container(settings))
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    uvicorn.run(
        app,
        host=os.environ.get("PAA_HOST", "127.0.0.1"),
        port=int(os.environ.get("PAA_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("PAA_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
