"""Full HTTP lifecycle through the ASGI app: REST, MCP and A2A with real bearer tokens."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    AGENT_SCOPES,
    agent_token,
    bearer,
    patient_token,
    staff_token,
)
from tests.fakes import FakeClock, FakeIdp, World

pytestmark = pytest.mark.e2e

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def mcp(client: TestClient, token: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={**MCP_HEADERS, **bearer(token)},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_operations_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.headers["x-content-type-options"] == "nosniff"
    assert ready.headers["x-correlation-id"]
    echoed = client.get("/healthz", headers={"X-Correlation-ID": "abc-123"})
    assert echoed.headers["x-correlation-id"] == "abc-123"
    metrics = client.get("/metrics").text
    assert "paa_http_request_duration_seconds" in metrics


def test_rest_lifecycle_patient_and_staff(
    client: TestClient, user_idp: FakeIdp, world: World, clock: FakeClock
) -> None:
    scheduler = bearer(staff_token(user_idp, "access-scheduler"))
    created = client.post(
        "/v1/referrals", json={"service_request_id": "sr-cardio"}, headers=scheduler
    )
    assert created.status_code == 201, created.text
    case_id = created.json()["case_id"]
    again = client.post(
        "/v1/referrals", json={"service_request_id": "sr-cardio"}, headers=scheduler
    )
    assert again.status_code == 200
    assert again.json()["case_id"] == case_id

    verified = client.post(f"/v1/cases/{case_id}/eligibility", headers=scheduler)
    assert verified.json()["status"] == "eligibility_verified"
    assert verified.json()["eligibility"]["member_id"] == "*****0123"
    assert verified.json()["eligibility"]["benefits"] == []

    ana = bearer(patient_token(user_idp))
    own = client.get(f"/v1/cases/{case_id}", headers=ana).json()
    assert own["eligibility"]["member_id"] == "SHP000123"
    assert own["eligibility"]["benefits"]

    prefs = client.put(
        f"/v1/cases/{case_id}/preferences",
        json={
            "timezone": "America/New_York",
            "availability": [{"weekdays": [1, 2, 3, 4, 5], "start": "08:00:00", "end": "12:00:00"}],
            "modalities": ["in_person"],
            "reminder_channel": "portal",
        },
        headers=ana,
    )
    assert prefs.status_code == 200, prefs.text

    search = client.post(f"/v1/cases/{case_id}/slot-search", headers=ana).json()
    assert search["feasible"] is True
    proposal = search["case"]["proposals"][0]
    assert proposal["modality"] == "in_person"

    booked = client.post(
        f"/v1/cases/{case_id}/booking", json={"proposal_id": proposal["proposal_id"]}, headers=ana
    )
    assert booked.status_code == 200, booked.text
    assert booked.json()["outcome"] == "booked"
    assert booked.json()["case"]["status"] == "scheduled"

    reminders = client.get(f"/v1/cases/{case_id}/reminders", headers=ana).json()["items"]
    assert any(r["kind"] == "appointment_reminder" for r in reminders)

    listed = client.get("/v1/cases", headers=ana).json()["items"]
    assert [c["case_id"] for c in listed] == [case_id]
    staff_list = client.get("/v1/cases?status=scheduled", headers=scheduler).json()["items"]
    assert len(staff_list) == 1


def test_patient_cannot_see_or_touch_another_patients_case(
    client: TestClient, user_idp: FakeIdp
) -> None:
    scheduler = bearer(staff_token(user_idp, "access-scheduler"))
    case_id = client.post(
        "/v1/referrals", json={"service_request_id": "sr-leo"}, headers=scheduler
    ).json()["case_id"]
    intruder = bearer(patient_token(user_idp, patient="pat-ana"))
    assert client.get(f"/v1/cases/{case_id}", headers=intruder).status_code == 404
    assert client.post(f"/v1/cases/{case_id}/slot-search", headers=intruder).status_code == 404
    listed = client.get("/v1/cases?patient_id=pat-leo", headers=intruder).json()["items"]
    assert listed == []
    guardian = bearer(patient_token(user_idp, patient="pat-leo", sub="leo-guardian@mail.test"))
    assert client.get(f"/v1/cases/{case_id}", headers=guardian).status_code == 200


def test_authentication_errors_use_oauth_bodies(client: TestClient, user_idp: FakeIdp) -> None:
    missing = client.get("/v1/cases")
    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["error"] == "unauthorized"
    no_role = client.get("/v1/cases", headers=bearer(user_idp.mint(sub="x", aud="patient-access")))
    assert no_role.status_code == 403
    invalid = client.post(
        "/v1/referrals",
        json={"service_request_id": "bad id!"},
        headers=bearer(staff_token(user_idp, "access-scheduler")),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_request"


def test_escalation_queue_over_http(client: TestClient, user_idp: FakeIdp) -> None:
    navigator = bearer(staff_token(user_idp, "care-navigator", sub="nia@corp.test"))
    case_id = client.post(
        "/v1/referrals", json={"service_request_id": "sr-unknown"}, headers=navigator
    ).json()["case_id"]
    items = client.get(
        "/v1/escalations?queue=care-navigation&status=open", headers=navigator
    ).json()["items"]
    assert items[0]["reason"] == "unroutable_referral"
    escalation = items[0]["escalation_id"]
    claimed = client.post(f"/v1/escalations/{escalation}/claim", headers=navigator)
    assert claimed.json()["status"] == "claimed"
    routed = client.post(
        f"/v1/cases/{case_id}/route", json={"service": "cardiology-consult"}, headers=navigator
    )
    assert routed.status_code == 200
    resolved = client.post(
        f"/v1/escalations/{escalation}/resolve",
        json={"resolution": "already routed"},
        headers=bearer(staff_token(user_idp, "access-supervisor", sub="sue@corp.test")),
    )
    assert resolved.status_code == 409  # routing resolved it already


def test_manual_eligibility_and_close_over_http(
    client: TestClient, user_idp: FakeIdp, world: World
) -> None:
    from tests.fakes import add_referral

    add_referral(world.fhir, "sr-nf", member="SHP000NF")
    scheduler = bearer(staff_token(user_idp, "access-scheduler"))
    case_id = client.post(
        "/v1/referrals", json={"service_request_id": "sr-nf"}, headers=scheduler
    ).json()["case_id"]
    denied = client.post(f"/v1/cases/{case_id}/eligibility", headers=scheduler).json()
    assert denied["status"] == "eligibility_denied"
    assert denied["eligibility"]["rejection_code"] == "75"
    eligibility = bearer(staff_token(user_idp, "eligibility-specialist", sub="eli@corp.test"))
    manual = client.post(
        f"/v1/cases/{case_id}/eligibility/manual",
        json={"active": True, "note": "verified on payer portal", "payer_reference": "PORTAL-77"},
        headers=eligibility,
    )
    assert manual.json()["status"] == "eligibility_verified"
    supervisor = bearer(staff_token(user_idp, "access-supervisor", sub="sue@corp.test"))
    closed = client.post(
        f"/v1/cases/{case_id}/close",
        json={"reason": "patient chose another provider"},
        headers=supervisor,
    )
    assert closed.json()["status"] == "closed"


def test_agent_over_mcp_with_human_approval(
    client: TestClient, agent_idp: FakeIdp, user_idp: FakeIdp
) -> None:
    token = agent_token(agent_idp)
    tools = mcp(client, token, "tools/list", {})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "process_referral",
        "get_case",
        "verify_eligibility",
        "propose_slots",
        "request_booking",
        "list_escalations",
    }
    assert all("outputSchema" in t for t in tools)

    result = mcp(
        client,
        token,
        "tools/call",
        {"name": "process_referral", "arguments": {"service_request_id": "sr-cardio"}},
    )["result"]
    case = result["structuredContent"]
    assert case["status"] == "slots_proposed"

    status = mcp(
        client, token, "tools/call", {"name": "get_case", "arguments": {"case_id": case["case_id"]}}
    )
    assert status["result"]["structuredContent"]["case_id"] == case["case_id"]

    searched = mcp(
        client,
        token,
        "tools/call",
        {"name": "propose_slots", "arguments": {"case_id": case["case_id"]}},
    )["result"]["structuredContent"]
    assert searched["feasible"] is True
    proposal = searched["case"]["proposals"][0]["proposal_id"]

    booking = mcp(
        client,
        token,
        "tools/call",
        {
            "name": "request_booking",
            "arguments": {"case_id": case["case_id"], "proposal_id": proposal},
        },
    )["result"]["structuredContent"]
    assert booking["outcome"] == "pending_approval"
    assert booking["decision"] == "require_approval"

    queue = mcp(
        client,
        token,
        "tools/call",
        {"name": "list_escalations", "arguments": {"queue": "scheduling"}},
    )
    assert queue["result"]["structuredContent"]["items"][0]["reason"] == "booking_approval"

    again = mcp(
        client,
        token,
        "tools/call",
        {"name": "verify_eligibility", "arguments": {"case_id": case["case_id"]}},
    )["result"]
    assert again["isError"] is True
    assert "invalid_state" in again["content"][0]["text"]

    scheduler = bearer(staff_token(user_idp, "access-scheduler"))
    approved = client.post(
        f"/v1/cases/{case['case_id']}/booking/decision",
        json={"approve": True, "note": "confirmed with patient by phone"},
        headers=scheduler,
    )
    assert approved.status_code == 200
    assert approved.json()["case"]["appointment"]["approved_by"] == "sam@corp.test"


def test_mcp_rejects_unauthenticated_and_out_of_scope_agents(
    client: TestClient, agent_idp: FakeIdp
) -> None:
    anonymous = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_case", "arguments": {"case_id": "abc"}},
        },
        headers=MCP_HEADERS,
    ).json()["result"]
    assert anonymous["isError"] is True
    assert "unauthorized" in anonymous["content"][0]["text"]
    narrow = agent_token(agent_idp, frozenset({"case:read"}))
    result = mcp(
        client,
        narrow,
        "tools/call",
        {"name": "process_referral", "arguments": {"service_request_id": "sr-cardio"}},
    )["result"]
    assert result["isError"] is True
    assert "forbidden" in result["content"][0]["text"]


def test_a2a_agent_card_and_message_send(client: TestClient, agent_idp: FakeIdp) -> None:
    card = client.get("/.well-known/agent-card.json").json()
    assert card["url"] == "https://access.corp.test/a2a"
    assert {s["id"] for s in card["skills"]} == {"process-referral", "case-status"}
    assert card["securitySchemes"]["bearer"]["scheme"] == "bearer"
    for field in ("name", "description", "version", "capabilities"):
        assert card[field]

    token = bearer(agent_token(agent_idp))
    message = {
        "kind": "message",
        "messageId": "m-1",
        "role": "user",
        "contextId": "ctx-1",
        "parts": [
            {
                "kind": "data",
                "data": {"skill": "process-referral", "service_request_id": "sr-cardio"},
            }
        ],
    }
    reply = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"message": message}},
        headers=token,
    ).json()
    task = reply["result"]
    assert reply["id"] == 7
    assert task["kind"] == "task"
    assert task["contextId"] == "ctx-1"
    assert task["status"]["state"] == "input-required"
    case = task["artifacts"][0]["parts"][0]["data"]
    assert case["status"] == "slots_proposed"

    text_message = {
        **message,
        "parts": [{"kind": "text", "text": "Please handle ServiceRequest/sr-cardio"}],
    }
    again = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "message/send",
            "params": {"message": text_message},
        },
        headers=token,
    ).json()
    assert again["result"]["artifacts"][0]["parts"][0]["data"]["case_id"] == case["case_id"]

    status = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "message/send",
            "params": {
                "message": {
                    **message,
                    "parts": [
                        {
                            "kind": "data",
                            "data": {"skill": "case-status", "case_id": case["case_id"]},
                        }
                    ],
                }
            },
        },
        headers=token,
    ).json()
    assert status["result"]["status"]["state"] == "input-required"


def test_a2a_protocol_errors(client: TestClient, agent_idp: FakeIdp) -> None:
    token = bearer(agent_token(agent_idp))
    assert client.post("/a2a", content=b"{nope", headers=token).json()["error"]["code"] == -32700
    assert client.post("/a2a", json=[1], headers=token).json()["error"]["code"] == -32600
    unknown = client.post(
        "/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get"}, headers=token
    )
    assert unknown.json()["error"]["code"] == -32601
    no_message = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {}},
        headers=token,
    )
    assert no_message.json()["error"]["code"] == -32602
    no_skill = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "hello"}]}},
        },
        headers=token,
    )
    assert no_skill.json()["error"]["code"] == -32602
    message = {
        "role": "user",
        "parts": [{"kind": "data", "data": {"service_request_id": "sr-draft"}}],
    }
    anonymous = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": message}},
    )
    assert anonymous.status_code == 401
    rejected = client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {"message": message}},
        headers=token,
    ).json()
    assert rejected["result"]["status"]["state"] == "rejected"
    missing = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "message/send",
            "params": {
                "message": {
                    **message,
                    "parts": [{"kind": "data", "data": {"service_request_id": "sr-nowhere"}}],
                }
            },
        },
        headers=token,
    ).json()
    assert missing["result"]["status"]["state"] == "failed"


def test_agent_scopes_are_enforced_over_rest(client: TestClient, agent_idp: FakeIdp) -> None:
    read_only = bearer(agent_token(agent_idp, frozenset({"case:read"})))
    denied = client.post(
        "/v1/referrals", json={"service_request_id": "sr-cardio"}, headers=read_only
    )
    assert denied.status_code == 403
    full = bearer(agent_token(agent_idp, AGENT_SCOPES))
    created = client.post(
        "/v1/referrals/process", json={"service_request_id": "sr-cardio"}, headers=full
    )
    assert created.status_code == 200
    assert created.json()["status"] == "slots_proposed"
    approve = client.post(
        f"/v1/cases/{created.json()['case_id']}/booking/decision",
        json={"approve": True, "note": "self approval"},
        headers=full,
    )
    assert approve.status_code == 403


def test_portal_messages_and_acknowledgement(client: TestClient, user_idp: FakeIdp) -> None:
    scheduler = bearer(staff_token(user_idp, "access-scheduler"))
    case = client.post(
        "/v1/referrals/process", json={"service_request_id": "sr-cardio"}, headers=scheduler
    ).json()
    container = client.app.state.container  # type: ignore[attr-defined]
    client.portal.call(container.service.run_due_work)  # type: ignore[attr-defined]
    ana = bearer(patient_token(user_idp))
    inbox = client.get("/v1/messages", headers=ana).json()["items"]
    assert inbox
    assert inbox[0]["case_id"] == case["case_id"]
    reminders = client.get(f"/v1/cases/{case['case_id']}/reminders", headers=ana).json()["items"]
    sent = next(r for r in reminders if r["status"] == "sent")
    ack = client.post(f"/v1/reminders/{sent['reminder_id']}/acknowledge", headers=ana)
    assert ack.json()["status"] == "acknowledged"
    staff_inbox = client.get("/v1/messages", headers=scheduler)
    assert staff_inbox.status_code == 403
