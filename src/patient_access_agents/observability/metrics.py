"""Service metrics exposed on /metrics in Prometheus text format."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class AccessMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        self.requests = Histogram(
            "paa_http_request_duration_seconds",
            "HTTP request latency",
            ["method", "route", "status"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        self.transitions = Counter(
            "paa_case_transitions_total",
            "Case state transitions",
            ["to_status"],
            registry=self.registry,
        )
        self.eligibility = Counter(
            "paa_eligibility_outcomes_total",
            "Eligibility verification outcomes",
            ["outcome"],
            registry=self.registry,
        )
        self.slot_options = Histogram(
            "paa_slot_options",
            "Ranked appointment options returned per search",
            registry=self.registry,
            buckets=(0, 1, 2, 3, 5, 10),
        )
        self.bookings = Counter(
            "paa_bookings_total",
            "Booking requests by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.reminders = Counter(
            "paa_reminder_deliveries_total",
            "Reminder delivery attempts",
            ["channel", "outcome"],
            registry=self.registry,
        )
        self.composer = Counter(
            "paa_message_composer_total",
            "Patient message drafting by composer and fallback reason",
            ["composer", "reason"],
            registry=self.registry,
        )
        self.escalations = Counter(
            "paa_escalations_total",
            "Escalations raised to human queues",
            ["reason", "queue"],
            registry=self.registry,
        )
        self.open_escalations = Gauge(
            "paa_open_escalations",
            "Open or claimed escalations by queue",
            ["queue"],
            registry=self.registry,
        )
        self.authz = Counter(
            "paa_authorization_decisions_total",
            "Authorization decisions",
            ["action", "decision"],
            registry=self.registry,
        )
        self.agent_calls = Counter(
            "paa_agent_protocol_calls_total",
            "MCP tool and A2A calls",
            ["protocol", "operation", "outcome"],
            registry=self.registry,
        )
        self.worker_runs = Counter(
            "paa_worker_runs_total",
            "Background work cycles",
            ["outcome"],
            registry=self.registry,
        )
