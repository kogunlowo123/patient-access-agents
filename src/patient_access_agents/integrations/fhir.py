"""FHIR R4 REST client and resource mapping for patient access.

Wire behaviour: ``application/fhir+json``; searchset bundles are paged by
following ``link[relation=next]`` (bounded); ``_include`` resources are
separated from matches by ``entry.search.mode``. Writes use optimistic
concurrency (``If-Match`` with the resource's version) and conditional
create (``If-None-Exist``) so retries never double-book.

Mapping functions turn ServiceRequest, Patient, Coverage, Schedule and Slot
resources into domain objects and build the Appointment resource. Only
fields the access workflow needs are read; clinical narrative is ignored.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from patient_access_agents.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    UpstreamError,
)
from patient_access_agents.domain.models import (
    AppointmentRef,
    Coding,
    CoverageInfo,
    Modality,
    PatientDemographics,
    Priority,
    Referral,
    SlotCandidate,
    SlotProposal,
    fhir_id,
)

FHIR_JSON = "application/fhir+json"
ACT_CODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
APPOINTMENT_IDENTIFIER = "https://github.com/kogunlowo123/patient-access-agents/appointment"
MODALITY_CODES = {"AMB": Modality.IN_PERSON, "VR": Modality.TELEHEALTH, "HH": Modality.HOME}
ORDER_INTENTS = frozenset(
    {"order", "original-order", "reflex-order", "filler-order", "instance-order", "plan"}
)

TokenProvider = Callable[[], Awaitable[str | None]]


class FhirClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient,
        token_provider: TokenProvider | None = None,
        max_pages: int = 20,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._origin = urlsplit(self._base)
        self._http = http
        self._token = token_provider
        self._max_pages = max_pages

    async def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": FHIR_JSON}
        if self._token is not None:
            token = await self._token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        headers.update(extra or {})
        return headers

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | Sequence[str]] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return await self._http.request(
                method,
                url,
                params=params,
                json=json,
                headers=await self._headers(headers),
            )
        except httpx.TimeoutException as exc:
            raise DependencyUnavailableError("FHIR server timed out") from exc
        except httpx.TransportError as exc:
            raise DependencyUnavailableError(
                f"FHIR server unreachable: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _body(response: httpx.Response, what: str) -> dict[str, Any]:
        if response.status_code in {404, 410}:
            raise NotFoundError(f"{what} not found")
        if response.status_code in {409, 412}:
            raise ConflictError(f"{what}: version conflict")
        if response.status_code >= 500:
            raise UpstreamError(f"FHIR server error {response.status_code} for {what}")
        if response.status_code >= 400:
            raise UpstreamError(f"FHIR request for {what} rejected: {_outcome_text(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamError(f"FHIR response for {what} is not JSON") from exc
        if not isinstance(body, dict):
            raise UpstreamError(f"FHIR response for {what} is not a resource")
        return body

    async def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        fhir_id(resource_id)
        response = await self._send("GET", f"{self._base}/{resource_type}/{resource_id}")
        body = self._body(response, f"{resource_type}/{resource_id}")
        if body.get("resourceType") != resource_type:
            raise UpstreamError(f"expected {resource_type}, got {body.get('resourceType')}")
        return body

    async def search(
        self, resource_type: str, params: Mapping[str, str | Sequence[str]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return (matches, included) across all pages."""
        matches: list[dict[str, Any]] = []
        included: list[dict[str, Any]] = []
        url: str | None = f"{self._base}/{resource_type}"
        query: Mapping[str, str | Sequence[str]] | None = params
        for _ in range(self._max_pages):
            if url is None:
                break
            response = await self._send("GET", url, params=query)
            bundle = self._body(response, f"{resource_type} search")
            if bundle.get("resourceType") != "Bundle" or bundle.get("type") != "searchset":
                raise UpstreamError("search did not return a searchset Bundle")
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") if isinstance(entry, dict) else None
                if not isinstance(resource, dict):
                    continue
                mode = (entry.get("search") or {}).get("mode", "match")
                (included if mode == "include" else matches).append(resource)
            url = self._next_link(bundle)
            query = None
        else:
            if url is not None:
                raise UpstreamError(f"{resource_type} search exceeded {self._max_pages} pages")
        return matches, included

    def _next_link(self, bundle: Mapping[str, Any]) -> str | None:
        for link in bundle.get("link", []) or []:
            if isinstance(link, dict) and link.get("relation") == "next" and link.get("url"):
                target = urlsplit(str(link["url"]))
                # Never follow a paging link off the configured server.
                if (target.scheme, target.netloc) != (self._origin.scheme, self._origin.netloc):
                    raise UpstreamError("paging link points outside the FHIR server")
                return str(link["url"])
        return None

    async def update(
        self, resource: Mapping[str, Any], *, version_id: str | None
    ) -> dict[str, Any]:
        resource_type, resource_id = str(resource["resourceType"]), str(resource["id"])
        headers = {"Content-Type": FHIR_JSON}
        if version_id is not None:
            headers["If-Match"] = f'W/"{version_id}"'
        response = await self._send(
            "PUT", f"{self._base}/{resource_type}/{resource_id}", json=resource, headers=headers
        )
        return self._body(response, f"{resource_type}/{resource_id}")

    async def create(
        self, resource: Mapping[str, Any], *, if_none_exist: str | None = None
    ) -> dict[str, Any]:
        resource_type = str(resource["resourceType"])
        headers = {"Content-Type": FHIR_JSON, "Prefer": "return=representation"}
        if if_none_exist:
            headers["If-None-Exist"] = if_none_exist
        response = await self._send(
            "POST", f"{self._base}/{resource_type}", json=resource, headers=headers
        )
        return self._body(response, f"create {resource_type}")


