"""能力 Gate —— 轻量阻塞状态机（平台无关）。

解决"权限缺失后继续执行并用低可信数据收口"的真实故障。
内核只认 required operation 的状态与动作，不读取任何平台 scope。

状态：blocked / ready / completed / cancelled
动作：require / complete / close

收口/验收前只检查：是否仍有未完成的 required operation。
具体企业平台怎么授权，是插件的事，不进内核。
"""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional


class OpStatus(str, Enum):
    BLOCKED = "blocked"
    READY = "ready"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class RequiredOperation:
    op_id: str
    reason: str
    task_id: str = ""
    requirement_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: OpStatus = OpStatus.BLOCKED
    created_at: float = field(default_factory=time.time)
    evidence_path: str = ""
    evidence_sha256: str = ""
    cancellation_reason: str = ""
    cancelled_by_user: bool = False
    cancellation_attestation_path: str = ""
    cancellation_attestation_sha256: str = ""

    def to_dict(self) -> dict:
        return {
            "op_id": self.op_id,
            "reason": self.reason,
            "task_id": self.task_id,
            "requirement_id": self.requirement_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "cancellation_reason": self.cancellation_reason,
            "cancelled_by_user": self.cancelled_by_user,
            "cancellation_attestation_path": self.cancellation_attestation_path,
            "cancellation_attestation_sha256": self.cancellation_attestation_sha256,
        }


