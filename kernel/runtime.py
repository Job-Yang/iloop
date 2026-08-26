"""Persistent orchestration for plan -> task -> capability -> evidence -> resume."""

from __future__ import annotations

import json
import hashlib
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .capability import Capability, CapabilityLike, Plugin, capability_id
from .action import ActionRisk, ActionSideEffect, ActionSpec
from .authorization import AuthorizationGrant, AuthorizationVerifier
from .acceptance import AcceptanceStore, RiskLevel, Verdict, assess_risk
from .case import Case, DispositionStatus
from .dashboard import Dashboard
from .evidence import EvidenceArtifact, EvidenceKind
from .flow import FlowRegistry
from .gate_capability import CapabilityGate
from .global_review import GlobalReview, analyze_global_impact
from .ledger import Ledger, RoundStatus
from .recipe import RecipeCatalog
from .provider import ProviderRegistry
from .task import StepStatus, TaskRecord, TaskStatus, TaskStep, TaskStore
from .storage import atomic_write_json, file_lock


FLOW_STEPS = {
    "core.investigate": ["对齐现象与范围", "建立候选假设", "执行最有区分度的取证", "四关收敛"],
    "core.diagnose": ["病例建档", "逐个排除候选假设", "补齐四关证据", "形成根因结论"],
    "core.bugfix": ["复现并定位", "实施最小可逆修复", "编译", "运行态验证", "验收收口"],
    "core.feature": ["对齐验收标准", "实施最小改动", "编译", "运行态验证", "验收收口"],
    "core.small_iter": ["实施局部改动", "编译", "可见结果验证"],
    "core.refactor": ["圈定影响面", "实施重构", "编译", "关键入口回归", "独立验收"],
    "core.verify": ["确定验证目标", "选择最小证据", "执行验证", "验收收口"],
    "core.env_doctor": ["环境体检", "定位单一根因", "修复或升级", "重跑同一探针"],
    "core.oncall": ["事件建档", "候选根因排查", "四关收敛", "通知与沉淀"],
    "core.extend": ["判断扩展边界", "生成扩展骨架", "实现扩展", "校验并验证路由"],
}

CAPABILITY_GATE_HINT = {
    capability_id(Capability.BUILD): "mechanism",
    capability_id(Capability.RUN): "time",
    capability_id(Capability.LAUNCH): "time",
    capability_id(Capability.LOGS): "mechanism",
    capability_id(Capability.VIEW_TREE): "scope",
    capability_id(Capability.SCREENSHOT): "scope",
    capability_id(Capability.CRASH): "time",
    capability_id(Capability.PROBE): "scope",
    capability_id(Capability.COUNTER_PROBE): "counter_evidence",
}