def _outcome_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict) and body.get("resourceType") == "OperationOutcome":
        issues = body.get("issue") or []
        texts = [
            str(i.get("diagnostics") or (i.get("details") or {}).get("text") or i.get("code"))
            for i in issues
            if isinstance(i, dict)
        ]
        return "; ".join(texts)[:300] or f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}"


# -------------------------------------------------------------------- mapping


def _codings(concept: Any) -> list[Coding]:
    if not isinstance(concept, dict):
        return []
    return [
        Coding(system=c.get("system"), code=str(c["code"]), display=c.get("display"))
        for c in concept.get("coding", []) or []
        if isinstance(c, dict) and c.get("code")
    ]


def _reference_id(reference: Any, resource_type: str) -> str | None:
    if not isinstance(reference, dict):
        return None
    value = reference.get("reference")
    if not isinstance(value, str):
        return None
    parts = value.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2] == resource_type:
        return fhir_id(parts[-1])
    return None


def _reference(reference: Any) -> str | None:
    if isinstance(reference, dict) and isinstance(reference.get("reference"), str):
        parts = str(reference["reference"]).rstrip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{fhir_id(parts[-1])}"
    return None


def parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def referral_from_service_request(resource: Mapping[str, Any]) -> Referral:
    if resource.get("resourceType") != "ServiceRequest":
        raise UpstreamError("resource is not a ServiceRequest")
    patient_id = _reference_id(resource.get("subject"), "Patient")
    if patient_id is None:
        raise UpstreamError("ServiceRequest.subject must reference a Patient")
    codes = [
        *_codings(resource.get("code")),
        *_codings(resource.get("performerType")),
        *(c for concept in resource.get("category", []) or [] for c in _codings(concept)),
    ]
    period = resource.get("occurrencePeriod") or {}
    priority = resource.get("priority") or "routine"
    return Referral(
        service_request_id=fhir_id(str(resource.get("id"))),
        patient_id=patient_id,
        status=str(resource.get("status")),
        intent=str(resource.get("intent")),
        priority=Priority(priority) if priority in set(Priority) else Priority.ROUTINE,
        codes=tuple(codes),
        requester=_reference(resource.get("requester")),
        performers=tuple(
            r for r in (_reference(p) for p in resource.get("performer", []) or []) if r
        ),
        locations=tuple(
            r
            for r in (_reference(p) for p in resource.get("locationReference", []) or [])
            if r and r.startswith("Location/")
        ),
        authored_on=parse_instant(resource.get("authoredOn")),
        desired_start=parse_instant(period.get("start")),
        desired_end=parse_instant(period.get("end")),
        insurance=tuple(
            r
            for r in (_reference(p) for p in resource.get("insurance", []) or [])
            if r and r.startswith("Coverage/")
        ),
    )


