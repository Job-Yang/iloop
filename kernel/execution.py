"""Signed local execution envelopes, receipts, and in-process recipe replay."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from .capability import capability_id
from .authorization import (
    AuthorizationGrant, AuthorizationVerifier,
)
from .deployment import DeploymentAssembly
from .evidence import EvidenceArtifact
from .storage import atomic_write_json, file_lock


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _signature(secret: bytes, payload: Mapping[str, object]) -> str:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("execution signing secret must contain at least 32 bytes")
    return hmac.new(secret, _canonical(payload), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class TaskEnvelope:
    envelope_id: str
    task_id: str
    assistant_id: str
    deployment_id: str
    deployment_digest: str
    target_node: str
    diagnosis_revision: int
    base_commit: str
    policy: Mapping[str, object]
    inputs: Mapping[str, object]
    lifecycle_stage: str
    lifecycle: Mapping[str, object]
    recipe_digest: str
    execution_plan: Tuple[str, ...]
    capability_plan: Tuple[str, ...]
    evidence_plan: Tuple[str, ...]
    issued_at: float
    expires_at: float
    authorization: Mapping[str, object] = field(default_factory=dict)
    signature: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "envelope_id", "task_id", "assistant_id", "deployment_id",
            "target_node", "base_commit",
        ):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"TaskEnvelope {field_name} cannot be empty")
        if self.diagnosis_revision < 0:
            raise ValueError("TaskEnvelope diagnosis_revision cannot be negative")
        if self.lifecycle_stage not in {
            "diagnosis", "disposition", "verification", "observation",
        }:
            raise ValueError("TaskEnvelope lifecycle_stage is invalid")
        if (
            not math.isfinite(self.issued_at)
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.issued_at
        ):
            raise ValueError("TaskEnvelope expires_at must follow issued_at")
        for name, digest in (
            ("recipe_digest", self.recipe_digest),
            ("deployment_digest", self.deployment_digest),
        ):
            if len(digest) != 64:
                raise ValueError(
                    f"TaskEnvelope requires a {name} sha256 digest"
                )
            try:
                int(digest, 16)
            except ValueError as error:
                raise ValueError(
                    f"TaskEnvelope {name} must be hexadecimal"
                ) from error
        plan = tuple(str(item) for item in self.execution_plan)
        if not plan:
            raise ValueError("TaskEnvelope execution_plan cannot be empty")
        object.__setattr__(self, "policy", dict(self.policy))
        object.__setattr__(self, "inputs", dict(self.inputs))
        object.__setattr__(self, "lifecycle", dict(self.lifecycle))
        object.__setattr__(self, "authorization", dict(self.authorization))
        object.__setattr__(self, "execution_plan", plan)
        object.__setattr__(
            self,
            "capability_plan",
            tuple(capability_id(item).value for item in self.capability_plan),
        )
        object.__setattr__(
            self,
            "evidence_plan",
            tuple(str(item) for item in self.evidence_plan),
        )

    def unsigned_payload(self) -> dict:
        return {
            "envelope_id": self.envelope_id,
            "task_id": self.task_id,
            "assistant_id": self.assistant_id,
            "deployment_id": self.deployment_id,
            "deployment_digest": self.deployment_digest,
            "target_node": self.target_node,
            "diagnosis_revision": self.diagnosis_revision,
            "base_commit": self.base_commit,
            "policy": dict(self.policy),
            "inputs": dict(self.inputs),
            "lifecycle_stage": self.lifecycle_stage,
            "lifecycle": dict(self.lifecycle),
            "recipe_digest": self.recipe_digest,
            "execution_plan": list(self.execution_plan),
            "capability_plan": list(self.capability_plan),
            "evidence_plan": list(self.evidence_plan),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "authorization": dict(self.authorization),
        }

    def task_digest(self) -> str:
        return hashlib.sha256(_canonical(self.unsigned_payload())).hexdigest()

    def to_dict(self) -> dict:
        return {**self.unsigned_payload(), "signature": self.signature}

    @classmethod
    def issue(
        cls,
        *,
        task_id: str,
        assistant_id: str,
        deployment_id: str,
        deployment_digest: str,
        target_node: str,
        diagnosis_revision: int,
        base_commit: str,
        policy: Mapping[str, object],
        inputs: Mapping[str, object],
        lifecycle_stage: str,
        lifecycle: Mapping[str, object],
        recipe_digest: str,
        execution_plan: Iterable[str],
        capability_plan: Iterable[str],
        evidence_plan: Iterable[str],
        secret: bytes,
        ttl_seconds: float = 300,
        now: Optional[float] = None,
        authorization: Optional[AuthorizationGrant] = None,
    ) -> "TaskEnvelope":
        issued_at = time.time() if now is None else float(now)
        if not math.isfinite(issued_at):
            raise ValueError("TaskEnvelope issued_at must be finite")
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("TaskEnvelope ttl_seconds must be positive")
        plan = tuple(str(item) for item in execution_plan)
        if not recipe_digest or len(recipe_digest) != 64:
            raise ValueError("TaskEnvelope requires a recipe sha256 digest")
        if not plan:
            raise ValueError("TaskEnvelope execution_plan cannot be empty")
        unsigned = cls(
            envelope_id=f"env-{uuid.uuid4().hex}",
            task_id=task_id,
            assistant_id=assistant_id,
            deployment_id=deployment_id,
            deployment_digest=deployment_digest,
            target_node=target_node,
            diagnosis_revision=int(diagnosis_revision),
            base_commit=base_commit,
            policy=dict(policy),
            inputs=dict(inputs),
            lifecycle_stage=lifecycle_stage,
            lifecycle=dict(lifecycle),
            recipe_digest=str(recipe_digest),
            execution_plan=plan,
            capability_plan=tuple(str(item) for item in capability_plan),
            evidence_plan=tuple(str(item) for item in evidence_plan),
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            authorization=(
                authorization.to_dict() if authorization is not None else {}
            ),
        )
        return replace(
            unsigned,
            signature=_signature(secret, unsigned.unsigned_payload()),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "TaskEnvelope":
        return cls(
            envelope_id=str(payload["envelope_id"]),
            task_id=str(payload["task_id"]),
            assistant_id=str(payload["assistant_id"]),
            deployment_id=str(payload["deployment_id"]),
            deployment_digest=str(payload["deployment_digest"]),
            target_node=str(payload["target_node"]),
            diagnosis_revision=int(payload["diagnosis_revision"]),
            base_commit=str(payload["base_commit"]),
            policy=dict(payload["policy"]),
            inputs=dict(payload["inputs"]),
            lifecycle_stage=str(payload["lifecycle_stage"]),
            lifecycle=dict(payload.get("lifecycle", {})),
            recipe_digest=str(payload["recipe_digest"]),
            execution_plan=tuple(payload["execution_plan"]),
            capability_plan=tuple(payload.get("capability_plan", [])),
            evidence_plan=tuple(payload.get("evidence_plan", [])),
            issued_at=float(payload["issued_at"]),
            expires_at=float(payload["expires_at"]),
            authorization=dict(payload.get("authorization", {})),
            signature=str(payload.get("signature", "")),
        )

    def verify(
        self,
        secret: bytes,
        *,
        target_node: str,
        base_commit: str,
        deployment_id: str,
        now: Optional[float] = None,
    ) -> None:
        expected = _signature(secret, self.unsigned_payload())
        if not hmac.compare_digest(expected, self.signature):
            raise ValueError("TaskEnvelope signature mismatch")
        current = time.time() if now is None else float(now)
        if not math.isfinite(current):
            raise ValueError("TaskEnvelope verification time must be finite")
        if current >= self.expires_at:
            raise ValueError("TaskEnvelope expired")
        if self.issued_at > current + 30:
            raise ValueError("TaskEnvelope issued_at is in the future")
        if self.target_node != target_node:
            raise ValueError("TaskEnvelope target_node mismatch")
        if self.base_commit != base_commit:
            raise ValueError("TaskEnvelope base_commit mismatch")
        if self.deployment_id != deployment_id:
            raise ValueError("TaskEnvelope deployment_id mismatch")


class ReplayGuard:
    """Single-use envelope ledger, optionally durable across worker restarts."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._consumed = set()
        if self.path and self.path.is_file():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._consumed = set(payload.get("consumed", []))

    def consume(self, envelope: TaskEnvelope) -> None:
        digest = envelope.task_digest()
        with self._lock:
            if digest in self._consumed:
                raise ValueError("TaskEnvelope replay detected")
            if not self.path:
                self._consumed.add(digest)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(Path(f"{self.path}.lock")):
                existing = set()
                if self.path.is_file():
                    existing.update(json.loads(
                        self.path.read_text(encoding="utf-8")
                    ).get("consumed", []))
                if digest in existing:
                    self._consumed = existing
                    raise ValueError("TaskEnvelope replay detected")
                existing.add(digest)
                atomic_write_json(
                    self.path, {"consumed": sorted(existing)}
                )
                self._consumed = existing


