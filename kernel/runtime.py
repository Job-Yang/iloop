"""Persistent orchestration for plan -> task -> capability -> evidence -> resume."""

from __future__ import annotations

import json
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

from .capability import Capability, Plugin
from .acceptance import AcceptanceStore, Verdict
from .case import Case
from .dashboard import Dashboard
from .evidence import EvidenceArtifact, EvidenceKind
from .flow import FlowRegistry
from .gate_capability import CapabilityGate
from .global_review import GlobalReview, analyze_global_impact
from .ledger import Ledger, RoundStatus
from .task import StepStatus, TaskRecord, TaskStatus, TaskStep, TaskStore


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
    Capability.BUILD: "mechanism",
    Capability.RUN: "time",
    Capability.LAUNCH: "time",
    Capability.LOGS: "mechanism",
    Capability.VIEW_TREE: "scope",
    Capability.SCREENSHOT: "scope",
    Capability.CRASH: "time",
    Capability.PROBE: "scope",
}


class Runtime:
    """One project-scoped runtime. All durable state stays below data_dir."""

    def __init__(self, data_dir: str | Path, registry: FlowRegistry, plugin: Plugin,
                 *, project_root: str | Path = "",
                 attestation_verifier: Optional[Callable[[str, Path, dict], bool]] = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.plugin = plugin
        self.attestation_verifier = attestation_verifier
        self.project_root = str(Path(project_root).resolve()) if project_root else ""
        self.tasks = TaskStore(self.data_dir)

    def _task_dir(self, task_id: str) -> Path:
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

    def start(self, title: str, *, constraints: Optional[list[str]] = None,
              acceptance: Optional[list[str]] = None,
              capabilities: Optional[Iterable[str]] = None) -> TaskRecord:
        decision = self.registry.plan_details(title)
        flow = decision["flow"]
        if flow is None:
            raise ValueError("no flow matched; clarify the task before execution")
        steps = [TaskStep(title=s) for s in FLOW_STEPS.get(flow.flow_id, ["执行", "验证", "收口"])]
        for cap in capabilities or []:
            capability = Capability(cap).value
            steps.append(TaskStep(title=f"执行能力: {capability}", capability=capability))
        task = self.tasks.create(
            title,
            goal=title,
            flow_id=flow.flow_id,
            autonomy=flow.autonomy.value,
            constraints=constraints,
            acceptance=acceptance,
            steps=steps,
        )
        task.project_root = self.project_root
        if self.project_root:
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
        task.independent_acceptance_required = decision["acceptance_gate"]
        task.acceptance_status = "pending" if task.independent_acceptance_required else "not_required"
        case = Case(task.id, title)
        case.add_hypothesis("当前实现满足任务目标")
        case.save(self._case_path(task.id))
        task.case_path = str(self._case_path(task.id))
        task.capability_gate_path = str(self._capability_gate_path(task.id))
        task.global_review_path = str(self._global_review_path(task.id))
        task.acceptance_path = str(self._acceptance_path(task.id))
        CapabilityGate().save(task.capability_gate_path)
        self._task_policy_path(task.id).write_text(
            json.dumps({
                "task_id": task.id,
                "title": task.title,
                "flow_id": task.flow_id,
                "autonomy": task.autonomy,
                "project_root": task.project_root,
                "base_commit": task.base_commit,
                "global_review_required": bool(task.global_review_required),
                "independent_acceptance_required": bool(
                    task.independent_acceptance_required
                ),
                "created_at": task.created_at,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._requirements_path(task.id).write_text(
            json.dumps({
                "task_id": task.id,
                "revision": 0,
                "operations": [],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.tasks.save(task)
        Ledger(self._task_dir(task.id)).flush()
        return task

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
            policy_path.write_text(
                json.dumps({
                    "task_id": task.id,
                    "title": task.title,
                    "flow_id": task.flow_id,
                    "autonomy": task.autonomy,
                    "project_root": task.project_root,
                    "base_commit": task.base_commit,
                    "global_review_required": bool(task.global_review_required),
                    "independent_acceptance_required": bool(
                        task.independent_acceptance_required
                    ),
                    "created_at": task.created_at,
                    "legacy_migration": True,
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if policy.get("task_id") != task.id:
            raise ValueError("task policy task_id mismatch")
        changed = False
        for field_name in (
            "title", "flow_id", "autonomy", "project_root", "base_commit"
        ):
            expected = policy.get(field_name, "")
            if getattr(task, field_name) != expected:
                setattr(task, field_name, expected)
                changed = True
        decision = self.registry.plan_details(str(policy.get("title", "")))
        flow_requires_review = (
            policy.get("flow_id") == "core.refactor"
            or bool(decision.get("global_review_gate"))
            or bool(policy.get("global_review_required"))
            or (
                bool(policy.get("project_root"))
                and policy.get("autonomy") in {"L2", "L3"}
            )
        )
        if task.global_review_required != flow_requires_review:
            task.global_review_required = flow_requires_review
            task.global_review_status = "pending" if flow_requires_review else "not_required"
            changed = True
        acceptance_required = (
            policy.get("flow_id") == "core.refactor"
            or bool(decision.get("acceptance_gate"))
            or bool(policy.get("independent_acceptance_required"))
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
        if self.attestation_verifier is None:
            raise ValueError("trusted host attestation verifier is not configured")
        path = Path(receipt_path)
        row = json.loads(path.read_text(encoding="utf-8"))
        if not self.attestation_verifier("evidence", path, row):
            raise ValueError("host rejected evidence attestation")
        if row.get("task_id") != task.id:
            raise ValueError("evidence attestation task_id mismatch")
        required = ("kind", "run_id", "capability", "source", "outcome", "summary",
                    "artifact_sha256", "flow_id", "subjects", "gates", "expires_at")
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
        evidence = EvidenceArtifact(
            capability=str(row["capability"]),
            source=str(row["source"]),
            kind=EvidenceKind.OBSERVED,
            outcome=str(row["outcome"]),
            summary=str(row["summary"]),
            path=str(artifact),
            metadata={
                "task_id": task.id,
                "run_id": row.get("run_id", ""),
                "source_run_id": row.get("source_run_id", ""),
                "subjects": list(row.get("subjects", [])),
                "gates": list(row.get("gates", [])),
                "flow_id": row.get("flow_id", ""),
                "flow_run_id": row.get("flow_run_id", ""),
                "device": row.get("device", ""),
                "device_id": row.get("device_id", ""),
                "attestation_path": str(path),
                "attestation_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "artifact_sha256": artifact_sha256,
                "human_confirmed": row.get("kind") == "user_confirmation",
                "user_id": row.get("user_id", ""),
            },
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
        if self.attestation_verifier is None:
            raise ValueError("trusted host attestation verifier is not configured")
        result = AcceptanceStore(task.acceptance_path).record_file(
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
        operations = list(requirements.get("operations", []))
        if not any(item.get("operation_id") == operation_id for item in operations):
            operations.append({
                "operation_id": operation_id,
                "reason": reason,
                "required_at": time.time(),
            })
            requirements["operations"] = operations
            requirements["revision"] = int(requirements.get("revision", 0)) + 1
            requirements_path.write_text(
                json.dumps(requirements, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if operation_id not in task.required_operation_ids:
            task.required_operation_ids.append(operation_id)
            self.tasks.save(task)
        return operation

    def execute_capabilities(self, task: TaskRecord, capabilities: Iterable[str],
                             **kwargs) -> TaskRecord:
        ledger = Ledger.load(self._task_dir(task.id))
        case = Case.load(self._case_path(task.id))
        task.status = TaskStatus.RUNNING
        for cap_name in capabilities:
            capability = Capability(cap_name)
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
                if capability == Capability.LOGS
                else ""
            )
            invoke_kwargs = dict(kwargs)
            invoke_kwargs["task_id"] = task.id
            invoke_kwargs["run_id"] = source_run_id or run_id
            result = self.plugin.invoke(capability, **invoke_kwargs)
            gate_hint = CAPABILITY_GATE_HINT.get(capability)
            explicit_subjects = kwargs.get("subjects", [])
            if isinstance(explicit_subjects, str):
                explicit_subjects = [
                    item.strip()
                    for item in explicit_subjects.replace(",", ";").split(";")
                    if item.strip()
                ]
            producer_subjects = [
                str(item) for item in result.metadata.get("subjects", [])
            ]
            attested_subjects = []
            if explicit_subjects:
                subject_claim = {
                    "task_id": task.id,
                    "run_id": run_id,
                    "capability": capability.value,
                    "subjects": sorted(str(item) for item in explicit_subjects),
                    "evidence_dir": result.evidence_dir,
                    "status": result.status.value,
                }
                claim_dir = self._task_dir(task.id) / "subject-claims"
                claim_dir.mkdir(parents=True, exist_ok=True)
                claim_path = claim_dir / f"{len(ledger.rounds)}-{capability.value}.json"
                claim_path.write_text(
                    json.dumps(subject_claim, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                if (
                    self.attestation_verifier is not None
                    and self.attestation_verifier(
                        "evidence_subjects", claim_path, subject_claim
                    )
                ):
                    attested_subjects = subject_claim["subjects"]
            subjects = sorted({*producer_subjects, *attested_subjects})
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
                    "device_id": kwargs.get("device_udid") or kwargs.get("sim_udid") or "",
                    "gates": [gate_hint] if gate_hint else [],
                    "flow_id": task.flow_id,
                    "flow_run_id": kwargs.get("flow_run_id", ""),
                    "device": result.metadata.get("device", ""),
                    "trusted_producer": True,
                },
            )
            self._seal_trusted_evidence(task, evidence)
            self._append_evidence(task.id, evidence)
            case.attach("h1", evidence, refutes=not result.ok())
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
        return task

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
            "flow_run_id": evidence.metadata.get("flow_run_id", ""),
            "device": evidence.metadata.get("device", ""),
            "device_id": evidence.metadata.get("device_id", ""),
            "subjects": evidence.metadata.get("subjects", []),
            "gates": evidence.metadata.get("gates", []),
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

    def prepare_global_review(self, task: TaskRecord, project_root: str | Path,
                              *, base: str = "") -> GlobalReview:
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
        review = analyze_global_impact(resolved_root, base=expected_base)
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
        if task.project_root and not Path(task.global_review_path).exists():
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
            if step.status != StepStatus.DONE:
                blockers.append(f"step {index} unfinished: {step.title}")
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
                        expected_root, base=task.base_commit
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
                package = acceptance_store.load_raw().get("package") or {}
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
