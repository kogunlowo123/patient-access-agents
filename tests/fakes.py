"""In-process stand-ins for external systems, speaking their real wire formats.

- ``FakeFhirServer``: FHIR R4 REST (read, search with paging and _include,
  versioned PUT with If-Match, conditional create with If-None-Exist).
- ``FakeClearinghouse``: accepts X12 270, answers with an X12 271 chosen by
  the subscriber's member id.
- ``FakeFabric``: agent-identity-fabric introspection, authorization, token.
- ``FakeMessaging``: the SMS/e-mail webhook.
- ``FakeGemini``: Vertex AI generateContent.

All patient data here is synthetic.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

FHIR_HOST = "fhir.corp.test"
FHIR_BASE = f"https://{FHIR_HOST}/fhir"
CLEARINGHOUSE_URL = "https://clearinghouse.corp.test/eligibility"
FABRIC_URL = "https://identity.corp.test"
MESSAGING_URL = "https://messaging.corp.test/send"
GEMINI_HOST = "us-central1-aiplatform.googleapis.com"
NY = ZoneInfo("America/New_York")
START = datetime(2026, 10, 14, 13, 0, tzinfo=UTC)  # Wednesday 09:00 New York


@dataclass
class FakeClock:
    current: datetime = START

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


@dataclass
class FakeIdp:
    """OIDC/agent token issuer: ES256 key pair, JWKS, token minting on an injected clock."""

    issuer: str
    clock: FakeClock
    kid: str = "k1"
    key: ec.EllipticCurvePrivateKey = field(
        default_factory=lambda: ec.generate_private_key(ec.SECP256R1())
    )

    def jwks(self) -> dict[str, Any]:
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "alg": "ES256", "use": "sig"})
        return {"keys": [jwk]}

    def mint(self, *, sub: str, aud: str, ttl: int = 300, **extra: Any) -> str:
        now = int(self.clock.now().timestamp())
        claims = {"iss": self.issuer, "sub": sub, "aud": aud, "iat": now, "exp": now + ttl}
        claims.update(extra)
        return jwt.encode(claims, self.key, algorithm="ES256", headers={"kid": self.kid})


def _fhir_instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeFhirServer:
    def __init__(self, page_size: int = 25) -> None:
        self.resources: dict[tuple[str, str], dict[str, Any]] = {}
        self.page_size = page_size
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, int] = {}
        self.down = False
        self._ids = 0

    def put(self, resource: dict[str, Any]) -> dict[str, Any]:
        key = (resource["resourceType"], resource["id"])
        version = int(self.resources.get(key, {}).get("meta", {}).get("versionId", "0")) + 1
        stored = {**resource, "meta": {"versionId": str(version)}}
        self.resources[key] = stored
        return stored

    def of_type(self, resource_type: str) -> list[dict[str, Any]]:
        return [r for (t, _), r in self.resources.items() if t == resource_type]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("fhir down", request=request)
        for prefix, status in list(self.fail.items()):
            if request.url.path.startswith(prefix):
                del self.fail[prefix]
                return httpx.Response(status, json={"resourceType": "OperationOutcome"})
        parts = [p for p in request.url.path.split("/") if p][1:]  # drop "fhir"
        handlers = {
            ("GET", 2): self._read,
            ("GET", 1): self._search_type,
            ("PUT", 2): self._update,
            ("POST", 1): self._create,
        }
        handler = handlers.get((request.method, len(parts)))
        if handler is None:
            return httpx.Response(400, json=_outcome("unsupported"))
        return handler(parts, request)

    def _read(self, parts: list[str], _: httpx.Request) -> httpx.Response:
        found = self.resources.get((parts[0], parts[1]))
        if found is None:
            return httpx.Response(404, json=_outcome("not found"))
        return httpx.Response(200, json=found)

    def _search_type(self, parts: list[str], request: httpx.Request) -> httpx.Response:
        return self._search(parts[0], request)

    def _update(self, parts: list[str], request: httpx.Request) -> httpx.Response:
        current = self.resources.get((parts[0], parts[1]))
        match = request.headers.get("if-match")
        if current and match and match != f'W/"{current["meta"]["versionId"]}"':
            return httpx.Response(412, json=_outcome("version conflict"))
        return httpx.Response(200, json=self.put(json.loads(request.content)))

    def _create(self, parts: list[str], request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        condition = request.headers.get("if-none-exist", "")
        if condition.startswith("identifier="):
            system, _, value = condition.removeprefix("identifier=").partition("|")
            for existing in self.of_type(parts[0]):
                for ident in existing.get("identifier", []):
                    if ident.get("system") == system and ident.get("value") == value:
                        return httpx.Response(200, json=existing)
        self._ids += 1
        body["id"] = f"{parts[0].lower()}-{self._ids}"
        return httpx.Response(201, json=self.put(body))

    def _search(self, resource_type: str, request: httpx.Request) -> httpx.Response:
        params = parse_qs(urlsplit(str(request.url)).query)
        page = int(params.pop("_page", ["0"])[0])
        size = min(int(params.get("_count", [str(self.page_size)])[0]), 100)
        matches = [r for r in self.of_type(resource_type) if _matches(r, params)]
        matches.sort(key=lambda r: (r.get("start", ""), r["id"]))
        chunk = matches[page * size : (page + 1) * size]
        entries = [{"resource": r, "search": {"mode": "match"}} for r in chunk]
        if "Slot:schedule" in params.get("_include", []):
            schedules = {r["schedule"]["reference"].split("/")[1] for r in chunk}
            included = [self.resources[("Schedule", s)] for s in sorted(schedules)]
            entries += [{"resource": s, "search": {"mode": "include"}} for s in included]
            if "Schedule:actor" in params.get("_include:iterate", []):
                actors = {
                    tuple(a["reference"].split("/")) for s in included for a in s.get("actor", [])
                }
                entries += [
                    {"resource": self.resources[a], "search": {"mode": "include"}}
                    for a in sorted(actors)
                    if a in self.resources
                ]
        bundle: dict[str, Any] = {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(matches),
            "entry": entries,
            "link": [{"relation": "self", "url": str(request.url)}],
        }
        if (page + 1) * size < len(matches):
            query = dict(params.items())
            query["_page"] = [str(page + 1)]
            pairs = "&".join(f"{k}={v}" for k, vs in query.items() for v in vs)
            bundle["link"].append(
                {"relation": "next", "url": f"{FHIR_BASE}/{resource_type}?{pairs}"}
            )
        return httpx.Response(200, json=bundle)


def _outcome(text: str) -> dict[str, Any]:
    return {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "processing", "diagnostics": text}],
    }


def _matches(resource: dict[str, Any], params: dict[str, list[str]]) -> bool:
    for name, values in params.items():
        if name.startswith("_"):
            continue
        for value in values:
            if not _match_one(resource, name, value):
                return False
    return True


def _match_one(resource: dict[str, Any], name: str, value: str) -> bool:
    if name == "status":
        return bool(resource.get("status") == value)
    if name == "patient":
        return bool(resource.get("beneficiary", {}).get("reference") == value)
    if name == "specialty":
        system, _, code = value.partition("|")
        return any(
            c.get("system") == system and c.get("code") == code
            for concept in resource.get("specialty", [])
            for c in concept.get("coding", [])
        )
    if name == "start":
        op, instant = value[:2], value[2:]
        start = resource.get("start", "")
        return start >= instant if op == "ge" else start <= instant
    if name == "identifier":
        system, _, code = value.partition("|")
        return any(
            i.get("system") == system and i.get("value") == code
            for i in resource.get("identifier", [])
        )
    return True


CARDIOLOGY = {"system": "http://snomed.info/sct", "code": "394579002", "display": "Cardiology"}


def seed_world(fhir: FakeFhirServer, *, days: int = 21) -> None:
    """A small synthetic health system: patients, coverage, a cardiology clinic and its slots."""
    fhir.put(
        {
            "resourceType": "Patient",
            "id": "pat-ana",
            "name": [{"use": "official", "family": "Rivera", "given": ["Ana", "Lucia"]}],
            "birthDate": "1980-04-02",
            "gender": "female",
            "telecom": [
                {"system": "phone", "value": "+1-555-0100"},
                {"system": "email", "value": "ana.rivera@mail.test"},
            ],
        }
    )
    fhir.put(
        {
            "resourceType": "Patient",
            "id": "pat-leo",
            "name": [{"family": "Rivera", "given": ["Leo"]}],
            "birthDate": "2015-09-30",
            "gender": "male",
            "communication": [
                {
                    "language": {"coding": [{"system": "urn:ietf:bcp:47", "code": "es-US"}]},
                    "preferred": True,
                }
            ],
        }
    )
    fhir.put(
        {
            "resourceType": "Organization",
            "id": "org-shp",
            "name": "Synthetic Health Plan",
            "identifier": [{"system": "urn:oid:2.16.840.1.113883.6.300", "value": "SHP01"}],
        }
    )
    coverages = {
        "cov-ana": ("pat-ana", "SHP000123", "self"),
        "cov-leo": ("pat-leo", "SHP000123", "child"),
    }
    for cid, (patient, member, relationship) in coverages.items():
        fhir.put(
            {
                "resourceType": "Coverage",
                "id": cid,
                "status": "active",
                "subscriberId": member,
                "beneficiary": {"reference": f"Patient/{patient}"},
                "subscriber": {"reference": "Patient/pat-ana"},
                "relationship": {"coding": [{"code": relationship}]},
                "payor": [{"reference": "Organization/org-shp"}],
                "class": [
                    {"type": {"coding": [{"code": "group"}]}, "value": "GRP100"},
                ],
            }
        )
    fhir.put(
        {
            "resourceType": "Practitioner",
            "id": "prac-lee",
            "name": [{"prefix": ["Dr."], "given": ["Morgan"], "family": "Lee"}],
        }
    )
    fhir.put(
        {
            "resourceType": "Practitioner",
            "id": "prac-okafor",
            "name": [{"text": "Dr. Chidi Okafor"}],
        }
    )
    fhir.put({"resourceType": "Location", "id": "loc-main", "name": "Riverbend Cardiology Clinic"})
    fhir.put({"resourceType": "Location", "id": "loc-north", "name": "North Campus Heart Center"})
    schedules = {
        "sch-lee": (["Practitioner/prac-lee", "Location/loc-main"], "AMB"),
        "sch-okafor": (["Practitioner/prac-okafor", "Location/loc-north"], "AMB"),
        "sch-lee-video": (["Practitioner/prac-lee"], "VR"),
    }
    for sid, (actors, mode) in schedules.items():
        fhir.put(
            {
                "resourceType": "Schedule",
                "id": sid,
                "active": True,
                "actor": [{"reference": a} for a in actors],
                "serviceCategory": [
                    {
                        "coding": [
                            {
                                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                                "code": mode,
                            }
                        ]
                    }
                ],
                "specialty": [{"coding": [CARDIOLOGY]}],
            }
        )
    for day in range(1, days + 1):
        local = START.astimezone(NY).date() + timedelta(days=day)
        if local.isoweekday() > 5:
            continue
        for sid, hours in (
            ("sch-lee", (9, 10, 11)),
            ("sch-okafor", (14, 15)),
            ("sch-lee-video", (16,)),
        ):
            for hour in hours:
                for minute in (0, 20, 40):
                    start = datetime(local.year, local.month, local.day, hour, minute, tzinfo=NY)
                    fhir.put(
                        {
                            "resourceType": "Slot",
                            "id": f"{sid}-{local:%m%d}-{hour:02d}{minute:02d}",
                            "schedule": {"reference": f"Schedule/{sid}"},
                            "status": "free",
                            "start": _fhir_instant(start),
                            "end": _fhir_instant(start + timedelta(minutes=20)),
                            "specialty": [{"coding": [CARDIOLOGY]}],
                        }
                    )
    referrals = {
        "sr-cardio": ("pat-ana", "routine", "active", "183519002", "Coverage/cov-ana"),
        "sr-cardio-stat": ("pat-ana", "stat", "active", "183519002", "Coverage/cov-ana"),
        "sr-leo": ("pat-leo", "urgent", "active", "183519002", "Coverage/cov-leo"),
        "sr-draft": ("pat-ana", "routine", "draft", "183519002", None),
        "sr-unknown": ("pat-ana", "routine", "active", "1234567", None),
    }
    for rid, (patient, priority, status, code, coverage) in referrals.items():
        resource: dict[str, Any] = {
            "resourceType": "ServiceRequest",
            "id": rid,
            "status": status,
            "intent": "order",
            "priority": priority,
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": code}]},
            "subject": {"reference": f"Patient/{patient}"},
            "requester": {"reference": "Practitioner/prac-okafor"},
            "authoredOn": _fhir_instant(START - timedelta(hours=1)),
        }
        if coverage:
            resource["insurance"] = [{"reference": coverage}]
        fhir.put(resource)


def add_referral(
    fhir: FakeFhirServer,
    rid: str,
    *,
    patient: str = "pat-ana",
    member: str = "SHP000123",
    priority: str = "routine",
    performer: str | None = None,
    location: str | None = None,
) -> None:
    """A referral with its own Coverage whose member id selects the clearinghouse scenario."""
    fhir.put(
        {
            "resourceType": "Coverage",
            "id": f"cov-{rid}",
            "status": "active",
            "subscriberId": member,
            "beneficiary": {"reference": f"Patient/{patient}"},
            "relationship": {"coding": [{"code": "self"}]},
            "payor": [{"reference": "Organization/org-shp"}],
        }
    )
    resource: dict[str, Any] = {
        "resourceType": "ServiceRequest",
        "id": rid,
        "status": "active",
        "intent": "order",
        "priority": priority,
        "code": {"coding": [{"system": "http://snomed.info/sct", "code": "183519002"}]},
        "subject": {"reference": f"Patient/{patient}"},
        "insurance": [{"reference": f"Coverage/cov-{rid}"}],
    }
    if performer:
        resource["performer"] = [{"reference": performer}]
    if location:
        resource["locationReference"] = [{"reference": location}]
    fhir.put(resource)


# ----------------------------------------------------------------------- X12


def x12_271(
    *,
    trace: str,
    member_id: str,
    scenario: str,
    control: str = "0001",
    interchange: int = 101,
) -> str:
    """A 005010X279A1 271 for the scenario, with correct envelope counts."""
    body: list[str] = [
        f"ST*271*{control}*005010X279A1",
        "BHT*0022*11*RESP*20261014*1300",
        "HL*1**20*1",
        "NM1*PR*2*SYNTHETIC HEALTH PLAN*****PI*SHP01",
    ]
    if scenario == "payer_down":
        body.append("AAA*N**42*R")
    body += [
        "HL*2*1*21*1",
        "NM1*1P*2*RIVERBEND SPECIALTY CLINIC*****XX*1234567893",
        "HL*3*2*22*0",
        f"TRN*2*{trace}*9877281234",
        f"NM1*IL*1*RIVERA*ANA****MI*{member_id}",
        "DMG*D8*19800402*F",
    ]
    if scenario == "not_found":
        body.append("AAA*N**75*C")
    elif scenario == "busy":
        body.append("AAA*Y**42*R")  # valid request, informational only
        body.append("AAA*N**80*R")
    else:
        body.append("DTP*291*RD8*20260101-20261231")
    if scenario in {"active", "prior_auth", "not_covered"}:
        auth = "Y" if scenario == "prior_auth" else "N"
        body += [
            "EB*1*IND*30^3^98*HM*GOLD PPO",
            "EB*B*IND*98*HM**27*25****N*Y",
            "MSG*COPAY APPLIES PER OFFICE VISIT",
            "EB*C*IND*30*HM**29*450",
            "LS*2120",
            "NM1*P3*1*CARE*PRIMARY",
            "LE*2120",
            f"EB*A*IND*3*HM**27**0.2***{auth}*Y",
        ]
        if scenario == "not_covered":
            body.append("EB*I*IND*3*HM")
    elif scenario == "inactive":
        body.append("EB*6*IND*30")
    elif scenario == "unknown":
        body.append("EB*P*IND*30")
    body.append(f"SE*{len(body) + 1}*{control}")
    isa = (
        "ISA*00*          *00*          *ZZ*CLEARING01     *ZZ*SUBMITTER01    "
        f"*261014*1300*^*00501*{interchange:09d}*0*P*:"
    )
    segments = [
        isa,
        "GS*HB*CH01*SUB01*20261014*1300*7*X*005010X279A1",
        *body,
        "GE*1*7",
        f"IEA*1*{interchange:09d}",
    ]
    return "~\n".join(segments) + "~\n"


MEMBER_SCENARIOS = {
    "SHP000123": "active",
    "SHP000PA": "prior_auth",
    "SHP000INACT": "inactive",
    "SHP000NF": "not_found",
    "SHP000BUSY": "busy",
    "SHP000NC": "not_covered",
    "SHP000UNK": "unknown",
}


class FakeClearinghouse:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.http_status: int | None = None
        self.raw_reply: str | None = None
        self.timeout = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        text = request.content.decode("ascii")
        self.requests.append(text)
        if self.timeout:
            raise httpx.ReadTimeout("slow payer", request=request)
        if self.http_status is not None:
            return httpx.Response(self.http_status, text="unavailable")
        if self.raw_reply is not None:
            return httpx.Response(200, text=self.raw_reply)
        segments = [s.strip() for s in text.split("~") if s.strip()]
        trace = next(s.split("*")[2] for s in segments if s.startswith("TRN*1*"))
        member = next(s.split("*")[9] for s in segments if s.startswith("NM1*IL*"))
        scenario = MEMBER_SCENARIOS.get(member, "not_found")
        return httpx.Response(
            200,
            text=x12_271(trace=trace, member_id=member, scenario=scenario),
            headers={"content-type": "application/EDI-X12"},
        )


@dataclass
class FakeFabric:
    """Introspection and authorization as served by agent-identity-fabric."""

    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    decision: str = "allow"
    down: bool = False
    calls: list[str] = field(default_factory=list)

    def issue(self, token: str, sub: str, scopes: str, act: dict[str, Any] | None = None) -> str:
        info: dict[str, Any] = {
            "active": True,
            "sub": sub,
            "scope": scopes,
            "exp": int(time.time()) + 600,
        }
        if act:
            info["act"] = act
        self.tokens[token] = info
        return token

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self.down:
            raise httpx.ConnectError("fabric down", request=request)
        if request.url.path == "/oauth2/introspect":
            token = parse_qs(request.content.decode())["token"][0]
            return httpx.Response(200, json=self.tokens.get(token, {"active": False}))
        if request.url.path == "/v1/authorize":
            token = request.headers.get("authorization", "")[7:]
            info = self.tokens.get(token)
            if not info:
                return httpx.Response(
                    401, json={"error": "unauthorized", "error_description": "inactive"}
                )
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "decision": self.decision,
                    "agent": info["sub"],
                    "action": body["action"],
                    "resource": f"{body['resource_type']}:{body['resource_id']}",
                    "matched_rules": ["fabric-rule"],
                    "policy_version": "2026.10.0",
                    "policy_digest": "sha256:" + "f" * 64,
                    "reason": f"fabric {self.decision}",
                    "delegation_chain": [info["sub"]],
                },
            )
        if request.url.path == "/oauth2/token":
            form = parse_qs(request.content.decode())
            if form.get("assertion", [""])[0] != "workload-token-value":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200,
                json={"access_token": "fhir-credential", "token_type": "Bearer", "expires_in": 600},
            )
        return httpx.Response(404)


@dataclass
class FakeMessaging:
    sent: list[dict[str, Any]] = field(default_factory=list)
    headers: list[httpx.Headers] = field(default_factory=list)
    status: int = 202

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status >= 400:
            return httpx.Response(self.status)
        self.sent.append(json.loads(request.content))
        self.headers.append(request.headers)
        return httpx.Response(self.status)


@dataclass
class FakeGemini:
    replies: list[str] = field(default_factory=list)
    status: int = 200
    requests: list[dict[str, Any]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "overloaded"}})
        text = self.replies.pop(0) if self.replies else ""
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 40},
            },
        )


@dataclass
class World:
    fhir: FakeFhirServer = field(default_factory=FakeFhirServer)
    clearinghouse: FakeClearinghouse = field(default_factory=FakeClearinghouse)
    fabric: FakeFabric = field(default_factory=FakeFabric)
    messaging: FakeMessaging = field(default_factory=FakeMessaging)
    gemini: FakeGemini = field(default_factory=FakeGemini)
    jwks: dict[str, dict[str, Any]] = field(default_factory=dict)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {
            FHIR_HOST: self.fhir.handle,
            "clearinghouse.corp.test": self.clearinghouse.handle,
            "identity.corp.test": self.fabric.handle,
            "messaging.corp.test": self.messaging.handle,
            GEMINI_HOST: self.gemini.handle,
        }
        if str(request.url) in self.jwks:
            return httpx.Response(200, json=self.jwks[str(request.url)])
        handler = routes.get(host)
        if handler is None:
            raise httpx.ConnectError(f"no route to {host}", request=request)
        return handler(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
