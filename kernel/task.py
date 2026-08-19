"""Durable task plans used as the external memory anchor for long-running work."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional


class TaskStatus(str, Enum):
    OPEN = "open"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class TaskStep:
    title: str
    capability: str = ""
    status: StepStatus = StepStatus.PENDING
    summary: str = ""
    evidence_ids: List[str] = field(default_factory=list)
    completion_source: str = ""  # machine | human_confirmed

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = StepStatus(self.status)


@dataclass
class TaskRecord:
    id: str
    title: str
    goal: str
    flow_id: str
    autonomy: str
    status: TaskStatus = TaskStatus.OPEN
    current_stage: str = "investigate"
    constraints: List[str] = field(default_factory=list)
    acceptance: List[str] = field(default_factory=list)
    steps: List[TaskStep] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    required_operation_ids: List[str] = field(default_factory=list)
    capability_runs: Dict[str, str] = field(default_factory=dict)
    case_path: str = ""
    capability_gate_path: str = ""
    global_review_path: str = ""
    acceptance_path: str = ""
    executor_id: str = ""
    project_root: str = ""
    base_commit: str = ""
    global_review_required: bool = False
    independent_acceptance_required: bool = False
    global_review_status: str = "not_required"
    acceptance_status: str = "not_required"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)
        self.steps = [s if isinstance(s, TaskStep) else TaskStep(**s) for s in self.steps]

    def next_step(self) -> Optional[TaskStep]:
        return next((s for s in self.steps if s.status != StepStatus.DONE), None)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        for step in data["steps"]:
            if isinstance(step["status"], StepStatus):
                step["status"] = step["status"].value
        return data


class TaskStore:
    """JSON task store with atomic writes and deterministic resume cards."""

    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(text: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text).strip("-")[:40]
        return slug or "task"

    def create(self, title: str, *, goal: str, flow_id: str, autonomy: str,
               constraints: Optional[List[str]] = None,
               acceptance: Optional[List[str]] = None,
               steps: Optional[List[TaskStep]] = None) -> TaskRecord:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        task_id = f"task-{stamp}-{self._slug(title)}"
        record = TaskRecord(
            id=task_id,
            title=title,
            goal=goal,
            flow_id=flow_id,
            autonomy=autonomy,
            constraints=constraints or [],
            acceptance=acceptance or [],
            steps=steps or [],
        )
        self.save(record)
        return record

    def path_for(self, task_id: str) -> Path:
        self.validate_id(task_id)
        return self.root / f"{task_id}.json"

    @staticmethod
    def validate_id(task_id: str) -> str:
        value = str(task_id)
        if not re.fullmatch(r"[\w-]{1,160}", value):
            raise ValueError("task_id must contain only word characters, '_' or '-'")
        return value

    def save(self, task: TaskRecord) -> Path:
        task.updated_at = time.time()
        path = self.path_for(task.id)
        fd, tmp = tempfile.mkstemp(prefix=f".{task.id}.", suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(task.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return path

    def load(self, task_id: str) -> TaskRecord:
        path = self.path_for(task_id)
        if not path.exists():
            matches = sorted(self.root.glob(f"*{task_id}*.json"))
            if len(matches) != 1:
                raise FileNotFoundError(f"task not found or ambiguous: {task_id}")
            path = matches[0]
        return TaskRecord(**json.loads(path.read_text(encoding="utf-8")))

    def list(self, *, include_done: bool = False) -> List[TaskRecord]:
        tasks = []
        for path in sorted(self.root.glob("*.json"), reverse=True):
            task = TaskRecord(**json.loads(path.read_text(encoding="utf-8")))
            if include_done or task.status != TaskStatus.DONE:
                tasks.append(task)
        return tasks

    def resume_card(self, task: TaskRecord) -> dict:
        next_step = task.next_step()
        return {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "flow_id": task.flow_id,
            "autonomy": task.autonomy,
            "current_stage": task.current_stage,
            "goal": task.goal,
            "constraints": list(task.constraints),
            "acceptance": list(task.acceptance),
            "next_step": next_step.title if next_step else "",
            "next_capability": next_step.capability if next_step else "",
            "evidence_ids": list(task.evidence_ids),
            "updated_at": task.updated_at,
        }
