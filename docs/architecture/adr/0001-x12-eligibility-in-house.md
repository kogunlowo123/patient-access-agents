# ADR-0001: Build the X12 270/271 path in-house, behind a clearinghouse, with a manual fallback

- Status: Accepted
- Date: 2026-10-14

## Context

Real-time eligibility in the US is the HIPAA-adopted ASC X12 005010X279A1
270/271 pair, exchanged through a clearinghouse. Open-source Python EDI
libraries either target other transaction sets, are unmaintained, or bring
large dependency trees into a PHI-handling service. The subset we need is
small and well specified: one inquiry shape and a response whose meaning is
carried by a handful of segments (AAA, TRN, EB, MSG, DTP).

## Decision

- Implement a focused builder and parser (`integrations/x12.py`): full
  envelope (ISA/GS/ST/SE/GE/IEA) with count and control-number checks,
  delimiters read from the fixed-width ISA, subscriber and dependent loops,
  999/TA1 acknowledgements reported as failures, LS/LE loops skipped.
- Free-text elements are sanitised; identifiers containing delimiters are
  rejected and the case goes to manual verification rather than altering a
  member id.
- Interpretation is conservative: rejections (AAA) escalate to people;
  "unable to respond" (AAA 42/80) and transport failures retry with back-off;
  anything ambiguous is `manual_required`. A 271 whose TRN does not echo our
  trace number is refused.
- With no clearinghouse configured, eligibility goes straight to the
  manual-verification queue; staff record the outcome with a payer reference.

## Consequences

- No third-party EDI dependency; the parser is property-tested and fuzzed
  with delimiter injection.
- Benefit detail beyond EB/MSG/DTP (for example 2120 related entities) is not
  modelled; it is not needed for an access decision.
- Clearinghouse-specific connectivity (CAQH CORE SOAP/MIME) needs a thin
  adapter if the chosen clearinghouse does not offer the HTTPS
  `application/EDI-X12` interface used here.
