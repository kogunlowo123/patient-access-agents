"""X12 005010X279A1: 270 construction and 271 parsing/evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from patient_access_agents.domain.errors import X12Error
from patient_access_agents.domain.models import EligibilityOutcome
from patient_access_agents.integrations.x12 import (
    EligibilityInquiry,
    Envelope,
    Person,
    build_270,
    evaluate_271,
    npi_valid,
    parse_271,
    tokenize,
)
from tests.fakes import x12_271

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def inquiry(**overrides: object) -> EligibilityInquiry:
    values: dict[str, object] = {
        "payer_name": "Synthetic Health Plan",
        "payer_id": "SHP01",
        "provider_name": "Riverbend Specialty Clinic",
        "provider_npi": "1234567893",
        "provider_tax_id": "123456789",
        "subscriber": Person(
            family="Rivera",
            given="Ana",
            birth_date=date(1980, 4, 2),
            gender="female",
            member_id="SHP000123",
        ),
        "trace_number": "PAA1",
        "service_types": ("3", "98"),
        "service_date": date(2026, 10, 14),
        "created_at": NOW,
    }
    values.update(overrides)
    return EligibilityInquiry.model_validate(values)


ENVELOPE = Envelope(
    sender_id="SUBMITTER01",
    receiver_id="CLEARING01",
    application_sender="SUB01",
    application_receiver="CH01",
    interchange_control=42,
    group_control=42,
)


def segments(text: str) -> list[list[str]]:
    return [s.split("*") for s in text.split("~") if s]


class TestBuild270:
    def test_envelope_and_loops(self) -> None:
        text = build_270(inquiry(group_number="GRP100"), ENVELOPE)
        rows = segments(text)
        isa = text.split("~")[0]
        assert len(isa) == 105
        assert isa[3] == "*"
        assert isa[104] == ":"
        assert isa[82] == "^"
        assert rows[0][13] == "000000042"
        tags = [r[0] for r in rows]
        assert tags == [
            "ISA",
            "GS",
            "ST",
            "BHT",
            "HL",
            "NM1",
            "HL",
            "NM1",
            "HL",
            "TRN",
            "NM1",
            "REF",
            "DMG",
            "DTP",
            "EQ",
            "SE",
            "GE",
            "IEA",
        ]
        se = rows[tags.index("SE")]
        assert se[1] == str(tags.index("SE") - tags.index("ST") + 1)
        assert rows[tags.index("GS")][8] == "005010X279A1"
        assert ["HL", "3", "2", "22", "0"] in rows
        assert ["TRN", "1", "PAA1", "1123456789"] in rows
        assert ["DMG", "D8", "19800402", "F"] in rows
        assert ["EQ", "3^98"] in rows
        assert ["REF", "6P", "GRP100"] in rows

    def test_dependent_loop(self) -> None:
        dependent = Person(
            family="Rivera", given="Leo", birth_date=date(2015, 9, 30), gender="male"
        )
        rows = segments(build_270(inquiry(dependent=dependent), ENVELOPE))
        assert ["HL", "3", "2", "22", "1"] in rows
        assert ["HL", "4", "3", "23", "0"] in rows
        assert ["NM1", "03", "1", "RIVERA", "LEO"] in rows
        assert ["DMG", "D8", "20150930", "M"] in rows

    def test_free_text_is_sanitised(self) -> None:
        sneaky = Person(
            family="O'Neil*~SE", given="Zoë:^", birth_date=date(1990, 1, 1), member_id="M1"
        )
        text = build_270(inquiry(subscriber=sneaky), ENVELOPE)
        nm1 = next(r for r in segments(text) if r[0] == "NM1" and r[1] == "IL")
        assert nm1[3] == "O'NEIL SE"
        assert nm1[4] == "ZO"
        assert ["DMG", "D8", "19900101", "U"] in segments(text)

    @pytest.mark.parametrize(
        "field",
        [
            {
                "subscriber": Person(
                    family="A", given="B", birth_date=date(1990, 1, 1), member_id="M*1"
                )
            },
            {"subscriber": Person(family="A", given="B", birth_date=date(1990, 1, 1))},
            {"payer_id": "SHP~01"},
            {"provider_npi": "1234567890"},
            {"service_types": ("ZZ",)},
        ],
    )
    def test_rejects_unsafe_or_invalid_identifiers(self, field: dict[str, object]) -> None:
        with pytest.raises(X12Error):
            build_270(inquiry(**field), ENVELOPE)

    def test_npi_check_digit(self) -> None:
        assert npi_valid("1234567893")
        assert not npi_valid("1234567890")
        assert not npi_valid("12345")

    @given(
        family=st.text(min_size=1, max_size=80),
        given_name=st.text(min_size=1, max_size=60),
    )
    @settings(max_examples=150, deadline=None)
    def test_any_name_yields_a_well_formed_interchange(self, family: str, given_name: str) -> None:
        person = Person(
            family=family, given=given_name, birth_date=date(1980, 1, 1), member_id="SHP000123"
        )
        text = build_270(inquiry(subscriber=person), ENVELOPE)
        _, rows = tokenize(text)
        assert rows[-1][0] == "IEA"
        nm1 = next(r for r in rows if r[0] == "NM1" and r[1] == "IL")
        assert len(nm1) == 10
        assert len(nm1[3]) <= 60
        assert len(nm1[4]) <= 35


class TestParse271:
    def evaluate(self, scenario: str, requested: tuple[str, ...] = ("3", "98")):  # type: ignore[no-untyped-def]
        response = parse_271(x12_271(trace="T1", member_id="SHP000123", scenario=scenario))
        return response, evaluate_271(
            response, requested=requested, trace_number="T1", checked_at=NOW
        )

    def test_active_coverage_with_benefits(self) -> None:
        response, result = self.evaluate("active")
        assert response.payer_id == "SHP01"
        assert response.member_id == "SHP000123"
        assert response.trace_numbers == ("T1",)
        assert result.outcome is EligibilityOutcome.ACTIVE
        assert not result.prior_authorization_required
        assert (result.plan_begin, result.plan_end) == ("20260101", "20261231")
        copay = next(b for b in result.benefits if b.code == "B")
        assert copay.amount == "25"
        assert copay.time_period == "27"
        assert copay.messages == ("COPAY APPLIES PER OFFICE VISIT",)
        assert copay.in_network is True
        assert copay.authorization_required is False
        coinsurance = next(b for b in result.benefits if b.code == "A")
        assert coinsurance.percent == "0.2"
        assert [b.code for b in result.benefits] == ["1", "B", "C", "A"]  # LS/LE skipped

    def test_prior_authorization(self) -> None:
        _, result = self.evaluate("prior_auth")
        assert result.outcome is EligibilityOutcome.ACTIVE
        assert result.prior_authorization_required

    def test_inactive_and_not_covered(self) -> None:
        assert self.evaluate("inactive")[1].outcome is EligibilityOutcome.INACTIVE
        _, result = self.evaluate("not_covered")
        assert result.outcome is EligibilityOutcome.INACTIVE
        assert result.rejection_reason == "service type(s) 3 not covered"

    def test_rejections_and_retryable_errors(self) -> None:
        _, result = self.evaluate("not_found")
        assert result.outcome is EligibilityOutcome.REJECTED
        assert result.rejection_code == "75"
        assert result.rejection_reason == "Subscriber/Insured Not Found"
        assert result.follow_up_action == "Please Correct and Resubmit"
        assert self.evaluate("busy")[1].outcome is EligibilityOutcome.UNAVAILABLE
        _, payer = self.evaluate("payer_down")
        assert payer.outcome is EligibilityOutcome.UNAVAILABLE
        assert payer.rejection_code == "42"

    def test_undeterminable_requires_manual_verification(self) -> None:
        assert self.evaluate("unknown")[1].outcome is EligibilityOutcome.MANUAL_REQUIRED

    def test_trace_mismatch_is_refused(self) -> None:
        response = parse_271(x12_271(trace="OTHER", member_id="M", scenario="active"))
        with pytest.raises(X12Error, match="TRN"):
            evaluate_271(response, requested=("3",), trace_number="T1", checked_at=NOW)

    def test_custom_delimiters(self) -> None:
        text = x12_271(trace="T1", member_id="M1", scenario="active")
        swapped = text.replace("*", "|").replace("^", "}")
        swapped = swapped.replace("|P|:", "|P|>")
        response = parse_271(swapped)
        assert response.benefits[0].service_types == ("30", "3", "98")

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda t: t.replace("SE*", "SE*9", 1), "SE01"),
            (lambda t: t.replace("IEA*1*000000101", "IEA*1*000000999"), "IEA02"),
            (lambda t: t.replace("IEA*1*", "IEA*2*"), "IEA01"),
            (lambda t: t.replace("GE*1*7", "GE*1*8"), "GE does not match"),
            (lambda t: t.replace("SE*", "XX*"), "without SE"),
            (lambda t: t.replace("GE*1*7~\n", ""), "expected ST, found IEA"),
            (lambda t: t.replace("GS*", "ZZ*", 1), "expected GS"),
            (lambda t: "garbage", "missing ISA"),
            (lambda t: t.replace("ST*271", "ST*270"), "expected a 271"),
        ],
    )
    def test_envelope_integrity(self, mutate, message: str) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(X12Error, match=message):
            parse_271(mutate(x12_271(trace="T1", member_id="M", scenario="active")))

    def test_acknowledgements_are_reported(self) -> None:
        isa = x12_271(trace="T", member_id="M", scenario="active").split("~")[0]
        ta1 = f"{isa}~TA1*000000101*261014*1300*R*022~IEA*0*000000101~"
        with pytest.raises(X12Error, match="TA1 status R"):
            parse_271(ta1)
        ack = (
            f"{isa}~GS*FA*CH01*SUB01*20261014*1300*7*X*005010X231A1~ST*999*0001*005010X231A1~"
            "AK1*HS*1*005010X279A1~AK9*R*1*1*0~SE*4*0001~GE*1*7~IEA*1*000000101~"
        )
        with pytest.raises(X12Error, match="AK9=R"):
            parse_271(ack)

    def test_bad_isa(self) -> None:
        text = x12_271(trace="T", member_id="M", scenario="active")
        with pytest.raises(X12Error, match="16 elements"):
            parse_271(text.replace("*00501*", "*00501**", 1).replace("ISA*00*  ", "ISA*00* ", 1))
        with pytest.raises(X12Error, match="distinct"):
            parse_271(text[:104] + "~" + text[105:])

    def test_empty_interchange(self) -> None:
        isa = x12_271(trace="T", member_id="M", scenario="active").split("~")[0]
        with pytest.raises(X12Error, match="no transaction sets"):
            parse_271(f"{isa}~IEA*0*000000101~")
