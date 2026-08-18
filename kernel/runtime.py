"""Persistent orchestration for plan -> task -> capability -> evidence -> resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from .capability import Capability, Plugin
from .case import Case
from .dashboard import Dashboard
from .evidence import EvidenceArtifact, EvidenceKind
from .flow import FlowRegistry
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


class Runtime:
    """One project-scoped runtime. All durable state stays below data_dir."""

    def __init__(self, data_dir: str | Path, registry: FlowRegistry, plugin: Plugin) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.plugin = plugin
        self.tasks = TaskStore(self.data_dir)

    def _task_dir(self, task_id: str) -> Path:
        path = self.data_dir / "runtime" / task_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _case_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "case.json"

    def _evidence_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "evidence.jsonl"

    def start(self, title: str, *, constraints: Optional[list[str]] = None,
              acceptance: Optional[list[str]] = None,
              capabilities: Optional[Iterable[str]] = None) -> TaskRecord:
        flow = self.registry.plan(title)
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
        case = Case(task.id, title)
        case.add_hypothesis("当前实现满足任务目标")
        case.save(self._case_path(task.id))
        task.case_path = str(self._case_path(task.id))
        self.tasks.save(task)
        Ledger(self._task_dir(task.id)).flush()
        return task

    def load(self, task_id: str) -> TaskRecord:
        return self.tasks.load(task_id)

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
            result = self.plugin.invoke(capability, **kwargs)
            evidence = EvidenceArtifact(
                capability=capability.value,
                source=result.platform,
                kind=EvidenceKind.OBSERVED,
                summary=result.summary,
                path=result.evidence_dir or None,
            )
            self._append_evidence(task.id, evidence)
            case.attach("h1", evidence, refutes=not result.ok())
            case.save(self._case_path(task.id))
            task.evidence_ids.append(evidence.id)
            step.summary = result.summary
            step.status = StepStatus.DONE if result.ok() else StepStatus.FAILED
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

    def complete(self, task: TaskRecord) -> TaskRecord:
        if any(s.status != StepStatus.DONE for s in task.steps):
            raise ValueError("task has unfinished steps")
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
        return Dashboard(ledger, evidence=self.evidence(task_id)).save(
            runtime_dir / "dashboard.html"
        )
