# ADR-0003: Deterministic scheduling; the model only words messages, without PHI

- Status: Accepted
- Date: 2026-10-14

## Context

Choosing an appointment means satisfying hard constraints (provider,
location, modality, patient availability in their time zone, lead time,
clinical deadline) and preferences. A language model adds variance and
cannot be held to constraints. Patient messages, on the other hand, benefit
from plain, warm wording in the patient's language.

## Decision

- Scheduling is a deterministic constraint solver (`domain/scheduling.py`):
  contiguous slot chaining for longer visits, ordered hard constraints with
  elimination counts and a binding-constraint report, weighted scoring and
  per-practitioner-day diversity. Property tests assert every option meets
  every constraint.
- The model (Gemini on Vertex AI through the agentic-core `ModelRouter`)
  drafts a message template from non-identifying facts. Names, times and
  places are filled locally after the draft passes ContentGuard and a strict
  template contract. Any failure uses a reviewed template; the composer is
  recorded on the reminder.

## Consequences

- Scheduling results are reproducible and explainable ("no option because of
  patient availability").
- PHI never leaves the service for model calls, so the model route can be
  classified CONFIDENTIAL and swapped without a new privacy review.
- Without a model the service is fully functional with templates.
