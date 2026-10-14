"""Constraint solver, reminder timing and operating rules."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from patient_access_agents.domain.models import AvailabilityWindow, Modality, SlotCandidate
from patient_access_agents.domain.rules import AccessRules, QuietHours, ReminderRules
from patient_access_agents.domain.scheduling import (
    Constraint,
    SchedulingRequest,
    SlotSolver,
    build_options,
    within_availability,
)
from patient_access_agents.domain.timing import (
    Shift,
    next_retry,
    outside_quiet_hours,
    plan_appointment_reminders,
    sla_elapsed_fraction,
)
from tests.conftest import ROOT

NY = ZoneInfo("America/New_York")
RULES = AccessRules.from_file(ROOT / "configs" / "access-rules.yaml")
NOW = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)


def slot(
    sid: str,
    start: datetime,
    minutes: int = 20,
    *,
    schedule: str = "s1",
    practitioner: str = "p1",
    location: str | None = "l1",
    modality: Modality = Modality.IN_PERSON,
    status: str = "free",
) -> SlotCandidate:
    return SlotCandidate(
        slot_id=sid,
        schedule_id=schedule,
        start=start,
        end=start + timedelta(minutes=minutes),
        status=status,
        practitioner_ids=(practitioner,),
        location_id=location,
        modality=modality,
    )


def request(**overrides: object) -> SchedulingRequest:
    values: dict[str, object] = {
        "duration": timedelta(minutes=40),
        "earliest": NOW,
        "latest": NOW + timedelta(days=30),
        "modalities": frozenset({Modality.IN_PERSON, Modality.TELEHEALTH}),
        "timezone": "America/New_York",
    }
    values.update(overrides)
    return SchedulingRequest.model_validate(values)


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=NY)


class TestOptions:
    def test_chains_back_to_back_slots(self) -> None:
        slots = [slot("a", local(16, 9)), slot("b", local(16, 9, 20)), slot("c", local(16, 9, 40))]
        options = build_options(slots, timedelta(minutes=40))
        assert [[s.slot_id for s in o.slots] for o in options] == [["a", "b"], ["b", "c"]]

    def test_gap_or_resource_change_breaks_chain(self) -> None:
        slots = [
            slot("a", local(16, 9)),
            slot("b", local(16, 9, 25)),
            slot("c", local(16, 10)),
            slot("d", local(16, 10, 20), practitioner="p2"),
        ]
        assert build_options(slots, timedelta(minutes=40)) == []
        assert len(build_options(slots, timedelta(minutes=20))) == 4

    def test_long_slot_alone_is_an_option(self) -> None:
        options = build_options([slot("a", local(16, 9), minutes=60)], timedelta(minutes=40))
        assert len(options) == 1


class TestSolver:
    solver = SlotSolver(RULES.solver)

    def test_hard_constraints_and_binding_constraint(self) -> None:
        slots = [slot(f"s{h}", local(16, h), minutes=60) for h in (9, 10, 11)]
        assert self.solver.solve([], request()).binding_constraint is Constraint.CAPACITY
        busy = [s.model_copy(update={"status": "busy"}) for s in slots]
        result = self.solver.solve(busy, request())
        assert result.binding_constraint is Constraint.CAPACITY
        assert result.eliminated[Constraint.STATUS] == 3
        short = request(duration=timedelta(minutes=200))
        assert self.solver.solve(slots, short).binding_constraint is Constraint.DURATION
        late = request(earliest=local(17, 0))
        assert self.solver.solve(slots, late).binding_constraint is Constraint.LEAD_TIME
        early = request(latest=local(15, 0))
        assert self.solver.solve(slots, early).binding_constraint is Constraint.DEADLINE
        video = request(modalities=frozenset({Modality.TELEHEALTH}))
        assert self.solver.solve(slots, video).binding_constraint is Constraint.MODALITY
        other = request(practitioners=frozenset({"p9"}))
        assert self.solver.solve(slots, other).binding_constraint is Constraint.PRACTITIONER
        elsewhere = request(locations=frozenset({"l9"}))
        assert self.solver.solve(slots, elsewhere).binding_constraint is Constraint.LOCATION
        excluded = request(excluded_locations=frozenset({"l1"}))
        assert self.solver.solve(slots, excluded).binding_constraint is Constraint.LOCATION
        weekend = request(
            availability=(AvailabilityWindow(weekdays=frozenset({6}), start=time(9), end=time(17)),)
        )
        result = self.solver.solve(slots, weekend)
        assert result.binding_constraint is Constraint.AVAILABILITY
        assert result.eliminated[Constraint.AVAILABILITY] == 3
        assert not result.feasible

    def test_location_constraint_ignores_virtual_visits(self) -> None:
        video = slot("v", local(16, 9), minutes=60, location=None, modality=Modality.TELEHEALTH)
        result = self.solver.solve([video], request(locations=frozenset({"l1"})))
        assert result.feasible

    def test_ranking_prefers_continuity_location_and_modality(self) -> None:
        slots = [
            slot("early", local(16, 9), minutes=60, practitioner="p2", location="l2"),
            slot("pref", local(20, 9), minutes=60, practitioner="p1", location="l1"),
        ]
        plain = self.solver.solve(slots, request())
        assert plain.options[0].slot_ids == ("early",)
        preferred = self.solver.solve(
            slots,
            request(
                preferred_practitioners=("p1",),
                preferred_locations=("l1",),
                preferred_modalities=(Modality.IN_PERSON,),
            ),
        )
        assert preferred.options[0].slot_ids == ("pref",)
        breakdown = preferred.options[0].score_breakdown
        assert breakdown["continuity"] == 0.25
        assert breakdown["location"] == 0.15

    def test_diversifies_per_practitioner_day_and_caps_options(self) -> None:
        slots = [
            slot(f"d{d}h{h}", local(d, h), minutes=60)
            for d in (16, 19, 20, 21)
            for h in (9, 10, 11)
        ]
        result = self.solver.solve(slots, request())
        days = [o.start.astimezone(NY).date() for o in result.options]
        assert len(result.options) == RULES.solver.max_options
        assert max(days.count(d) for d in set(days)) <= RULES.solver.max_per_practitioner_day

    def test_availability_survives_daylight_saving(self) -> None:
        window = AvailabilityWindow(
            weekdays=frozenset({1, 2, 3, 4, 5}), start=time(9), end=time(10)
        )
        before = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)  # 09:00 EDT
        after = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)  # 09:00 EST
        for start in (before, after):
            assert within_availability(start, start + timedelta(minutes=40), [window], NY)
        assert not within_availability(
            after - timedelta(hours=1), after - timedelta(minutes=20), [window], NY
        )
        overnight = datetime(2026, 10, 16, 3, 30, tzinfo=UTC)
        assert not within_availability(overnight, overnight + timedelta(hours=1), [window], NY)

    @given(
        starts=st.lists(st.integers(min_value=0, max_value=24 * 14), min_size=0, max_size=40),
        duration=st.sampled_from([20, 40, 60]),
        lead=st.integers(min_value=0, max_value=72),
    )
    @settings(max_examples=120, deadline=None)
    def test_every_option_satisfies_every_constraint(
        self, starts: list[int], duration: int, lead: int
    ) -> None:
        window = AvailabilityWindow(
            weekdays=frozenset({1, 2, 3, 4, 5}), start=time(8), end=time(17)
        )
        slots = [slot(f"s{i}", NOW + timedelta(hours=h)) for i, h in enumerate(sorted(set(starts)))]
        req = request(
            duration=timedelta(minutes=duration),
            earliest=NOW + timedelta(hours=lead),
            latest=NOW + timedelta(days=10),
            availability=(window,),
        )
        for option in self.solver.solve(slots, req).options:
            assert option.end - option.start >= req.duration
            assert req.earliest <= option.start <= req.latest
            assert within_availability(option.start, option.end, req.availability, NY)


class TestTiming:
    rules: ReminderRules = RULES.reminders

    def test_quiet_hours(self) -> None:
        quiet = QuietHours(start=time(21), end=time(8))
        assert quiet.contains(time(23))
        assert quiet.contains(time(7, 59))
        assert not quiet.contains(time(8))
        assert not quiet.contains(time(20, 59))
        day = QuietHours(start=time(12), end=time(13))
        assert day.contains(time(12, 30))
        assert not day.contains(time(13))
        three_am = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)  # 03:00 New York
        earlier = outside_quiet_hours(three_am, quiet, NY, Shift.EARLIER)
        later = outside_quiet_hours(three_am, quiet, NY, Shift.LATER)
        assert earlier.astimezone(NY).time() == time(20, 45)
        assert later.astimezone(NY).time() == time(8, 0)

    def test_plan_reminders(self) -> None:
        visit = local(20, 9)
        due = plan_appointment_reminders(visit, NOW, self.rules, NY)
        assert [d.astimezone(NY) for d in due] == [local(17, 9), local(19, 9)]
        night_visit = local(20, 7)  # 24h-before falls at 07:00, pulled to 20:45 the evening before
        due = plan_appointment_reminders(night_visit, NOW, self.rules, NY)
        assert due[-1].astimezone(NY) == local(18, 20, 45)
        soon = NOW + timedelta(hours=5)
        assert plan_appointment_reminders(soon, NOW, self.rules, NY) == [NOW]
        assert plan_appointment_reminders(NOW + timedelta(hours=1), NOW, self.rules, NY) == []

    def test_retry_backoff_and_budget(self) -> None:
        visit = NOW + timedelta(days=3)
        first = next_retry(NOW, 1, self.rules, NY, visit)
        assert first == NOW + timedelta(minutes=10)
        third = next_retry(NOW, 3, self.rules, NY, visit)
        assert third == NOW + timedelta(minutes=40)
        assert next_retry(NOW, 4, self.rules, NY, visit) is None
        assert next_retry(NOW, 1, self.rules, NY, NOW + timedelta(hours=2)) is None
        assert next_retry(NOW, 1, self.rules, NY, None) is not None

    def test_sla_fraction(self) -> None:
        assert sla_elapsed_fraction(NOW, NOW + timedelta(hours=4), NOW + timedelta(hours=3)) == 0.75
        assert sla_elapsed_fraction(NOW, NOW, NOW) == 1.0
        assert sla_elapsed_fraction(NOW, NOW + timedelta(hours=1), NOW - timedelta(hours=1)) == 0.0


class TestRules:
    def test_shipped_rules_are_complete(self) -> None:
        assert RULES.version
        assert RULES.service("cardiology-consult").duration_minutes == 40
        with pytest.raises(KeyError):
            RULES.service("nothing")

    def test_validation(self) -> None:
        base = yaml.safe_load((ROOT / "configs" / "access-rules.yaml").read_text(encoding="utf-8"))

        def broken(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
            data = copy.deepcopy(base)
            change(data)
            return data

        cases: list[tuple[Callable[[dict[str, Any]], None], str]] = [
            (lambda d: d["sla"].pop("stat"), "sla missing"),
            (lambda d: d["escalation_queues"].pop("booking_approval"), "escalation_queues missing"),
            (lambda d: d["escalation_queues"].update(booking_approval="nobody"), "owning role"),
            (lambda d: d["services"].append(d["services"][0]), "unique"),
            (
                lambda d: d["solver"].update(weights=dict.fromkeys(d["solver"]["weights"], 0)),
                "weight",
            ),
        ]
        for change, message in cases:
            with pytest.raises(ValidationError, match=message):
                AccessRules.model_validate(broken(change))
        with pytest.raises(ValidationError, match="after start"):
            AvailabilityWindow(weekdays=frozenset({1}), start=time(10), end=time(9))
        with pytest.raises(ValidationError, match="ISO weekday"):
            AvailabilityWindow(weekdays=frozenset({8}), start=time(9), end=time(10))
