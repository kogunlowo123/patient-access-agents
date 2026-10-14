"""ASC X12 005010X279A1 eligibility: 270 inquiry builder and 271 response parser.

Builder: emits a complete interchange (ISA/GS/ST ... SE/GE/IEA) with the
2000A information source (payer), 2000B information receiver (provider,
NPI), 2000C subscriber and optional 2000D dependent loops. Values are
validated against delimiter injection and element length limits; identifiers
containing delimiter characters are rejected rather than altered.

Parser: reads delimiters from the fixed-width ISA segment, verifies envelope
integrity (ISA13/IEA02, GS06/GE02, ST02/SE02, SE01 segment counts, GE01 and
IEA01 counts), handles 999 and TA1 acknowledgements, and extracts AAA
request-validation errors, TRN trace echoes, EB benefit segments (with
their MSG/DTP children) and plan dates. LS/LE (2120) loops are skipped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from patient_access_agents.domain.errors import X12Error
from patient_access_agents.domain.models import Benefit, EligibilityOutcome, EligibilityResult

ELEMENT = "*"
COMPONENT = ":"
REPETITION = "^"
TERMINATOR = "~"
IMPLEMENTATION = "005010X279A1"

# EB01 Eligibility or Benefit Information codes (X12 element 1390).
EB01: dict[str, str] = {
    "1": "Active Coverage",
    "2": "Active - Full Risk Capitation",
    "3": "Active - Services Capitated",
    "4": "Active - Services Capitated to Primary Care Physician",
    "5": "Active - Pending Investigation",
    "6": "Inactive",
    "7": "Inactive - Pending Eligibility Update",
    "8": "Inactive - Pending Investigation",
    "A": "Co-Insurance",
    "B": "Co-Payment",
    "C": "Deductible",
    "CB": "Coverage Basis",
    "D": "Benefit Description",
    "E": "Exclusions",
    "F": "Limitations",
    "G": "Out of Pocket (Stop Loss)",
    "H": "Unlimited",
    "I": "Non-Covered",
    "J": "Cost Containment",
    "K": "Reserve",
    "L": "Primary Care Provider",
    "M": "Pre-existing Condition",
    "MC": "Managed Care Coordinator",
    "N": "Services Restricted to Following Provider",
    "O": "Not Deemed a Medical Necessity",
    "P": "Benefit Disclaimer",
    "Q": "Second Surgical Opinion Required",
    "R": "Other or Additional Payor",
    "S": "Prior Year(s) History",
    "T": "Card(s) Reported Lost/Stolen",
    "U": "Contact Following Entity for Eligibility or Benefit Information",
    "V": "Cannot Process",
    "W": "Other Source of Data",
    "X": "Health Care Facility",
    "Y": "Spend Down",
}
ACTIVE_CODES = frozenset({"1", "2", "3", "4", "5"})
INACTIVE_CODES = frozenset({"6", "7", "8"})

# AAA03 Reject Reason codes (X12 element 901) used in 270/271.
AAA_REASONS: dict[str, str] = {
    "04": "Authorized Quantity Exceeded",
    "15": "Required application data missing",
    "41": "Authorization/Access Restrictions",
    "42": "Unable to Respond at Current Time",
    "43": "Invalid/Missing Provider Identification",
    "45": "Invalid/Missing Provider Specialty",
    "47": "Invalid/Missing Provider State",
    "48": "Invalid/Missing Referring Provider Identification Number",
    "49": "Provider is Not Primary Care Physician",
    "51": "Provider Not on File",
    "52": "Service Dates Not Within Provider Plan Enrollment",
    "56": "Inappropriate Date",
    "57": "Invalid/Missing Date(s) of Service",
    "58": "Invalid/Missing Date-of-Birth",
    "60": "Date of Birth Follows Date(s) of Service",
    "61": "Date of Death Precedes Date(s) of Service",
    "62": "Date of Service Not Within Allowable Inquiry Period",
    "63": "Date of Service in Future",
    "71": "Patient Birth Date Does Not Match That for the Patient on the Database",
    "72": "Invalid/Missing Subscriber/Insured ID",
    "73": "Invalid/Missing Subscriber/Insured Name",
    "74": "Invalid/Missing Subscriber/Insured Gender Code",
    "75": "Subscriber/Insured Not Found",
    "76": "Duplicate Subscriber/Insured ID Number",
    "78": "Subscriber/Insured Not in Group/Plan Identified",
    "79": "Invalid Participant Identification",
    "80": "No Response received - Transaction Terminated",
    "97": "Invalid or Missing Provider Address",
    "T4": "Payer Name or Identifier Missing",
}
RETRYABLE_AAA = frozenset({"42", "80"})

AAA_FOLLOW_UP: dict[str, str] = {
    "C": "Please Correct and Resubmit",
    "N": "Resubmission Not Allowed",
    "P": "Please Resubmit Original Transaction",
    "R": "Resubmission Allowed",
    "S": "Do Not Resubmit; Inquiry Initiated to a Third Party",
    "W": "Please Wait 30 Days and Resubmit",
    "X": "Please Wait 10 Days and Resubmit",
    "Y": "Do Not Resubmit; We Will Hand Back Response",
}

# Service Type Codes (X12 element 1365) accepted in EQ01 by this service.
SERVICE_TYPES: dict[str, str] = {
    "1": "Medical Care",
    "2": "Surgical",
    "3": "Consultation",
    "4": "Diagnostic X-Ray",
    "5": "Diagnostic Lab",
    "30": "Health Benefit Plan Coverage",
    "33": "Chiropractic",
    "35": "Dental Care",
    "42": "Home Health Care",
    "47": "Hospital",
    "48": "Hospital - Inpatient",
    "50": "Hospital - Outpatient",
    "62": "MRI/CAT Scan",
    "73": "Diagnostic Medical",
    "76": "Dialysis",
    "86": "Emergency Services",
    "88": "Pharmacy",
    "98": "Professional (Physician) Visit - Office",
    "A4": "Psychiatric",
    "AL": "Vision (Optometry)",
    "MH": "Mental Health",
    "UC": "Urgent Care",
}

_FORBIDDEN = re.compile(r"[*~:^\r\n]")
_PRINTABLE = re.compile(r"[^\x20-\x7E]")
_GENDER = {"male": "M", "female": "F"}


def npi_valid(npi: str) -> bool:
    """NPI check digit: Luhn over the 80840 prefix plus the first nine digits."""
    if not re.fullmatch(r"\d{10}", npi):
        return False
    digits = "80840" + npi[:9]
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10 == int(npi[9])


def _text(value: str, max_len: int) -> str:
    """Free-text element: upper-case printable ASCII, delimiters replaced, truncated."""
    cleaned = _FORBIDDEN.sub(" ", _PRINTABLE.sub("", value.upper()))
    return " ".join(cleaned.split())[:max_len]


def _ident(value: str, max_len: int, name: str) -> str:
    """Identifier element: must already be clean; never silently altered."""
    if not value or _FORBIDDEN.search(value) or _PRINTABLE.search(value) or len(value) > max_len:
        raise X12Error(f"{name} is not a valid X12 identifier")
    return value


class Envelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    sender_qualifier: str = Field(default="ZZ", pattern=r"^[0-9A-Z]{2}$")
    sender_id: str = Field(min_length=1, max_length=15)
    receiver_qualifier: str = Field(default="ZZ", pattern=r"^[0-9A-Z]{2}$")
    receiver_id: str = Field(min_length=1, max_length=15)
    application_sender: str = Field(min_length=2, max_length=15)
    application_receiver: str = Field(min_length=2, max_length=15)
    usage: str = Field(default="P", pattern=r"^[PT]$")
    interchange_control: int = Field(ge=1, le=999_999_999)
    group_control: int = Field(ge=1, le=999_999_999)


class Person(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: str = Field(min_length=1)
    given: str = Field(min_length=1)
    birth_date: date
    gender: str = "unknown"
    member_id: str | None = None


class EligibilityInquiry(BaseModel):
    model_config = ConfigDict(frozen=True)

    payer_name: str
    payer_id: str
    provider_name: str
    provider_npi: str
    provider_tax_id: str = Field(pattern=r"^\d{9}$")
    subscriber: Person
    dependent: Person | None = None
    group_number: str | None = None
    trace_number: str = Field(pattern=r"^[A-Za-z0-9]{1,50}$")
    service_types: tuple[str, ...] = Field(min_length=1, max_length=99)
    service_date: date
    created_at: datetime


def build_270(inquiry: EligibilityInquiry, envelope: Envelope) -> str:
    if not npi_valid(inquiry.provider_npi):
        raise X12Error("provider NPI fails the check-digit test")
    unknown = [c for c in inquiry.service_types if c not in SERVICE_TYPES]
    if unknown:
        raise X12Error(f"unsupported service type codes {unknown}")
    if inquiry.subscriber.member_id is None:
        raise X12Error("subscriber member id is required")
    stamp = inquiry.created_at
    isa = ELEMENT.join(
        [
            "ISA",
            "00",
            " " * 10,
            "00",
            " " * 10,
            envelope.sender_qualifier,
            _ident(envelope.sender_id, 15, "ISA06").ljust(15),
            envelope.receiver_qualifier,
            _ident(envelope.receiver_id, 15, "ISA08").ljust(15),
            stamp.strftime("%y%m%d"),
            stamp.strftime("%H%M"),
            REPETITION,
            "00501",
            f"{envelope.interchange_control:09d}",
            "0",
            envelope.usage,
            COMPONENT,
        ]
    )
    group = [
        "GS",
        "HS",
        _ident(envelope.application_sender, 15, "GS02"),
        _ident(envelope.application_receiver, 15, "GS03"),
        stamp.strftime("%Y%m%d"),
        stamp.strftime("%H%M"),
        str(envelope.group_control),
        "X",
        IMPLEMENTATION,
    ]
    control = "0001"
    body: list[list[str]] = [
        ["ST", "270", control, IMPLEMENTATION],
        [
            "BHT",
            "0022",
            "13",
            inquiry.trace_number,
            stamp.strftime("%Y%m%d"),
            stamp.strftime("%H%M"),
        ],
        ["HL", "1", "", "20", "1"],
        [
            "NM1",
            "PR",
            "2",
            _text(inquiry.payer_name, 60),
            "",
            "",
            "",
            "",
            "PI",
            _ident(inquiry.payer_id, 80, "payer id"),
        ],
        ["HL", "2", "1", "21", "1"],
        [
            "NM1",
            "1P",
            "2",
            _text(inquiry.provider_name, 60),
            "",
            "",
            "",
            "",
            "XX",
            inquiry.provider_npi,
        ],
        ["HL", "3", "2", "22", "1" if inquiry.dependent else "0"],
        ["TRN", "1", inquiry.trace_number, "1" + inquiry.provider_tax_id],
        [
            "NM1",
            "IL",
            "1",
            _text(inquiry.subscriber.family, 60),
            _text(inquiry.subscriber.given, 35),
            "",
            "",
            "",
            "MI",
            _ident(inquiry.subscriber.member_id, 80, "member id"),
        ],
    ]
    if inquiry.group_number:
        body.append(["REF", "6P", _ident(inquiry.group_number, 50, "group number")])
    body.append(_dmg(inquiry.subscriber))
    if inquiry.dependent is not None:
        body.extend(
            [
                ["HL", "4", "3", "23", "0"],
                ["TRN", "1", inquiry.trace_number, "1" + inquiry.provider_tax_id],
                [
                    "NM1",
                    "03",
                    "1",
                    _text(inquiry.dependent.family, 60),
                    _text(inquiry.dependent.given, 35),
                ],
                _dmg(inquiry.dependent),
            ]
        )
    body.append(["DTP", "291", "D8", inquiry.service_date.strftime("%Y%m%d")])
    body.append(["EQ", REPETITION.join(inquiry.service_types)])
    body.append(["SE", str(len(body) + 1), control])
    segments = [
        isa,
        _join(group),
        *(_join(s) for s in body),
        _join(["GE", "1", str(envelope.group_control)]),
        _join(["IEA", "1", f"{envelope.interchange_control:09d}"]),
    ]
    return TERMINATOR.join(segments) + TERMINATOR


def _dmg(person: Person) -> list[str]:
    return [
        "DMG",
        "D8",
        person.birth_date.strftime("%Y%m%d"),
        _GENDER.get(person.gender, "U"),
    ]


def _join(elements: Sequence[str]) -> str:
    trimmed = list(elements)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return ELEMENT.join(trimmed)


# --------------------------------------------------------------------- parsing


class Delimiters(BaseModel):
    model_config = ConfigDict(frozen=True)

    element: str
    component: str
    repetition: str
    terminator: str


class AaaError(BaseModel):
    model_config = ConfigDict(frozen=True)

    level: str
    valid_request: bool
    reason_code: str
    reason: str
    follow_up: str | None = None


class Response271(BaseModel):
    model_config = ConfigDict(frozen=True)

    control_number: str
    payer_name: str | None = None
    payer_id: str | None = None
    member_id: str | None = None
    trace_numbers: tuple[str, ...] = ()
    errors: tuple[AaaError, ...] = ()
    benefits: tuple[Benefit, ...] = ()
    dates: dict[str, str] = Field(default_factory=dict)


def tokenize(data: str) -> tuple[Delimiters, list[list[str]]]:
    text = data.lstrip("﻿ \r\n\t")
    if not text.startswith("ISA") or len(text) < 106:
        raise X12Error("not an X12 interchange (missing ISA)")
    delimiters = Delimiters(
        element=text[3], repetition=text[82], component=text[104], terminator=text[105]
    )
    if len({delimiters.element, delimiters.component, delimiters.terminator}) != 3:
        raise X12Error("ISA delimiters are not distinct")
    isa_elements = text[:105].split(delimiters.element)
    if len(isa_elements) != 17:
        raise X12Error("ISA segment must have 16 elements")
    rows = [
        segment.strip("\r\n ").split(delimiters.element)
        for segment in text.split(delimiters.terminator)
        if segment.strip("\r\n ")
    ]
    return delimiters, rows


def _transaction_set(rows: list[list[str]], start: int) -> int:
    """Validate the ST..SE set starting at ``start``; return the index of its SE."""
    index = start
    while index < len(rows) and rows[index][0] != "SE":
        index += 1
    if index >= len(rows):
        raise X12Error("transaction set without SE")
    st, se = rows[start], rows[index]
    if len(se) < 3 or len(st) < 3 or se[2] != st[2]:
        raise X12Error("SE02 does not match ST02")
    if se[1] != str(index - start + 1):
        raise X12Error("SE01 segment count is wrong")
    return index


def _functional_group(
    rows: list[list[str]], start: int, transactions: list[list[list[str]]]
) -> int:
    """Validate the GS..GE group starting at ``start``; return the index after its GE."""
    gs = rows[start]
    index = start + 1
    sets_in_group = 0
    while index < len(rows) and rows[index][0] != "GE":
        if rows[index][0] != "ST":
            raise X12Error(f"expected ST, found {rows[index][0]}")
        end = _transaction_set(rows, index)
        transactions.append(rows[index : end + 1])
        sets_in_group += 1
        index = end + 1
    ge = rows[index]  # the interchange ends with IEA, so the loop stopped at a GE
    if len(ge) < 3 or ge[2] != gs[6] or ge[1] != str(sets_in_group):
        raise X12Error("GE does not match its GS group")
    return index + 1


def _check_envelope(rows: list[list[str]]) -> list[list[list[str]]]:
    """Validate the envelope and return the transaction sets it contains."""
    if rows[0][0] != "ISA" or rows[-1][0] != "IEA":
        raise X12Error("interchange must start with ISA and end with IEA")
    isa, iea = rows[0], rows[-1]
    if len(iea) < 3 or iea[2] != isa[13]:
        raise X12Error("IEA02 does not match ISA13")
    transactions: list[list[list[str]]] = []
    groups = 0
    index = 1
    while index < len(rows) - 1:
        row = rows[index]
        if row[0] == "TA1":
            note = row[4] if len(row) > 4 else ""
            raise X12Error(f"interchange acknowledgement TA1 status {note or 'unknown'}")
        if row[0] != "GS":
            raise X12Error(f"expected GS, found {row[0]}")
        groups += 1
        index = _functional_group(rows, index, transactions)
    if iea[1] != str(groups):
        raise X12Error("IEA01 group count is wrong")
    if not transactions:
        raise X12Error("interchange contains no transaction sets")
    return transactions


def _element(row: Sequence[str], position: int) -> str | None:
    return row[position] if len(row) > position and row[position] != "" else None


class _Reader271:
    """Walks the segments of one 271 transaction set, loop by loop."""

    def __init__(self, delimiters: Delimiters) -> None:
        self.delimiters = delimiters
        self.level = ""
        self.payer_name: str | None = None
        self.payer_id: str | None = None
        self.member_id: str | None = None
        self.traces: list[str] = []
        self.errors: list[AaaError] = []
        self.benefits: list[Benefit] = []
        self.dates: dict[str, str] = {}
        self.current: dict[str, Any] | None = None
        self.in_2120 = False

    def flush(self) -> None:
        if self.current is not None:
            self.benefits.append(Benefit.model_validate(self.current))
            self.current = None

    def feed(self, row: list[str]) -> None:
        tag = row[0]
        if self.in_2120:
            self.in_2120 = tag != "LE"
            return
        handler = getattr(self, f"_on_{tag.lower()}", None)
        if handler is not None:
            handler(row)

    def _on_ls(self, _row: list[str]) -> None:
        self.in_2120 = True

    def _on_hl(self, row: list[str]) -> None:
        self.flush()
        self.level = _element(row, 3) or ""

    def _on_nm1(self, row: list[str]) -> None:
        self.flush()
        if _element(row, 1) == "PR":
            self.payer_name, self.payer_id = _element(row, 3), _element(row, 9)
        elif _element(row, 1) == "IL":
            self.member_id = _element(row, 9)

    def _on_trn(self, row: list[str]) -> None:
        trace = _element(row, 2)
        if _element(row, 1) == "2" and trace:
            self.traces.append(trace)

    def _on_aaa(self, row: list[str]) -> None:
        code = _element(row, 3) or ""
        self.errors.append(
            AaaError(
                level=self.level,
                valid_request=_element(row, 1) == "Y",
                reason_code=code,
                reason=AAA_REASONS.get(code, f"reject reason {code}"),
                follow_up=AAA_FOLLOW_UP.get(_element(row, 4) or ""),
            )
        )

    def _on_eb(self, row: list[str]) -> None:
        self.flush()
        self.current = _benefit(row, self.delimiters)

    def _on_msg(self, row: list[str]) -> None:
        if self.current is not None:
            self.current["messages"].append(_element(row, 1) or "")

    def _on_dtp(self, row: list[str]) -> None:
        qualifier, value = _element(row, 1), _element(row, 3)
        if qualifier and value and self.current is None:
            self.dates[qualifier] = value


def parse_271(data: str) -> Response271:
    delimiters, rows = tokenize(data)
    st = _check_envelope(rows)[0]
    kind = st[0][1]
    if kind == "999":
        ak9 = next((r for r in st if r[0] == "AK9"), None)
        status = _element(ak9, 1) if ak9 else None
        raise X12Error(f"inquiry rejected by receiver: 999 acknowledgement AK9={status}")
    if kind != "271":
        raise X12Error(f"expected a 271 transaction, received {kind}")
    reader = _Reader271(delimiters)
    for row in st[1:-1]:
        reader.feed(row)
    reader.flush()
    return Response271(
        control_number=st[0][2],
        payer_name=reader.payer_name,
        payer_id=reader.payer_id,
        member_id=reader.member_id,
        trace_numbers=tuple(reader.traces),
        errors=tuple(reader.errors),
        benefits=tuple(reader.benefits),
        dates=reader.dates,
    )


def _yes_no(value: str | None) -> bool | None:
    return {"Y": True, "N": False}.get(value or "")


def _benefit(row: Sequence[str], delimiters: Delimiters) -> dict[str, Any]:
    code = _element(row, 1) or ""
    service_types = _element(row, 3)
    return {
        "code": code,
        "description": EB01.get(code, f"benefit code {code}"),
        "coverage_level": _element(row, 2),
        "service_types": tuple(service_types.split(delimiters.repetition)) if service_types else (),
        "insurance_type": _element(row, 4),
        "plan_description": _element(row, 5),
        "time_period": _element(row, 6),
        "amount": _element(row, 7),
        "percent": _element(row, 8),
        "authorization_required": _yes_no(_element(row, 11)),
        "in_network": _yes_no(_element(row, 12)),
        "messages": [],
    }


def _applies(benefit: Benefit, requested: Iterable[str]) -> bool:
    if not benefit.service_types:
        return True
    return bool(set(benefit.service_types).intersection({*requested, "30"}))


def _range(dates: dict[str, str], begin: str, end: str, combined: str) -> tuple[str | None, ...]:
    if combined in dates and "-" in dates[combined]:
        first, last = dates[combined].split("-", 1)
        return first, last
    return dates.get(begin), dates.get(end)


def evaluate_271(
    response: Response271,
    *,
    requested: Sequence[str],
    trace_number: str,
    checked_at: datetime,
) -> EligibilityResult:
    """Turn a parsed 271 into the access decision for the requested service types."""
    plan_begin, plan_end = _range(response.dates, "346", "347", "291")
    base: dict[str, Any] = {
        "checked_at": checked_at,
        "payer_id": response.payer_id,
        "member_id": response.member_id,
        "trace_number": trace_number,
        "service_types": tuple(requested),
        "benefits": response.benefits,
        "plan_begin": plan_begin,
        "plan_end": plan_end,
    }
    rejections = [e for e in response.errors if not e.valid_request]
    if rejections:
        first = rejections[0]
        outcome = (
            EligibilityOutcome.UNAVAILABLE
            if first.reason_code in RETRYABLE_AAA
            else EligibilityOutcome.REJECTED
        )
        return EligibilityResult.model_validate(
            {
                **base,
                "outcome": outcome,
                "rejection_code": first.reason_code,
                "rejection_reason": first.reason,
                "follow_up_action": first.follow_up,
            }
        )
    if trace_number not in response.trace_numbers:
        raise X12Error("271 does not answer this inquiry (TRN trace mismatch)")

    relevant = [b for b in response.benefits if _applies(b, requested)]
    active = [b for b in relevant if b.code in ACTIVE_CODES]
    not_covered = [
        b for b in relevant if b.code == "I" and set(b.service_types).intersection(requested)
    ]
    if active and not not_covered:
        prior_auth = any(b.authorization_required for b in relevant)
        return EligibilityResult.model_validate(
            {
                **base,
                "outcome": EligibilityOutcome.ACTIVE,
                "prior_authorization_required": prior_auth,
            }
        )
    if not_covered:
        codes = sorted({c for b in not_covered for c in b.service_types if c in requested})
        return EligibilityResult.model_validate(
            {
                **base,
                "outcome": EligibilityOutcome.INACTIVE,
                "rejection_reason": f"service type(s) {', '.join(codes)} not covered",
            }
        )
    if any(b.code in INACTIVE_CODES for b in relevant):
        return EligibilityResult.model_validate(
            {
                **base,
                "outcome": EligibilityOutcome.INACTIVE,
                "rejection_reason": "coverage inactive",
            }
        )
    return EligibilityResult.model_validate(
        {
            **base,
            "outcome": EligibilityOutcome.MANUAL_REQUIRED,
            "rejection_reason": "271 carried no determinable coverage for the requested services",
        }
    )