def demographics_from_patient(resource: Mapping[str, Any]) -> PatientDemographics:
    if resource.get("resourceType") not in {"Patient", "RelatedPerson"}:
        raise UpstreamError("resource is not a Patient or RelatedPerson")
    names = [n for n in resource.get("name", []) or [] if isinstance(n, dict)]
    official = next((n for n in names if n.get("use") == "official"), names[0] if names else {})
    given = " ".join(official.get("given", []) or [])
    family = official.get("family") or ""
    if not family or not given or not resource.get("birthDate"):
        raise UpstreamError(f"{resource.get('resourceType')} lacks name or birth date")
    telecom = [t for t in resource.get("telecom", []) or [] if isinstance(t, dict)]
    phone = next((t.get("value") for t in telecom if t.get("system") in {"sms", "phone"}), None)
    email = next((t.get("value") for t in telecom if t.get("system") == "email"), None)
    comms = [c for c in resource.get("communication", []) or [] if isinstance(c, dict)]
    chosen = next((c for c in comms if c.get("preferred")), comms[0] if comms else None)
    languages = _codings(chosen.get("language")) if chosen else []
    language = languages[0].code.split("-")[0].lower() if languages else "en"
    return PatientDemographics(
        patient_id=fhir_id(str(resource.get("id"))),
        family=str(family),
        given=given,
        birth_date=str(resource["birthDate"]),
        gender=str(resource.get("gender") or "unknown"),
        phone=phone,
        email=email,
        language=language,
    )


def coverage_from_resource(
    resource: Mapping[str, Any], payer: Mapping[str, Any] | None
) -> CoverageInfo:
    if resource.get("resourceType") != "Coverage":
        raise UpstreamError("resource is not a Coverage")
    member = resource.get("subscriberId")
    if not member:
        member = next(
            (
                i.get("value")
                for i in resource.get("identifier", []) or []
                if isinstance(i, dict) and any(c.code == "MB" for c in _codings(i.get("type")))
            ),
            None,
        )
    if not member:
        raise UpstreamError("Coverage has no member identifier")
    payer_id = payer_name = None
    if payer is not None:
        payer_name = payer.get("name")
        payer_id = next(
            (
                i.get("value")
                for i in payer.get("identifier", []) or []
                if isinstance(i, dict) and i.get("value")
            ),
            None,
        )
    if not payer_id or not payer_name:
        raise UpstreamError("Coverage payor Organization lacks a name or payer identifier")
    relationship = next((c.code for c in _codings(resource.get("relationship"))), "self")
    group = next(
        (
            c.get("value")
            for c in resource.get("class", []) or []
            if isinstance(c, dict) and any(t.code == "group" for t in _codings(c.get("type")))
        ),
        None,
    )
    return CoverageInfo(
        coverage_id=fhir_id(str(resource.get("id"))),
        status=str(resource.get("status")),
        member_id=str(member),
        payer_id=str(payer_id),
        payer_name=str(payer_name),
        relationship=relationship,
        group_number=group,
    )


def modality_of(*concepts: Any) -> Modality:
    for concept in concepts:
        items = concept if isinstance(concept, list) else [concept]
        for item in items:
            for coding in _codings(item):
                if coding.system == ACT_CODE and coding.code in MODALITY_CODES:
                    return MODALITY_CODES[coding.code]
    return Modality.IN_PERSON


def display_name(resource: Mapping[str, Any]) -> str | None:
    """Human-readable name of a Practitioner or Location."""
    if resource.get("resourceType") == "Location":
        name = resource.get("name")
        return str(name) if name else None
    for entry in resource.get("name", []) or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("text"):
            return str(entry["text"])
        parts = [*(entry.get("prefix") or []), *(entry.get("given") or [])]
        if entry.get("family"):
            parts.append(str(entry["family"]))
        if parts:
            return " ".join(str(p) for p in parts)
    return None


