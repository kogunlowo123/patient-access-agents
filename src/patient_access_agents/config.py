"""Runtime settings, read from PAA_* environment variables."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from patient_access_agents.domain.models import valid_timezone


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAA_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="External base URL; used in the A2A Agent Card")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    audit_log_path: Path = Path("/var/lib/patient-access/audit/audit.jsonl")
    audit_key_file: Path | None = Field(default=None, description="Ed25519 PEM for audit signing")
    dev_ephemeral_audit_key: bool = Field(
        default=False, description="In-memory audit key; refused when environment=production"
    )

    fhir_base_url: HttpUrl
    fhir_auth: Literal["none", "fabric"] = "none"
    fhir_scopes: tuple[str, ...] = ("fhir:read:*", "fhir:write:slot", "fhir:write:appointment")

    oidc_issuer: str = Field(description="OIDC issuer for patients and staff")
    oidc_audience: str
    oidc_jwks_url: str
    roles_claim: str = "roles"
    patient_claim: str = "patient"

    fabric_url: str | None = Field(default=None, description="agent-identity-fabric base URL")
    fabric_client_id: str | None = Field(default=None, description="This service's SPIFFE ID")
    workload_token_file: Path | None = Field(
        default=None, description="Projected service-account token presented to the fabric"
    )
    agent_issuer: str | None = None
    agent_audience: str | None = None
    agent_jwks_url: str | None = None

    clearinghouse_url: str | None = None
    clearinghouse_api_key: SecretStr | None = None
    x12_sender_id: str | None = None
    x12_receiver_id: str | None = None
    x12_application_sender: str | None = None
    x12_application_receiver: str | None = None
    x12_usage: Literal["P", "T"] = "P"
    provider_name: str | None = None
    provider_npi: str | None = None
    provider_tax_id: str | None = None

    notification_webhook_url: str | None = None
    notification_signing_key: SecretStr | None = None

    policy_file: Path = Path("policies/patient-access.yaml")
    rules_file: Path = Path("configs/access-rules.yaml")
    providers_file: Path | None = Field(
        default=None, description="configs/providers.yaml; unset disables model drafting"
    )

    model_token_source: Literal["env", "file", "gke-metadata"] = Field(
        default="env",
        description="Where the model access token comes from: the environment, a file "
        "refreshed by the platform, or the GKE metadata server (Workload Identity)",
    )
    model_token_file: Path | None = None
    model_token_env: str = Field(
        default="GCP_ACCESS_TOKEN", description="providers.yaml variable the token file feeds"
    )
    model_refresh_minutes: float = Field(default=30.0, ge=1.0, le=55.0)

    default_timezone: str = "America/New_York"
    workers_enabled: bool = True
    worker_interval_s: float = Field(default=60.0, ge=1.0, le=3600.0)

    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @field_validator("default_timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        return valid_timezone(value)

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        if self.dev_ephemeral_audit_key and self.environment == "production":
            raise ValueError("dev_ephemeral_audit_key cannot be used in production")
        if not self.dev_ephemeral_audit_key and self.audit_key_file is None:
            raise ValueError("PAA_AUDIT_KEY_FILE is required")
        if self.fhir_auth == "fabric" and not (
            self.fabric_url and self.fabric_client_id and self.workload_token_file
        ):
            raise ValueError(
                "fhir_auth=fabric needs PAA_FABRIC_URL, PAA_FABRIC_CLIENT_ID and "
                "PAA_WORKLOAD_TOKEN_FILE"
            )
        agent = (self.agent_issuer, self.agent_audience, self.agent_jwks_url)
        if any(agent) and not all(agent):
            raise ValueError("agent_issuer, agent_audience and agent_jwks_url go together")
        if self.clearinghouse_url:
            missing = [
                name
                for name, value in (
                    ("PAA_X12_SENDER_ID", self.x12_sender_id),
                    ("PAA_X12_RECEIVER_ID", self.x12_receiver_id),
                    ("PAA_X12_APPLICATION_SENDER", self.x12_application_sender),
                    ("PAA_X12_APPLICATION_RECEIVER", self.x12_application_receiver),
                    ("PAA_PROVIDER_NAME", self.provider_name),
                    ("PAA_PROVIDER_NPI", self.provider_npi),
                    ("PAA_PROVIDER_TAX_ID", self.provider_tax_id),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"clearinghouse configuration incomplete: {missing}")
        # Comparing a configuration enum value, not a credential.
        token_from_file = self.model_token_source == "file"  # noqa: S105  # nosec B105
        if token_from_file and self.model_token_file is None:
            raise ValueError("model_token_source=file needs PAA_MODEL_TOKEN_FILE")
        if self.notification_webhook_url and (
            self.notification_signing_key is None
            or len(self.notification_signing_key.get_secret_value()) < 32
        ):
            raise ValueError("PAA_NOTIFICATION_SIGNING_KEY (>= 32 chars) is required")
        return self

    @property
    def public_url_str(self) -> str:
        return str(self.public_url).rstrip("/")
