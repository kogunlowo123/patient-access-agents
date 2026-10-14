"""Adapters against recorded wire shapes: FHIR R4, clearinghouse, messaging, identity fabric."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from agentic_core.domain.classification import DataClassification, Decision

from patient_access_agents.domain.errors import (
    ConflictError,
    DependencyUnavailableError,
    NotFoundError,
    UnauthorizedError,
    UpstreamError,
)
from patient_access_agents.domain.models import Modality, Priority
from patient_access_agents.integrations.clearinghouse import ClearinghouseGateway
from patient_access_agents.integrations.fhir import (
    FhirClient,
    coverage_from_resource,
    demographics_from_patient,
    display_name,
    modality_of,
    parse_instant,
    referral_from_service_request,
    slots_from_bundle,
)
from patient_access_agents.integrations.identity_fabric import FabricClient
from patient_access_agents.integrations.notifications import DeliveryError, WebhookNotifier
from tests.fakes import FHIR_BASE, FakeClock, World


def client_for(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFhirClient:
    async def test_search_follows_paging_and_separates_includes(self, world: World) -> None:
        world.fhir.page_size = 30
        async with client_for(world.handle) as http:
            fhir = FhirClient(FHIR_BASE, http=http)
            slots, included = await fhir.search(
                "Slot",
                {
                    "status": "free",
                    "_include": "Slot:schedule",
                    "_include:iterate": "Schedule:actor",
                },
            )
        total = len(world.fhir.of_type("Slot"))
        assert len(slots) == total
        assert len([r for r in world.fhir.requests if r.url.path.endswith("/Slot")]) == -(
            -total // 30
        )
        assert {r["resourceType"] for r in included} == {"Schedule", "Practitioner", "Location"}

    async def test_paging_is_bounded_and_stays_on_server(self, world: World) -> None:
        world.fhir.page_size = 5
        async with client_for(world.handle) as http:
            with pytest.raises(UpstreamError, match="exceeded 2 pages"):
                await FhirClient(FHIR_BASE, http=http, max_pages=2).search("Slot", {})

        def foreign(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "type": "searchset",
                    "entry": [],
                    "link": [{"relation": "next", "url": "https://evil.test/fhir/Slot?page=2"}],
                },
            )

        async with client_for(foreign) as http:
            with pytest.raises(UpstreamError, match="outside the FHIR server"):
                await FhirClient(FHIR_BASE, http=http).search("Slot", {})

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (404, {}, NotFoundError),
            (410, {}, NotFoundError),
            (412, {}, ConflictError),
            (500, {}, UpstreamError),
            (
                400,
                {"resourceType": "OperationOutcome", "issue": [{"diagnostics": "bad search"}]},
                UpstreamError,
            ),
            (200, [1, 2], UpstreamError),
            (200, {"resourceType": "Bundle", "type": "batch"}, UpstreamError),
        ],
    )
    async def test_error_mapping(self, status: int, body: Any, error: type[Exception]) -> None:
        async with client_for(lambda r: httpx.Response(status, json=body)) as http:
            with pytest.raises(error):
                await FhirClient(FHIR_BASE, http=http).search("Slot", {})

    async def test_transport_failures_and_bad_bodies(self) -> None:
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        for handler in (timeout, broken):
            async with client_for(handler) as http:
                with pytest.raises(DependencyUnavailableError):
                    await FhirClient(FHIR_BASE, http=http).read("Patient", "p1")
        async with client_for(lambda r: httpx.Response(200, text="<html>")) as http:
            with pytest.raises(UpstreamError, match="not JSON"):
                await FhirClient(FHIR_BASE, http=http).read("Patient", "p1")
        async with client_for(lambda r: httpx.Response(400, text="nope")) as http:
            with pytest.raises(UpstreamError, match="HTTP 400"):
                await FhirClient(FHIR_BASE, http=http).read("Patient", "p1")
        async with client_for(lambda r: httpx.Response(400, json={"x": 1})) as http:
            with pytest.raises(UpstreamError, match="HTTP 400"):
                await FhirClient(FHIR_BASE, http=http).read("Patient", "p1")
        async with client_for(
            lambda r: httpx.Response(200, json={"resourceType": "Group"})
        ) as http:
            with pytest.raises(UpstreamError, match="expected Patient"):
                await FhirClient(FHIR_BASE, http=http).read("Patient", "p1")
        with pytest.raises(ValueError, match="invalid FHIR id"):
            async with client_for(lambda r: httpx.Response(200)) as http:
                await FhirClient(FHIR_BASE, http=http).read("Patient", "../admin")

    async def test_writes_use_concurrency_headers_and_bearer(self, world: World) -> None:
        async def token() -> str | None:
            return "fhir-credential"

        async with client_for(world.handle) as http:
            fhir = FhirClient(FHIR_BASE, http=http, token_provider=token)
            slot = await fhir.read("Slot", "sch-lee-1015-0900")
            await fhir.update({**slot, "status": "busy"}, version_id=slot["meta"]["versionId"])
            with pytest.raises(ConflictError):
                await fhir.update({**slot, "status": "free"}, version_id=slot["meta"]["versionId"])
            unconditional = await fhir.update({**slot, "status": "free"}, version_id=None)
            assert unconditional["status"] == "free"
            created = await fhir.create(
                {"resourceType": "Appointment", "identifier": [{"system": "s", "value": "v"}]},
                if_none_exist="identifier=s|v",
            )
            again = await fhir.create(
                {"resourceType": "Appointment", "identifier": [{"system": "s", "value": "v"}]},
                if_none_exist="identifier=s|v",
            )
        assert again["id"] == created["id"]
        sent = world.fhir.requests
        assert all(r.headers["authorization"] == "Bearer fhir-credential" for r in sent)
        assert sent[1].headers["if-match"] == 'W/"1"'
        assert sent[-1].headers["if-none-exist"] == "identifier=s|v"
        assert sent[-1].headers["content-type"] == "application/fhir+json"


class TestMapping:
    def test_service_request(self, world: World) -> None:
        resource = dict(world.fhir.resources[("ServiceRequest", "sr-cardio")])
        resource.update(
            {
                "priority": "bogus",
                "performer": [{"reference": "Practitioner/prac-lee"}, {"display": "no ref"}],
                "locationReference": [{"reference": "Location/loc-main"}],
                "performerType": {
                    "coding": [{"system": "http://snomed.info/sct", "code": "17561000"}]
                },
                "category": [{"coding": [{"code": "referral"}]}],
                "occurrencePeriod": {"start": "2026-10-20T00:00:00Z", "end": "2026-11-20"},
            }
        )
        referral = referral_from_service_request(resource)
        assert referral.priority is Priority.ROUTINE
        assert referral.practitioner_ids == ("prac-lee",)
        assert referral.location_ids == ("loc-main",)
        assert [c.token for c in referral.codes][1:] == [
            "http://snomed.info/sct|17561000",
            "referral",
        ]
        assert referral.desired_end == datetime(2026, 11, 20, tzinfo=UTC)
        with pytest.raises(UpstreamError):
            referral_from_service_request({"resourceType": "Patient"})
        with pytest.raises(UpstreamError, match="subject"):
            referral_from_service_request({**resource, "subject": {"reference": "Group/g1"}})

    def test_patient_and_coverage(self, world: World) -> None:
        demo = demographics_from_patient(world.fhir.resources[("Patient", "pat-ana")])
        assert (demo.given, demo.family, demo.phone, demo.email) == (
            "Ana Lucia",
            "Rivera",
            "+1-555-0100",
            "ana.rivera@mail.test",
        )
        with pytest.raises(UpstreamError, match="lacks name"):
            demographics_from_patient({"resourceType": "Patient", "id": "x", "name": []})
        with pytest.raises(UpstreamError):
            demographics_from_patient({"resourceType": "Observation"})
        payer = world.fhir.resources[("Organization", "org-shp")]
        coverage = world.fhir.resources[("Coverage", "cov-ana")]
        info = coverage_from_resource(coverage, payer)
        assert (info.member_id, info.payer_id, info.group_number) == (
            "SHP000123",
            "SHP01",
            "GRP100",
        )
        by_identifier = {
            **{k: v for k, v in coverage.items() if k != "subscriberId"},
            "identifier": [{"type": {"coding": [{"code": "MB"}]}, "value": "MEMBER9"}],
        }
        assert coverage_from_resource(by_identifier, payer).member_id == "MEMBER9"
        with pytest.raises(UpstreamError, match="member identifier"):
            coverage_from_resource({"resourceType": "Coverage", "id": "c"}, payer)
        with pytest.raises(UpstreamError, match="payer identifier"):
            coverage_from_resource(coverage, None)
        with pytest.raises(UpstreamError):
            coverage_from_resource({"resourceType": "Account"}, payer)

    def test_slots_names_and_modalities(self, world: World) -> None:
        schedule = world.fhir.resources[("Schedule", "sch-lee")]
        inactive = {**world.fhir.resources[("Schedule", "sch-okafor")], "active": False}
        slots = [
            world.fhir.resources[("Slot", "sch-lee-1015-0900")],
            world.fhir.resources[("Slot", "sch-okafor-1015-1400")],
            {"resourceType": "Slot", "id": "bad", "schedule": {"reference": "Schedule/sch-lee"}},
            {"resourceType": "Bundle"},
        ]
        included = [
            schedule,
            inactive,
            world.fhir.resources[("Practitioner", "prac-lee")],
            world.fhir.resources[("Location", "loc-main")],
        ]
        candidates = slots_from_bundle(slots, included)
        assert len(candidates) == 1
        assert candidates[0].practitioner_names == ("Dr. Morgan Lee",)
        assert candidates[0].location_name == "Riverbend Cardiology Clinic"
        assert candidates[0].version_id == "1"
        assert display_name({"resourceType": "Practitioner", "name": ["x", {}]}) is None
        assert display_name({"resourceType": "Location"}) is None
        video = {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "VR"}]
        }
        assert modality_of(None, [video]) is Modality.TELEHEALTH
        assert modality_of({"coding": [{"code": "VR"}]}) is Modality.IN_PERSON
        assert parse_instant("2026-10-14") == datetime(2026, 10, 14, tzinfo=UTC)
        assert parse_instant("not a date") is None
        assert parse_instant(None) is None


class TestClearinghouse:
    async def test_posts_x12_and_maps_errors(self) -> None:
        seen: list[httpx.Request] = []

        def ok(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="ISA...")

        async with client_for(ok) as http:
            reply = await ClearinghouseGateway(
                "https://ch.test/270", http=http, api_key="k"
            ).exchange("ISA*")
        assert reply == "ISA..."
        assert seen[0].headers["content-type"] == "application/EDI-X12"
        assert seen[0].headers["authorization"] == "Bearer k"
        for status, error in (
            (429, DependencyUnavailableError),
            (503, DependencyUnavailableError),
            (401, UpstreamError),
        ):
            async with client_for(lambda r, s=status: httpx.Response(s)) as http:
                with pytest.raises(error):
                    await ClearinghouseGateway("https://ch.test/270", http=http).exchange("ISA*")

        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("t", request=request)

        def refused(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("r", request=request)

        for handler in (timeout, refused):
            async with client_for(handler) as http:
                with pytest.raises(DependencyUnavailableError):
                    await ClearinghouseGateway("https://ch.test/270", http=http).exchange("ISA*")


class TestNotifier:
    async def test_failures_are_classified(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            WebhookNotifier("https://m.test", http=httpx.AsyncClient(), signing_key=b"short")
        for status, retryable in ((500, True), (429, True), (400, False)):
            async with client_for(lambda r, s=status: httpx.Response(s)) as http:
                notifier = WebhookNotifier("https://m.test", http=http, signing_key=b"k" * 32)
                with pytest.raises(DeliveryError) as caught:
                    await notifier.send(reminder_id="r", channel="sms", recipient="+1", body="hi")
                assert caught.value.retryable is retryable

        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        async with client_for(down) as http:
            notifier = WebhookNotifier("https://m.test", http=http, signing_key=b"k" * 32)
            with pytest.raises(DeliveryError) as caught:
                await notifier.send(reminder_id="r", channel="sms", recipient="+1", body="hi")
            assert caught.value.retryable


class TestFabricClient:
    async def test_token_cache_and_refresh(self, world: World, clock: FakeClock) -> None:
        async with client_for(world.handle) as http:
            fabric = FabricClient(
                "https://identity.corp.test",
                http=http,
                clock=clock.now,
                client_id="spiffe://agents.corp.test/agent/access/service",
                workload_token=lambda: "workload-token-value",
            )
            assert await fabric.token(["fhir:read:*"]) == "fhir-credential"
            assert await fabric.token(["fhir:read:*"]) == "fhir-credential"
            assert world.fabric.calls.count("/oauth2/token") == 1
            clock.advance(minutes=10)
            await fabric.token(["fhir:read:*"])
            assert world.fabric.calls.count("/oauth2/token") == 2

            refused = FabricClient(
                "https://identity.corp.test",
                http=http,
                clock=clock.now,
                client_id="spiffe://x/agent/a/b",
                workload_token=lambda: "wrong",
            )
            with pytest.raises(UnauthorizedError, match="invalid_grant"):
                await refused.token(["fhir:read:*"])
            anonymous = FabricClient("https://identity.corp.test", http=http, clock=clock.now)
            with pytest.raises(UnauthorizedError, match="no workload identity"):
                await anonymous.token([])

    async def test_introspect_and_authorize(self, world: World, clock: FakeClock) -> None:
        world.fabric.issue("agent-cred", "spiffe://a/agent/x/y", "case:read case:create")
        async with client_for(world.handle) as http:
            fabric = FabricClient("https://identity.corp.test", http=http, clock=clock.now)
            info = await fabric.introspect("agent-cred")
            assert info.active
            assert info.scopes == {"case:read", "case:create"}
            assert not (await fabric.introspect("unknown")).active
            decision = await fabric.authorize(
                "agent-cred",
                action="case:read",
                resource_type="access_case",
                resource_id="c1",
                classification=DataClassification.RESTRICTED,
                attributes={},
                environment={},
            )
            assert decision.decision is Decision.ALLOW
            with pytest.raises(UnauthorizedError):
                await fabric.authorize(
                    "unknown",
                    action="case:read",
                    resource_type="access_case",
                    resource_id="c1",
                    classification=DataClassification.RESTRICTED,
                    attributes={},
                    environment={},
                )

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(500),
            httpx.Response(200, text="nope"),
            httpx.Response(200, json=[1]),
            httpx.Response(200, json={"decision": "maybe"}),
            httpx.Response(418, json={"error": "teapot"}),
        ],
    )
    async def test_fabric_failures_fail_closed(
        self, response: httpx.Response, clock: FakeClock
    ) -> None:
        async with client_for(lambda r: response) as http:
            fabric = FabricClient("https://identity.corp.test", http=http, clock=clock.now)
            with pytest.raises(DependencyUnavailableError):
                await fabric.authorize(
                    "t",
                    action="a",
                    resource_type="r",
                    resource_id="i",
                    classification=DataClassification.RESTRICTED,
                    attributes={},
                    environment={},
                )

    async def test_unreachable_or_malformed_introspection(self, clock: FakeClock) -> None:
        def down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        async with client_for(down) as http:
            with pytest.raises(DependencyUnavailableError):
                await FabricClient("https://f.test", http=http, clock=clock.now).introspect("t")
        async with client_for(lambda r: httpx.Response(200, json={"active": "perhaps"})) as http:
            with pytest.raises(DependencyUnavailableError):
                await FabricClient("https://f.test", http=http, clock=clock.now).introspect("t")
