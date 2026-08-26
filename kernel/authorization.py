"""Host-verifiable authorization grants for actions with side effects."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import uuid
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Optional, Protocol, runtime_checkable


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class AuthorizationGrant:
    """A short-lived permission bound to one task and diagnosis revision."""

    grant_id: str
    subject: str
    kind: str
    allowed_actions: tuple[str, ...]
    task_id: str
    case_id: str
    diagnosis_revision: int
    policy_digest: str
    issued_at: float
    expires_at: float
    source_id: str = ""
    allowed_capabilities: tuple[str, ...] = ()
    signature: str = ""

    def __post_init__(self) -> None:
        if not self.grant_id.strip():
            raise ValueError("authorization grant_id is required")
        if not self.subject.strip():
            raise ValueError("authorization subject is required")
        if self.kind not in {"human", "automation"}:
            raise ValueError("authorization kind must be human or automation")
        actions = tuple(str(item).strip() for item in self.allowed_actions)
        capabilities = tuple(
            str(item).strip() for item in self.allowed_capabilities
        )
        if (
            (not actions and not capabilities)
            or any(not item for item in actions)
            or any(not item for item in capabilities)
            or len(set(actions)) != len(actions)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise ValueError(
                "authorization actions/capabilities must be non-empty "
                "and unique"
            )
        if not self.task_id.strip() or not self.case_id.strip():
            raise ValueError("authorization task_id and case_id are required")
        if self.diagnosis_revision < 0:
            raise ValueError(
                "authorization diagnosis_revision cannot be negative"
            )
        if (
            len(self.policy_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.policy_digest.lower()
            )
        ):
            raise ValueError("authorization policy_digest must be sha256")
        if (
            not math.isfinite(self.issued_at)
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.issued_at
        ):
            raise ValueError("authorization expiry must follow issuance")
        if self.kind == "human" and not self.source_id.strip():
            raise ValueError(
                "human authorization requires a source_id"
            )
        object.__setattr__(self, "allowed_actions", actions)
        object.__setattr__(
            self, "allowed_capabilities", capabilities
        )

    def unsigned_payload(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "subject": self.subject,
            "kind": self.kind,
            "allowed_actions": list(self.allowed_actions),
            "task_id": self.task_id,
            "case_id": self.case_id,
            "diagnosis_revision": self.diagnosis_revision,
            "policy_digest": self.policy_digest,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "source_id": self.source_id,
            "allowed_capabilities": list(self.allowed_capabilities),
        }

    def to_dict(self) -> dict:
        return {**self.unsigned_payload(), "signature": self.signature}

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuthorizationGrant":
        return cls(
            grant_id=str(payload["grant_id"]),
            subject=str(payload["subject"]),
            kind=str(payload["kind"]),
            allowed_actions=tuple(payload["allowed_actions"]),
            task_id=str(payload["task_id"]),
            case_id=str(payload["case_id"]),
            diagnosis_revision=int(payload["diagnosis_revision"]),
            policy_digest=str(payload["policy_digest"]),
            issued_at=float(payload["issued_at"]),
            expires_at=float(payload["expires_at"]),
            source_id=str(payload.get("source_id", "")),
            allowed_capabilities=tuple(
                payload.get("allowed_capabilities", ())
            ),
            signature=str(payload.get("signature", "")),
        )


@runtime_checkable
class AuthorizationVerifier(Protocol):
    def verify(
        self,
        grant: AuthorizationGrant,
        *,
        action_id: str,
        task_id: str,
        case_id: str,
        diagnosis_revision: int,
        policy_digest: str,
        now: Optional[float] = None,
    ) -> bool: ...

    def verify_capability(
        self,
        grant: AuthorizationGrant,
        *,
        capability_id: str,
        task_id: str,
        case_id: str,
        diagnosis_revision: int,
        policy_digest: str,
        now: Optional[float] = None,
    ) -> bool: ...


class HMACAuthorizationAuthority:
    """Reference authority for hosts that can protect a local signing key."""

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError(
                "authorization secret must contain at least 32 bytes"
            )
        self._secret = secret

    def _signature(self, payload: Mapping[str, object]) -> str:
        return hmac.new(
            self._secret, _canonical(payload), hashlib.sha256
        ).hexdigest()

    def issue(
        self,
        *,
        subject: str,
        kind: str,
        allowed_actions: Iterable[str],
        task_id: str,
        case_id: str,
        diagnosis_revision: int,
        policy_digest: str,
        source_id: str = "",
        allowed_capabilities: Iterable[str] = (),
        ttl_seconds: float = 900,
        now: Optional[float] = None,
    ) -> AuthorizationGrant:
        issued_at = time.time() if now is None else float(now)
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError(
                "authorization ttl_seconds must be positive and finite"
            )
        grant = AuthorizationGrant(
            grant_id=f"grant-{uuid.uuid4().hex}",
            subject=subject,
            kind=kind,
            allowed_actions=tuple(allowed_actions),
            task_id=task_id,
            case_id=case_id,
            diagnosis_revision=int(diagnosis_revision),
            policy_digest=policy_digest,
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            source_id=source_id,
            allowed_capabilities=tuple(allowed_capabilities),
        )
        return replace(
            grant,
            signature=self._signature(grant.unsigned_payload()),
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        *,
        action_id: str,
        task_id: str,
        case_id: str,
        diagnosis_revision: int,
        policy_digest: str,
        now: Optional[float] = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            return False
        if (
            current >= grant.expires_at
            or grant.issued_at > current + 30
            or action_id not in grant.allowed_actions
            or grant.task_id != task_id
            or grant.case_id != case_id
            or grant.diagnosis_revision != int(diagnosis_revision)
            or grant.policy_digest != policy_digest
        ):
            return False
        return hmac.compare_digest(
            grant.signature,
            self._signature(grant.unsigned_payload()),
        )

    def verify_capability(
        self,
        grant: AuthorizationGrant,
        *,
        capability_id: str,
        task_id: str,
        case_id: str,
        diagnosis_revision: int,
        policy_digest: str,
        now: Optional[float] = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            return False
        if (
            current >= grant.expires_at
            or grant.issued_at > current + 30
            or capability_id not in grant.allowed_capabilities
            or grant.task_id != task_id
            or grant.case_id != case_id
            or grant.diagnosis_revision != int(diagnosis_revision)
            or grant.policy_digest != policy_digest
        ):
            return False
        return hmac.compare_digest(
            grant.signature,
            self._signature(grant.unsigned_payload()),
        )
