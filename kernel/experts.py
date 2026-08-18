"""诊断方法专家层 —— 加载 experts.json，按任务路由专家。

专家只回答限定问题、只描述"怎么想"，通过 wants_capabilities 声明它想要哪类证据，
但不认识任何具体平台。coordinator 拥有病例和最终结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_DEFAULT = Path(__file__).resolve().parent / "experts.json"

REQUIRED_OUTPUT = ["verdict", "summary", "evidence_ids", "remaining_uncertainty", "suggested_experts", "next_test"]
VERDICTS = ["supports", "contradicts", "inconclusive", "cross_domain"]


@dataclass
class Expert:
    id: str
    name: str
    role: str  # coordinator | method_expert
    triggers: List[str] = field(default_factory=list)
    can_answer: List[str] = field(default_factory=list)
    default_hypotheses: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    wants_capabilities: List[str] = field(default_factory=list)
    cost: str = "medium"

    def score(self, task: str) -> int:
        t = task.lower()
        return sum(1 for kw in self.triggers if kw.lower() in t)


class ExpertRegistry:
    def __init__(self, path: str | Path = _DEFAULT) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.protocol = data.get("expert_protocol", {})
        self._experts = {}
        for item in data.get("experts", []):
            # 只保留 schema 认识的字段，忽略内部版遗留字段
            allowed = {k: item[k] for k in Expert.__dataclass_fields__ if k in item}
            self._experts[item["id"]] = Expert(**allowed)

    def coordinator(self) -> Optional[Expert]:
        for e in self._experts.values():
            if e.role == "coordinator":
                return e
        return None

    def method_experts(self) -> List[Expert]:
        return [e for e in self._experts.values() if e.role == "method_expert"]

    def get(self, expert_id: str) -> Optional[Expert]:
        return self._experts.get(expert_id)

    def route(self, task: str) -> List[Expert]:
        """按 triggers 命中数排序，返回相关方法专家（命中>0）。"""
        ranked = sorted(self.method_experts(), key=lambda e: e.score(task), reverse=True)
        return [e for e in ranked if e.score(task) > 0]
