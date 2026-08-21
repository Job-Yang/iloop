"""Reusable UI path graph with evidence-bearing verification state."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .evidence import EvidenceArtifact
from .storage import atomic_write_json, file_lock


ACTION_CAPABILITY = {
    "tap": "tap",
    "swipe": "swipe",
    "type": "type_text",
    "screenshot": "screenshot",
    "snapshot": "view_tree",
    "assert": "view_tree",
    "launch": "launch",
}
BINDING_RESERVATION_TTL = 300.0


@dataclass
class UINode:
    id: str
    action: str
    target: str
    next: List[str] = field(default_factory=list)
    condition: str = ""
    screenshot: str = ""
    evidence_ids: List[str] = field(default_factory=list)


@dataclass
class UIFlow:
    id: str
    name: str
    goal: str
    device: str
    entry: str
    nodes: List[UINode]
    device_id: str = ""
    status: str = "draft"
    source: str = "user_taught"
    task_id: str = ""
    verification_run_id: str = ""
    binding_token: str = ""
    binding_started_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    verified_at: float = 0.0

    def to_dict(self) -> dict:
        return {**asdict(self), "nodes": [asdict(node) for node in self.nodes]}

    def validate(self) -> List[str]:
        issues = []
        by_id = {node.id: node for node in self.nodes}
        if self.entry not in by_id:
            issues.append(f"entry not found: {self.entry}")
        for node in self.nodes:
            if node.action not in ACTION_CAPABILITY:
                issues.append(f"unsupported action {node.action} at {node.id}")
            for target in node.next:
                if target not in by_id:
                    issues.append(f"node {node.id} points to missing {target}")
            if not node.evidence_ids:
                issues.append(f"node {node.id} has no execution evidence")
        return issues


class UIFlowStore:
    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "ui_flows"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _slug(text: str) -> str:
        return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", text).strip("-")[:48] or "flow"

    def create(self, name: str, goal: str, device: str = "auto") -> UIFlow:
        flow_id = f"{self._slug(name)}-{uuid.uuid4().hex[:10]}"
        flow = UIFlow(
            id=flow_id,
            name=name,
            goal=goal,
            device=device,
            entry="n1",
            nodes=[
                UINode("n1", "launch", "app", ["n2"]),
                UINode("n2", "assert", goal, []),
            ],
        )
        self.save(flow)
        return flow

    def path(self, flow_id: str) -> Path:
        value = str(flow_id)
        if not re.fullmatch(r"[\w-]{1,160}", value):
            raise ValueError("flow_id contains unsafe path characters")
        target = (self.root / value / "flow.json").resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("flow_id escapes ui_flows root")
        return target

    def save(self, flow: UIFlow) -> Path:
        path = self.path(flow.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "shots").mkdir(exist_ok=True)
        with file_lock(self.root / ".ui-flows.lock"):
            atomic_write_json(path, flow.to_dict())
        return path

    def load(self, flow_id: str) -> UIFlow:
        data = json.loads(self.path(flow_id).read_text(encoding="utf-8"))
        data["nodes"] = [UINode(**row) for row in data.get("nodes", [])]
        return UIFlow(**data)

    def list(self) -> List[UIFlow]:
        return [self.load(path.parent.name) for path in sorted(self.root.glob("*/flow.json"))]

    def verify(
        self,
        flow_id: str,
        evidence_by_id: Dict[str, EvidenceArtifact],
        verify_attestation=None,
        bindings: Optional[dict] = None,
    ) -> UIFlow:
        flow = self.load(flow_id)
        for key, value in (bindings or {}).items():
            if not value or key not in {
                "task_id", "verification_run_id", "device", "device_id"
            }:
                continue
            current = getattr(flow, key)
            if key == "device" and current == "auto":
                setattr(flow, key, value)
            elif not current:
                setattr(flow, key, value)
            elif current != value:
                raise ValueError(
                    f"UI flow {key} is already bound to {current}"
                )
        used = {
            evidence_id
            for node in flow.nodes
            for evidence_id in node.evidence_ids
        }
        candidates = sorted(
            evidence_by_id.values(),
            key=lambda item: item.created_at,
        )
        for node in flow.nodes:
            if node.evidence_ids:
                continue
            required = ACTION_CAPABILITY.get(node.action)
            match = next(
                (
                    evidence
                    for evidence in candidates
                    if evidence.id not in used
                    and evidence.capability == required
                    and evidence.metadata.get("task_id") == flow.task_id
                    and evidence.metadata.get("ui_flow_id") == flow.id
                    and evidence.metadata.get("flow_run_id")
                    == flow.verification_run_id
                    and (
                        flow.device == "auto"
                        or evidence.metadata.get("device") == flow.device
                    )
                    and (
                        not flow.device_id
                        or evidence.metadata.get("device_id") == flow.device_id
                    )
                    and evidence.supports_success(verify_attestation)
                ),
                None,
            )
            if match is not None:
                node.evidence_ids = [match.id]
                used.add(match.id)
        issues = flow.validate()
        referenced = {
            evidence_id
            for node in flow.nodes
            for evidence_id in node.evidence_ids
        }
        unknown = sorted(referenced - set(evidence_by_id))
        if unknown:
            issues.append(f"unknown evidence ids: {unknown}")
        for node in flow.nodes:
            required = ACTION_CAPABILITY.get(node.action)
            for evidence_id in node.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    continue
                if not evidence.supports_success(verify_attestation):
                    issues.append(f"node {node.id} evidence {evidence_id} is not successful observed evidence")
                if required and evidence.capability != required:
                    issues.append(
                        f"node {node.id} requires {required}, got {evidence.capability}"
                    )
                if not flow.task_id or evidence.metadata.get("task_id") != flow.task_id:
                    issues.append(f"node {node.id} evidence is from another task")
                if (
                    not flow.verification_run_id
                    or evidence.metadata.get("flow_run_id") != flow.verification_run_id
                    or evidence.metadata.get("ui_flow_id") != flow.id
                ):
                    issues.append(f"node {node.id} evidence is from another flow run")
                if flow.device != "auto" and evidence.metadata.get("device") != flow.device:
                    issues.append(f"node {node.id} evidence is from another device type")
                if flow.device_id and evidence.metadata.get("device_id") != flow.device_id:
                    issues.append(f"node {node.id} evidence is from another device id")
        if issues:
            raise ValueError("; ".join(issues))
        flow.status = "verified"
        flow.verified_at = time.time()
        self.save(flow)
        return flow

    def bind_task(self, flow_id: str, task_id: str, run_id: str,
                  device: str, device_id: str = "",
                  binding_token: str = "") -> UIFlow:
        with file_lock(self.root / ".ui-flows.lock"):
            flow = self.load(flow_id)
            requested = (task_id, run_id, device, device_id)
            current = (
                flow.task_id,
                flow.verification_run_id,
                flow.device,
                flow.device_id,
            )
            if flow.task_id or flow.verification_run_id:
                if current == requested:
                    return flow
                raise ValueError(
                    f"UI flow {flow.id} is already bound to "
                    f"{flow.task_id}/{flow.verification_run_id}"
                )
            if flow.binding_token and flow.binding_token != binding_token:
                raise ValueError(f"UI flow {flow.id} is reserved by another task")
            flow.task_id = task_id
            flow.verification_run_id = run_id
            flow.device = device
            flow.device_id = device_id
            flow.binding_token = ""
            flow.binding_started_at = 0.0
            atomic_write_json(self.path(flow.id), flow.to_dict())
            return flow

    def reserve_task(self, flow_id: str, binding_token: str) -> UIFlow:
        if not binding_token:
            raise ValueError("UI flow task reservation requires a token")
        with file_lock(self.root / ".ui-flows.lock"):
            flow = self.load(flow_id)
            if flow.task_id or flow.verification_run_id:
                raise ValueError(
                    f"UI Flow {flow.id} 已绑定 Task {flow.task_id}，不能重复转换"
                )
            if flow.binding_token and flow.binding_token != binding_token:
                age = time.time() - flow.binding_started_at
                if age < BINDING_RESERVATION_TTL:
                    raise ValueError(
                        f"UI flow {flow.id} is reserved by another task"
                    )
            flow.binding_token = binding_token
            flow.binding_started_at = time.time()
            atomic_write_json(self.path(flow.id), flow.to_dict())
            return flow

    def release_task_reservation(
        self,
        flow_id: str,
        binding_token: str,
    ) -> UIFlow:
        with file_lock(self.root / ".ui-flows.lock"):
            flow = self.load(flow_id)
            if flow.binding_token == binding_token and not flow.task_id:
                flow.binding_token = ""
                flow.binding_started_at = 0.0
                atomic_write_json(self.path(flow.id), flow.to_dict())
            return flow
