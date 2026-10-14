"""Shipped configuration is valid and consistent with the code."""

from __future__ import annotations

import json
import re

import pytest
import yaml
from agentic_core.models.factory import ProvidersConfig, env_references
from agentic_core.policy import PolicySet

from patient_access_agents.domain.errors import UnauthorizedError
from patient_access_agents.observability.metrics import AccessMetrics
from patient_access_agents.security.authn import claimed_issuer
from tests.conftest import ROOT


def metric_families() -> set[str]:
    registry = AccessMetrics().registry
    names: set[str] = set()
    for family in registry.collect():
        suffixes = ("", "_total", "_count", "_sum", "_bucket", "_created")
        names.update(family.name + suffix for suffix in suffixes)
    return names


def test_alert_rules_reference_real_metrics() -> None:
    rules = yaml.safe_load((ROOT / "configs" / "prometheus" / "alerts.yaml").read_text("utf-8"))
    known = metric_families() | {"up"}
    recorded = {r["record"] for g in rules["groups"] for r in g["rules"] if "record" in r}
    for group in rules["groups"]:
        for rule in group["rules"]:
            for name in re.findall(r"\b(paa_[a-z_]+)\b", rule["expr"]):
                assert name in known, name
            for name in re.findall(r"\b(paa:[a-z_:0-9]+)\b", rule["expr"]):
                assert name in recorded, name
            if "alert" in rule:
                runbook = rule["annotations"]["runbook"].split("#")[0]
                assert (ROOT / runbook).is_file(), runbook


def test_dashboard_queries_reference_real_metrics() -> None:
    dashboard = json.loads(
        (ROOT / "configs" / "grafana" / "patient-access-agents.json").read_text("utf-8")
    )
    known = metric_families()
    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            for name in re.findall(r"\b(paa_[a-z_]+)\b", target["expr"]):
                assert name in known, name


def test_policy_and_providers_parse() -> None:
    policy = PolicySet.from_file(ROOT / "policies" / "patient-access.yaml")
    assert policy.name == "patient-access"
    assert all(rule.description or rule.id.startswith("allow-") for rule in policy.rules)
    text = (ROOT / "configs" / "providers.yaml").read_text("utf-8")
    references = env_references(text)
    values = {name: "2.5" if name.endswith("_MTOK") else "us-central1" for name in references}
    config = ProvidersConfig.from_yaml(text, env=values)
    assert [p.profile.name for p in config.providers] == ["gemini-pro", "gemini-flash"]
    assert all(p.profile.max_classification == 2 for p in config.providers)


@pytest.mark.parametrize("token", ["a.b", "a.%%%.c", "a.bm90LWpzb24.c"])
def test_malformed_tokens_never_reach_a_verifier(token: str) -> None:
    with pytest.raises(UnauthorizedError):
        claimed_issuer(token)


def test_issuer_peek_tolerates_non_object_payloads() -> None:
    assert claimed_issuer("a.WzFd.c") is None  # payload "[1]"
    assert claimed_issuer("a.eyJpc3MiOjF9.c") is None  # {"iss":1}


def test_linkedin_article_length_and_hashtags() -> None:
    article = (ROOT / "docs" / "linkedin-article.md").read_text("utf-8").strip()
    assert 2000 <= len(article) <= 2500
    for tag in ("#AI", "#AgenticAI", "#LLMOps", "#Kubernetes", "#MLOps", "#Innovation"):
        assert tag in article
    assert "github.com/kogunlowo123/patient-access-agents" in article
