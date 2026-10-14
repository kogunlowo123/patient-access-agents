"""Load profile for a deployed patient access service.

Run against a staging deployment with synthetic data, never production:

    PAA_LOAD_STAFF_TOKEN=<scheduler token> PAA_LOAD_AGENT_TOKEN=<agent credential> \
    PAA_LOAD_SERVICE_REQUESTS=sr-1,sr-2,sr-3 \
    locust -f tests/performance/locustfile.py --host https://<staging host> \
           --users 100 --spawn-rate 10 --run-time 10m --headless

Status reads and queue views dominate access-centre traffic; referral
processing (FHIR + X12 round trips) is the expensive, rarer path.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, between, task

STAFF = os.environ.get("PAA_LOAD_STAFF_TOKEN", "")
AGENT = os.environ.get("PAA_LOAD_AGENT_TOKEN", "")
REFERRALS = [r for r in os.environ.get("PAA_LOAD_SERVICE_REQUESTS", "").split(",") if r]


class AccessCentre(HttpUser):
    wait_time = between(0.2, 1.0)

    @task(10)
    def list_cases(self) -> None:
        self.client.get(
            "/v1/cases?limit=50", headers={"Authorization": f"Bearer {STAFF}"}, name="/v1/cases"
        )

    @task(5)
    def queue(self) -> None:
        self.client.get(
            "/v1/escalations?status=open",
            headers={"Authorization": f"Bearer {STAFF}"},
            name="/v1/escalations",
        )

    @task(2)
    def process_referral(self) -> None:
        if not REFERRALS:
            return
        self.client.post(
            "/v1/referrals/process",
            json={"service_request_id": random.choice(REFERRALS)},
            headers={"Authorization": f"Bearer {AGENT}"},
            name="/v1/referrals/process",
        )

    @task(1)
    def agent_card(self) -> None:
        self.client.get("/.well-known/agent-card.json", name="/.well-known/agent-card.json")