def slots_from_bundle(
    slots: Sequence[Mapping[str, Any]], included: Sequence[Mapping[str, Any]]
) -> list[SlotCandidate]:
    schedules = {str(r.get("id")): r for r in included if r.get("resourceType") == "Schedule"}
    names = {
        f"{r.get('resourceType')}/{r.get('id')}": display_name(r)
        for r in included
        if r.get("resourceType") in {"Practitioner", "Location"}
    }
    candidates: list[SlotCandidate] = []
    for slot in slots:
        if slot.get("resourceType") != "Slot":
            continue
        schedule_id = _reference_id(slot.get("schedule"), "Schedule")
        start, end = parse_instant(slot.get("start")), parse_instant(slot.get("end"))
        if schedule_id is None or start is None or end is None:
            continue
        schedule = schedules.get(schedule_id, {})
        if schedule and schedule.get("active") is False:
            continue
        actors = schedule.get("actor", []) or []
        practitioners = tuple(
            sorted(p for p in (_reference_id(a, "Practitioner") for a in actors) if p)
        )
        location = next(
            (loc for loc in (_reference_id(a, "Location") for a in actors) if loc), None
        )
        candidates.append(
            SlotCandidate(
                slot_id=fhir_id(str(slot.get("id"))),
                schedule_id=schedule_id,
                start=start,
                end=end,
                status=str(slot.get("status")),
                practitioner_ids=practitioners,
                location_id=location,
                location_name=names.get(f"Location/{location}") if location else None,
                practitioner_names=tuple(
                    n for n in (names.get(f"Practitioner/{p}") for p in practitioners) if n
                ),
                modality=modality_of(
                    slot.get("serviceCategory"),
                    slot.get("serviceType"),
                    schedule.get("serviceType"),
                    schedule.get("serviceCategory"),
                ),
                version_id=(slot.get("meta") or {}).get("versionId"),
            )
        )
    return candidates


def appointment_resource(
    *,
    case_id: str,
    referral: Referral,
    proposal: SlotProposal,
    service_display: str,
) -> dict[str, Any]:
    participants: list[dict[str, Any]] = [
        {
            "actor": {"reference": f"Patient/{referral.patient_id}"},
            "required": "required",
            "status": "accepted",
        }
    ]
    participants.extend(
        {
            "actor": {"reference": f"Practitioner/{pid}"},
            "required": "required",
            "status": "accepted",
        }
        for pid in proposal.practitioner_ids
    )
    if proposal.location_id:
        participants.append(
            {
                "actor": {"reference": f"Location/{proposal.location_id}"},
                "required": "required",
                "status": "accepted",
            }
        )
    modality_code = next(k for k, v in MODALITY_CODES.items() if v is proposal.modality)
    return {
        "resourceType": "Appointment",
        "identifier": [{"system": APPOINTMENT_IDENTIFIER, "value": case_id}],
        "status": "booked",
        "serviceCategory": [{"coding": [{"system": ACT_CODE, "code": modality_code}]}],
        "description": service_display,
        "start": proposal.start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end": proposal.end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "minutesDuration": int((proposal.end - proposal.start).total_seconds() // 60),
        "slot": [{"reference": f"Slot/{sid}"} for sid in proposal.slot_ids],
        "basedOn": [{"reference": f"ServiceRequest/{referral.service_request_id}"}],
        "participant": participants,
    }


def appointment_ref(
    resource: Mapping[str, Any],
    proposal: SlotProposal,
    *,
    booked_by: str,
    approved_by: str | None,
    booked_at: datetime,
) -> AppointmentRef:
    return AppointmentRef(
        appointment_id=fhir_id(str(resource.get("id"))),
        slot_ids=proposal.slot_ids,
        start=proposal.start,
        end=proposal.end,
        practitioner_ids=proposal.practitioner_ids,
        location_id=proposal.location_id,
        location_name=proposal.location_name,
        modality=proposal.modality,
        booked_by=booked_by,
        approved_by=approved_by,
        booked_at=booked_at,
    )
