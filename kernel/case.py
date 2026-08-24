"""病例状态机 —— 把任务当成一份持续的档案，而不是一次性对话。

现象、候选原因、每个原因手上的证据、卡在哪，都记在病例里，跨轮次跨会话。
收敛必须过四道关卡（gate），不能靠"我觉得差不多了"。

对应 VDD 设计决定：
  - 任务是档案，不是对话
  - 每一步只推进一个最能分辨真假的检查
  - 证据分级并标清来源
  - 收敛过四关
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Union

from .evidence import EvidenceArtifact
from .gate import FourGate, GateResult


class CaseStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class HypothesisStatus(str, Enum):
    OPEN = "open"
    SUPPORTED = "supported"
    REFUTED = "refuted"


class DiagnosisStatus(str, Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    FROZEN = "frozen"


class DispositionKind(str, Enum):
    CODE_CHANGE = "code_change"
    ISOLATION = "isolation"
    HUMAN_HANDOFF = "human_handoff"
    OBSERVE = "observe"


class DispositionStatus(str, Enum):
    PENDING = "pending"
    PLANNED = "planned"
    EXECUTING = "executing"
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class ObservationStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    OBSERVING = "observing"
    STABLE = "stable"
    REGRESSION = "regression"


@dataclass(frozen=True)
class DiagnosisRevision:
    revision: int
    hypothesis_id: str
    root_cause: str
    evidence_ids: List[str]
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class DispositionPlan:
    plan_id: str
    diagnosis_revision: int
    kind: DispositionKind
    action_id: str
    reason: str
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DispositionKind(self.kind))


@dataclass(frozen=True)
class VerificationRecord:
    diagnosis_revision: int
    passed: bool
    evidence_ids: List[str]
    summary: str
    plan_id: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class Hypothesis:
    id: str
    text: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    evidence_ids: List[str] = field(default_factory=list)
    wants_capability: str = ""   # 验它最需要哪类证据（对接 method_expert.wants_capabilities）

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = HypothesisStatus(self.status)


@dataclass
class TestSpec:
    """一条"下一步该做的最有区分度的检查"（tick 产物）。

    对应 VDD：每一步只推进一个最能分辨真假的检查，做完写回病例再定下一步。
    """
    hypothesis_id: str
    capability: str          # 该用哪类能力取证（build/logs/screenshot/view_tree/crash...）
    rationale: str           # 为什么这条最能分辨真假
    gate: str = ""           # 这条证据若成立，绑到四关的哪一关


class Case:
    """一份病例。建档 → 列可能原因 → 逐个证据排除 → 过四关收敛。"""

    def __init__(self, case_id: str, symptom: str) -> None:
        self.case_id = case_id
        self.symptom = symptom
        self.status = CaseStatus.OPEN
        self.diagnosis_status = DiagnosisStatus.OPEN
        self.diagnosis_started_at = time.time()
        self.diagnosis_revision = 0
        self.diagnosis_revisions: List[DiagnosisRevision] = []
        self.disposition_status = DispositionStatus.PENDING
        self.disposition_plans: List[DispositionPlan] = []
        self.disposition_progress: Dict[str, DispositionStatus] = {}
        self.disposition_completed_at: Dict[str, float] = {}
        self.verification_status = VerificationStatus.PENDING
        self.verifications: List[VerificationRecord] = []
        self.observation_status = ObservationStatus.NOT_REQUIRED
        self.observation_started_at = 0.0
        self.observation_evidence_ids: List[str] = []
        self.created_at = time.time()
        self.hypotheses: Dict[str, Hypothesis] = {}
        self.evidence: Dict[str, EvidenceArtifact] = {}
        self._gate = FourGate()
        self.timeline: List[str] = []

    def add_hypothesis(self, text: str, *, wants_capability: str = "") -> Hypothesis:
        if self.diagnosis_status == DiagnosisStatus.FROZEN:
            self.reopen_diagnosis("a new hypothesis was added")
        elif self.diagnosis_status == DiagnosisStatus.OPEN:
            self.diagnosis_started_at = time.time()
        hid = f"h{len(self.hypotheses) + 1}"
        h = Hypothesis(id=hid, text=text, wants_capability=wants_capability)
        self.hypotheses[hid] = h
        self.status = CaseStatus.INVESTIGATING
        self.diagnosis_status = DiagnosisStatus.INVESTIGATING
        self.timeline.append(f"+ 候选原因 {hid}: {text}")
        return h

    def attach(self, hypothesis_id: str, evidence: EvidenceArtifact,
               *, refutes: bool = False, gate: Optional[str] = None) -> None:
        """给某个候选原因挂证据。refutes=True 表示这条证据排除了它。"""
        if hypothesis_id not in self.hypotheses:
            raise KeyError(f"no hypothesis '{hypothesis_id}'")
        if (
            self.diagnosis_status == DiagnosisStatus.FROZEN
            and self.diagnosis_revisions
            and (
                (
                    refutes
                    and self.diagnosis_revisions[-1].hypothesis_id
                    == hypothesis_id
                )
                or (
                    not refutes
                    and self.diagnosis_revisions[-1].hypothesis_id
                    != hypothesis_id
                )
            )
        ):
            if (
                self.diagnosis_revisions
                and evidence.created_at
                < self.diagnosis_revisions[-1].created_at
            ):
                raise ValueError(
                    "stale evidence cannot reopen a frozen diagnosis"
                )
            self.reopen_diagnosis(
                f"frozen hypothesis {hypothesis_id} was refuted",
                started_at=evidence.created_at,
            )
        self.evidence[evidence.id] = evidence
        h = self.hypotheses[hypothesis_id]
        h.evidence_ids.append(evidence.id)
        if refutes:
            h.status = HypothesisStatus.REFUTED
            self.timeline.append(f"- {hypothesis_id} 被证据 {evidence.id} 排除")
        else:
            h.status = HypothesisStatus.SUPPORTED
            self.timeline.append(f"* {hypothesis_id} 获证据 {evidence.id} 支持")
        if gate:
            self._gate.bind(gate, evidence)

    def bind_gate(self, gate: str, evidence: EvidenceArtifact) -> None:
        self.evidence[evidence.id] = evidence
        self._gate.bind(gate, evidence)

    def open_hypotheses(self) -> List[Hypothesis]:
        return [h for h in self.hypotheses.values() if h.status == HypothesisStatus.OPEN]

    def surviving(self) -> List[Hypothesis]:
        """未被排除的候选原因。"""
        return [h for h in self.hypotheses.values() if h.status != HypothesisStatus.REFUTED]

    def tick(self) -> Optional[TestSpec]:
        """给出下一步最有区分度的检查（不一口气干完，每步只推进一个）。

        策略：优先选还没挂过任何证据的存活候选——验它最能缩小不确定性。
        返回 None 表示所有存活候选都已有证据，该去 try_resolve 或补四关了。
        """
        candidates = [h for h in self.surviving() if not h.evidence_ids]
        if not candidates:
            return None
        h = candidates[0]
        cap = h.wants_capability or "logs"
        return TestSpec(hypothesis_id=h.id, capability=cap,
                        rationale=f"{h.id} 尚无证据，先验它最能分辨真假", gate="")

    def consult(self, expert_id: str, verdict: str, summary: str) -> None:
        """记录一次有边界的专家会诊结论（不替代证据，只是把判断写进档案）。

        verdict ∈ supports/contradicts/inconclusive/cross_domain（见 experts 协议）。
        """
        self.timeline.append(f"? 会诊[{expert_id}] {verdict}: {summary}")

    def reroute(self, reason: str) -> List[Hypothesis]:
        """证据互相矛盾/全被排除时重分诊：把被排除的候选重新打开，回到调查态。

        对应内部版 reroute——不是推倒重来，是承认"当前假设集不够"，重开继续找。
        """
        for h in self.hypotheses.values():
            if h.status == HypothesisStatus.REFUTED:
                h.status = HypothesisStatus.OPEN
        self.reopen_diagnosis(reason)
        self._gate = FourGate()  # 证据矛盾，四关重置，避免带着脏证据收敛
        self.timeline.append(f"↻ 重分诊：{reason}")
        return self.surviving()

    def reopen_diagnosis(
        self,
        reason: str,
        *,
        started_at: Optional[float] = None,
    ) -> None:
        """Reopen diagnosis and invalidate every plan bound to an older revision."""
        self.status = CaseStatus.INVESTIGATING
        self.diagnosis_status = DiagnosisStatus.INVESTIGATING
        self.diagnosis_started_at = (
            time.time() if started_at is None else float(started_at)
        )
        for plan in self.disposition_plans:
            if plan.diagnosis_revision <= self.diagnosis_revision:
                self.disposition_progress[plan.plan_id] = (
                    DispositionStatus.INVALIDATED
                )
        self.disposition_status = DispositionStatus.PENDING
        self.verification_status = VerificationStatus.PENDING
        self.observation_status = ObservationStatus.NOT_REQUIRED
        self.observation_started_at = 0.0
        self.observation_evidence_ids = []
        self._gate = FourGate()
        self.timeline.append(f"diagnosis reopened: {reason}")

    def evaluate_gate(
        self,
        verify_attestation: Optional[Callable] = None,
        expected_bindings: Optional[dict] = None,
    ) -> GateResult:
        return self._gate.evaluate(
            verify_attestation,
            expected_bindings,
            min_created_at=self.diagnosis_started_at,
        )

    def gate_bindings(self) -> Dict[str, List[str]]:
        return self._gate.bindings()

    def try_resolve(
        self,
        verify_attestation: Optional[Callable] = None,
        expected_bindings: Optional[dict] = None,
    ) -> tuple[bool, str]:
        """能否收敛：必须过四关 且 只剩一个存活候选。"""
        gate = self.evaluate_gate(verify_attestation, expected_bindings)
        surviving = self.surviving()
        if not gate.passed:
            return False, f"四关未过：缺 {gate.missing}"
        if len(surviving) != 1:
            return False, f"存活候选不唯一（{len(surviving)} 个），无法定论"
        if self.diagnosis_status != DiagnosisStatus.FROZEN:
            self.diagnosis_revision += 1
            diagnosis = DiagnosisRevision(
                revision=self.diagnosis_revision,
                hypothesis_id=surviving[0].id,
                root_cause=surviving[0].text,
                evidence_ids=sorted(set(
                    surviving[0].evidence_ids
                    + [
                        evidence_id
                        for ids in self._gate.bindings().values()
                        for evidence_id in ids
                    ]
                )),
            )
            self.diagnosis_revisions.append(diagnosis)
            self.diagnosis_status = DiagnosisStatus.FROZEN
            self.disposition_status = DispositionStatus.PENDING
            self.verification_status = VerificationStatus.PENDING
            self.observation_status = ObservationStatus.NOT_REQUIRED
            self.timeline.append(
                f"diagnosis r{diagnosis.revision} frozen: "
                f"{diagnosis.hypothesis_id}"
            )
        self.status = CaseStatus.RESOLVED
        return True, f"根因收敛到 {surviving[0].id}: {surviving[0].text}"

    def route_disposition(
        self,
        available_actions: Union[Iterable[str], Mapping[str, str]],
        *,
        reason: str = "",
    ) -> DispositionPlan:
        """Choose the safest available disposition for the frozen diagnosis."""
        if self.diagnosis_status != DiagnosisStatus.FROZEN:
            raise ValueError("disposition requires a frozen diagnosis revision")
        if isinstance(available_actions, Mapping):
            actions = {
                str(action_id): DispositionKind(kind)
                for action_id, kind in available_actions.items()
            }
        else:
            legacy = set(str(item) for item in available_actions)
            actions = {
                action_id: kind
                for action_id, kind in (
                    ("code.apply", DispositionKind.CODE_CHANGE),
                    ("isolation.apply", DispositionKind.ISOLATION),
                    ("case.observe", DispositionKind.OBSERVE),
                )
                if action_id in legacy
            }
        selected = next(
            (
                (kind, action_id)
                for kind in (
                    DispositionKind.CODE_CHANGE,
                    DispositionKind.ISOLATION,
                    DispositionKind.OBSERVE,
                    DispositionKind.HUMAN_HANDOFF,
                )
                for action_id, action_kind in actions.items()
                if action_kind == kind
            ),
            (DispositionKind.HUMAN_HANDOFF, "human.handoff"),
        )
        kind, action_id = selected
        return self.create_disposition(kind, action_id=action_id, reason=reason)

    def create_disposition(
        self,
        kind: DispositionKind,
        *,
        action_id: str,
        reason: str = "",
    ) -> DispositionPlan:
        if self.diagnosis_status != DiagnosisStatus.FROZEN:
            raise ValueError("disposition requires a frozen diagnosis revision")
        if any(
            plan.diagnosis_revision == self.diagnosis_revision
            and self.disposition_progress.get(plan.plan_id)
            != DispositionStatus.INVALIDATED
            for plan in self.disposition_plans
        ):
            raise ValueError(
                f"diagnosis revision {self.diagnosis_revision} already has a plan"
            )
        plan = DispositionPlan(
            plan_id=f"plan-r{self.diagnosis_revision}",
            diagnosis_revision=self.diagnosis_revision,
            kind=DispositionKind(kind),
            action_id=action_id,
            reason=reason,
        )
        self.disposition_plans.append(plan)
        self.disposition_progress[plan.plan_id] = DispositionStatus.PLANNED
        self.disposition_status = DispositionStatus.PLANNED
        self.observation_status = (
            ObservationStatus.PENDING
            if plan.kind == DispositionKind.OBSERVE
            else ObservationStatus.NOT_REQUIRED
        )
        self.timeline.append(
            f"disposition {plan.plan_id}: {plan.kind.value}/{plan.action_id}"
        )
        return plan

    def advance_disposition(
        self,
        plan_id: str,
        status: DispositionStatus,
    ) -> None:
        plan = next(
            (item for item in self.disposition_plans if item.plan_id == plan_id),
            None,
        )
        if plan is None:
            raise KeyError(f"unknown disposition plan '{plan_id}'")
        if (
            plan.diagnosis_revision != self.diagnosis_revision
            or self.diagnosis_status != DiagnosisStatus.FROZEN
            or self.disposition_progress.get(plan_id)
            == DispositionStatus.INVALIDATED
        ):
            raise ValueError(
                f"disposition plan '{plan_id}' is stale for diagnosis "
                f"revision {self.diagnosis_revision}"
            )
        target = DispositionStatus(status)
        current = self.disposition_progress[plan_id]
        allowed = {
            DispositionStatus.PLANNED: {DispositionStatus.EXECUTING},
            DispositionStatus.EXECUTING: {DispositionStatus.COMPLETED},
        }
        if target not in allowed.get(current, set()):
            raise ValueError(
                f"invalid disposition transition: {current.value} -> {target.value}"
            )
        self.disposition_progress[plan_id] = target
        self.disposition_status = target
        if target == DispositionStatus.COMPLETED:
            self.disposition_completed_at[plan_id] = time.time()

    def record_verification(
        self,
        *,
        passed: bool,
        evidence_ids: Iterable[str],
        summary: str,
        verify_attestation: Optional[Callable] = None,
    ) -> VerificationRecord:
        if self.disposition_status != DispositionStatus.COMPLETED:
            raise ValueError("verification requires completed disposition")
        if self.verification_status != VerificationStatus.PENDING:
            raise ValueError(
                "verification is already recorded for this diagnosis revision"
            )
        ids = [str(item) for item in evidence_ids]
        if not ids:
            raise ValueError("verification requires evidence_ids")
        missing = [item for item in ids if item not in self.evidence]
        if missing:
            raise ValueError(
                f"verification evidence not found: {', '.join(missing)}"
            )
        plans = [
            plan for plan in self.disposition_plans
            if plan.diagnosis_revision == self.diagnosis_revision
            and self.disposition_progress.get(plan.plan_id)
            == DispositionStatus.COMPLETED
        ]
        if len(plans) != 1:
            raise ValueError(
                "verification requires one completed current-revision plan"
            )
        plan = plans[0]
        completed_at = self.disposition_completed_at.get(plan.plan_id, 0)
        invalid = [
            evidence_id for evidence_id in ids
            if (
                self.evidence[evidence_id].created_at < completed_at
                or not self.evidence[evidence_id].supports_outcome(
                    "success" if passed else "failure",
                    verify_attestation,
                    {
                        "task_id": self.case_id,
                        "diagnosis_revision": self.diagnosis_revision,
                        "disposition_plan_id": plan.plan_id,
                    },
                )
            )
        ]
        if invalid:
            raise ValueError(
                "verification requires matching observed evidence created "
                f"after disposition completion: {', '.join(invalid)}"
            )
        record = VerificationRecord(
            diagnosis_revision=self.diagnosis_revision,
            passed=bool(passed),
            evidence_ids=ids,
            summary=summary,
            plan_id=plan.plan_id,
        )
        self.verifications.append(record)
        self.verification_status = (
            VerificationStatus.PASSED if passed
            else VerificationStatus.FAILED
        )
        if passed:
            self.observation_status = ObservationStatus.PENDING
        return record

    def retry_verification(self, reason: str) -> None:
        if self.verification_status != VerificationStatus.FAILED:
            raise ValueError("only a failed verification can be retried")
        self.verification_status = VerificationStatus.PENDING
        self.observation_status = ObservationStatus.NOT_REQUIRED
        self.timeline.append(f"verification retry: {reason}")

    def verification_is_valid(
        self,
        verify_attestation: Optional[Callable] = None,
    ) -> bool:
        if (
            self.verification_status != VerificationStatus.PASSED
            or not self.verifications
        ):
            return False
        record = self.verifications[-1]
        if record.diagnosis_revision != self.diagnosis_revision:
            return False
        completed_at = self.disposition_completed_at.get(record.plan_id, 0)
        return bool(completed_at) and all(
            evidence_id in self.evidence
            and self.evidence[evidence_id].created_at >= completed_at
            and self.evidence[evidence_id].supports_outcome(
                "success",
                verify_attestation,
                {
                    "task_id": self.case_id,
                    "diagnosis_revision": self.diagnosis_revision,
                    "disposition_plan_id": record.plan_id,
                },
            )
            for evidence_id in record.evidence_ids
        )

    def start_observation(
        self,
        verify_attestation: Optional[Callable] = None,
    ) -> None:
        if self.verification_status != VerificationStatus.PASSED:
            raise ValueError("observation requires passed verification")
        if not self.verification_is_valid(verify_attestation):
            raise ValueError("observation requires valid verification evidence")
        self.observation_status = ObservationStatus.OBSERVING
        self.observation_started_at = time.time()
        self.observation_evidence_ids = []

    def observation_is_valid(
        self,
        verify_attestation: Optional[Callable] = None,
    ) -> bool:
        if (
            self.observation_status != ObservationStatus.STABLE
            or not self.observation_evidence_ids
            or not self.verifications
        ):
            return False
        plan_id = self.verifications[-1].plan_id
        return all(
            evidence_id in self.evidence
            and self.evidence[evidence_id].created_at
            >= self.observation_started_at
            and self.evidence[evidence_id].supports_outcome(
                "success",
                verify_attestation,
                {
                    "task_id": self.case_id,
                    "diagnosis_revision": self.diagnosis_revision,
                    "disposition_plan_id": plan_id,
                },
            )
            for evidence_id in self.observation_evidence_ids
        )

    def finish_observation(
        self,
        *,
        stable: bool,
        evidence_ids: Iterable[str],
        reason: str = "",
        verify_attestation: Optional[Callable] = None,
    ) -> None:
        if self.observation_status != ObservationStatus.OBSERVING:
            raise ValueError("case is not observing")
        if self.verification_status != VerificationStatus.PASSED:
            raise ValueError("observation requires the verification to remain passed")
        if not self.verification_is_valid(verify_attestation):
            raise ValueError("observation requires valid verification evidence")
        ids = [str(item) for item in evidence_ids]
        if not ids:
            raise ValueError("observation requires fresh evidence")
        plan_id = self.verifications[-1].plan_id
        invalid = [
            evidence_id for evidence_id in ids
            if (
                evidence_id not in self.evidence
                or self.evidence[evidence_id].created_at
                < self.observation_started_at
                or not self.evidence[evidence_id].supports_outcome(
                    "success" if stable else "failure",
                    verify_attestation,
                    {
                        "task_id": self.case_id,
                        "diagnosis_revision": self.diagnosis_revision,
                        "disposition_plan_id": plan_id,
                    },
                )
            )
        ]
        if invalid:
            raise ValueError(
                "observation evidence is invalid: " + ", ".join(invalid)
            )
        self.observation_evidence_ids = ids
        self.observation_status = (
            ObservationStatus.STABLE if stable
            else ObservationStatus.REGRESSION
        )
        if not stable:
            self.reopen_diagnosis(reason or "observation found a regression")

    def escalate(self, reason: str) -> None:
        self.status = CaseStatus.ESCALATED
        self.timeline.append(f"⛔ 升级：{reason}")

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "symptom": self.symptom,
            "status": self.status.value,
            "diagnosis_status": self.diagnosis_status.value,
            "diagnosis_started_at": self.diagnosis_started_at,
            "diagnosis_revision": self.diagnosis_revision,
            "diagnosis_revisions": [
                asdict(item) for item in self.diagnosis_revisions
            ],
            "disposition_status": self.disposition_status.value,
            "disposition_plans": [
                {
                    **asdict(item),
                    "kind": item.kind.value,
                }
                for item in self.disposition_plans
            ],
            "disposition_progress": {
                key: value.value
                for key, value in self.disposition_progress.items()
            },
            "disposition_completed_at": dict(self.disposition_completed_at),
            "verification_status": self.verification_status.value,
            "verifications": [
                asdict(item) for item in self.verifications
            ],
            "observation_status": self.observation_status.value,
            "observation_started_at": self.observation_started_at,
            "observation_evidence_ids": list(
                self.observation_evidence_ids
            ),
            "created_at": self.created_at,
            "hypotheses": [asdict_h(h) for h in self.hypotheses.values()],
            "evidence": [e.to_dict() for e in self.evidence.values()],
            "gate_bindings": self._gate.bindings(),
            "timeline": self.timeline,
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Case":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        case = cls(data["case_id"], data["symptom"])
        case.status = CaseStatus(data["status"])
        case.diagnosis_status = DiagnosisStatus(data.get(
            "diagnosis_status",
            "frozen" if case.status == CaseStatus.RESOLVED
            else "investigating" if case.status == CaseStatus.INVESTIGATING
            else "open",
        ))
        case.diagnosis_started_at = float(data.get(
            "diagnosis_started_at", data.get("created_at", case.created_at)
        ))
        case.diagnosis_revision = int(data.get("diagnosis_revision", 0))
        case.diagnosis_revisions = [
            DiagnosisRevision(**row)
            for row in data.get("diagnosis_revisions", [])
        ]
        case.disposition_status = DispositionStatus(
            data.get("disposition_status", "pending")
        )
        case.disposition_plans = [
            DispositionPlan(**row)
            for row in data.get("disposition_plans", [])
        ]
        case.disposition_progress = {
            str(key): DispositionStatus(value)
            for key, value in data.get("disposition_progress", {}).items()
        }
        case.disposition_completed_at = {
            str(key): float(value)
            for key, value in data.get(
                "disposition_completed_at", {}
            ).items()
        }
        case.verification_status = VerificationStatus(
            data.get("verification_status", "pending")
        )
        case.verifications = [
            VerificationRecord(**row)
            for row in data.get("verifications", [])
        ]
        case.observation_status = ObservationStatus(
            data.get("observation_status", "not_required")
        )
        case.observation_started_at = float(
            data.get("observation_started_at", 0)
        )
        case.observation_evidence_ids = list(
            data.get("observation_evidence_ids", [])
        )
        case.created_at = data.get("created_at", case.created_at)
        case.hypotheses = {
            row["id"]: Hypothesis(
                id=row["id"],
                text=row["text"],
                status=row.get("status", "open"),
                evidence_ids=list(row.get("evidence_ids", [])),
                wants_capability=row.get("wants_capability", ""),
            )
            for row in data.get("hypotheses", [])
        }
        case.evidence = {
            row["id"]: EvidenceArtifact(**row)
            for row in data.get("evidence", [])
        }
        for gate, evidence_ids in data.get("gate_bindings", {}).items():
            for evidence_id in evidence_ids:
                evidence = case.evidence.get(evidence_id)
                if evidence is not None:
                    case._gate.bind(gate, evidence)
        case.timeline = list(data.get("timeline", []))
        if (
            case.status == CaseStatus.RESOLVED
            and not case.diagnosis_revisions
        ):
            surviving = case.surviving()
            if len(surviving) == 1:
                case.diagnosis_revision = 1
                case.diagnosis_revisions = [DiagnosisRevision(
                    revision=1,
                    hypothesis_id=surviving[0].id,
                    root_cause=surviving[0].text,
                    evidence_ids=sorted(set(
                        surviving[0].evidence_ids
                        + [
                            evidence_id
                            for ids in data.get("gate_bindings", {}).values()
                            for evidence_id in ids
                        ]
                    )),
                    created_at=case.created_at,
                )]
                case.diagnosis_status = DiagnosisStatus.FROZEN
        return case


def asdict_h(h: Hypothesis) -> dict:
    return {
        "id": h.id,
        "text": h.text,
        "status": h.status.value,
        "evidence_ids": h.evidence_ids,
        "wants_capability": h.wants_capability,
    }