class Runtime:
    """One project-scoped runtime. All durable state stays below data_dir."""

    def __init__(self, data_dir: str | Path, registry: FlowRegistry,
                 plugin: Optional[Plugin] = None,
                 *, project_root: str | Path = "",
                 recipe_catalog: Optional[RecipeCatalog] = None,
                 provider_registry: Optional[ProviderRegistry] = None,
                 attestation_verifier: Optional[Callable[[str, Path, dict], bool]] = None,
                 attestation_recorder: Optional[Callable[[str, Path, dict], None]] = None,
                 authorization_verifier: Optional[AuthorizationVerifier] = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.plugin = plugin
        self.recipe_catalog = recipe_catalog
        if provider_registry is None:
            if plugin is None:
                raise ValueError("Runtime requires a plugin or ProviderRegistry")
            provider_registry = ProviderRegistry(
                [plugin],
                capability_catalog=(
                    recipe_catalog.actions.capabilities
                    if recipe_catalog is not None
                    else None
                ),
            )
        elif plugin is not None and all(
            item.platform_id != plugin.platform_id
            for item in provider_registry.providers()
        ):
            provider_registry.register(plugin)
        if recipe_catalog is not None:
            recipe_catalog.freeze()
        provider_registry.freeze()
        self.providers = provider_registry
        self.attestation_verifier = attestation_verifier
        self.attestation_recorder = attestation_recorder
        self.authorization_verifier = authorization_verifier
        self.project_root = str(Path(project_root).resolve()) if project_root else ""
        self.tasks = TaskStore(self.data_dir)

    def _task_dir(self, task_id: str) -> Path:
        TaskStore.validate_id(task_id)
        path = self.data_dir / "runtime" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _case_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "case.json"

    def _evidence_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "evidence.jsonl"

    def _capability_gate_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "capability-gate.json"

    def _global_review_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "global-review.json"

    def _acceptance_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "acceptance.json"

    def _task_policy_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task-policy.json"

    def _requirements_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "capability-requirements.json"

    def _action_result_path(self, task_id: str, action_id: str) -> Path:
        digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:16]
        return self._task_dir(task_id) / "action-results" / f"{digest}.json"

    def _action_execution_path(self, task_id: str, action_id: str) -> Path:
        digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:16]
        return self._task_dir(task_id) / "action-executions" / f"{digest}.json"

    @contextmanager
    def _transaction(self, task_id: str, operation: str):
        task_dir = self._task_dir(task_id)
        journal = task_dir / "transaction.json"
        with file_lock(task_dir / ".task.lock"):
            before = self._transaction_fingerprint(task_dir)
            atomic_write_json(
                journal,
                {
                    "task_id": task_id,
                    "operation": operation,
                    "started_at": time.time(),
                },
            )
            try:
                yield
            except BaseException as error:
                after = self._transaction_fingerprint(task_dir)
                if after == before:
                    journal.unlink(missing_ok=True)
                else:
                    atomic_write_json(
                        journal,
                        {
                            "task_id": task_id,
                            "operation": operation,
                            "started_at": time.time(),
                            "status": "interrupted",
                            "error": f"{type(error).__name__}: {error}",
                        },
                    )
                raise
            else:
                journal.unlink(missing_ok=True)

    def _transaction_fingerprint(self, task_dir: Path) -> str:
        rows = []
        task_path = self.tasks.path_for(task_dir.name)
        if task_path.is_file():
            rows.append({
                "path": f"../tasks/{task_path.name}",
                "sha256": hashlib.sha256(task_path.read_bytes()).hexdigest(),
            })
        for path in sorted(
            item for item in task_dir.rglob("*")
            if item.is_file()
            and item.name not in {".task.lock", "transaction.json"}
        ):
            rows.append({
                "path": str(path.relative_to(task_dir)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        return hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def start(self, title: str, *, constraints: Optional[list[str]] = None,
              acceptance: Optional[list[str]] = None,
              capabilities: Optional[Iterable[str]] = None,
              executor_id: str = "",
              assistant_id: str = "",
              steps: Optional[list[TaskStep]] = None,
              design_contract: Optional[dict] = None,
              execution_context: Optional[dict[str, str]] = None) -> TaskRecord:
        decision = self.registry.plan_details(title)
        flow = decision["flow"]
        if flow is None:
            raise ValueError("no flow matched; clarify the task before execution")
        assistant_id = assistant_id.strip()
        assistant_recipe_digest = ""
        assistant_provider_bindings = {}
        assembly = None
        assistant_high_risk = False
        if assistant_id:
            if self.recipe_catalog is None:
                raise ValueError(
                    "assistant_id requires a configured RecipeCatalog"
                )
            assembly = self.recipe_catalog.assemble(assistant_id)
            self.providers.validate_capabilities(
                assembly.required_capabilities
            )
            assistant_recipe_digest = assembly.fingerprint()
            assistant_provider_bindings = {
                capability.value: self.providers.resolve(
                    capability
                ).platform_id
                for capability in assembly.required_capabilities
            }
            assistant_high_risk = any(
                spec.risk == ActionRisk.HIGH for spec in assembly.actions
            )
            if flow.autonomy.value == "L1" and any(
                effect not in {
                    ActionSideEffect.NONE, ActionSideEffect.READ,
                }
                for spec in assembly.actions
                for effect in spec.side_effects
            ):
                raise ValueError(
                    "L1 flow cannot execute assistant actions with write/process "
                    "side effects"
                )
            if steps is not None:
                raise ValueError(
                    "assistant tasks derive steps from AssistantRecipe"
                )
            if list(capabilities or []):
                raise ValueError(
                    "assistant tasks execute capabilities through their Recipe"
                )
        if flow.autonomy.value in {"L2", "L3"} and not self.project_root:
            raise ValueError(
                "L2/L3 tasks require project_root (set ILOOP_PROJECT_ROOT or "
                "pass project_root=...) before task creation"
            )
        if (
            decision["acceptance_gate"]
            or assess_risk(title) == RiskLevel.HIGH
            or assistant_high_risk
        ) and not executor_id.strip():
            raise ValueError(
                "high-risk tasks require executor_id before task creation"
            )
        if assembly is not None:
            task_steps = [
                TaskStep(
                    title=f"执行动作: {spec.action_id}",
                    action_id=spec.action_id,
                )
                for spec in assembly.actions
            ]
        else:
            task_steps = steps or [
                TaskStep(title=s)
                for s in FLOW_STEPS.get(
                    flow.flow_id, ["执行", "验证", "收口"]
                )
            ]
            for cap in capabilities or []:
                capability = capability_id(cap).value
                task_steps.append(TaskStep(
                    title=f"执行能力: {capability}",
                    capability=capability,
                ))
        task = self.tasks.create(
            title,
            goal=title,
            flow_id=flow.flow_id,
            autonomy=flow.autonomy.value,
            assistant_id=assistant_id,
            assistant_recipe_digest=assistant_recipe_digest,
            assistant_provider_bindings=assistant_provider_bindings,
            constraints=constraints,
            acceptance=acceptance,
            design_contract=design_contract,
            steps=task_steps,
        )
        task.project_root = self.project_root
        task.executor_id = executor_id.strip()
        task.execution_context = dict(execution_context or {})
        if self.project_root and flow.autonomy.value in {"L2", "L3"}:
            result = subprocess.run(
                ["git", "-C", self.project_root, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode:
                raise ValueError(
                    "project_root must be a Git worktree for L2/L3 global review"
                )
            task.base_commit = result.stdout.strip()
        task.global_review_required = decision["global_review_gate"]
        task.global_review_status = "pending" if task.global_review_required else "not_required"
        # A change touching a core-risk keyword forces independent review even
        # when its size score is low; keep this consistent with _enforce_policy.
        task.independent_acceptance_required = (
            decision["acceptance_gate"]
            or assess_risk(title) == RiskLevel.HIGH
            or assistant_high_risk
        )
        task.acceptance_status = "pending" if task.independent_acceptance_required else "not_required"
        case = Case(task.id, title)
        case.add_hypothesis("当前实现满足任务目标")
        case.save(self._case_path(task.id))
        task.case_path = str(self._case_path(task.id))
        task.capability_gate_path = str(self._capability_gate_path(task.id))
        task.global_review_path = str(self._global_review_path(task.id))
        task.acceptance_path = str(self._acceptance_path(task.id))
        CapabilityGate().save(task.capability_gate_path)
        policy = {
                "task_id": task.id,
                "title": task.title,
                "goal": task.goal,
                "flow_id": task.flow_id,
                "assistant_id": task.assistant_id,
                "assistant_recipe_digest": task.assistant_recipe_digest,
                "assistant_provider_bindings": dict(
                    task.assistant_provider_bindings
                ),
                "autonomy": task.autonomy,
                "constraints": list(task.constraints),
                "acceptance": list(task.acceptance),
                "design_contract": dict(task.design_contract),
                "steps": [
                    {
                        "id": step.id,
                        "title": step.title,
                        "capability": step.capability,
                        "action_id": step.action_id,
                    }
                    for step in task.steps
                ],
                "project_root": task.project_root,
                "base_commit": task.base_commit,
                "executor_id": task.executor_id,
                "execution_context": dict(task.execution_context),
                "global_review_required": bool(task.global_review_required),
                "independent_acceptance_required": bool(
                    task.independent_acceptance_required
                ),
                "created_at": task.created_at,
        }
        policy_path = self._task_policy_path(task.id)
        policy_path.write_text(
            json.dumps(policy, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        requirements = {
                "task_id": task.id,
                "revision": 0,
                "operations": [],
        }
        requirements_path = self._requirements_path(task.id)
        requirements_path.write_text(
            json.dumps(requirements, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._attest("task_policy", policy_path, policy)
        self._attest(
            "capability_requirements",
            requirements_path,
            requirements,
        )
        self.tasks.save(task)
        Ledger(self._task_dir(task.id)).flush()
        return task

    def _attest(self, kind: str, path: Path, payload: dict) -> None:
        if self.attestation_recorder is not None:
            self.attestation_recorder(kind, path, payload)

    def _policy_digest(self, task_id: str) -> str:
        payload = json.loads(
            self._task_policy_path(task_id).read_text(encoding="utf-8")
        )
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _require_action_authorization(
        self,
        task: TaskRecord,
        case: Case,
        spec: ActionSpec,
        grant: Optional[AuthorizationGrant],
    ) -> None:
        action_effects = {
            item.value for item in spec.side_effects
        }
        capability_effects = {
            self.recipe_catalog.actions.capabilities.get(
                capability
            ).side_effect
            for capability in spec.required_capabilities
        }
        needs_grant = bool(
            (action_effects | capability_effects)
            - {"none", "read"}
        )
        if not needs_grant:
            return
        if grant is None or self.authorization_verifier is None:
            raise ValueError(
                f"action '{spec.action_id}' requires host-verified authorization"
            )
        if not self.authorization_verifier.verify(
            grant,
            action_id=spec.action_id,
            task_id=task.id,
            case_id=case.case_id,
            diagnosis_revision=case.diagnosis_revision,
            policy_digest=self._policy_digest(task.id),
        ):
            raise ValueError(
                f"authorization rejected for action '{spec.action_id}'"
            )

    def _require_direct_capability_authorization(
        self,
        task: TaskRecord,
        case: Case,
        capabilities: Iterable[CapabilityLike],
        grant: Optional[AuthorizationGrant],
    ) -> frozenset[str]:
        side_effects = frozenset(
            capability_id(item).value
            for item in capabilities
            if self.providers.capability_catalog.get(item).side_effect
            not in {"none", "read"}
        )
        if not side_effects:
            return side_effects
        if grant is None or self.authorization_verifier is None:
            raise ValueError(
                "direct side-effect capabilities require host-verified "
                "authorization"
            )
        verify_capability = getattr(
            self.authorization_verifier,
            "verify_capability",
            None,
        )
        if not callable(verify_capability):
            raise ValueError(
                "authorization verifier cannot verify direct capabilities"
            )
        for capability in sorted(side_effects):
            if not verify_capability(
                grant,
                capability_id=capability,
                task_id=task.id,
                case_id=case.case_id,
                diagnosis_revision=case.diagnosis_revision,
                policy_digest=self._policy_digest(task.id),
            ):
                raise ValueError(
                    f"authorization rejected for direct capability "
                    f"'{capability}'"
                )
        return side_effects

    def load(self, task_id: str) -> TaskRecord:
        task = self.tasks.load(task_id)
        changed = False
        defaults = {
            "case_path": self._case_path(task.id),
            "capability_gate_path": self._capability_gate_path(task.id),
            "global_review_path": self._global_review_path(task.id),
            "acceptance_path": self._acceptance_path(task.id),
        }
        for field_name, path in defaults.items():
            value = getattr(task, field_name, "")
            if not value or Path(value).is_dir():
                setattr(task, field_name, str(path))
                changed = True
        if not Path(task.capability_gate_path).exists():
            CapabilityGate().save(task.capability_gate_path)
        if not task.project_root and self.project_root:
            task.project_root = self.project_root
            changed = True
        if self._enforce_policy(task):
            changed = True
        if changed:
            self.tasks.save(task)
        return task

    def _enforce_policy(self, task: TaskRecord) -> bool:
        """Re-derive mandatory gates instead of trusting mutable task JSON flags."""
        policy_path = self._task_policy_path(task.id)
        if not policy_path.is_file():
            legacy_policy = {
                    "task_id": task.id,
                    "title": task.title,
                    "goal": task.goal,
                    "flow_id": task.flow_id,
                    "assistant_id": task.assistant_id,
                    "assistant_recipe_digest": task.assistant_recipe_digest,
                    "assistant_provider_bindings": dict(
                        task.assistant_provider_bindings
                    ),
                    "autonomy": task.autonomy,
                    "constraints": list(task.constraints),
                    "acceptance": list(task.acceptance),
                    "design_contract": dict(task.design_contract),
                    "steps": [
                        {
                            "id": step.id,
                            "title": step.title,
                            "capability": step.capability,
                            "action_id": step.action_id,
                        }
                        for step in task.steps
                    ],
                    "project_root": task.project_root,
                    "base_commit": task.base_commit,
                    "executor_id": task.executor_id,
                    "execution_context": dict(task.execution_context),
                    "global_review_required": bool(task.global_review_required),
                    "independent_acceptance_required": bool(
                        task.independent_acceptance_required
                    ),
                    "created_at": task.created_at,
                    "legacy_migration": True,
            }
            policy_path.write_text(
                json.dumps(legacy_policy, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._attest("task_policy", policy_path, legacy_policy)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if (
            self.attestation_verifier is not None
            and not self.attestation_verifier(
                "task_policy", policy_path, policy
            )
        ):
            raise ValueError("task creation policy is not host attested")
        if policy.get("task_id") != task.id:
            raise ValueError("task policy task_id mismatch")
        changed = False
        for field_name in (
            "title", "goal", "flow_id", "autonomy", "project_root", "base_commit",
            "executor_id", "assistant_id", "assistant_recipe_digest",
        ):
            expected = policy.get(field_name, "")
            if getattr(task, field_name) != expected:
                setattr(task, field_name, expected)
                changed = True
        expected_provider_bindings = {
            str(key): str(value)
            for key, value in policy.get(
                "assistant_provider_bindings", {}
            ).items()
        }
        if task.assistant_provider_bindings != expected_provider_bindings:
            task.assistant_provider_bindings = expected_provider_bindings
            changed = True
        if task.assistant_id:
            if self.recipe_catalog is None:
                raise ValueError(
                    "task policy requires assistant_id but no RecipeCatalog is configured"
                )
            assembly = self.recipe_catalog.assemble(task.assistant_id)
            if (
                task.assistant_recipe_digest
                and assembly.fingerprint() != task.assistant_recipe_digest
            ):
                raise ValueError(
                    "task assistant recipe changed since task creation"
                )
            self.providers.validate_capabilities(
                assembly.required_capabilities
            )
            current_bindings = {
                capability.value: self.providers.resolve(
                    capability
                ).platform_id
                for capability in assembly.required_capabilities
            }
            if (
                task.assistant_provider_bindings
                and current_bindings != task.assistant_provider_bindings
            ):
                raise ValueError(
                    "task assistant provider bindings changed since task creation"
                )
        for field_name in ("constraints", "acceptance"):
            expected = list(policy.get(field_name, []))
            if getattr(task, field_name) != expected:
                setattr(task, field_name, expected)
                changed = True
        expected_context = {
            str(key): str(value)
            for key, value in policy.get("execution_context", {}).items()
        }
        if task.execution_context != expected_context:
            task.execution_context = expected_context
            changed = True
        # Design contract is the review baseline frozen at planning time; restore
        # it from policy so multi-round review compares against a fixed ruler.
        expected_contract = dict(policy.get("design_contract", {}))
        if task.design_contract != expected_contract:
            task.design_contract = expected_contract
            changed = True
        contracts = list(policy.get("steps", []))
        if contracts:
            existing = {step.id: step for step in task.steps}
            restored = []
            for contract in contracts:
                step = existing.get(str(contract.get("id", "")))
                if step is None:
                    step = TaskStep(
                        id=str(contract["id"]),
                        title=str(contract["title"]),
                        capability=str(contract.get("capability", "")),
                        action_id=str(contract.get("action_id", "")),
                    )
                else:
                    step.title = str(contract["title"])
                    step.capability = str(contract.get("capability", ""))
                    step.action_id = str(contract.get("action_id", ""))
                restored.append(step)
            if [step.id for step in task.steps] != [step.id for step in restored]:
                task.steps = restored
                changed = True
        decision = self.registry.plan_details(str(policy.get("title", "")))
        # Restore the gate decision frozen at creation time; never re-derive it
        # from project_root + autonomy, which would silently escalate every
        # L2/L3 task with a worktree into full global review (review overreach).
        # The flow-level safety net only prevents a tampered policy from
        # downgrading a review that plan() had required.
        flow_requires_review = (
            bool(policy.get("global_review_required"))
            or policy.get("flow_id") == "core.refactor"
            or bool(decision.get("global_review_gate"))
        )
        if task.global_review_required != flow_requires_review:
            task.global_review_required = flow_requires_review
            task.global_review_status = "pending" if flow_requires_review else "not_required"
            changed = True
        acceptance_required = (
            policy.get("flow_id") == "core.refactor"
            or bool(decision.get("acceptance_gate"))
            or bool(policy.get("independent_acceptance_required"))
            # Small change that touches a core-risk keyword (payment, auth,
            # signing, crash, data write...) must still force independent
            # review even when its size score stays low (review underreach).
            or assess_risk(str(policy.get("title", ""))) == RiskLevel.HIGH
        )
        if task.global_review_path and Path(task.global_review_path).is_file():
            review = GlobalReview.load(task.global_review_path)
            acceptance_required = acceptance_required or review.risk_level == "high"
        if task.independent_acceptance_required != acceptance_required:
            task.independent_acceptance_required = acceptance_required
            task.acceptance_status = "pending" if acceptance_required else "not_required"
            changed = True
        return changed

    def add_attested_evidence(self, task: TaskRecord, receipt_path: str | Path) -> EvidenceArtifact:
        with self._transaction(task.id, "add_attested_evidence"):
            return self._add_attested_evidence_locked(task, receipt_path)

    def _add_attested_evidence_locked(
        self,
        task: TaskRecord,
        receipt_path: str | Path,
    ) -> EvidenceArtifact:
        if self.attestation_verifier is None:
            raise ValueError("trusted host attestation verifier is not configured")
        path = Path(receipt_path)
        row = json.loads(path.read_text(encoding="utf-8"))
        if not self.attestation_verifier("evidence", path, row):
            raise ValueError("host rejected evidence attestation")
        if row.get("task_id") != task.id:
            raise ValueError("evidence attestation task_id mismatch")
        required = ("kind", "run_id", "capability", "source", "outcome", "summary",
                    "artifact_sha256", "flow_id", "subjects", "gates", "expires_at",
                    "created_at")
        missing = [key for key in required if key not in row]
        if missing:
            raise ValueError(f"evidence attestation missing bindings: {missing}")
        if not str(row["run_id"]).strip():
            raise ValueError("evidence attestation requires run_id")
        if row["flow_id"] != task.flow_id:
            raise ValueError("evidence attestation flow_id mismatch")
        if row["kind"] not in {"observed", "user_confirmation"}:
            raise ValueError("evidence attestation kind is invalid")
        if not isinstance(row["subjects"], list) or not isinstance(row["gates"], list):
            raise ValueError("evidence attestation subjects and gates must be arrays")
        if float(row["expires_at"]) <= time.time():
            raise ValueError("evidence attestation has expired")
        artifact = Path(str(row.get("path", "")))
        if not artifact.exists():
            raise ValueError("attested evidence artifact is missing")
        artifact_sha256 = EvidenceArtifact._artifact_digest(artifact)
        if not artifact_sha256 or artifact_sha256 != row["artifact_sha256"]:
            raise ValueError("evidence attestation artifact hash mismatch")
        metadata = {
            "task_id": task.id,
            "run_id": row.get("run_id", ""),
            "source_run_id": row.get("source_run_id", ""),
            "subjects": list(row.get("subjects", [])),
            "gates": list(row.get("gates", [])),
            "flow_id": row.get("flow_id", ""),
            "ui_flow_id": row.get("ui_flow_id", ""),
            "flow_run_id": row.get("flow_run_id", ""),
            "device": row.get("device", ""),
            "device_id": row.get("device_id", ""),
            "attestation_path": str(path),
            "attestation_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "artifact_sha256": artifact_sha256,
            "human_confirmed": row.get("kind") == "user_confirmation",
            "user_id": row.get("user_id", ""),
        }
        for key in ("diagnosis_revision", "disposition_plan_id"):
            if key in row:
                metadata[key] = row[key]
        evidence = EvidenceArtifact(
            capability=str(row["capability"]),
            source=str(row["source"]),
            kind=EvidenceKind.OBSERVED,
            outcome=str(row["outcome"]),
            summary=str(row["summary"]),
            path=str(artifact),
            metadata=metadata,
            created_at=float(row["created_at"]),
        )
        case = Case.load(task.case_path)
        hypothesis_id = str(row.get("hypothesis_id", "h1"))
        if hypothesis_id not in case.hypotheses:
            raise ValueError(f"evidence attestation hypothesis not found: {hypothesis_id}")
        case.attach(
            hypothesis_id,
            evidence,
            refutes=bool(row.get("refutes", False)),
        )
        for gate in evidence.metadata.get("gates", []):
            case.bind_gate(gate, evidence)
        self._append_evidence(task.id, evidence)
        case.save(task.case_path)
        task.evidence_ids.append(evidence.id)
        self.tasks.save(task)
        return evidence

    def record_external_acceptance(self, task: TaskRecord, result_path: str | Path):
        with self._transaction(task.id, "record_external_acceptance"):
            return self._record_external_acceptance_locked(task, result_path)

    def _record_external_acceptance_locked(
        self,
        task: TaskRecord,
        result_path: str | Path,
    ):
        if self.attestation_verifier is None:
            raise ValueError("trusted host attestation verifier is not configured")
        store = AcceptanceStore(task.acceptance_path)
        package = store.load_raw().get("package") or {}
        if not self.attestation_verifier(
            "acceptance_package",
            Path(task.acceptance_path),
            package,
        ):
            raise ValueError("acceptance package is not host attested")
        result = store.record_file(
            result_path,
            verify_attestation=lambda path, row: self.attestation_verifier(
                "independent_review", path, row
            ),
        )
        task.acceptance_status = result.verdict.value
        self.tasks.save(task)
        return result

    def require_operation(
        self,
        task: TaskRecord,
        operation_id: str,
        reason: str,
    ):
        with self._transaction(task.id, "require_operation"):
            return self._require_operation_locked(task, operation_id, reason)

    def _require_operation_locked(
        self,
        task: TaskRecord,
        operation_id: str,
        reason: str,
    ):
        gate = CapabilityGate.load(task.capability_gate_path)
        operation = gate.require(operation_id, reason, task_id=task.id)
        gate.save(task.capability_gate_path)
        requirements_path = self._requirements_path(task.id)
        requirements = (
            json.loads(requirements_path.read_text(encoding="utf-8"))
            if requirements_path.is_file()
            else {"task_id": task.id, "revision": 0, "operations": []}
        )
        if requirements.get("task_id") != task.id:
            raise ValueError("capability requirements task_id mismatch")
        operations = [
            item for item in requirements.get("operations", [])
            if item.get("operation_id") != operation_id
        ]
        operations.append({
                "operation_id": operation_id,
                "reason": reason,
                "requirement_id": operation.requirement_id,
                "required_at": operation.created_at,
        })
        requirements["operations"] = operations
        requirements["revision"] = int(requirements.get("revision", 0)) + 1
        requirements_path.write_text(
            json.dumps(requirements, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._attest(
            "capability_requirements",
            requirements_path,
            requirements,
        )
        if operation_id not in task.required_operation_ids:
            task.required_operation_ids.append(operation_id)
            self.tasks.save(task)
        return operation

    def execute_capabilities(
        self,
        task: TaskRecord,
        capabilities: Iterable[str],
        authorization: Optional[AuthorizationGrant] = None,
        **kwargs,
    ) -> TaskRecord:
        task = self.load(task.id)
        if task.assistant_id:
            raise ValueError(
                "assistant task capabilities must execute through AssistantRecipe"
            )
        requested = tuple(capability_id(item) for item in capabilities)
        case = Case.load(self._case_path(task.id))
        authorized_direct = self._require_direct_capability_authorization(
            task,
            case,
            requested,
            authorization,
        )
        with self._transaction(task.id, "execute_capabilities"):
            return self._execute_capabilities_locked(
                task,
                requested,
                direct_authorized_capabilities=authorized_direct,
                provider_kwargs=kwargs,
            )

    def execute_assistant(
        self,
        task: TaskRecord,
        inputs: Optional[dict[str, object]] = None,
        authorization: Optional[AuthorizationGrant] = None,
        **provider_kwargs,
    ) -> TaskRecord:
        with file_lock(
            self._task_dir(task.id) / ".assistant-execution.lock"
        ):
            return self._execute_assistant_locked(
                task, inputs, authorization, **provider_kwargs
            )

    def _execute_assistant_locked(
        self,
        task: TaskRecord,
        inputs: Optional[dict[str, object]] = None,
        authorization: Optional[AuthorizationGrant] = None,
        **provider_kwargs,
    ) -> TaskRecord:
        task = self.load(task.id)
        if not task.assistant_id or self.recipe_catalog is None:
            raise ValueError("task is not bound to an AssistantRecipe")
        assembly = self.recipe_catalog.assemble(task.assistant_id)
        context: dict[str, object] = dict(task.execution_context)
        context.update(inputs or {})
        for spec in assembly.actions:
            step = next(
                item for item in task.steps if item.action_id == spec.action_id
            )
            case = Case.load(task.case_path)
            disposition_plan = next(
                (
                    plan for plan in reversed(case.disposition_plans)
                    if plan.diagnosis_revision == case.diagnosis_revision
                    and case.disposition_progress.get(plan.plan_id)
                    != DispositionStatus.INVALIDATED
                ),
                None,
            )
            action_binding = self._action_binding(
                case, spec.lifecycle_stage, disposition_plan
            )
            if step.status == StepStatus.DONE:
                try:
                    outputs = self._load_action_result(
                        task, spec.action_id, action_binding
                    )
                except ValueError as error:
                    if "action result lifecycle binding mismatch" not in str(error):
                        raise
                    step.status = StepStatus.PENDING
                    step.evidence_ids = []
                    step.summary = ""
                    self.tasks.save(task)
                else:
                    context.update(outputs)
                    if spec.lifecycle_stage == "disposition":
                        case = Case.load(task.case_path)
                        plan = next(
                            (
                                item for item in reversed(case.disposition_plans)
                                if item.diagnosis_revision
                                == case.diagnosis_revision
                                and item.action_id == spec.action_id
                            ),
                            None,
                        )
                        if (
                            plan is not None
                            and case.disposition_progress.get(plan.plan_id)
                            == DispositionStatus.EXECUTING
                        ):
                            case.advance_disposition(
                                plan.plan_id, DispositionStatus.COMPLETED
                            )
                            case.save(task.case_path)
                    elif spec.lifecycle_stage == "observation":
                        case = Case.load(task.case_path)
                        if case.observation_status.value == "observing":
                            evidence_id = self._ensure_action_evidence(
                                task, spec.action_id
                            )
                            verifier = (
                                lambda path, row: self.attestation_verifier(
                                    "evidence", path, row
                                )
                            ) if self.attestation_verifier is not None else None
                            case.finish_observation(
                                stable=True,
                                evidence_ids=[evidence_id],
                                verify_attestation=verifier,
                            )
                            case.save(task.case_path)
                    continue
            if spec.lifecycle_stage == "disposition":
                if (
                    case.diagnosis_status.value != "frozen"
                    or disposition_plan is None
                ):
                    return task
                if disposition_plan.action_id != spec.action_id:
                    step.status = StepStatus.SKIPPED
                    step.summary = (
                        f"not selected by {disposition_plan.plan_id}"
                    )
                    self.tasks.save(task)
                    continue
            elif (
                spec.lifecycle_stage == "verification"
                and case.disposition_status != DispositionStatus.COMPLETED
            ):
                return task
            elif (
                spec.lifecycle_stage == "observation"
            ):
                verifier = (
                    lambda path, row: self.attestation_verifier(
                        "evidence", path, row
                    )
                ) if self.attestation_verifier is not None else None
                if not case.verification_is_valid(verifier):
                    return task
                if case.observation_status.value not in {
                    "pending", "observing",
                }:
                    return task
            try:
                spec.validate_input(context)
            except Exception as error:
                task = self.load(task.id)
                step = next(
                    item for item in task.steps
                    if item.action_id == spec.action_id
                )
                step.status = StepStatus.FAILED
                step.summary = (
                    f"{type(error).__name__}: {error}"
                )
                task.status = TaskStatus.BLOCKED
                self.tasks.save(task)
                return task
            if (
                spec.lifecycle_stage == "disposition"
                and disposition_plan is not None
                and case.disposition_progress.get(disposition_plan.plan_id)
                == DispositionStatus.PLANNED
            ):
                case.advance_disposition(
                    disposition_plan.plan_id,
                    DispositionStatus.EXECUTING,
                )
                case.save(task.case_path)
            if (
                spec.lifecycle_stage == "observation"
                and case.observation_status.value == "pending"
            ):
                case.start_observation(verifier)
                case.save(task.case_path)
            result_path = self._action_result_path(task.id, spec.action_id)
            if result_path.is_file():
                try:
                    outputs = self._load_action_result(
                        task, spec.action_id, action_binding
                    )
                except ValueError as error:
                    if "action result lifecycle binding mismatch" not in str(error):
                        raise
                    outputs = None
                if outputs is None:
                    pass
                else:
                    task = self.load(task.id)
                    step = next(
                        item for item in task.steps
                        if item.action_id == spec.action_id
                    )
                    action_evidence_id = self._ensure_action_evidence(
                        task, spec.action_id
                    )
                    result_row = json.loads(
                        result_path.read_text(encoding="utf-8")
                    )
                    step.evidence_ids = sorted(set(
                        list(result_row.get("evidence_ids", []))
                        + [action_evidence_id]
                    ))
                    step.completion_source = "machine"
                    step.status = StepStatus.DONE
                    step.summary = json.dumps(
                        outputs, ensure_ascii=False, sort_keys=True
                    )
                    task.status = TaskStatus.OPEN
                    self.tasks.save(task)
                    self._write_action_execution(
                        task, spec.action_id, "completed"
                    )
                    if (
                        spec.lifecycle_stage == "disposition"
                        and disposition_plan is not None
                    ):
                        case = Case.load(task.case_path)
                        if (
                            case.disposition_progress.get(
                                disposition_plan.plan_id
                            )
                            == DispositionStatus.EXECUTING
                        ):
                            case.advance_disposition(
                                disposition_plan.plan_id,
                                DispositionStatus.COMPLETED,
                            )
                            case.save(task.case_path)
                    elif spec.lifecycle_stage == "observation":
                        case = Case.load(task.case_path)
                        if case.observation_status.value == "observing":
                            verifier = (
                                lambda path, row: self.attestation_verifier(
                                    "evidence", path, row
                                )
                            ) if self.attestation_verifier is not None else None
                            case.finish_observation(
                                stable=True,
                                evidence_ids=[action_evidence_id],
                                verify_attestation=verifier,
                            )
                            case.save(task.case_path)
                    context.update(outputs)
                    continue
            execution_path = self._action_execution_path(
                task.id, spec.action_id
            )
            if execution_path.is_file():
                execution = json.loads(
                    execution_path.read_text(encoding="utf-8")
                )
                if (
                    self.attestation_verifier is not None
                    and not self.attestation_verifier(
                        "action_execution", execution_path, execution
                    )
                ):
                    task.status = TaskStatus.BLOCKED
                    step.status = StepStatus.FAILED
                    step.summary = "action execution journal is not host attested"
                    self.tasks.save(task)
                    return task
                has_side_effect = any(
                    effect not in {
                        ActionSideEffect.NONE, ActionSideEffect.READ,
                    }
                    for effect in spec.side_effects
                )
                if (
                    execution.get("status") in {"executing", "failed"}
                    and has_side_effect
                ):
                    task.status = TaskStatus.BLOCKED
                    step.status = StepStatus.FAILED
                    step.summary = (
                        "action side effect is indeterminate; reconcile before retry"
                    )
                    self.tasks.save(task)
                    return task
            self._require_action_authorization(
                task,
                case,
                spec,
                authorization,
            )
            before = set(task.evidence_ids)
            task = self.load(task.id)
            step = next(
                item for item in task.steps if item.action_id == spec.action_id
            )
            step.status = StepStatus.RUNNING
            task.status = TaskStatus.RUNNING
            self.tasks.save(task)
            self._write_action_execution(task, spec.action_id, "executing")
            if spec.required_capabilities:
                with self._transaction(
                    task.id, f"execute_action_capabilities:{spec.action_id}"
                ):
                    task = self._execute_capabilities_locked(
                        task,
                        spec.required_capabilities,
                        authorized_action=spec,
                        provider_kwargs=provider_kwargs,
                    )
                if task.status == TaskStatus.BLOCKED:
                    self._write_action_execution(
                        task,
                        spec.action_id,
                        "failed",
                        error="provider capability execution failed",
                    )
                    step = next(
                        item for item in task.steps
                        if item.action_id == spec.action_id
                    )
                    step.status = StepStatus.FAILED
                    self.tasks.save(task)
                    return task
            try:
                action_ledger = Ledger.load(self._task_dir(task.id))
                action_timing = action_ledger.start_timing(
                    "action",
                    task_id=task.id,
                    run_id=f"{task.id}:action:{spec.action_id}",
                    action_id=spec.action_id,
                )
                action_ledger.flush()
                result = self.recipe_catalog.execute(
                    task.assistant_id, spec.action_id, context
                )
                json.dumps(result.outputs)
            except Exception as error:
                action_ledger = Ledger.load(self._task_dir(task.id))
                try:
                    action_ledger.end_timing(
                        action_timing.event_id, "failed"
                    )
                except (KeyError, ValueError):
                    pass
                action_ledger.flush()
                self._write_action_execution(
                    task,
                    spec.action_id,
                    "failed",
                    error=f"{type(error).__name__}: {error}",
                )
                task = self.load(task.id)
                step = next(
                    item for item in task.steps
                    if item.action_id == spec.action_id
                )
                step.status = StepStatus.FAILED
                step.summary = f"{type(error).__name__}: {error}"
                task.status = TaskStatus.BLOCKED
                self.tasks.save(task)
                return task
            action_ledger = Ledger.load(self._task_dir(task.id))
            action_ledger.end_timing(action_timing.event_id, "success")
            action_ledger.flush()
            context.update(result.outputs)
            task = self.load(task.id)
            step = next(
                item for item in task.steps if item.action_id == spec.action_id
            )
            new_evidence = [
                evidence_id for evidence_id in task.evidence_ids
                if evidence_id not in before
            ]
            step.evidence_ids = new_evidence
            step.completion_source = "machine"
            step.status = StepStatus.DONE
            step.summary = json.dumps(
                dict(result.outputs), ensure_ascii=False, sort_keys=True
            )
            self._save_action_result(
                task,
                spec.action_id,
                dict(result.outputs),
                new_evidence,
                action_binding,
            )
            action_evidence_id = self._ensure_action_evidence(
                task, spec.action_id
            )
            step.evidence_ids = sorted(set(
                step.evidence_ids + [action_evidence_id]
            ))
            task.status = TaskStatus.OPEN
            self.tasks.save(task)
            self._write_action_execution(
                task, spec.action_id, "completed"
            )
            if (
                spec.lifecycle_stage == "disposition"
                and disposition_plan is not None
            ):
                case = Case.load(task.case_path)
                if (
                    case.disposition_progress.get(disposition_plan.plan_id)
                    == DispositionStatus.EXECUTING
                ):
                    case.advance_disposition(
                        disposition_plan.plan_id,
                        DispositionStatus.COMPLETED,
                    )
                    case.save(task.case_path)
            elif spec.lifecycle_stage == "observation":
                case = Case.load(task.case_path)
                if case.observation_status.value == "observing":
                    verifier = (
                        lambda path, row: self.attestation_verifier(
                            "evidence", path, row
                        )
                    ) if self.attestation_verifier is not None else None
                    case.finish_observation(
                        stable=True,
                        evidence_ids=[action_evidence_id],
                        verify_attestation=verifier,
                    )
                    case.save(task.case_path)
        self.write_dashboard(task.id)
        return self.load(task.id)

    def _write_action_execution(
        self,
        task: TaskRecord,
        action_id: str,
        status: str,
        *,
        error: str = "",
    ) -> None:
        payload = {
            "task_id": task.id,
            "assistant_id": task.assistant_id,
            "assistant_recipe_digest": task.assistant_recipe_digest,
            "action_id": action_id,
            "status": status,
            "error": error,
            "updated_at": time.time(),
        }
        path = self._action_execution_path(task.id, action_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)
        self._attest("action_execution", path, payload)

    def _ensure_action_evidence(
        self,
        task: TaskRecord,
        action_id: str,
    ) -> str:
        result_path = self._action_result_path(task.id, action_id)
        case = Case.load(task.case_path)
        plan_id = next(
            (
                plan.plan_id
                for plan in reversed(case.disposition_plans)
                if plan.diagnosis_revision == case.diagnosis_revision
                and case.disposition_progress.get(plan.plan_id)
                == DispositionStatus.COMPLETED
            ),
            "",
        )
        for item in self.evidence(task.id):
            if (
                item.capability == f"action:{action_id}"
                and item.path == str(result_path)
                and self._supports_success(item, task)
            ):
                return item.id
        evidence = EvidenceArtifact(
            capability=f"action:{action_id}",
            source="iloop-runtime",
            kind=EvidenceKind.OBSERVED,
            outcome="success",
            summary=f"application action completed: {action_id}",
            path=str(result_path),
            metadata={
                "task_id": task.id,
                "run_id": f"{task.id}:action:{action_id}:{time.time_ns()}",
                "source_run_id": "",
                "subjects": [action_id],
                "gates": [],
                "flow_id": task.flow_id,
                "ui_flow_id": "",
                "flow_run_id": "",
                "device": "",
                "device_id": "",
                "trusted_producer": True,
                "diagnosis_revision": case.diagnosis_revision,
                "disposition_plan_id": plan_id,
            },
        )
        self._seal_trusted_evidence(task, evidence)
        self._append_evidence(task.id, evidence)
        task.evidence_ids.append(evidence.id)
        return evidence.id

    def _save_action_result(
        self,
        task: TaskRecord,
        action_id: str,
        outputs: dict[str, object],
        evidence_ids: list[str],
        lifecycle_binding: dict[str, object],
    ) -> None:
        payload = {
            "task_id": task.id,
            "assistant_id": task.assistant_id,
            "assistant_recipe_digest": task.assistant_recipe_digest,
            "action_id": action_id,
            "outputs": outputs,
            "evidence_ids": list(evidence_ids),
            "diagnosis_revision": lifecycle_binding[
                "diagnosis_revision"
            ],
            "disposition_plan_id": lifecycle_binding[
                "disposition_plan_id"
            ],
            "created_at": time.time(),
        }
        path = self._action_result_path(task.id, action_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, payload)
        self._attest("action_result", path, payload)

    def _load_action_result(
        self,
        task: TaskRecord,
        action_id: str,
        lifecycle_binding: Optional[dict[str, object]] = None,
    ) -> dict[str, object]:
        path = self._action_result_path(task.id, action_id)
        if not path.is_file():
            raise ValueError(
                f"completed action '{action_id}' has no durable result"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "task_id": task.id,
            "assistant_id": task.assistant_id,
            "assistant_recipe_digest": task.assistant_recipe_digest,
            "action_id": action_id,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError(f"action result binding mismatch: {action_id}")
        if lifecycle_binding and any(
            payload.get(key) != value
            for key, value in lifecycle_binding.items()
        ):
            raise ValueError(
                f"action result lifecycle binding mismatch: {action_id}"
            )
        if (
            self.attestation_verifier is None
            or not self.attestation_verifier("action_result", path, payload)
        ):
            raise ValueError(
                f"action result is not host attested: {action_id}"
            )
        outputs = payload.get("outputs")
        if not isinstance(outputs, dict):
            raise ValueError(f"action result outputs are invalid: {action_id}")
        return dict(outputs)

    @staticmethod
    def _action_binding(
        case: Case,
        lifecycle_stage: str,
        disposition_plan,
    ) -> dict[str, object]:
        revision = case.diagnosis_revision
        if (
            lifecycle_stage == "diagnosis"
            and case.diagnosis_status.value != "frozen"
        ):
            revision += 1
        return {
            "diagnosis_revision": revision,
            "disposition_plan_id": (
                disposition_plan.plan_id
                if disposition_plan is not None
                and lifecycle_stage != "diagnosis"
                else ""
            ),
        }

    def _execute_capabilities_locked(
        self,
        task: TaskRecord,
        capabilities: Iterable[CapabilityLike],
        *,
        authorized_action: Optional[ActionSpec] = None,
        direct_authorized_capabilities: frozenset[str] = frozenset(),
        provider_kwargs: Optional[Mapping[str, object]] = None,
    ) -> TaskRecord:
        requested = tuple(capability_id(item) for item in capabilities)
        options = dict(provider_kwargs or {})
        action_id = (
            authorized_action.action_id
            if authorized_action is not None else ""
        )
        if authorized_action is not None:
            outside_action = sorted(
                capability.value
                for capability in requested
                if capability not in authorized_action.required_capabilities
            )
            if outside_action:
                raise ValueError(
                    f"action '{authorized_action.action_id}' does not allow "
                    f"capabilities: {', '.join(outside_action)}"
                )
        unauthorized = sorted(
            capability.value
            for capability in requested
            if (
                self.providers.capability_catalog.get(capability).side_effect
                not in {"none", "read"}
                and authorized_action is None
                and capability.value not in direct_authorized_capabilities
            )
        )
        if unauthorized:
            raise ValueError(
                "side-effect capabilities were not authorized before "
                f"dispatch: {', '.join(unauthorized)}"
            )
        if task.assistant_id:
            assembly = self.recipe_catalog.assemble(task.assistant_id)
            allowed = set(assembly.required_capabilities)
            outside = sorted(
                capability.value
                for capability in requested
                if capability not in allowed
            )
            if outside:
                raise ValueError(
                    f"assistant '{task.assistant_id}' does not allow driver "
                    f"capabilities: {', '.join(outside)}"
                )
        self.providers.validate_capabilities(requested)
        ledger = Ledger.load(self._task_dir(task.id))
        case = Case.load(self._case_path(task.id))
        task.status = TaskStatus.RUNNING
        for capability in requested:
            stop, reason = ledger.should_stop(capability.value)
            if stop:
                task.status = TaskStatus.BLOCKED
                ledger.trace("blocked", reason)
                ledger.flush()
                break
            step = next(
                (s for s in task.steps
                 if s.capability == capability.value and s.status != StepStatus.DONE),
                None,
            )
            if step is None:
                step = TaskStep(title=f"执行能力: {capability.value}", capability=capability.value)
                task.steps.append(step)
            step.status = StepStatus.RUNNING
            task.current_stage = capability.value
            self.tasks.save(task)
            ledger.log_round_start(step.title, root_cause_tag=capability.value)
            ledger.trace("evidence", f"调用 {capability.value}")
            run_id = f"{task.id}:round:{len(ledger.rounds)}"
            source_run_id = (
                task.capability_runs.get(Capability.RUN.value, "")
                if capability in {
                    capability_id(Capability.LOGS),
                    capability_id(Capability.CRASH),
                }
                else ""
            )
            invoke_kwargs = dict(options)
            invoke_kwargs["task_id"] = task.id
            invoke_kwargs["run_id"] = source_run_id or run_id
            provider = self.providers.resolve(capability)
            timing = ledger.start_timing(
                "capability",
                task_id=task.id,
                run_id=run_id,
                action_id=action_id,
                capability_id=capability.value,
                provider_id=provider.platform_id if provider else "",
            )
            try:
                result = self.providers.invoke(
                    capability, **invoke_kwargs
                )
            except BaseException:
                ledger.end_timing(timing.event_id, "failed")
                ledger.flush()
                raise
            ledger.end_timing(
                timing.event_id,
                "success" if result.ok() else "failed",
            )
            gate_hint = CAPABILITY_GATE_HINT.get(capability)
            producer_subjects = [
                str(item) for item in result.metadata.get("subjects", [])
            ]
            subjects = sorted(set(producer_subjects))
            ui_flow_id = str(options.get("ui_flow_id") or "")
            evidence = EvidenceArtifact(
                capability=capability.value,
                source=result.platform,
                kind=EvidenceKind.OBSERVED,
                summary=result.summary,
                path=result.evidence_dir or None,
                outcome="success" if result.ok() else "failure",
                metadata={
                    "status": result.status.value,
                    "artifact_ids": list(result.artifacts),
                    "task_id": task.id,
                    "run_id": run_id,
                    "source_run_id": source_run_id,
                    "subjects": subjects,
                    "device_id": (
                        options.get("device_udid")
                        or options.get("sim_udid")
                        or ""
                    ),
                    "gates": [gate_hint] if gate_hint else [],
                    "flow_id": task.flow_id,
                    "ui_flow_id": ui_flow_id,
                    "flow_run_id": options.get("flow_run_id", ""),
                    "device": result.metadata.get("device", ""),
                    "trusted_producer": True,
                    "diagnosis_revision": case.diagnosis_revision,
                    "disposition_plan_id": next(
                        (
                            plan.plan_id
                            for plan in reversed(case.disposition_plans)
                            if plan.diagnosis_revision
                            == case.diagnosis_revision
                            and case.disposition_progress.get(plan.plan_id)
                            == DispositionStatus.COMPLETED
                        ),
                        "",
                    ),
                },
            )
            self._seal_trusted_evidence(task, evidence)
            self._append_evidence(task.id, evidence)
            hypothesis_id = str(options.get("hypothesis_id") or "")
            if not hypothesis_id and case.diagnosis_revisions:
                hypothesis_id = case.diagnosis_revisions[-1].hypothesis_id
            if not hypothesis_id:
                surviving = case.surviving()
                hypothesis_id = surviving[0].id if surviving else "h1"
            case.attach(hypothesis_id, evidence, refutes=not result.ok())
            if result.ok() and gate_hint:
                case.bind_gate(gate_hint, evidence)
            case.save(self._case_path(task.id))
            task.evidence_ids.append(evidence.id)
            step.evidence_ids.append(evidence.id)
            step.completion_source = "machine"
            step.summary = result.summary
            step.status = StepStatus.DONE if result.ok() else StepStatus.FAILED
            if result.ok():
                task.capability_runs[capability.value] = run_id
            ledger.log_round_end(RoundStatus.SUCCESS if result.ok() else RoundStatus.FAILED)
            ledger.trace("verify" if result.ok() else "blocked", result.summary)
            ledger.flush()
            self.tasks.save(task)
            if not result.ok():
                task.status = TaskStatus.BLOCKED
                break
        if task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.OPEN
        self.tasks.save(task)
        self.write_dashboard(task.id)
        return self.load(task.id)

    def _seal_trusted_evidence(
        self,
        task: TaskRecord,
        evidence: EvidenceArtifact,
    ) -> None:
        """Persist the exact scope emitted by the in-process plugin call."""
        payload = {
            "producer": "iloop-runtime",
            "capability": evidence.capability,
            "source": evidence.source,
            "outcome": evidence.outcome,
            "summary": evidence.summary,
            "path": evidence.path,
            "artifact_sha256": evidence.metadata.get("artifact_sha256", ""),
            "task_id": task.id,
            "run_id": evidence.metadata.get("run_id", ""),
            "source_run_id": evidence.metadata.get("source_run_id", ""),
            "flow_id": task.flow_id,
            "ui_flow_id": evidence.metadata.get("ui_flow_id", ""),
            "flow_run_id": evidence.metadata.get("flow_run_id", ""),
            "device": evidence.metadata.get("device", ""),
            "device_id": evidence.metadata.get("device_id", ""),
            "subjects": evidence.metadata.get("subjects", []),
            "gates": evidence.metadata.get("gates", []),
            "diagnosis_revision": evidence.metadata.get(
                "diagnosis_revision", 0
            ),
            "disposition_plan_id": evidence.metadata.get(
                "disposition_plan_id", ""
            ),
            "created_at": evidence.created_at,
        }
        proof_dir = self._task_dir(task.id) / "producer-receipts"
        proof_dir.mkdir(parents=True, exist_ok=True)
        path = proof_dir / f"{evidence.id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        evidence.metadata["producer_receipt_path"] = str(path)
        evidence.metadata["producer_receipt_sha256"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        self._attest("evidence", path, payload)

    def prepare_global_review(
        self,
        task: TaskRecord,
        project_root: str | Path,
        *,
        base: str = "",
        symptom_is_ui: bool = False,
        scope_rules: Optional[dict] = None,
    ) -> GlobalReview:
        with self._transaction(task.id, "prepare_global_review"):
            return self._prepare_global_review_locked(
                task,
                project_root,
                base=base,
                symptom_is_ui=symptom_is_ui,
                scope_rules=scope_rules,
            )

    def _prepare_global_review_locked(
        self,
        task: TaskRecord,
        project_root: str | Path,
        *,
        base: str = "",
        symptom_is_ui: bool = False,
        scope_rules: Optional[dict] = None,
    ) -> GlobalReview:
        if self._enforce_policy(task):
            self.tasks.save(task)
        resolved_root = str(Path(project_root).resolve())
        if not task.project_root or resolved_root != task.project_root:
            raise ValueError(
                f"global review project_root must match task: {task.project_root}"
            )
        expected_base = task.base_commit
        if not expected_base:
            raise ValueError("task has no immutable Git base_commit")
        if base and base != expected_base:
            raise ValueError(
                f"global review base must match task base_commit: {expected_base}"
            )
        review = analyze_global_impact(
            resolved_root,
            base=expected_base,
            symptom_is_ui=symptom_is_ui,
            scope_rules=scope_rules,
        )
        review.save(task.global_review_path)
        task.global_review_required = True
        task.global_review_status = review.status
        if review.risk_level == "high":
            task.independent_acceptance_required = True
            task.acceptance_status = "pending"
        self.tasks.save(task)
        return review

    def next_action(self, task: TaskRecord) -> dict:
        gate = CapabilityGate.load(task.capability_gate_path)
        if gate.pending():
            operation = gate.pending()[0]
            return {"kind": "capability_gate", "target": operation.op_id, "reason": operation.reason}
        failed = next((step for step in task.steps if step.status == StepStatus.FAILED), None)
        if failed:
            return {"kind": "repair", "target": failed.title, "reason": failed.summary}
        case = Case.load(task.case_path)
        spec = case.tick()
        if spec:
            return {"kind": "test_spec", **spec.__dict__}
        verifier = None
        if self.attestation_verifier is not None:
            verifier = lambda path, row: self.attestation_verifier(
                "evidence", path, row
            )
        gate_result = case.evaluate_gate(
            verifier,
            expected_bindings={"task_id": task.id, "flow_id": task.flow_id}
        )
        if not gate_result.passed:
            missing = gate_result.missing[0]
            return {"kind": "four_gate", "target": missing,
                    "reason": f"四关仍缺 {missing} 的成功 observed 证据"}
        next_step = task.next_step()
        if next_step:
            return {"kind": "task_step", "target": next_step.title,
                    "capability": next_step.capability}
        if task.assistant_id:
            assembly = self.recipe_catalog.assemble(task.assistant_id)
            if case.status.value != "resolved":
                return {
                    "kind": "diagnosis",
                    "target": task.case_path,
                    "reason": "case diagnosis is not frozen",
                }
            if case.disposition_status.value != "completed":
                return {
                    "kind": "disposition",
                    "target": task.case_path,
                    "reason": (
                        "route and complete a plan for the current "
                        "diagnosis revision"
                    ),
                }
            if not case.verification_is_valid(verifier):
                return {
                    "kind": "verification",
                    "target": task.case_path,
                    "reason": "record fresh post-disposition evidence",
                }
            if (
                assembly.recipe.continuous_observation
                and not case.observation_is_valid(verifier)
            ):
                return {
                    "kind": "observation",
                    "target": task.case_path,
                    "reason": "continuous observation has not reached stable",
                }
        if task.global_review_required and task.global_review_status != "completed":
            suggested_tests = []
            if Path(task.global_review_path).is_file():
                review = GlobalReview.load(task.global_review_path)
                suggested_tests = sorted({
                    test
                    for impact in review.pending()
                    for test in impact.suggested_tests
                })
            return {
                "kind": "global_review",
                "target": task.global_review_path,
                "suggested_tests": suggested_tests,
            }
        if task.independent_acceptance_required and task.acceptance_status != "pass":
            return {"kind": "independent_acceptance", "target": task.acceptance_path}
        return {"kind": "wrapup", "target": task.id}

    def _supports_success(
        self,
        evidence: EvidenceArtifact,
        task: TaskRecord,
    ) -> bool:
        verifier = None
        if self.attestation_verifier is not None:
            verifier = lambda path, row: self.attestation_verifier("evidence", path, row)
        if not str(evidence.metadata.get("run_id", "")).strip():
            return False
        return evidence.supports_success(
            verifier,
            {"task_id": task.id, "flow_id": task.flow_id},
        )

    def can_wrapup(self, task: TaskRecord) -> tuple[bool, list[str]]:
        blockers = []
        journal = self._task_dir(task.id) / "transaction.json"
        if journal.is_file():
            blockers.append(
                f"incomplete task transaction requires recovery: {journal}"
            )
        if self._enforce_policy(task):
            self.tasks.save(task)
        policy_path = self._task_policy_path(task.id)
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if (
            self.attestation_verifier is None
            or not self.attestation_verifier("task_policy", policy_path, policy)
        ):
            blockers.append("task creation policy is not host attested")
        evidence_by_id = {item.id: item for item in self.evidence(task.id)}
        known_evidence = set(evidence_by_id)
        if task.autonomy in {"L2", "L3"} and not task.project_root:
            blockers.append("L2/L3 task has no project_root; whole-diff review cannot run")
        if (
            task.global_review_required
            and task.project_root
            and not Path(task.global_review_path).exists()
        ):
            try:
                review = self.prepare_global_review(task, task.project_root)
                if not review.changed_files:
                    review.status = "completed"
                    review.completed_at = time.time()
                    review.save(task.global_review_path)
                    task.global_review_status = "completed"
                    self.tasks.save(task)
            except (RuntimeError, OSError, ValueError) as error:
                blockers.append(f"global review preparation failed: {error}")
        for index, step in enumerate(task.steps, 1):
            if step.status not in {StepStatus.DONE, StepStatus.SKIPPED}:
                blockers.append(f"step {index} unfinished: {step.title}")
            elif step.status == StepStatus.SKIPPED:
                continue
            elif not step.evidence_ids and step.completion_source != "human_confirmed":
                blockers.append(f"step {index} has no evidence or human confirmation: {step.title}")
            elif step.evidence_ids:
                unknown = [item for item in step.evidence_ids if item not in known_evidence]
                if unknown:
                    blockers.append(f"step {index} references unknown evidence: {unknown}")
                else:
                    bound = [evidence_by_id[item] for item in step.evidence_ids]
                    if step.capability and not any(
                        self._supports_success(item, task) and item.capability == step.capability
                        for item in bound
                    ):
                        blockers.append(
                            f"step {index} has no matching successful capability evidence: {step.title}"
                        )
                    if not step.capability and step.completion_source != "human_confirmed" and not any(
                        self._supports_success(item, task) for item in bound
                    ):
                        blockers.append(
                            f"step {index} has no successful evidence: {step.title}"
                        )
                    if step.completion_source == "human_confirmed" and not any(
                        item.metadata.get("human_confirmed") is True
                        and self._supports_success(item, task)
                        for item in bound
                    ):
                        blockers.append(
                            f"step {index} human confirmation evidence is invalid: {step.title}"
                        )
        capability_gate = CapabilityGate.load(task.capability_gate_path)
        requirements_path = self._requirements_path(task.id)
        requirements = (
            json.loads(requirements_path.read_text(encoding="utf-8"))
            if requirements_path.is_file()
            else {}
        )
        if (
            requirements.get("task_id") != task.id
            or self.attestation_verifier is None
            or not self.attestation_verifier(
                "capability_requirements", requirements_path, requirements
            )
        ):
            blockers.append("capability requirements are not host attested")
        required_operation_ids = {
            str(item.get("operation_id", ""))
            for item in requirements.get("operations", [])
            if item.get("operation_id")
        }
        missing_operations = sorted(
            (
                set(task.required_operation_ids)
                | required_operation_ids
            ) - set(capability_gate.operation_ids())
        )
        if missing_operations:
            blockers.append(
                f"capability gate lost required operations: {missing_operations}"
            )
        gate_ok, gate_message = capability_gate.can_wrapup(
            self.attestation_verifier,
            expected_task_id=task.id,
        )
        if not gate_ok:
            blockers.append(gate_message)
        case = Case.load(task.case_path)
        evidence_verifier = None
        if self.attestation_verifier is not None:
            evidence_verifier = lambda path, row: self.attestation_verifier(
                "evidence", path, row
            )
        gate_result = case.evaluate_gate(
            evidence_verifier,
            {"task_id": task.id, "flow_id": task.flow_id},
        )
        if not gate_result.passed:
            blockers.append(f"four gates missing: {', '.join(gate_result.missing)}")
        if case.status.value != "resolved":
            blockers.append(f"case is not resolved: {case.status.value}")
        if task.assistant_id:
            assembly = self.recipe_catalog.assemble(task.assistant_id)
            for step in task.steps:
                if not step.action_id or step.status != StepStatus.DONE:
                    continue
                try:
                    self._load_action_result(task, step.action_id)
                except ValueError as error:
                    blockers.append(str(error))
            if case.disposition_status.value != "completed":
                blockers.append(
                    "assistant case disposition is not completed"
                )
            if not case.verification_is_valid(evidence_verifier):
                blockers.append(
                    "assistant case verification has not passed with valid evidence"
                )
            if (
                assembly.recipe.continuous_observation
                and not case.observation_is_valid(evidence_verifier)
            ):
                blockers.append(
                    "continuous-observation assistant case is not stable"
                )
        if task.global_review_required:
            if not Path(task.global_review_path).exists():
                blockers.append("global review has not been prepared")
            else:
                review = GlobalReview.load(task.global_review_path)
                expected_root = str(Path(task.project_root).resolve())
                if review.project_root != expected_root:
                    blockers.append(
                        "global review project_root does not match task"
                    )
                if review.base != task.base_commit:
                    blockers.append(
                        "global review base does not match task base_commit"
                    )
                try:
                    current_review = analyze_global_impact(
                        expected_root,
                        base=task.base_commit,
                        symptom_is_ui=review.symptom_is_ui,
                        scope_rules=review.scope_rules,
                    )
                    if current_review.fingerprint != review.fingerprint:
                        blockers.append("global review is stale: project diff changed after review")
                    current_scope = [
                        (
                            item.kind,
                            item.target,
                            item.reason,
                            tuple(item.consumers),
                            tuple(item.entry_points),
                            tuple(item.suggested_tests),
                        )
                        for item in current_review.impacts
                    ]
                    saved_scope = [
                        (
                            item.kind,
                            item.target,
                            item.reason,
                            tuple(item.consumers),
                            tuple(item.entry_points),
                            tuple(item.suggested_tests),
                        )
                        for item in review.impacts
                    ]
                    if current_scope != saved_scope:
                        blockers.append(
                            "global review impact scope was altered after analysis"
                        )
                except (RuntimeError, OSError) as error:
                    blockers.append(f"cannot refresh global review fingerprint: {error}")
                if review.status != "completed" or review.pending():
                    blockers.append(
                        "global review incomplete: " +
                        ", ".join(item.target for item in review.pending())
                    )
                for impact in review.impacts:
                    unknown = [item for item in impact.evidence_ids if item not in known_evidence]
                    if unknown:
                        blockers.append(
                            f"global review {impact.target} references unknown evidence: {unknown}"
                        )
                    invalid = [
                        item for item in impact.evidence_ids
                        if item in evidence_by_id
                        and not self._supports_success(evidence_by_id[item], task)
                    ]
                    if invalid:
                        blockers.append(
                            f"global review {impact.target} has invalid evidence: {invalid}"
                        )
                    stale = [
                        item for item in impact.evidence_ids
                        if item in evidence_by_id
                        and evidence_by_id[item].created_at < review.created_at
                    ]
                    if stale:
                        blockers.append(
                            f"global review {impact.target} uses evidence older than the review: {stale}"
                        )
                    if impact.visual_required:
                        screenshots = [
                            evidence_by_id[item]
                            for item in impact.evidence_ids
                            if item in evidence_by_id
                            and evidence_by_id[item].capability
                            == Capability.SCREENSHOT.value
                            and self._supports_success(
                                evidence_by_id[item], task
                            )
                            and impact.target
                            in evidence_by_id[item].metadata.get(
                                "subjects", []
                            )
                        ]
                        if not screenshots:
                            blockers.append(
                                f"global review {impact.target} lacks "
                                "target-bound screenshot evidence"
                            )
                    if impact.status == "verified":
                        subjects = {
                            str(subject)
                            for item in impact.evidence_ids
                            if item in evidence_by_id
                            and self._supports_success(evidence_by_id[item], task)
                            for subject in evidence_by_id[item].metadata.get("subjects", [])
                        }
                        uncovered = [
                            subject
                            for subject in [impact.target, *impact.consumers]
                            if subject not in subjects
                        ]
                        if uncovered:
                            blockers.append(
                                f"global review {impact.target} has uncovered subjects: {uncovered}"
                            )
                    if impact.status == "accepted":
                        confirmations = [
                            evidence_by_id[item]
                            for item in impact.evidence_ids
                            if item in evidence_by_id
                            and evidence_by_id[item].metadata.get("human_confirmed") is True
                            and self._supports_success(evidence_by_id[item], task)
                            and impact.target
                            in evidence_by_id[item].metadata.get("subjects", [])
                        ]
                        if not confirmations:
                            blockers.append(
                                f"global review {impact.target} lacks user confirmation evidence"
                            )
        if task.independent_acceptance_required:
            acceptance_store = AcceptanceStore(task.acceptance_path)
            package = acceptance_store.load_raw().get("package") or {}
            if (
                self.attestation_verifier is None
                or not self.attestation_verifier(
                    "acceptance_package",
                    Path(task.acceptance_path),
                    package,
                )
            ):
                blockers.append("acceptance package is not host attested")
            verifier = None
            if self.attestation_verifier is not None:
                verifier = lambda path, row: self.attestation_verifier(
                    "independent_review", path, row
                )
            result = acceptance_store.result(
                verifier,
                expected_case_id=task.id,
            )
            if result is None or result.verdict != Verdict.PASS:
                blockers.append("independent acceptance has not passed")
            else:
                if task.global_review_required and Path(task.global_review_path).exists():
                    current_fingerprint = GlobalReview.load(task.global_review_path).fingerprint
                    if package.get("subject_fingerprint") != current_fingerprint:
                        blockers.append("independent acceptance reviewed a different diff fingerprint")
        return not blockers, blockers

    def complete(self, task: TaskRecord) -> TaskRecord:
        ok, blockers = self.can_wrapup(task)
        if not ok:
            raise ValueError("; ".join(blockers))
        task.status = TaskStatus.DONE
        task.current_stage = "done"
        self.tasks.save(task)
        return task

    def _append_evidence(self, task_id: str, evidence: EvidenceArtifact) -> None:
        path = self._evidence_path(task_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(evidence.to_dict(), ensure_ascii=False) + "\n")

    def evidence(self, task_id: str) -> list[EvidenceArtifact]:
        path = self._evidence_path(task_id)
        if not path.exists():
            return []
        return [
            EvidenceArtifact(**json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def write_dashboard(self, task_id: str) -> str:
        runtime_dir = self._task_dir(task_id)
        ledger = Ledger.load(runtime_dir)
        task = self.load(task_id)
        global_review = (
            GlobalReview.load(task.global_review_path).to_dict()
            if task.global_review_path and Path(task.global_review_path).exists()
            else {}
        )
        acceptance = AcceptanceStore(task.acceptance_path).load_raw()
        return Dashboard(
            ledger,
            evidence=self.evidence(task_id),
            task=task.to_dict(),
            global_review=global_review,
            acceptance=acceptance,
        ).save(
            runtime_dir / "dashboard.html"
        )
