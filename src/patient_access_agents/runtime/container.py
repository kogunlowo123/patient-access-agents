"""Composition root: builds every dependency once per process from Settings."""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

import httpx
from agentic_core.audit import AuditLog, JsonlAuditSink
from agentic_core.models.factory import ProvidersConfig, build_providers
from agentic_core.models.router import ModelRouter
from agentic_core.observability import ModelTelemetryObserver
from agentic_core.policy import PolicyEngine, PolicySet
from agentic_core.security import ContentGuard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine

from patient_access_agents.application.eligibility import EligibilityVerifier, Submitter
from patient_access_agents.application.messaging import MessageComposer, ModelAccess
from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.config import Settings
from patient_access_agents.domain.ports import Clock
from patient_access_agents.domain.rules import AccessRules
from patient_access_agents.infrastructure.database import (
    SqlCaseRepository,
    SqlEscalationRepository,
    SqlPortalInbox,
    SqlReminderRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from patient_access_agents.integrations.clearinghouse import ClearinghouseGateway
from patient_access_agents.integrations.fhir import FhirClient
from patient_access_agents.integrations.gcp import metadata_access_token
from patient_access_agents.integrations.identity_fabric import FabricClient
from patient_access_agents.integrations.notifications import Notifier, WebhookNotifier
from patient_access_agents.observability.metrics import AccessMetrics
from patient_access_agents.security.access import AccessController
from patient_access_agents.security.authn import Authenticator
from patient_access_agents.security.jwks import KeyResolver, RemoteJwks


@dataclass
class Container:
    settings: Settings
    service: PatientAccessService
    authn: Authenticator
    metrics: AccessMetrics
    engine: AsyncEngine
    http: httpx.AsyncClient
    models: ModelAccess | None

    async def aclose(self) -> None:
        if self.models is not None:
            await self.models.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def _audit_signer(settings: Settings) -> tuple[Ed25519PrivateKey, str]:
    if settings.audit_key_file is None:
        return Ed25519PrivateKey.generate(), "audit-ephemeral"
    key = serialization.load_pem_private_key(settings.audit_key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("audit key must be Ed25519")
    return key, settings.audit_key_file.stem


def _submitter(settings: Settings) -> Submitter | None:
    if not settings.clearinghouse_url:
        return None
    return Submitter(
        sender_id=settings.x12_sender_id or "",
        receiver_id=settings.x12_receiver_id or "",
        application_sender=settings.x12_application_sender or "",
        application_receiver=settings.x12_application_receiver or "",
        usage=settings.x12_usage,
        provider_name=settings.provider_name or "",
        provider_npi=settings.provider_npi or "",
        provider_tax_id=settings.provider_tax_id or "",
    )


def build_model_access(
    settings: Settings,
    clock: Clock,
    *,
    http: httpx.AsyncClient,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ModelAccess | None:
    """ModelRouter from configs/providers.yaml, or None when model drafting is disabled."""
    providers_file = settings.providers_file
    if providers_file is None:
        return None
    token_file = settings.model_token_file
    source = settings.model_token_source

    async def factory() -> ModelRouter:
        values = dict(os.environ if env is None else env)
        if source == "file" and token_file is not None:
            values[settings.model_token_env] = token_file.read_text(encoding="utf-8").strip()
        elif source == "gke-metadata":
            values[settings.model_token_env] = await metadata_access_token(http)
        config = ProvidersConfig.from_file(providers_file, env=values)
        providers = build_providers(config, env=values, transport=transport)
        return ModelRouter(providers, observers=[ModelTelemetryObserver()])

    return ModelAccess(
        factory,
        clock=clock.now,
        refresh_after=(
            timedelta(minutes=settings.model_refresh_minutes) if source != "env" else None
        ),
    )


async def build_container(
    settings: Settings,
    *,
    http: httpx.AsyncClient | None = None,
    user_resolver: KeyResolver | None = None,
    agent_resolver: KeyResolver | None = None,
    clock: Clock | None = None,
    model_env: Mapping[str, str] | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
) -> Container:
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    clock = clock or SystemClock()
    rules = AccessRules.from_file(settings.rules_file)
    metrics = AccessMetrics()

    fabric = None
    if settings.fabric_url:
        token_file = settings.workload_token_file
        fabric = FabricClient(
            settings.fabric_url,
            http=http,
            clock=clock.now,
            client_id=settings.fabric_client_id,
            workload_token=(
                (lambda: token_file.read_text(encoding="utf-8").strip())
                if token_file is not None
                else None
            ),
        )

    async def fhir_token() -> str | None:
        if fabric is None:  # pragma: no cover - validated by Settings
            return None
        return await fabric.token(settings.fhir_scopes)

    fhir = FhirClient(
        str(settings.fhir_base_url),
        http=http,
        token_provider=fhir_token if settings.fhir_auth == "fabric" else None,
    )
    gateway = (
        ClearinghouseGateway(
            settings.clearinghouse_url,
            http=http,
            api_key=(
                settings.clearinghouse_api_key.get_secret_value()
                if settings.clearinghouse_api_key
                else None
            ),
        )
        if settings.clearinghouse_url
        else None
    )
    notifier: Notifier | None = None
    if settings.notification_webhook_url and settings.notification_signing_key:
        notifier = WebhookNotifier(
            settings.notification_webhook_url,
            http=http,
            signing_key=settings.notification_signing_key.get_secret_value().encode(),
        )

    agent_jwks = agent_resolver
    if agent_jwks is None and settings.agent_jwks_url:
        agent_jwks = RemoteJwks(settings.agent_jwks_url, client=http)
    authn = Authenticator(
        user_resolver=user_resolver or RemoteJwks(settings.oidc_jwks_url, client=http),
        user_issuer=settings.oidc_issuer,
        user_audience=settings.oidc_audience,
        clock=clock.now,
        roles_claim=settings.roles_claim,
        patient_claim=settings.patient_claim,
        fabric=fabric,
        agent_resolver=agent_jwks,
        agent_issuer=settings.agent_issuer,
        agent_audience=settings.agent_audience,
    )

    engine = make_engine(settings.database_url)
    await create_schema(engine)
    signer, key_id = _audit_signer(settings)
    audit = AuditLog(JsonlAuditSink(settings.audit_log_path), signer, key_id=key_id)
    models = build_model_access(
        settings, clock, http=http, env=model_env, transport=model_transport
    )
    if models is not None:
        await models.router()  # fail at start-up, not on first use, if configuration is bad

    service = PatientAccessService(
        rules=rules,
        cases=SqlCaseRepository(engine),
        reminders=SqlReminderRepository(engine),
        escalations=SqlEscalationRepository(engine),
        inbox=SqlPortalInbox(engine),
        fhir=fhir,
        verifier=EligibilityVerifier(fhir=fhir, gateway=gateway, submitter=_submitter(settings)),
        notifier=notifier,
        composer=MessageComposer(models=models, guard=ContentGuard(), metrics=metrics),
        access=AccessController(
            PolicyEngine(PolicySet.from_file(settings.policy_file)), fabric=fabric
        ),
        audit=audit,
        clock=clock,
        metrics=metrics,
        default_timezone=settings.default_timezone,
        worker_id=f"{socket.gethostname()}-{os.getpid()}",
    )
    return Container(
        settings=settings,
        service=service,
        authn=authn,
        metrics=metrics,
        engine=engine,
        http=http,
        models=models,
    )
