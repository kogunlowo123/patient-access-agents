"""Committed JSON Schemas and OpenAPI must match the code."""

from __future__ import annotations

from pathlib import Path

from patient_access_agents.contracts import main
from tests.conftest import ROOT


def test_committed_contracts_are_current() -> None:
    assert main(["--check"], root=ROOT) == 0


def test_export_and_check_round_trip(tmp_path: Path) -> None:
    assert main(["--check"], root=tmp_path) == 1
    assert main([], root=tmp_path) == 0
    assert main(["--check"], root=tmp_path) == 0
    assert (tmp_path / "schemas" / "intake.request.schema.json").is_file()
    assert (tmp_path / "docs" / "api" / "openapi.json").is_file()
