"""Slot constraint solver.

Turns free FHIR Slots into ranked appointment options for one referral.

1. Contiguous free slots on the same Schedule are chained until the service
   duration is covered, so a 40-minute visit can use two 20-minute slots
   (FHIR Appointment.slot is a list).
2. Hard constraints are applied in a fixed order and the number of options
   each one eliminates is recorded: lead time, deadline, modality,
   practitioner, location, patient availability. When nothing survives, the
   constraint that removed the last options is reported as binding so the
   escalation can say *why* there is no appointment (capacity versus patient
   availability versus provider restriction).
3. Survivors are scored on earliness, continuity with preferred
   practitioners, preferred location and preferred modality, then
   diversified (at most N options per practitioner per day).

Availability windows are evaluated in the patient's IANA time zone, so a
window of 09:00-12:00 stays correct across daylight-saving changes.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from patient_access_agents.domain.models import (
    AvailabilityWindow,
    Modality,
    SlotCandidate,
    SlotProposal,
)
from patient_access_agents.domain.rules import SolverRules


class Constraint(StrEnum):
    CAPACITY = "capacity"
    STATUS = "status"
    DURATION = "duration"
    LEAD_TIME = "lead_time"
    DEADLINE = "deadline"
    MODALITY = "modality"
    PRACTITIONER = "practitioner"
    LOCATION = "location"
    AVAILABILITY = "patient_availability"


class SchedulingRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration: timedelta
    earliest: datetime
    latest: datetime
    modalities: frozenset[Modality] = Field(min_length=1)
    practitioners: frozenset[str] = frozenset()
    locations: frozenset[str] = frozenset()
    excluded_locations: frozenset[str] = frozenset()
    availability: tuple[AvailabilityWindow, ...] = ()
    timezone: str = "UTC"
    preferred_practitioners: tuple[str, ...] = ()
    preferred_locations: tuple[str, ...] = ()
    preferred_modalities: tuple[Modality, ...] = ()


class SolverResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    options: tuple[SlotProposal, ...]
    considered: int
    eliminated: dict[Constraint, int]
    binding_constraint: Constraint | None

    @property
    def feasible(self) -> bool:
        return bool(self.options)


class _Option(BaseModel):
    model_config = ConfigDict(frozen=True)

    slots: tuple[SlotCandidate, ...]

    @property
    def first(self) -> SlotCandidate:
        return self.slots[0]

    @property
    def start(self) -> datetime:
        return self.slots[0].start

    @property
    def end(self) -> datetime:
        return self.slots[-1].end


def build_options(slots: Iterable[SlotCandidate], duration: timedelta) -> list[_Option]:
    """Chain back-to-back free slots on one schedule until ``duration`` is covered."""
    by_schedule: dict[str, list[SlotCandidate]] = defaultdict(list)
    for slot in slots:
        if slot.end > slot.start:
            by_schedule[slot.schedule_id].append(slot)
    options: list[_Option] = []
    for schedule_slots in by_schedule.values():
        ordered = sorted(schedule_slots, key=lambda s: (s.start, s.slot_id))
        for index, first in enumerate(ordered):
            chain = [first]
            cursor = index
            while chain[-1].end - first.start < duration and cursor + 1 < len(ordered):
                nxt = ordered[cursor + 1]
                if nxt.start != chain[-1].end or not _same_resources(first, nxt):
                    break
                chain.append(nxt)
                cursor += 1
            if chain[-1].end - first.start >= duration:
                options.append(_Option(slots=tuple(chain)))
    return options


def _same_resources(a: SlotCandidate, b: SlotCandidate) -> bool:
    return (
        a.practitioner_ids == b.practitioner_ids
        and a.location_id == b.location_id
        and a.modality is b.modality
    )


def within_availability(
    start: datetime, end: datetime, windows: Sequence[AvailabilityWindow], tz: ZoneInfo
) -> bool:
    """True when [start, end) lies inside one window on one local calendar day."""
    if not windows:
        return True
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)
    if local_end.date() != local_start.date():
        return False
    return any(
        local_start.isoweekday() in window.weekdays
        and window.start <= local_start.time()
        and local_end.time() <= window.end
        for window in windows
    )


class SlotSolver:
    def __init__(self, rules: SolverRules) -> None:
        self._rules = rules

    def solve(self, slots: Sequence[SlotCandidate], request: SchedulingRequest) -> SolverResult:
        tz = ZoneInfo(request.timezone)
        free = [s for s in slots if s.status == "free"]
        eliminated: dict[Constraint, int] = dict.fromkeys(Constraint, 0)
        eliminated[Constraint.STATUS] = len(slots) - len(free)
        options = build_options(free, request.duration)
        eliminated[Constraint.DURATION] = max(0, len(free) - len(options))
        considered = len(options)

        checks: list[tuple[Constraint, Callable[[_Option], bool]]] = [
            (Constraint.LEAD_TIME, lambda o: o.start >= request.earliest),
            (Constraint.DEADLINE, lambda o: o.start <= request.latest),
            (Constraint.MODALITY, lambda o: o.first.modality in request.modalities),
            (
                Constraint.PRACTITIONER,
                lambda o: not request.practitioners
                or bool(request.practitioners.intersection(o.first.practitioner_ids)),
            ),
            (
                Constraint.LOCATION,
                lambda o: o.first.modality is not Modality.IN_PERSON
                or (
                    (not request.locations or o.first.location_id in request.locations)
                    and o.first.location_id not in request.excluded_locations
                ),
            ),
            (
                Constraint.AVAILABILITY,
                lambda o: within_availability(o.start, o.end, request.availability, tz),
            ),
        ]
        binding: Constraint | None = None
        if not free:
            binding = Constraint.CAPACITY
        elif not options:
            binding = Constraint.DURATION
        for constraint, check in checks:
            survivors = [o for o in options if check(o)]
            eliminated[constraint] = len(options) - len(survivors)
            if options and not survivors:
                binding = constraint
            options = survivors

        ranked = sorted(
            ((self._score(o, request), o) for o in options),
            key=lambda pair: (-pair[0][0], pair[1].start, pair[1].first.slot_id),
        )
        chosen: list[SlotProposal] = []
        per_day: dict[tuple[tuple[str, ...], str], int] = defaultdict(int)
        for (total, breakdown), option in ranked:
            day_key = (
                option.first.practitioner_ids,
                option.start.astimezone(tz).date().isoformat(),
            )
            if per_day[day_key] >= self._rules.max_per_practitioner_day:
                continue
            per_day[day_key] += 1
            chosen.append(
                SlotProposal(
                    slot_ids=tuple(s.slot_id for s in option.slots),
                    schedule_id=option.first.schedule_id,
                    start=option.start,
                    end=option.end,
                    practitioner_ids=option.first.practitioner_ids,
                    location_id=option.first.location_id,
                    location_name=option.first.location_name,
                    practitioner_names=option.first.practitioner_names,
                    modality=option.first.modality,
                    score=round(total, 4),
                    score_breakdown=breakdown,
                )
            )
            if len(chosen) >= self._rules.max_options:
                break
        return SolverResult(
            options=tuple(chosen),
            considered=considered,
            eliminated=eliminated,
            binding_constraint=None if chosen else binding,
        )

    def _score(self, option: _Option, request: SchedulingRequest) -> tuple[float, dict[str, float]]:
        weights = self._rules.weights
        horizon = (request.latest - request.earliest).total_seconds()
        offset = (option.start - request.earliest).total_seconds()
        earliness = 1.0 if horizon <= 0 else max(0.0, 1.0 - offset / horizon)
        continuity = (
            1.0
            if set(request.preferred_practitioners).intersection(option.first.practitioner_ids)
            else 0.0
        )
        location = 1.0 if option.first.location_id in request.preferred_locations else 0.0
        modality = 0.0
        if option.first.modality in request.preferred_modalities:
            rank = request.preferred_modalities.index(option.first.modality)
            modality = 1.0 / (rank + 1)
        breakdown = {
            "earliness": round(weights.earliness * earliness, 4),
            "continuity": round(weights.continuity * continuity, 4),
            "location": round(weights.location * location, 4),
            "modality": round(weights.modality * modality, 4),
        }
        return sum(breakdown.values()), breakdown
