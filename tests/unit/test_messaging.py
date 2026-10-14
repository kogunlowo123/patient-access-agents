"""Patient message drafting through the agentic-core ModelRouter (Gemini on Vertex AI)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from agentic_core.security import ContentGuard

from patient_access_agents.application.messaging import (
    MessageComposer,
    MessageContext,
    Recipient,
    format_when,
    format_where,
    validate_template,
)
from patient_access_agents.config import Settings
from patient_access_agents.domain.models import Modality, ReminderKind
from patient_access_agents.observability.metrics import AccessMetrics
from patient_access_agents.runtime.container import build_model_access
from tests.conftest import ROOT, make_settings
from tests.fakes import FakeClock, World

MODEL_ENV = {
    "GCP_ACCESS_TOKEN": "vertex-token",
    "GCP_REGION": "us-central1",
    "GCP_PROJECT_ID": "paa-test",
    "GEMINI_PRO_INPUT_USD_PER_MTOK": "1.25",
    "GEMINI_PRO_OUTPUT_USD_PER_MTOK": "10",
    "GEMINI_FLASH_INPUT_USD_PER_MTOK": "0.3",
    "GEMINI_FLASH_OUTPUT_USD_PER_MTOK": "2.5",
}
VISIT = datetime(2026, 10, 20, 13, 30, tzinfo=UTC)
CONTEXT = MessageContext(
    kind=ReminderKind.APPOINTMENT_REMINDER,
    service="cardiology consultation",
    modality=Modality.IN_PERSON,
    preparation="Bring a list of current medications.",
)
RECIPIENT = Recipient(
    first_name="Ana",
    appointment_start=VISIT,
    location_name="Riverbend Cardiology Clinic",
    timezone="America/New_York",
)


@pytest.fixture
def model_settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, providers_file=ROOT / "configs" / "providers.yaml")


def composer(
    world: World, settings: Settings, clock: FakeClock
) -> tuple[MessageComposer, AccessMetrics]:
    metrics = AccessMetrics()
    models = build_model_access(
        settings,
        clock,
        http=httpx.AsyncClient(transport=world.transport()),
        env=MODEL_ENV,
        transport=world.transport(),
    )
    return MessageComposer(models=models, guard=ContentGuard(), metrics=metrics), metrics


async def test_model_draft_is_validated_and_filled_locally(
    world: World, model_settings: Settings, clock: FakeClock
) -> None:
    world.gemini.replies = [
        "Hi {first_name}! Your heart check-up is on {when} at {where}. See you soon."
    ]
    service, metrics = composer(world, model_settings, clock)
    message = await service.compose(CONTEXT, RECIPIENT)
    assert message.composer == "model:gemini-pro"
    assert message.text == (
        "Hi Ana! Your heart check-up is on Tuesday, October 20 at 9:30 AM at "
        "Riverbend Cardiology Clinic. See you soon."
    )
    sent = world.gemini.requests[0]
    prompt = str(sent)
    for phi in ("Ana", "Rivera", "Riverbend", "2026-10-20", "pat-ana"):
        assert phi not in prompt  # the model never sees identifiers or the visit time
    assert sent["generationConfig"]["maxOutputTokens"] == 300
    sample = metrics.registry.get_sample_value(
        "paa_message_composer_total", {"composer": "model", "reason": "none"}
    )
    assert sample == 1


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ("Hello {first_name}, see you {when}.", "invalid_placeholders"),
        ("Hello {first_name} {first_name}, {when} at {where}.", "invalid_placeholders"),
        ("Hello {first_name}, {when} at {where}. Call 5550199 now.", "invalid_digits"),
        ("Hello {first_name}, {when} at {where}. Visit https://phish.test", "invalid_url"),
        ("Hello {first_name}, {when} at {where} }", "invalid_braces"),
        ("Hello {first_name}, {when} at {where}, write to ana@mail.test", "output_pii"),
        ("x" * 400, "invalid_length"),
        ("Use key AKIAIOSFODNN7EXAMPLE {first_name} {when} {where}", "output_guard"),
    ],
)
async def test_invalid_drafts_fall_back_to_reviewed_template(
    world: World, model_settings: Settings, clock: FakeClock, reply: str, reason: str
) -> None:
    world.gemini.replies = [reply]
    service, _ = composer(world, model_settings, clock)
    message = await service.compose(CONTEXT, RECIPIENT)
    assert message.composer == "template"
    assert message.fallback_reason == reason
    assert message.text.startswith("Hello Ana, this is a reminder of your cardiology consultation")
    assert "Bring a list of current medications." in message.text


async def test_provider_outage_falls_back(
    world: World, model_settings: Settings, clock: FakeClock
) -> None:
    world.gemini.status = 503
    service, _ = composer(world, model_settings, clock)
    message = await service.compose(CONTEXT, RECIPIENT)
    assert (message.composer, message.fallback_reason) == ("template", "model_unavailable")
    assert len(world.gemini.requests) >= 2  # retried and failed over to the flash route


async def test_no_model_configured_uses_templates_in_patient_language() -> None:
    service = MessageComposer(models=None, guard=ContentGuard(), metrics=AccessMetrics())
    spanish = await service.compose(
        CONTEXT.model_copy(update={"language": "es-US", "modality": Modality.TELEHEALTH}),
        RECIPIENT,
    )
    assert spanish.fallback_reason == "no_model_configured"
    assert spanish.text.startswith(
        "Hola Ana, le recordamos su cardiology consultation el martes 20"
    )
    assert "visita por video" in spanish.text
    outreach = await service.compose(
        CONTEXT.model_copy(update={"kind": ReminderKind.SCHEDULING_OUTREACH, "language": "fr"}),
        RECIPIENT.model_copy(update={"appointment_start": None}),
    )
    assert outreach.text.startswith("Hello Ana, we have received a referral")


def test_formatting_helpers() -> None:
    assert format_when(VISIT, "America/New_York", "en") == "Tuesday, October 20 at 9:30 AM"
    evening = VISIT + timedelta(hours=9)
    assert format_when(evening, "America/New_York", "en").endswith("6:30 PM")
    assert format_when(VISIT, "America/New_York", "es") == "martes 20 de octubre a las 09:30"
    assert format_where(Modality.IN_PERSON, None, "en") == "at the clinic"
    assert format_where(Modality.IN_PERSON, None, "es") == "en la clínica"
    assert format_where(Modality.HOME, None, "en") == "home visit"
    assert validate_template("", ReminderKind.SCHEDULING_OUTREACH) == "length"
    assert validate_template("Hi {first_name}", ReminderKind.SCHEDULING_OUTREACH) is None


async def test_router_is_rebuilt_when_the_token_file_ages(
    world: World, tmp_path: Path, clock: FakeClock
) -> None:
    token = tmp_path / "token"
    token.write_text("first-token\n", encoding="utf-8")
    settings = make_settings(
        tmp_path,
        providers_file=ROOT / "configs" / "providers.yaml",
        model_token_source="file",
        model_token_file=token,
        model_refresh_minutes=30,
    )
    env = {k: v for k, v in MODEL_ENV.items() if k != "GCP_ACCESS_TOKEN"}
    models = build_model_access(
        settings, clock, http=httpx.AsyncClient(), env=env, transport=world.transport()
    )
    assert models is not None
    first = await models.router()
    assert await models.router() is first
    token.write_text("second-token", encoding="utf-8")
    clock.advance(minutes=31)
    second = await models.router()
    assert second is not first
    await models.aclose()


def test_missing_model_settings_fail_closed(tmp_path: Path, clock: FakeClock) -> None:
    settings = make_settings(tmp_path, providers_file=ROOT / "configs" / "providers.yaml")
    models = build_model_access(settings, clock, http=httpx.AsyncClient(), env={})
    assert models is not None
    with pytest.raises(ValueError, match="unset environment variables"):
        asyncio.run(models.router())


async def test_gke_metadata_token_source(world: World, tmp_path: Path, clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def metadata(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": f"ya29.t{len(seen)}", "expires_in": 3599})

    settings = make_settings(
        tmp_path,
        providers_file=ROOT / "configs" / "providers.yaml",
        model_token_source="gke-metadata",
    )
    env = {k: v for k, v in MODEL_ENV.items() if k != "GCP_ACCESS_TOKEN"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(metadata)) as http:
        models = build_model_access(
            settings, clock, http=http, env=env, transport=world.transport()
        )
        assert models is not None
        await models.router()
        clock.advance(minutes=31)
        await models.router()
        await models.aclose()
    assert len(seen) == 2
    assert seen[0].headers["metadata-flavor"] == "Google"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403),
        httpx.Response(200, text="x"),
        httpx.Response(200, json={"access_token": ""}),
    ],
)
async def test_metadata_failures(response: httpx.Response) -> None:
    from patient_access_agents.domain.errors import DependencyUnavailableError
    from patient_access_agents.integrations.gcp import metadata_access_token

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
        with pytest.raises(DependencyUnavailableError):
            await metadata_access_token(http)

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no metadata server", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        with pytest.raises(DependencyUnavailableError):
            await metadata_access_token(http)


def test_file_token_source_requires_a_file(tmp_path: Path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="PAA_MODEL_TOKEN_FILE"):
        make_settings(tmp_path, model_token_source="file")