class CapabilityGate:
    def __init__(self) -> None:
        self._ops: Dict[str, RequiredOperation] = {}
        self._integrity_valid = True

    def _integrity_sha256(self) -> str:
        payload = [operation.to_dict() for operation in self._ops.values()]
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def require(self, op_id: str, reason: str, *, task_id: str = "") -> RequiredOperation:
        op = RequiredOperation(
            op_id=op_id, reason=reason, task_id=task_id, status=OpStatus.BLOCKED
        )
        self._ops[op_id] = op
        return op

    def complete(
        self,
        op_id: str,
        evidence_path: str = "",
        *,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
    ) -> None:
        if op_id not in self._ops:
            raise ValueError(f"unknown required operation: {op_id}")
        if not evidence_path:
            raise ValueError("capability completion requires a non-empty evidence path")
        if op_id in self._ops:
            path = Path(evidence_path)
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"capability evidence does not exist or is empty: {path}")
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as error:
                raise ValueError(f"capability evidence must be JSON: {error}") from error
            if verify_attestation is None or not verify_attestation(path, receipt):
                raise ValueError("capability completion requires trusted platform attestation")
            if receipt.get("operation_id") != op_id:
                raise ValueError("capability evidence operation_id mismatch")
            if receipt.get("task_id") != self._ops[op_id].task_id:
                raise ValueError("capability evidence task_id mismatch")
            if receipt.get("requirement_id") != self._ops[op_id].requirement_id:
                raise ValueError("capability evidence requirement_id mismatch")
            if float(receipt.get("required_at", 0)) != self._ops[op_id].created_at:
                raise ValueError("capability evidence required_at mismatch")
            if receipt.get("kind") != "observed" or receipt.get("outcome") != "success":
                raise ValueError("capability evidence must be observed success")
            if (
                float(receipt.get("created_at", 0)) < self._ops[op_id].created_at
                or float(receipt.get("created_at", 0)) > time.time() + 5
            ):
                raise ValueError("capability evidence timestamp is outside requirement window")
            if float(receipt.get("expires_at", 0)) <= time.time():
                raise ValueError("capability evidence attestation has expired")
            self._ops[op_id].status = OpStatus.COMPLETED
            self._ops[op_id].evidence_path = str(path)
            self._ops[op_id].evidence_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    def close(
        self,
        op_id: str,
        *,
        attestation_path: str,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
    ) -> None:
        if op_id not in self._ops:
            raise ValueError(f"unknown required operation: {op_id}")
        if op_id in self._ops:
            path = Path(attestation_path)
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("capability cancellation requires an attestation file")
            row = json.loads(path.read_text(encoding="utf-8"))
            if verify_attestation is None or not verify_attestation(path, row):
                raise ValueError("capability cancellation requires trusted user attestation")
            if (
                row.get("operation_id") != op_id
                or row.get("task_id") != self._ops[op_id].task_id
                or row.get("requirement_id") != self._ops[op_id].requirement_id
                or float(row.get("required_at", 0)) != self._ops[op_id].created_at
                or row.get("confirmed") is not True
                or not str(row.get("reason", "")).strip()
                or not row.get("user_id")
                or float(row.get("created_at", 0)) < self._ops[op_id].created_at
                or float(row.get("created_at", 0)) > time.time() + 5
                or float(row.get("expires_at", 0)) <= time.time()
            ):
                raise ValueError("capability cancellation attestation is incomplete")
            self._ops[op_id].status = OpStatus.CANCELLED
            self._ops[op_id].cancellation_reason = str(row["reason"]).strip()
            self._ops[op_id].cancelled_by_user = True
            self._ops[op_id].cancellation_attestation_path = str(path)
            self._ops[op_id].cancellation_attestation_sha256 = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()

    def pending(self) -> List[RequiredOperation]:
        """仍未完成（阻塞或待就绪）的 required operation。"""
        return [o for o in self._ops.values()
                if o.status in (OpStatus.BLOCKED, OpStatus.READY)]

    def operation_ids(self) -> List[str]:
        return list(self._ops)

    def can_wrapup(
        self,
        verify_attestation: Optional[Callable[[str, Path, dict], bool]] = None,
        *,
        expected_task_id: str = "",
    ) -> tuple[bool, str]:
        """收口/验收 gate：还有未完成 required operation 就不许收口。"""
        if not self._integrity_valid:
            return False, "必需操作状态文件完整性校验失败"
        invalid = []
        for operation in self._ops.values():
            if expected_task_id and operation.task_id != expected_task_id:
                invalid.append(f"{operation.op_id}(task_id 不匹配)")
            if operation.status == OpStatus.COMPLETED:
                path = Path(operation.evidence_path)
                if (
                    not path.is_file()
                    or path.stat().st_size == 0
                    or hashlib.sha256(path.read_bytes()).hexdigest() != operation.evidence_sha256
                ):
                    invalid.append(f"{operation.op_id}(回读证据已丢失)")
                else:
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        verify_attestation is None
                        or not verify_attestation("capability", path, receipt)
                    ):
                        invalid.append(f"{operation.op_id}(平台证明无法复验)")
                    if (
                        receipt.get("operation_id") != operation.op_id
                        or receipt.get("task_id") != operation.task_id
                        or receipt.get("requirement_id") != operation.requirement_id
                        or float(receipt.get("required_at", 0)) != operation.created_at
                        or receipt.get("kind") != "observed"
                        or receipt.get("outcome") != "success"
                        or float(receipt.get("created_at", 0)) < operation.created_at
                        or float(receipt.get("created_at", 0)) > time.time() + 5
                    ):
                        invalid.append(f"{operation.op_id}(平台证明主体或结论不匹配)")
                    if float(receipt.get("expires_at", 0)) <= time.time():
                        invalid.append(f"{operation.op_id}(回读证明已过期)")
            if operation.status == OpStatus.CANCELLED and (
                not operation.cancelled_by_user or not operation.cancellation_reason
            ):
                invalid.append(f"{operation.op_id}(取消未获用户确认)")
            if operation.status == OpStatus.CANCELLED:
                path = Path(operation.cancellation_attestation_path)
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest()
                    != operation.cancellation_attestation_sha256
                ):
                    invalid.append(f"{operation.op_id}(取消证明已变化)")
                else:
                    row = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        verify_attestation is None
                        or not verify_attestation("user_confirmation", path, row)
                    ):
                        invalid.append(f"{operation.op_id}(用户取消证明无法复验)")
                    if (
                        row.get("operation_id") != operation.op_id
                        or row.get("task_id") != operation.task_id
                        or row.get("requirement_id") != operation.requirement_id
                        or float(row.get("required_at", 0)) != operation.created_at
                        or row.get("confirmed") is not True
                        or str(row.get("reason", "")).strip()
                        != operation.cancellation_reason
                        or not row.get("user_id")
                        or float(row.get("created_at", 0)) < operation.created_at
                        or float(row.get("created_at", 0)) > time.time() + 5
                    ):
                        invalid.append(
                            f"{operation.op_id}(用户取消证明主体或理由不匹配)"
                        )
                    if float(row.get("expires_at", 0)) <= time.time():
                        invalid.append(f"{operation.op_id}(取消证明已过期)")
        if invalid:
            return False, "必需操作状态无效：" + ", ".join(invalid)
        pend = self.pending()
        if pend:
            names = ", ".join(f"{o.op_id}({o.reason})" for o in pend)
            return False, f"仍有未完成的必需操作：{names}"
        return True, "无未完成必需操作，可收口"

    def to_dict(self) -> dict:
        return {
            "operations": [operation.to_dict() for operation in self._ops.values()],
            "integrity_sha256": self._integrity_sha256(),
        }

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "CapabilityGate":
        gate = cls()
        target = Path(path)
        if not target.exists():
            return gate
        data = json.loads(target.read_text(encoding="utf-8"))
        for row in data.get("operations", []):
            operation = RequiredOperation(
                op_id=row["op_id"],
                reason=row["reason"],
                task_id=row.get("task_id", ""),
                requirement_id=row.get("requirement_id", ""),
                status=OpStatus(row.get("status", "blocked")),
                created_at=row.get("created_at", time.time()),
                evidence_path=row.get("evidence_path", ""),
                evidence_sha256=row.get("evidence_sha256", ""),
                cancellation_reason=row.get("cancellation_reason", ""),
                cancelled_by_user=bool(row.get("cancelled_by_user", False)),
                cancellation_attestation_path=row.get("cancellation_attestation_path", ""),
                cancellation_attestation_sha256=row.get("cancellation_attestation_sha256", ""),
            )
            gate._ops[operation.op_id] = operation
        gate._integrity_valid = (
            bool(data.get("integrity_sha256"))
            and data.get("integrity_sha256") == gate._integrity_sha256()
        )
        return gate
