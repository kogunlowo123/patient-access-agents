"""Eligibility verification: FHIR Coverage -> X12 270 -> clearinghouse -> 271 -> decision."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from patient_access_agents.domain.errors import (
    DependencyUnavailableError,
    NotFoundError,
    UpstreamError,
    X12Error,
)
from patient_access_agents.domain.models import (
    CoverageInfo,
    EligibilityOutcome,
    EligibilityResult,
    PatientDemographics,
    Referral,
)
from patient_access_agents.integrations.clearinghouse import ClearinghouseGateway
from patient_access_agents.integrations.fhir import (
    FhirClient,
    coverage_from_resource,
    demographics_from_patient,
)
from patient_access_agents.integrations.x12 import (
    EligibilityInquiry,
    Envelope,
    Person,
    build_270,
    evaluate_271,
    parse_271,
)


class Submitter(BaseModel):
    """Trading-partner identifiers for the 270 envelope and information receiver loop."""

    model_config = ConfigDict(frozen=True)

    sender_id: str = Field(min_length=1, max_length=15)
    receiver_id: str = Field(min_length=1, max_length=15)
    application_sender: str = Field(min_length=2, max_length=15)
    application_receiver: str = Field(min_length=2, max_length=15)
    usage: str = Field(default="P", pattern=r"^[PT]$")
    provider_name: str = Field(min_length=1, max_length=60)
    provider_npi: str = Field(pattern=r"^\d{10}$")
    provider_tax_id: str = Field(pattern=r"^\d{9}$")


class Verification(BaseModel):
    model_config = ConfigDict(frozen=True)

    result: EligibilityResult
    retryable: bool = False
    coverage_id: str | None = None


def _person(demo: PatientDemographics, member_id: str | None = None) -> Person:
    return Person(
        family=demo.family,
        given=demo.given,
        birth_date=date.fromisoformat(demo.birth_date),
        gender=demo.gender,
        member_id=member_id,
    )


def _control_number() -> int:
    return secrets.randbelow(999_999_999) + 1


class EligibilityVerifier:
    def __init__(
        self,
        *,
        fhir: FhirClient,
        gateway: ClearinghouseGateway | None,
        submitter: Submitter | None,
    ) -> None:
        if (gateway is None) != (submitter is None):
            raise ValueError("clearinghouse gateway and submitter identifiers go together")
        self._fhir = fhir
        self._gateway = gateway
        self._submitter = submitter

    async def coverage(self, referral: Referral) -> CoverageInfo | None:
        """The Coverage named on the referral, else the patient's first active Coverage."""
        resources: list[dict[str, Any]] = []
        for reference in referral.insurance:
            try:
                resources.append(await self._fhir.read("Coverage", reference.split("/", 1)[1]))
            except NotFoundError:
                continue
        if not resources:
            matches, _ = await self._fhir.search(
                "Coverage", {"patient": f"Patient/{referral.patient_id}", "status": "active"}
            )
            resources = list(matches)
        for resource in resources:
            if resource.get("status") != "active":
                continue
            info = coverage_from_resource(resource, await self._payer(resource))
            if info.relationship != "self":
                subscriber_ref = resource.get("subscriber")
                ref = subscriber_ref.get("reference") if isinstance(subscriber_ref, dict) else None
                if isinstance(ref, str) and "/" in ref:
                    kind, ident = ref.rsplit("/", 2)[-2:]
                    subscriber = demographics_from_patient(await self._fhir.read(kind, ident))
                    info = info.model_copy(update={"subscriber": subscriber})
            return info
        return None

    async def _payer(self, coverage: Mapping[str, Any]) -> dict[str, Any] | None:
        for payor in coverage.get("payor") or []:
            reference = payor.get("reference") if isinstance(payor, dict) else None
            if isinstance(reference, str) and reference.startswith("Organization/"):
                return await self._fhir.read("Organization", reference.split("/", 1)[1])
        return None

    async def verify(
        self,
        referral: Referral,
        service_types: tuple[str, ...],
        *,
        now: datetime,
        service_date: date,
    ) -> Verification:
        patient = demographics_from_patient(await self._fhir.read("Patient", referral.patient_id))
        coverage = await self.coverage(referral)
        if coverage is None:
            return Verification(
                result=EligibilityResult(
                    outcome=EligibilityOutcome.MANUAL_REQUIRED,
                    checked_at=now,
                    service_types=service_types,
                    source="manual",
                    rejection_reason="no active Coverage on file for the patient",
                )
            )
        if self._gateway is None or self._submitter is None:
            return Verification(
                coverage_id=coverage.coverage_id,
                result=EligibilityResult(
                    outcome=EligibilityOutcome.MANUAL_REQUIRED,
                    checked_at=now,
                    payer_id=coverage.payer_id,
                    member_id=coverage.member_id,
                    service_types=service_types,
                    source="manual",
                    rejection_reason="no clearinghouse configured; verify with the payer directly",
                ),
            )
        trace = f"PAA{secrets.token_hex(6).upper()}"
        dependent = coverage.relationship != "self" and coverage.subscriber is not None
        subscriber_demo = coverage.subscriber if dependent and coverage.subscriber else patient
        inquiry = EligibilityInquiry(
            payer_name=coverage.payer_name,
            payer_id=coverage.payer_id,
            provider_name=self._submitter.provider_name,
            provider_npi=self._submitter.provider_npi,
            provider_tax_id=self._submitter.provider_tax_id,
            subscriber=_person(subscriber_demo, coverage.member_id),
            dependent=_person(patient) if dependent else None,
            group_number=coverage.group_number,
            trace_number=trace,
            service_types=service_types,
            service_date=service_date,
            created_at=now,
        )
        control = _control_number()
        envelope = Envelope(
            sender_id=self._submitter.sender_id,
            receiver_id=self._submitter.receiver_id,
            application_sender=self._submitter.application_sender,
            application_receiver=self._submitter.application_receiver,
            usage=self._submitter.usage,
            interchange_control=control,
            group_control=control,
        )
        try:
            interchange = build_270(inquiry, envelope)
        except X12Error as exc:
            # Our data cannot be encoded safely (e.g. a member id containing
            # delimiters); a person must check the coverage record.
            return Verification(
                coverage_id=coverage.coverage_id,
                result=EligibilityResult(
                    outcome=EligibilityOutcome.MANUAL_REQUIRED,
                    checked_at=now,
                    payer_id=coverage.payer_id,
                    service_types=service_types,
                    source="manual",
                    rejection_reason=(
                        f"inquiry cannot be built from the coverage record: {exc.message}"
                    ),
                ),
            )
        try:
            response = parse_271(await self._gateway.exchange(interchange))
            result = evaluate_271(
                response, requested=service_types, trace_number=trace, checked_at=now
            )
        except DependencyUnavailableError as exc:
            return Verification(
                coverage_id=coverage.coverage_id,
                retryable=True,
                result=EligibilityResult(
                    outcome=EligibilityOutcome.UNAVAILABLE,
                    checked_at=now,
                    payer_id=coverage.payer_id,
                    member_id=coverage.member_id,
                    trace_number=trace,
                    service_types=service_types,
                    source="unavailable",
                    note=exc.message,
                ),
            )
        except (X12Error, UpstreamError) as exc:
            return Verification(
                coverage_id=coverage.coverage_id,
                retryable=False,
                result=EligibilityResult(
                    outcome=EligibilityOutcome.UNAVAILABLE,
                    checked_at=now,
                    payer_id=coverage.payer_id,
                    member_id=coverage.member_id,
                    trace_number=trace,
                    service_types=service_types,
                    source="unavailable",
                    note=exc.message,
                ),
            )
        return Verification(
            coverage_id=coverage.coverage_id,
            retryable=result.outcome is EligibilityOutcome.UNAVAILABLE,
            result=result,
        )
