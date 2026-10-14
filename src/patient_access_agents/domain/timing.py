"""Time arithmetic for SLAs and patient contact: quiet hours, reminder plans, retry times.

All instants are timezone-aware UTC; patient-facing rules (quiet hours) are
evaluated in the patient's IANA time zone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from patient_access_agents.domain.rules import QuietHours, ReminderRules

_STEP = timedelta(minutes=15)
_MAX_STEPS = 4 * 24  # one day of quarter hours always clears a quiet window


class Shift(StrEnum):
    EARLIER = "earlier"
    LATER = "later"


def outside_quiet_hours(at: datetime, quiet: QuietHours, tz: ZoneInfo, shift: Shift) -> datetime:
    """Move ``at`` out of the quiet window, earlier (before it starts) or later (after it ends)."""
    candidate = at
    for _ in range(_MAX_STEPS):
        if not quiet.contains(candidate.astimezone(tz).time()):
            return candidate
        candidate = candidate - _STEP if shift is Shift.EARLIER else candidate + _STEP
    return candidate  # pragma: no cover - a quiet window cannot span a full day


def plan_appointment_reminders(
    appointment_start: datetime, now: datetime, rules: ReminderRules, tz: ZoneInfo
) -> list[datetime]:
    """Due times for appointment reminders.

    Each configured offset before the visit is pulled earlier out of quiet
    hours (a reminder the evening before beats one at 3 a.m.). Offsets already
    in the past are dropped; if every offset is past but the visit is still
    beyond the minimum notice, one reminder is sent as soon as quiet hours
    allow.
    """
    latest_useful = appointment_start - timedelta(hours=rules.min_notice_hours)
    due: list[datetime] = []
    for hours in sorted(rules.appointment_offsets_hours, reverse=True):
        at = outside_quiet_hours(
            appointment_start - timedelta(hours=hours), rules.quiet_hours, tz, Shift.EARLIER
        )
        if now <= at <= latest_useful and at not in due:
            due.append(at)
    if not due:
        at = outside_quiet_hours(now, rules.quiet_hours, tz, Shift.LATER)
        if at <= latest_useful:
            due.append(at)
    return due


def next_retry(
    now: datetime,
    attempts: int,
    rules: ReminderRules,
    tz: ZoneInfo,
    appointment_start: datetime | None,
) -> datetime | None:
    """When to retry a failed delivery, or None when the retry budget is exhausted."""
    if attempts >= rules.retry.max_attempts:
        return None
    at = outside_quiet_hours(now + rules.retry.delay(attempts), rules.quiet_hours, tz, Shift.LATER)
    if appointment_start is not None and at > appointment_start - timedelta(
        hours=rules.min_notice_hours
    ):
        return None
    return at


def sla_elapsed_fraction(created_at: datetime, due_at: datetime, now: datetime) -> float:
    total = (due_at - created_at).total_seconds()
    if total <= 0:
        return 1.0
    return max(0.0, (now - created_at).total_seconds() / total)
