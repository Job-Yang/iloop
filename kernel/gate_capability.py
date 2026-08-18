"""能力 Gate —— 轻量阻塞状态机（平台无关）。

解决"权限缺失后继续执行并用低可信数据收口"的真实故障。
内核只认 required operation 的状态与动作，不读取任何平台 scope。

状态：blocked / ready / completed / cancelled
动作：require / complete / close

收口/验收前只检查：是否仍有未完成的 required operation。
具体某个平台（飞书/自建）怎么授权，是插件的事，不进内核。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class OpStatus(str, Enum):
    BLOCKED = "blocked"
    READY = "ready"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class RequiredOperation:
    op_id: str
    reason: str
    status: OpStatus = OpStatus.BLOCKED
    created_at: float = field(default_factory=time.time)


class CapabilityGate:
    def __init__(self) -> None:
        self._ops: Dict[str, RequiredOperation] = {}

    def require(self, op_id: str, reason: str) -> RequiredOperation:
        op = RequiredOperation(op_id=op_id, reason=reason, status=OpStatus.BLOCKED)
        self._ops[op_id] = op
        return op

    def complete(self, op_id: str) -> None:
        if op_id in self._ops:
            self._ops[op_id].status = OpStatus.COMPLETED

    def close(self, op_id: str) -> None:
        if op_id in self._ops:
            self._ops[op_id].status = OpStatus.CANCELLED

    def pending(self) -> List[RequiredOperation]:
        """仍未完成（阻塞或待就绪）的 required operation。"""
        return [o for o in self._ops.values()
                if o.status in (OpStatus.BLOCKED, OpStatus.READY)]

    def can_wrapup(self) -> tuple[bool, str]:
        """收口/验收 gate：还有未完成 required operation 就不许收口。"""
        pend = self.pending()
        if pend:
            names = ", ".join(f"{o.op_id}({o.reason})" for o in pend)
            return False, f"仍有未完成的必需操作：{names}"
        return True, "无未完成必需操作，可收口"
