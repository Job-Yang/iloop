"""四道关卡 Gate —— 时间 / 范围 / 机制 / 反证。

收敛必须过四关，缺一不算完成（VDD 守则 7）。
每一关都要求由 observed 证据支撑，或至少显式声明缺口，不许"我觉得差不多了"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .evidence import EvidenceArtifact

GATES = ("time", "scope", "mechanism", "counter_evidence")
GATE_CN = {
    "time": "时间对得上",
    "scope": "范围对得上",
    "mechanism": "机制说得通",
    "counter_evidence": "有反证",
}


@dataclass
class GateResult:
    passed: bool
    detail: Dict[str, bool] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)

    def report(self) -> str:
        lines = []
        for g in GATES:
            mark = "✅" if self.detail.get(g) else "❌"
            lines.append(f"  {mark} {GATE_CN[g]}")
        verdict = "全部通过，可收敛" if self.passed else f"未过：缺 {', '.join(GATE_CN[m] for m in self.missing)}"
        return "\n".join(lines) + f"\n  => {verdict}"


class FourGate:
    """把证据按其 for_hypothesis 归到四关。一关有 observed 证据即算过。"""

    def __init__(self) -> None:
        self._bind: Dict[str, List[EvidenceArtifact]] = {g: [] for g in GATES}

    def bind(self, gate: str, evidence: EvidenceArtifact) -> None:
        if gate not in GATES:
            raise ValueError(f"unknown gate '{gate}'; expected one of {GATES}")
        self._bind[gate].append(evidence)

    def evaluate(self) -> GateResult:
        detail = {}
        for g in GATES:
            detail[g] = any(e.is_observed() for e in self._bind[g])
        missing = [g for g in GATES if not detail[g]]
        return GateResult(passed=not missing, detail=detail, missing=missing)

    def bindings(self) -> Dict[str, List[str]]:
        """Serializable evidence ids bound to each gate."""
        return {gate: [e.id for e in evidence] for gate, evidence in self._bind.items()}
