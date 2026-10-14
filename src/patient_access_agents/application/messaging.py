"""Patient message drafting.

The model drafts a plain-language message *template* from non-identifying
facts (service, visit type, preparation note, language). It never sees the
patient's name, date of birth, contact details or appointment time; those are
filled in locally after the draft passes validation:

- agentic-core ContentGuard on the request and on the output (secrets block,
  PII is redacted; any PII the model invents rejects the draft),
- exactly the placeholders the message kind needs, and no others,
- no URLs, no long digit runs (invented phone numbers), length limit.

Any failure, or no configured model, falls back to a reviewed template in the
patient's language. The composer used is recorded on the reminder for audit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agentic_core.domain.classification import DataClassification, TrustLevel
from agentic_core.domain.errors import ProviderError, ProviderUnavailableError
from agentic_core.models.router import ModelRouter
from agentic_core.models.types import Message, ModelRequest, Role
from agentic_core.observability import get_logger
from agentic_core.security import ContentGuard
from pydantic import BaseModel, ConfigDict

from patient_access_agents.domain.models import Modality, ReminderKind
from patient_access_agents.observability.metrics import AccessMetrics

log = get_logger("patient_access_agents.messaging")

MAX_TEMPLATE_CHARS = 320
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_URL = re.compile(r"(?:https?://|www\.)", re.I)
_DIGITS = re.compile(r"\d{4,}")

REQUIRED: dict[ReminderKind, frozenset[str]] = {
    ReminderKind.APPOINTMENT_REMINDER: frozenset({"first_name", "when", "where"}),
    ReminderKind.SCHEDULING_OUTREACH: frozenset({"first_name"}),
}

TEMPLATES: dict[tuple[str, ReminderKind], str] = {
    (
        "en",
        ReminderKind.APPOINTMENT_REMINDER,
    ): "Hello {first_name}, this is a reminder of your {service} on {when} ({where}). "
    "{preparation}Please confirm in the patient portal or call the access centre "
    "if you need to change it.",
    (
        "en",
        ReminderKind.SCHEDULING_OUTREACH,
    ): "Hello {first_name}, we have received a referral for your {service}. "
    "Please choose an appointment time in the patient portal, or call the access centre "
    "and we will help you book.",
    (
        "es",
        ReminderKind.APPOINTMENT_REMINDER,
    ): "Hola {first_name}, le recordamos su {service} el {when} ({where}). "
    "{preparation}Confirme en el portal del paciente o llame al centro de citas "
    "si necesita cambiarla.",
    (
        "es",
        ReminderKind.SCHEDULING_OUTREACH,
    ): "Hola {first_name}, recibimos una remisión para su {service}. "
    "Elija una cita en el portal del paciente o llame al centro de citas y le ayudaremos.",
}

_DAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "es": ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"],
}
_MONTHS = {
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
    "es": [
        "enero",
        "febrero",
        "marzo",
        "abril",
        "mayo",
        "junio",
        "julio",
        "agosto",
        "septiembre",
        "octubre",
        "noviembre",
        "diciembre",
    ],
}
_VISIT = {
    "en": {Modality.TELEHEALTH: "video visit", Modality.HOME: "home visit"},
    "es": {Modality.TELEHEALTH: "visita por video", Modality.HOME: "visita a domicilio"},
}

SYSTEM_PROMPT = (
    "You write short, friendly patient messages for a hospital access centre. "
    "Write in the requested language at about a sixth-grade reading level. "
    "Return only the message text. Use these placeholders literally and exactly once each: "
    "{placeholders}. Do not add any other braces, names, dates, times, phone numbers, "
    "links or medical advice. At most 300 characters."
)


class MessageContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ReminderKind
    service: str
    modality: Modality
    preparation: str = ""
    language: str = "en"


class Recipient(BaseModel):
    model_config = ConfigDict(frozen=True)

    first_name: str
    appointment_start: datetime | None = None
    location_name: str | None = None
    timezone: str = "UTC"


class ComposedMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    composer: str
    fallback_reason: str | None = None


def language_of(code: str) -> str:
    base = code.split("-", maxsplit=1)[0].lower()
    return base if base in _DAYS else "en"


def format_when(start: datetime, tz: str, language: str) -> str:
    local = start.astimezone(ZoneInfo(tz))
    lang = language_of(language)
    day = _DAYS[lang][local.weekday()]
    month = _MONTHS[lang][local.month - 1]
    if lang == "es":
        return f"{day} {local.day} de {month} a las {local:%H:%M}"
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{day}, {month} {local.day} at {hour}:{local:%M} {suffix}"


def format_where(modality: Modality, location: str | None, language: str) -> str:
    lang = language_of(language)
    if modality is Modality.IN_PERSON:
        return location or ("en la clínica" if lang == "es" else "at the clinic")
    return _VISIT[lang][modality]


def validate_template(text: str, kind: ReminderKind) -> str | None:
    """Return a rejection reason, or None when the draft is acceptable."""
    if not text or len(text) > MAX_TEMPLATE_CHARS:
        return "length"
    found = _PLACEHOLDER.findall(text)
    required = REQUIRED[kind]
    if set(found) != required or len(found) != len(required):
        return "placeholders"
    if text.count("{") != len(found) or text.count("}") != len(found):
        return "braces"
    if _URL.search(text):
        return "url"
    if _DIGITS.search(text):
        return "digits"
    return None


class ModelAccess:
    """Holds the ModelRouter and rebuilds it before short-lived credentials expire.

    Vertex AI access tokens from workload identity last about an hour; the
    router is rebuilt from configuration (re-reading the token) once
    ``refresh_after`` has passed. Without a refresh interval it is built once.
    """

    def __init__(
        self,
        factory: Callable[[], Awaitable[ModelRouter]],
        *,
        clock: Callable[[], datetime],
        refresh_after: timedelta | None = None,
    ) -> None:
        self._factory = factory
        self._clock = clock
        self._refresh_after = refresh_after
        self._router: ModelRouter | None = None
        self._built_at: datetime | None = None

    async def router(self) -> ModelRouter:
        now = self._clock()
        stale = (
            self._refresh_after is not None
            and self._built_at is not None
            and now - self._built_at >= self._refresh_after
        )
        if self._router is None or stale:
            previous = self._router
            self._router = await self._factory()
            self._built_at = now
            if previous is not None:
                await previous.aclose()
        return self._router

    async def aclose(self) -> None:
        if self._router is not None:
            await self._router.aclose()
            self._router = None


class MessageComposer:
    def __init__(
        self,
        *,
        models: ModelAccess | None,
        guard: ContentGuard,
        metrics: AccessMetrics,
    ) -> None:
        self._models = models
        self._guard = guard
        self._metrics = metrics

    async def compose(self, context: MessageContext, recipient: Recipient) -> ComposedMessage:
        lang = language_of(context.language)
        template, composer, reason = await self._draft(context, lang)
        values = {
            "first_name": recipient.first_name,
            "service": context.service,
            "preparation": f"{context.preparation} " if context.preparation else "",
            "when": (
                format_when(recipient.appointment_start, recipient.timezone, lang)
                if recipient.appointment_start
                else ""
            ),
            "where": format_where(context.modality, recipient.location_name, lang),
        }
        self._metrics.composer.labels(composer.split(":")[0], reason or "none").inc()
        return ComposedMessage(
            text=template.format_map(values), composer=composer, fallback_reason=reason
        )

    async def _draft(self, context: MessageContext, lang: str) -> tuple[str, str, str | None]:
        fallback = TEMPLATES[(lang, context.kind)]
        if self._models is None:
            return fallback, "template", "no_model_configured"
        try:
            draft, provider = await self._model_draft(await self._models.router(), context, lang)
        except _DraftRejectedError as exc:
            return fallback, "template", exc.reason
        return draft, f"model:{provider}", None

    async def _model_draft(
        self, router: ModelRouter, context: MessageContext, lang: str
    ) -> tuple[str, str]:
        placeholders = ", ".join(f"{{{p}}}" for p in sorted(REQUIRED[context.kind]))
        facts = {
            "message_type": context.kind.value,
            "language": lang,
            "service": context.service,
            "visit_type": context.modality.value,
            "preparation": context.preparation,
        }
        request = ModelRequest(
            messages=(
                Message(
                    role=Role.SYSTEM,
                    content=SYSTEM_PROMPT.format(placeholders=placeholders),
                    trust=TrustLevel.SYSTEM,
                ),
                Message(role=Role.USER, content=json.dumps(facts), trust=TrustLevel.VERIFIED),
            ),
            max_tokens=300,
            temperature=0.2,
            classification=DataClassification.CONFIDENTIAL,
            task="patient_messaging",
        )
        screened = self._guard.inspect_request(request)
        if screened.blocked or screened.request is None:
            raise _DraftRejectedError("input_guard")
        try:
            routed = await router.generate(screened.request)
        except (ProviderUnavailableError, ProviderError) as exc:
            log.warning("message_model_unavailable", error=exc.code)
            raise _DraftRejectedError("model_unavailable") from exc
        checked = self._guard.inspect_output(routed.response.text.strip())
        if checked.blocked or checked.text is None:
            raise _DraftRejectedError("output_guard")
        if any(f.category == "pii" for f in checked.findings):
            raise _DraftRejectedError("output_pii")
        draft = checked.text.strip()
        problem = validate_template(draft, context.kind)
        if problem is not None:
            raise _DraftRejectedError(f"invalid_{problem}")
        return draft, routed.response.provider


class _DraftRejectedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
