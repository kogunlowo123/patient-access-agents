"""Latency budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from agentic_core.policy import PolicyEngine, PolicySet

from patient_access_agents.application.service import PatientAccessService
from patient_access_agents.domain.rules import AccessRules
from patient_access_agents.domain.scheduling import SlotSolver
from patient_access_agents.integrations.x12 import evaluate_271, parse_271
from patient_access_agents.security.access import AccessController
from tests.conftest import PATIENT_ANA, ROOT, SCHEDULER
from tests.fakes import x12_271
from tests.unit.test_scheduling import NOW, request, slot

pytestmark = pytest.mark.performance


def test_solver_over_a_quarter_of_slots() -> None:
    solver = SlotSolver(AccessRules.from_file(ROOT / "configs" / "access-rules.yaml").solver)
    slots = [
        slot(
            f"s{i}",
            NOW + timedelta(minutes=20 * (i // 8)),
            schedule=f"sch{i % 8}",
            practitioner=f"p{i % 8}",
        )
        for i in range(3000)
    ]
    started = time.perf_counter()
    result = solver.solve(slots, request(latest=NOW + timedelta(days=90)))
    elapsed_ms = (time.perf_counter() - started) * 1000
    assert result.considered > 0
    assert elapsed_ms < 1500


def test_271_parse_and_evaluate_budget() -> None:
    text = x12_271(trace="T1", member_id="SHP000123", scenario="active")
    started = time.perf_counter()
    for _ in range(500):
        evaluate_271(parse_271(text), requested=("3", "98"), trace_number="T1", checked_at=NOW)
    per_call_ms = (time.perf_counter() - started) * 1000 / 500
    assert per_call_ms < 5


async def test_policy_decision_budget() -> None:
    access = AccessController(
        PolicyEngine(PolicySet.from_file(ROOT / "policies" / "patient-access.yaml"))
    )
    attrs = {"patient_id": "pat-ana", "status": "slots_proposed", "prior_auth_pending": False}
    started = time.perf_counter()
    for _ in range(1000):
        await access.decide(PATIENT_ANA, "case:read", resource_type="access_case", attributes=attrs)
    per_call_ms = (time.perf_counter() - started) * 1000 / 1000
    assert per_call_ms < 2


async def test_case_read_budget(service: PatientAccessService) -> None:
    case, _ = await service.intake(SCHEDULER, "sr-cardio")
    started = time.perf_counter()
    for _ in range(100):
        await service.get_case(SCHEDULER, case.id)
    per_call_ms = (time.perf_counter() - started) * 1000 / 100
    assert per_call_ms < 30
