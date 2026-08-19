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
from typing import Callable, Dict, List, Optional

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
        self.created_at = time.time()
        self.hypotheses: Dict[str, Hypothesis] = {}
        self.evidence: Dict[str, EvidenceArtifact] = {}
        self._gate = FourGate()
        self.timeline: List[str] = []

    def add_hypothesis(self, text: str, *, wants_capability: str = "") -> Hypothesis:
        hid = f"h{len(self.hypotheses) + 1}"
        h = Hypothesis(id=hid, text=text, wants_capability=wants_capability)
        self.hypotheses[hid] = h
        self.status = CaseStatus.INVESTIGATING
        self.timeline.append(f"+ 候选原因 {hid}: {text}")
        return h

    def attach(self, hypothesis_id: str, evidence: EvidenceArtifact,
               *, refutes: bool = False, gate: Optional[str] = None) -> None:
        """给某个候选原因挂证据。refutes=True 表示这条证据排除了它。"""
        if hypothesis_id not in self.hypotheses:
            raise KeyError(f"no hypothesis '{hypothesis_id}'")
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
        self._gate = FourGate()  # 证据矛盾，四关重置，避免带着脏证据收敛
        self.status = CaseStatus.INVESTIGATING
        self.timeline.append(f"↻ 重分诊：{reason}")
        return self.surviving()

    def evaluate_gate(
        self,
        verify_attestation: Optional[Callable] = None,
        expected_bindings: Optional[dict] = None,
    ) -> GateResult:
        return self._gate.evaluate(verify_attestation, expected_bindings)

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
        self.status = CaseStatus.RESOLVED
        return True, f"根因收敛到 {surviving[0].id}: {surviving[0].text}"

    def escalate(self, reason: str) -> None:
        self.status = CaseStatus.ESCALATED
        self.timeline.append(f"⛔ 升级：{reason}")

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "symptom": self.symptom,
            "status": self.status.value,
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
        return case


def asdict_h(h: Hypothesis) -> dict:
    return {
        "id": h.id,
        "text": h.text,
        "status": h.status.value,
        "evidence_ids": h.evidence_ids,
        "wants_capability": h.wants_capability,
    }
