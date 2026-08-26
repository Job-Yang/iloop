"""记账与外显 —— round 记账、trace 时间线、【iLoop】前缀协议。

iLoop 真正参与的关键阶段用固定前缀外显。归因硬边界：只有 iLoop 流程/工具
实际参与的动作才带【iLoop】。防死循环：同根因失败最多修 N 轮。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
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


@dataclass
class TimingEvent:
    event_id: str
    phase: str
    status: str = "running"
    task_id: str = ""
    run_id: str = ""
    worker_id: str = ""
    action_id: str = ""
    capability_id: str = ""
    provider_id: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    blocked_seconds: float = 0.0

    @property
    def duration(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, self.ended_at - self.started_at)


class Ledger:
    """轮次记账 + trace 时间线 + 反循环闸门。"""

    SAME_CAUSE_LIMIT = 3   # 同根因构建失败最多修 3 轮
    TOTAL_FAIL_LIMIT = 6   # 单任务总失败最多 6 轮

    def __init__(self, data_dir: str | Path) -> None:
        self.dir = Path(data_dir)
        self.rounds: List[Round] = []
        self.traces: List[str] = []
        self.timings: List[TimingEvent] = []

    @classmethod
    def load(cls, data_dir: str | Path) -> "Ledger":
        ledger = cls(data_dir)
        rounds_path = ledger.dir / "rounds.json"
        traces_path = ledger.dir / "trace.jsonl"
        timings_path = ledger.dir / "timings.json"
        if rounds_path.exists():
            for row in json.loads(rounds_path.read_text(encoding="utf-8")):
                ledger.rounds.append(Round(
                    index=row["index"],
                    goal=row["goal"],
                    status=RoundStatus(row["status"]),
                    root_cause_tag=row.get("root_cause_tag", ""),
                    started_at=row.get("started_at", time.time()),
                    ended_at=row.get("ended_at"),
                ))
        if traces_path.exists():
            ledger.traces = [
                json.loads(line)["line"]
                for line in traces_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if timings_path.exists():
            for row in json.loads(
                timings_path.read_text(encoding="utf-8")
            ):
                ledger.timings.append(TimingEvent(**row))
        return ledger

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

    def start_timing(
        self,
        phase: str,
        *,
        task_id: str = "",
        run_id: str = "",
        worker_id: str = "",
        action_id: str = "",
        capability_id: str = "",
        provider_id: str = "",
        started_at: Optional[float] = None,
    ) -> TimingEvent:
        event = TimingEvent(
            event_id=(
                f"timing-{len(self.timings) + 1}-"
                f"{int((started_at or time.time()) * 1000000)}"
            ),
            phase=phase,
            task_id=task_id,
            run_id=run_id,
            worker_id=worker_id,
            action_id=action_id,
            capability_id=capability_id,
            provider_id=provider_id,
            started_at=time.time() if started_at is None else started_at,
        )
        self.timings.append(event)
        return event

    def end_timing(
        self,
        event_id: str,
        status: str,
        *,
        blocked_seconds: float = 0.0,
        ended_at: Optional[float] = None,
    ) -> TimingEvent:
        event = next(
            (item for item in self.timings if item.event_id == event_id),
            None,
        )
        if event is None:
            raise KeyError(f"timing event not found: {event_id}")
        if event.ended_at is not None:
            raise ValueError(f"timing event already ended: {event_id}")
        event.status = str(status)
        event.ended_at = time.time() if ended_at is None else ended_at
        event.blocked_seconds = max(0.0, float(blocked_seconds))
        return event

    def timing_metrics(self) -> dict:
        completed = [
            item for item in self.timings
            if item.ended_at is not None
        ]
        by_phase = {}
        by_action = {}
        by_capability = {}
        by_provider = {}
        for item in completed:
            for target, key in (
                (by_phase, item.phase),
                (by_action, item.action_id),
                (by_capability, item.capability_id),
                (by_provider, item.provider_id),
            ):
                if key:
                    target[key] = round(
                        target.get(key, 0.0) + item.duration, 6
                    )
        sweep = []
        for item in completed:
            sweep.append((item.started_at, 1))
            sweep.append((float(item.ended_at), -1))
        active = max_concurrency = 0
        for _, delta in sorted(sweep, key=lambda row: (row[0], row[1])):
            active += delta
            max_concurrency = max(max_concurrency, active)
        wall_seconds = (
            max(float(item.ended_at) for item in completed)
            - min(item.started_at for item in completed)
            if completed else 0.0
        )
        return {
            "events": len(completed),
            "wall_seconds": round(max(0.0, wall_seconds), 3),
            "work_seconds": round(
                sum(item.duration for item in completed), 3
            ),
            "blocked_seconds": round(
                sum(item.blocked_seconds for item in completed), 3
            ),
            "retry_events": sum(
                item.status in {"failed", "blocked", "retry"}
                for item in completed
            ),
            "max_concurrency": max_concurrency,
            "by_phase": by_phase,
            "by_action": by_action,
            "by_capability": by_capability,
            "by_provider": by_provider,
        }

    def flush(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rounds = []
        for round_ in self.rounds:
            row = asdict(round_)
            row["status"] = round_.status.value
            rounds.append(row)
        (self.dir / "rounds.json").write_text(
            json.dumps(rounds, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.dir / "trace.jsonl").write_text(
            "\n".join(json.dumps({"line": t}, ensure_ascii=False) for t in self.traces),
            encoding="utf-8",
        )
        (self.dir / "timings.json").write_text(
            json.dumps(
                [asdict(item) for item in self.timings],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
