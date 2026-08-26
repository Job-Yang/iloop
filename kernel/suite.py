"""Generic assistant-suite lifecycle and production-readiness receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple

from .capability import CapabilityId, CapabilityLike, capability_id
from .deployment import DeploymentProfile, assemble_deployment
from .evidence import EvidenceArtifact
from .provider import ProviderRegistry
from .recipe import RecipeCatalog
from .storage import atomic_write_json


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class SuiteMember:
    assistant_id: str
    deployment_id: str

    def __post_init__(self) -> None:
        if not self.assistant_id or not self.deployment_id:
            raise ValueError("suite member identity is incomplete")


@dataclass(frozen=True)
class SmokeCheck:
    check_id: str
    assistant_id: str
    deployment_id: str
    capability: CapabilityLike
    inputs: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all((
            self.check_id, self.assistant_id, self.deployment_id,
        )):
            raise ValueError("smoke check identity is incomplete")
        object.__setattr__(
            self, "capability", capability_id(self.capability)
        )
        object.__setattr__(self, "inputs", dict(self.inputs))


@dataclass(frozen=True)
class SuiteManifest:
    suite_id: str
    members: Tuple[SuiteMember, ...]
    smoke_checks: Tuple[SmokeCheck, ...]
    smoke_ttl_seconds: float = 3600
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.suite_id.strip():
            raise ValueError("suite_id is required")
        if not self.members:
            raise ValueError("suite requires at least one member")
        if not self.smoke_checks:
            raise ValueError(
                "suite requires at least one live smoke check"
            )
        object.__setattr__(self, "members", tuple(self.members))
        object.__setattr__(
            self, "smoke_checks", tuple(self.smoke_checks)
        )
        member_keys = {
            (item.assistant_id, item.deployment_id)
            for item in self.members
        }
        if len(member_keys) != len(self.members):
            raise ValueError("suite members must be unique")
        check_ids = {item.check_id for item in self.smoke_checks}
        if len(check_ids) != len(self.smoke_checks):
            raise ValueError("suite smoke check IDs must be unique")
        if not math.isfinite(self.smoke_ttl_seconds) or (
            self.smoke_ttl_seconds <= 0
        ):
            raise ValueError("suite smoke TTL must be positive and finite")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict:
        return {
            "suite_id": self.suite_id,
            "members": [asdict(item) for item in self.members],
            "smoke_checks": [
                {
                    **asdict(item),
                    "capability": item.capability.value,
                    "inputs": dict(item.inputs),
                }
                for item in self.smoke_checks
            ],
            "smoke_ttl_seconds": self.smoke_ttl_seconds,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "SuiteManifest":
        return cls(
            suite_id=str(payload["suite_id"]),
            members=tuple(
                SuiteMember(**dict(item))
                for item in payload["members"]
            ),
            smoke_checks=tuple(
                SmokeCheck(**dict(item))
                for item in payload.get("smoke_checks", [])
            ),
            smoke_ttl_seconds=float(
                payload.get("smoke_ttl_seconds", 3600)
            ),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(frozen=True)
class SmokeReceipt:
    suite_id: str
    suite_fingerprint: str
    checks: Tuple[Mapping[str, object], ...]
    created_at: float
    expires_at: float
    signature: str = ""

    def __post_init__(self) -> None:
        if not self.suite_id or len(self.suite_fingerprint) != 64:
            raise ValueError("smoke receipt identity is invalid")
        if (
            not math.isfinite(self.created_at)
            or not math.isfinite(self.expires_at)
            or self.expires_at <= self.created_at
        ):
            raise ValueError("smoke receipt expiry is invalid")
        object.__setattr__(
            self, "checks", tuple(dict(item) for item in self.checks)
        )

    def unsigned_payload(self) -> dict:
        return {
            "suite_id": self.suite_id,
            "suite_fingerprint": self.suite_fingerprint,
            "checks": [dict(item) for item in self.checks],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def to_dict(self) -> dict:
        return {**self.unsigned_payload(), "signature": self.signature}

    @classmethod
    def issue(
        cls,
        *,
        suite_id: str,
        suite_fingerprint: str,
        checks: Tuple[Mapping[str, object], ...],
        ttl_seconds: float,
        secret: bytes,
        now: Optional[float] = None,
    ) -> "SmokeReceipt":
        created_at = time.time() if now is None else float(now)
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError(
                "smoke signing secret must contain at least 32 bytes"
            )
        if (
            not checks
            or any(item.get("status") != "success" for item in checks)
        ):
            raise ValueError(
                "production smoke requires every configured check to pass"
            )
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("smoke TTL must be positive and finite")
        unsigned = cls(
            suite_id=suite_id,
            suite_fingerprint=suite_fingerprint,
            checks=tuple(dict(item) for item in checks),
            created_at=created_at,
            expires_at=created_at + ttl_seconds,
        )
        return replace(
            unsigned,
            signature=hmac.new(
                secret,
                _canonical(unsigned.unsigned_payload()),
                hashlib.sha256,
            ).hexdigest(),
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "SmokeReceipt":
        return cls(
            suite_id=str(payload["suite_id"]),
            suite_fingerprint=str(payload["suite_fingerprint"]),
            checks=tuple(
                dict(item) for item in payload.get("checks", [])
            ),
            created_at=float(payload["created_at"]),
            expires_at=float(payload["expires_at"]),
            signature=str(payload.get("signature", "")),
        )

    def verify(
        self,
        *,
        suite_id: str,
        suite_fingerprint: str,
        required_checks: Tuple[str, ...],
        secret: bytes,
        now: Optional[float] = None,
    ) -> bool:
        current = time.time() if now is None else float(now)
        if not isinstance(secret, bytes) or len(secret) < 32:
            return False
        expected = hmac.new(
            secret,
            _canonical(self.unsigned_payload()),
            hashlib.sha256,
        ).hexdigest()
        check_ids = tuple(
            str(item.get("check_id") or "") for item in self.checks
        )
        metadata_valid = bool(
            math.isfinite(current)
            and current < self.expires_at
            and self.created_at <= current + 30
            and self.suite_id == suite_id
            and self.suite_fingerprint == suite_fingerprint
            and check_ids == required_checks
            and all(
                item.get("status") == "success"
                and len(str(item.get("artifact_sha256") or "")) == 64
                for item in self.checks
            )
            and hmac.compare_digest(self.signature, expected)
        )
        if not metadata_valid:
            return False
        artifacts_valid = all(
            bool(item.get("artifact_path"))
            and Path(str(item["artifact_path"])).exists()
            and EvidenceArtifact._artifact_digest(
                Path(str(item["artifact_path"]))
            ) == str(item.get("artifact_sha256") or "")
            for item in self.checks
        )
        return artifacts_valid


class AssistantSuite:
    """Assemble arbitrary assistants and prove their current runtime state."""

    def __init__(
        self,
        manifest: SuiteManifest,
        recipes: RecipeCatalog,
        deployments: Mapping[str, DeploymentProfile],
        providers: ProviderRegistry,
        *,
        state_dir: str | Path,
        secret: bytes,
        member_installer: Optional[
            Callable[[SuiteMember, Mapping[str, object]], None]
        ] = None,
        tool_resolver: Optional[
            Callable[[str], Optional[str]]
        ] = None,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("suite secret must contain at least 32 bytes")
        self.manifest = manifest
        self.recipes = recipes
        self.deployments = dict(deployments)
        self.providers = providers
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.secret = secret
        self.member_installer = member_installer
        self.tool_resolver = tool_resolver or (lambda tool: None)

    def compile(self) -> dict:
        members = []
        for member in self.manifest.members:
            try:
                profile = self.deployments[member.deployment_id]
            except KeyError as error:
                raise ValueError(
                    f"unknown suite deployment '{member.deployment_id}'"
                ) from error
            assembly = assemble_deployment(
                profile,
                self.recipes,
                self.providers,
                member.assistant_id,
            )
            members.append({
                "assistant_id": member.assistant_id,
                "deployment_id": member.deployment_id,
                "recipe_digest": assembly.assistant.fingerprint(),
                "deployment_digest": assembly.fingerprint(),
                "actions": list(assembly.assistant.recipe.actions),
                "capabilities": list(
                    assembly.assistant.driver_plan()
                ),
            })
        known_members = {
            (item.assistant_id, item.deployment_id)
            for item in self.manifest.members
        }
        for check in self.manifest.smoke_checks:
            if (check.assistant_id, check.deployment_id) not in known_members:
                raise ValueError(
                    f"smoke check '{check.check_id}' references "
                    "an unknown suite member"
                )
            assembly = assemble_deployment(
                self.deployments[check.deployment_id],
                self.recipes,
                self.providers,
                check.assistant_id,
            )
            if check.capability.value not in (
                assembly.assistant.driver_plan()
            ):
                raise ValueError(
                    f"smoke check '{check.check_id}' capability is not "
                    "declared by its Recipe"
                )
            if self.providers.capability_catalog.get(
                check.capability
            ).side_effect not in {"none", "read"}:
                raise ValueError(
                    f"smoke check '{check.check_id}' cannot execute "
                    "a side-effect capability"
                )
        payload = {
            "manifest": self.manifest.to_dict(),
            "members": members,
        }
        return {
            **payload,
            "fingerprint": hashlib.sha256(
                _canonical(payload)
            ).hexdigest(),
        }

    def preflight(self) -> dict:
        compiled = self.compile()
        checks = []
        handler_installed = True
        providers_versioned = True
        for member in self.manifest.members:
            assembly = assemble_deployment(
                self.deployments[member.deployment_id],
                self.recipes,
                self.providers,
                member.assistant_id,
            )
            provider_fingerprints = (
                assembly.providers.runtime_fingerprints()
            )
            handler_installed = handler_installed and all(
                self.recipes.actions.has_handler(spec.action_id)
                for spec in assembly.assistant.actions
            )
            for capability in assembly.assistant.required_capabilities:
                provider = assembly.providers.resolve(capability)
                provider_fingerprint = provider_fingerprints.get(
                    provider.platform_id, ""
                )
                providers_versioned = (
                    providers_versioned
                    and bool(provider_fingerprint)
                )
                capability_spec = self.providers.capability_catalog.get(
                    capability
                )
                missing_tools = [
                    list(group)
                    for group in capability_spec.required_tools
                    if not any(
                        self.tool_resolver(tool) for tool in group
                    )
                ]
                detail = {
                    "capability": capability.value,
                    "declared": True,
                    "provider_fingerprint": provider_fingerprint,
                    "missing_tool_groups": missing_tools,
                    "runtime_ready": (
                        not missing_tools
                        and bool(provider_fingerprint)
                    ),
                }
                readiness = getattr(provider, "readiness", None)
                if callable(readiness):
                    try:
                        provider_detail = readiness(capability)
                        if isinstance(provider_detail, Mapping):
                            detail.update(dict(provider_detail))
                    except (Exception, SystemExit) as error:
                        detail.update({
                            "runtime_ready": False,
                            "error": (
                                f"{type(error).__name__}: {error}"
                            ),
                        })
                detail["runtime_ready"] = bool(
                    detail.get("runtime_ready", False)
                    and provider_fingerprint
                )
                checks.append({
                    "assistant_id": member.assistant_id,
                    "deployment_id": member.deployment_id,
                    "capability": capability.value,
                    "provider": provider.platform_id,
                    "runtime_ready": bool(
                        detail.get("runtime_ready", False)
                    ),
                    "detail": detail,
                })
        runtime_ready = all(
            item["runtime_ready"] for item in checks
        )
        implementation_ready = (
            handler_installed and providers_versioned
        )
        return {
            "suite_id": self.manifest.suite_id,
            "suite_fingerprint": compiled["fingerprint"],
            "declared": True,
            "handler_installed": handler_installed,
            "providers_versioned": providers_versioned,
            "implementation_ready": implementation_ready,
            "runtime_ready": runtime_ready,
            "checks": checks,
        }

    def install(self) -> dict:
        preflight = self.preflight()
        if (
            not preflight["implementation_ready"]
            or not preflight["runtime_ready"]
        ):
            raise ValueError("suite preflight is blocked")
        compiled = self.compile()
        if self.member_installer is not None:
            for member in self.manifest.members:
                self.member_installer(member, compiled)
        atomic_write_json(self.state_dir / "compiled.json", compiled)
        return {
            **preflight,
            "installed": True,
        }

    def smoke(self) -> SmokeReceipt:
        installed = self._installed()
        compiled = self.compile()
        if installed.get("fingerprint") != compiled["fingerprint"]:
            raise ValueError(
                "installed suite differs from the current manifest"
            )
        results = []
        for check in self.manifest.smoke_checks:
            profile = self.deployments[check.deployment_id]
            assembly = assemble_deployment(
                profile,
                self.recipes,
                self.providers,
                check.assistant_id,
            )
            result = assembly.providers.invoke(
                check.capability,
                **dict(check.inputs),
            )
            path = (
                Path(result.evidence_dir)
                if result.evidence_dir else None
            )
            artifact_sha256 = (
                EvidenceArtifact._artifact_digest(path)
                if path is not None and path.exists()
                else ""
            )
            if not result.ok() or not artifact_sha256:
                raise ValueError(
                    f"smoke check '{check.check_id}' has no durable "
                    "successful evidence"
                )
            results.append({
                "check_id": check.check_id,
                "assistant_id": check.assistant_id,
                "deployment_id": check.deployment_id,
                "capability": check.capability.value,
                "provider": result.platform,
                "status": "success",
                "artifact_sha256": artifact_sha256,
                "artifact_path": str(path),
            })
        receipt = SmokeReceipt.issue(
            suite_id=self.manifest.suite_id,
            suite_fingerprint=compiled["fingerprint"],
            checks=tuple(results),
            ttl_seconds=self.manifest.smoke_ttl_seconds,
            secret=self.secret,
        )
        atomic_write_json(
            self.state_dir / "smoke.json", receipt.to_dict()
        )
        return receipt

    def status(self) -> dict:
        preflight = self.preflight()
        compiled = self.compile()
        installed = self._installed(optional=True)
        receipt = self._smoke(optional=True)
        required_checks = tuple(
            item.check_id for item in self.manifest.smoke_checks
        )
        installed_ready = (
            installed.get("fingerprint") == compiled["fingerprint"]
        )
        smoke_ready = bool(
            receipt
            and receipt.verify(
                suite_id=self.manifest.suite_id,
                suite_fingerprint=compiled["fingerprint"],
                required_checks=required_checks,
                secret=self.secret,
            )
        )
        ready = bool(
            preflight["implementation_ready"]
            and preflight["runtime_ready"]
            and installed_ready
            and smoke_ready
        )
        return {
            **preflight,
            "installed": installed_ready,
            "smoke_ready": smoke_ready,
            "production_ready": ready,
            "status": "ready" if ready else "blocked",
        }

    def _installed(self, *, optional: bool = False) -> dict:
        path = self.state_dir / "compiled.json"
        if not path.is_file():
            if optional:
                return {}
            raise ValueError("suite is not installed")
        return json.loads(path.read_text(encoding="utf-8"))

    def _smoke(
        self, *, optional: bool = False
    ) -> Optional[SmokeReceipt]:
        path = self.state_dir / "smoke.json"
        if not path.is_file():
            if optional:
                return None
            raise ValueError("suite smoke receipt is missing")
        return SmokeReceipt.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