@dataclass(frozen=True)
class WorkerEvidence:
    evidence_type: str
    capability: str
    source: str
    outcome: str
    artifact_sha256: str
    artifact_path: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.evidence_type not in {
            "action_result", "execution_record", "observed",
        }:
            raise ValueError("unsupported worker evidence_type")
        if self.outcome not in {"success", "failure"}:
            raise ValueError("unsupported worker evidence outcome")
        if len(self.artifact_sha256) != 64:
            raise ValueError("worker evidence requires a sha256 digest")
        try:
            int(self.artifact_sha256, 16)
        except ValueError as error:
            raise ValueError(
                "worker evidence sha256 must be hexadecimal"
            ) from error
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "WorkerEvidence":
        return cls(
            evidence_type=str(payload["evidence_type"]),
            capability=str(payload["capability"]),
            source=str(payload["source"]),
            outcome=str(payload["outcome"]),
            artifact_sha256=str(payload["artifact_sha256"]),
            artifact_path=str(payload.get("artifact_path", "")),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class WorkerReceipt:
    receipt_id: str
    envelope_id: str
    task_digest: str
    target_node: str
    status: str
    evidence: Tuple[WorkerEvidence, ...]
    created_at: float
    signature: str = ""

    def unsigned_payload(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "envelope_id": self.envelope_id,
            "task_digest": self.task_digest,
            "target_node": self.target_node,
            "status": self.status,
            "evidence": [asdict(item) for item in self.evidence],
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict:
        return {**self.unsigned_payload(), "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "WorkerReceipt":
        return cls(
            receipt_id=str(payload["receipt_id"]),
            envelope_id=str(payload["envelope_id"]),
            task_digest=str(payload["task_digest"]),
            target_node=str(payload["target_node"]),
            status=str(payload["status"]),
            evidence=tuple(
                WorkerEvidence.from_dict(item)
                for item in payload.get("evidence", [])
            ),
            created_at=float(payload["created_at"]),
            signature=str(payload.get("signature", "")),
        )

    @classmethod
    def issue(
        cls,
        envelope: TaskEnvelope,
        *,
        target_node: str,
        status: str,
        evidence: Iterable[WorkerEvidence],
        secret: bytes,
    ) -> "WorkerReceipt":
        items = tuple(evidence)
        if status not in {"success", "failure"}:
            raise ValueError("unsupported WorkerReceipt status")
        if target_node != envelope.target_node:
            raise ValueError("WorkerReceipt target_node must match TaskEnvelope")
        if status == "success" and not items:
            raise ValueError("successful WorkerReceipt requires typed evidence")
        cls._validate_observed_artifacts(items)
        cls._validate_success_evidence(envelope, status, items)
        unsigned = cls(
            receipt_id=f"receipt-{uuid.uuid4().hex}",
            envelope_id=envelope.envelope_id,
            task_digest=envelope.task_digest(),
            target_node=target_node,
            status=status,
            evidence=items,
            created_at=time.time(),
        )
        return replace(
            unsigned,
            signature=_signature(secret, unsigned.unsigned_payload()),
        )

    def verify(
        self,
        envelope: TaskEnvelope,
        secret: bytes,
        *,
        target_node: str,
    ) -> None:
        expected = _signature(secret, self.unsigned_payload())
        if not hmac.compare_digest(expected, self.signature):
            raise ValueError("WorkerReceipt signature mismatch")
        if self.envelope_id != envelope.envelope_id:
            raise ValueError("WorkerReceipt envelope_id mismatch")
        if self.task_digest != envelope.task_digest():
            raise ValueError("WorkerReceipt task_digest mismatch")
        if self.target_node != target_node:
            raise ValueError("WorkerReceipt target_node mismatch")
        if self.target_node != envelope.target_node:
            raise ValueError("WorkerReceipt does not match envelope target_node")
        if self.status == "success" and not self.evidence:
            raise ValueError("successful WorkerReceipt requires typed evidence")
        self._validate_observed_artifacts(self.evidence)
        self._validate_success_evidence(
            envelope, self.status, self.evidence
        )

    @staticmethod
    def _validate_observed_artifacts(
        evidence: Tuple[WorkerEvidence, ...],
    ) -> None:
        for item in evidence:
            if item.evidence_type != "observed":
                continue
            path = Path(item.artifact_path)
            if (
                not item.artifact_path
                or not path.exists()
                or EvidenceArtifact._artifact_digest(path)
                != item.artifact_sha256
            ):
                raise ValueError(
                    "WorkerReceipt observed artifact changed or is missing"
                )

    @staticmethod
    def _validate_success_evidence(
        envelope: TaskEnvelope,
        status: str,
        evidence: Tuple[WorkerEvidence, ...],
    ) -> None:
        if status != "success":
            return
        if any(item.outcome != "success" for item in evidence):
            raise ValueError(
                "successful WorkerReceipt cannot contain failure evidence"
            )
        completed_actions = tuple(
            item.capability
            for item in evidence
            if item.evidence_type == "action_result"
        )
        if completed_actions != envelope.execution_plan:
            raise ValueError(
                "successful WorkerReceipt does not cover the signed execution plan"
            )
        completed_capabilities = tuple(
            item.capability
            for item in evidence
            if item.evidence_type in {"execution_record", "observed"}
        )
        if completed_capabilities != envelope.capability_plan:
            raise ValueError(
                "successful WorkerReceipt does not cover the signed capability plan"
            )
        actual_plan = tuple(
            (
                f"action:{item.capability}"
                if item.evidence_type == "action_result"
                else f"capability:{item.capability}"
            )
            for item in evidence
        )
        if actual_plan != envelope.evidence_plan:
            raise ValueError(
                "successful WorkerReceipt evidence order does not match "
                "the signed evidence plan"
            )


class LocalRecipeWorker:
    """In-process contract runner. Transport and remote identity stay external."""

    def __init__(
        self,
        assembly: DeploymentAssembly,
        *,
        secret: bytes,
        replay_guard: ReplayGuard,
        base_commit: str,
        authorization_verifier: Optional[AuthorizationVerifier] = None,
    ) -> None:
        self.assembly = assembly
        self.recipes = assembly.recipes
        self.secret = secret
        self.replay_guard = replay_guard
        self.base_commit = base_commit
        self.authorization_verifier = authorization_verifier

    def execute(
        self,
        envelope: TaskEnvelope,
    ) -> WorkerReceipt:
        profile = self.assembly.profile
        envelope.verify(
            self.secret,
            target_node=profile.target_node,
            base_commit=self.base_commit,
            deployment_id=profile.deployment_id,
        )
        if envelope.assistant_id != self.assembly.assistant.recipe.assistant_id:
            raise ValueError("TaskEnvelope assistant_id mismatch")
        if envelope.recipe_digest != self.assembly.assistant.fingerprint():
            raise ValueError("TaskEnvelope recipe_digest mismatch")
        if envelope.deployment_digest != self.assembly.fingerprint():
            raise ValueError("TaskEnvelope deployment_digest mismatch")
        specs_by_id = {
            spec.action_id: spec for spec in self.assembly.assistant.actions
        }
        try:
            selected_specs = tuple(
                specs_by_id[action_id]
                for action_id in envelope.execution_plan
            )
        except KeyError as error:
            raise ValueError("TaskEnvelope execution_plan is unknown") from error
        recipe_order = [
            action_id for action_id in self.assembly.assistant.recipe.actions
            if action_id in envelope.execution_plan
        ]
        if recipe_order != list(envelope.execution_plan):
            raise ValueError("TaskEnvelope execution_plan order mismatch")
        if any(
            spec.lifecycle_stage != envelope.lifecycle_stage
            for spec in selected_specs
        ):
            raise ValueError("TaskEnvelope lifecycle_stage does not match actions")
        expected_capabilities = tuple(
            capability.value
            for spec in selected_specs
            for capability in spec.required_capabilities
        )
        if envelope.capability_plan != expected_capabilities:
            raise ValueError("TaskEnvelope capability_plan mismatch")
        expected_evidence = tuple(
            item
            for spec in selected_specs
            for item in (
                *(
                    f"capability:{capability.value}"
                    for capability in spec.required_capabilities
                ),
                f"action:{spec.action_id}",
            )
        )
        if envelope.evidence_plan != expected_evidence:
            raise ValueError("TaskEnvelope evidence_plan mismatch")
        self._validate_lifecycle(envelope, selected_specs)
        self._validate_authorization(envelope, selected_specs)
        self.replay_guard.consume(envelope)
        context: Dict[str, object] = dict(envelope.inputs)
        evidence: List[WorkerEvidence] = []
        for spec in selected_specs:
            action_id = spec.action_id
            try:
                spec.validate_input(context)
            except Exception as error:
                return self._failure_receipt(
                    envelope,
                    evidence,
                    action_id,
                    action_id,
                    error,
                )
            for capability in spec.required_capabilities:
                try:
                    result = self.assembly.providers.invoke(
                        capability,
                        task_id=envelope.task_id,
                        envelope_id=envelope.envelope_id,
                        context=dict(context),
                    )
                except Exception as error:
                    return self._failure_receipt(
                        envelope,
                        evidence,
                        action_id,
                        capability.value,
                        error,
                    )
                result_payload = result.to_dict()
                durable_path = Path(result.evidence_dir) if result.evidence_dir else None
                durable_digest = (
                    EvidenceArtifact._artifact_digest(durable_path)
                    if durable_path is not None and durable_path.exists()
                    else ""
                )
                evidence.append(WorkerEvidence(
                    evidence_type=(
                        "observed" if durable_digest else "execution_record"
                    ),
                    capability=capability.value,
                    source=result.platform,
                    outcome="success" if result.ok() else "failure",
                    artifact_sha256=durable_digest or hashlib.sha256(
                        _canonical(result_payload)
                    ).hexdigest(),
                    artifact_path=str(durable_path) if durable_digest else "",
                    metadata={
                        "action_id": action_id,
                        "durable_artifact": bool(durable_digest),
                    },
                ))
                if not result.ok():
                    return WorkerReceipt.issue(
                        envelope,
                        target_node=profile.target_node,
                        status="failure",
                        evidence=evidence,
                        secret=self.secret,
                    )
            try:
                result = self.recipes.execute(
                    envelope.assistant_id, action_id, context
                )
            except Exception as error:
                return self._failure_receipt(
                    envelope,
                    evidence,
                    action_id,
                    action_id,
                    error,
                )
            context.update(result.outputs)
            digest = hashlib.sha256(_canonical(dict(result.outputs))).hexdigest()
            evidence.append(WorkerEvidence(
                evidence_type="action_result",
                capability=action_id,
                source="local_recipe_worker",
                outcome="success",
                artifact_sha256=digest,
                metadata={"output_keys": sorted(result.outputs)},
            ))
        return WorkerReceipt.issue(
            envelope,
            target_node=profile.target_node,
            status="success",
            evidence=evidence,
            secret=self.secret,
        )

    @staticmethod
    def _validate_lifecycle(
        envelope: TaskEnvelope,
        selected_specs,
    ) -> None:
        lifecycle = envelope.lifecycle
        if envelope.lifecycle_stage == "diagnosis":
            return
        if (
            envelope.diagnosis_revision <= 0
            or lifecycle.get("diagnosis_status") != "frozen"
        ):
            raise ValueError(
                "TaskEnvelope requires a frozen diagnosis revision"
            )
        if envelope.lifecycle_stage == "disposition":
            if (
                len(selected_specs) != 1
                or not lifecycle.get("disposition_plan_id")
                or lifecycle.get("disposition_action_id")
                != selected_specs[0].action_id
                or lifecycle.get("disposition_status")
                not in {"planned", "executing"}
            ):
                raise ValueError(
                    "TaskEnvelope disposition lifecycle mismatch"
                )
        elif (
            envelope.lifecycle_stage == "verification"
            and lifecycle.get("disposition_status") != "completed"
        ):
            raise ValueError(
                "TaskEnvelope verification requires completed disposition"
            )
        elif (
            envelope.lifecycle_stage == "observation"
            and lifecycle.get("verification_status") != "passed"
        ):
            raise ValueError(
                "TaskEnvelope observation requires passed verification"
            )

    def _validate_authorization(
        self,
        envelope: TaskEnvelope,
        selected_specs,
    ) -> None:
        needs_grant = any(
            bool(
                {
                    item.value for item in spec.side_effects
                }.union({
                    self.recipes.actions.capabilities.get(
                        capability
                    ).side_effect
                    for capability in spec.required_capabilities
                }) - {"none", "read"}
            )
            for spec in selected_specs
        )
        if not needs_grant:
            return
        if (
            not envelope.authorization
            or self.authorization_verifier is None
        ):
            raise ValueError(
                "TaskEnvelope side effects require host-verified authorization"
            )
        grant = AuthorizationGrant.from_dict(envelope.authorization)
        policy_digest = hashlib.sha256(
            _canonical(envelope.policy)
        ).hexdigest()
        for spec in selected_specs:
            spec_effects = {
                item.value for item in spec.side_effects
            }.union({
                self.recipes.actions.capabilities.get(
                    capability
                ).side_effect
                for capability in spec.required_capabilities
            })
            if spec_effects <= {"none", "read"}:
                continue
            if not self.authorization_verifier.verify(
                grant,
                action_id=spec.action_id,
                task_id=envelope.task_id,
                case_id=envelope.task_id,
                diagnosis_revision=envelope.diagnosis_revision,
                policy_digest=policy_digest,
            ):
                raise ValueError(
                    f"authorization rejected for action '{spec.action_id}'"
                )

    def _failure_receipt(
        self,
        envelope: TaskEnvelope,
        evidence: List[WorkerEvidence],
        action_id: str,
        capability: str,
        error: Exception,
    ) -> WorkerReceipt:
        payload = {
            "action_id": action_id,
            "capability": capability,
            "error_type": type(error).__name__,
        }
        evidence.append(WorkerEvidence(
            evidence_type="action_result",
            capability=capability,
            source="local_recipe_worker",
            outcome="failure",
            artifact_sha256=hashlib.sha256(_canonical(payload)).hexdigest(),
            metadata=payload,
        ))
        return WorkerReceipt.issue(
            envelope,
            target_node=self.assembly.profile.target_node,
            status="failure",
            evidence=evidence,
            secret=self.secret,
        )
