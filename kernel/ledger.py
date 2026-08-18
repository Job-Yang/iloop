"""记账与外显 —— round 记账、trace 时间线、【iLoop】前缀协议。

iLoop 真正参与的关键阶段用固定前缀外显。归因硬边界：只有 iLoop 流程/工具
实际参与的动作才带【iLoop】。防死循环：同根因失败最多修 N 轮。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

BRAND = "【iLoop】"

# 固定前缀（勿自创）
PHASES = {
    "plan": "📋 计划",
    "connect": "🔌 接入",
    "evidence": "🔍 取证",
    "change": "🔧 改动",
    "build": "🛠 构建",
    "verify": "✅ 验证",
    "blocked": "⛔ 阻塞",
    "wrapup": "📝 收口",
}


def render(phase: str, what: str, *, basis: str = "", result: str = "") -> str:
    """外显一条：【iLoop】<前缀> <做什么> · 依据=<为什么> · 结果=<发现/下一步>"""
    icon = PHASES.get(phase, phase)
    line = f"{BRAND}{icon} {what}"
    if basis:
        line += f" · 依据={basis}"
    if result:
        line += f" · 结果={result}"
    return line


class RoundStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class Round:
    index: int
    goal: str
    status: RoundStatus = RoundStatus.RUNNING
    root_cause_tag: str = ""   # 用于同根因失败计数
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None


class Ledger:
    """轮次记账 + trace 时间线 + 反循环闸门。"""

    SAME_CAUSE_LIMIT = 3   # 同根因构建失败最多修 3 轮
    TOTAL_FAIL_LIMIT = 6   # 单任务总失败最多 6 轮

    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir)
        self.rounds: List[Round] = []
        self.traces: List[str] = []

    def log_round_start(self, goal: str, root_cause_tag: str = "") -> Round:
        r = Round(index=len(self.rounds) + 1, goal=goal, root_cause_tag=root_cause_tag)
        self.rounds.append(r)
        return r

    def log_round_end(self, status: RoundStatus | str) -> Round:
        if not self.rounds:
            raise RuntimeError("no round to end")
        r = self.rounds[-1]
        r.status = RoundStatus(status) if isinstance(status, str) else status
        r.ended_at = time.time()
        return r

    def same_cause_failures(self, tag: str) -> int:
        return sum(1 for r in self.rounds
                   if r.status == RoundStatus.FAILED and r.root_cause_tag == tag)

    def total_failures(self) -> int:
        return sum(1 for r in self.rounds if r.status == RoundStatus.FAILED)

    def should_stop(self, tag: str = "") -> tuple[bool, str]:
        """反循环闸门：超限即停手给证据。"""
        if tag and self.same_cause_failures(tag) >= self.SAME_CAUSE_LIMIT:
            return True, f"同根因'{tag}'失败已达 {self.SAME_CAUSE_LIMIT} 轮，停手升级"
        if self.total_failures() >= self.TOTAL_FAIL_LIMIT:
            return True, f"单任务总失败已达 {self.TOTAL_FAIL_LIMIT} 轮，停手升级"
        return False, ""

    def trace(self, phase: str, what: str) -> str:
        line = render(phase, what)
        self.traces.append(line)
        return line

    def flush(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "trace.jsonl").write_text(
            "\n".join(json.dumps({"line": t}, ensure_ascii=False) for t in self.traces),
            encoding="utf-8",
        )
