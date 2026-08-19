"""证据落盘 —— 把一次能力调用的产物写进 evidence_dir，返回 EvidenceArtifact。

统一目录布局：<data>/evidence/<capability>-<时间戳>/
命令类证据落 cmd.log；二进制产物（截图/录屏）落原文件。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from kernel.evidence import EvidenceArtifact, EvidenceKind
from kernel.runner import CommandOutput


class EvidenceWriter:
    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir) / "evidence"

    def _dir(self, capability: str) -> Path:
        d = self.root / f"{capability}-{time.strftime('%Y%m%d-%H%M%S')}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def from_command(self, *, capability: str, source: str, out: CommandOutput,
                     summary: str, kind: EvidenceKind = EvidenceKind.OBSERVED) -> tuple[EvidenceArtifact, str]:
        d = self._dir(capability)
        log = d / "cmd.log"
        log.write_text(
            f"$ {' '.join(out.argv)}\n[exit={out.returncode} dur={out.duration:.2f}s]\n\n"
            f"--- stdout ---\n{out.stdout}\n--- stderr ---\n{out.stderr}\n",
            encoding="utf-8",
        )
        ev = EvidenceArtifact(capability=capability, source=source, kind=kind,
                              summary=summary, path=str(log),
                              outcome="success" if out.ok() else "failure")
        return ev, str(d)

    def register_file(self, *, capability: str, source: str, file_path: str,
                      summary: str) -> EvidenceArtifact:
        return EvidenceArtifact(capability=capability, source=source,
                                kind=EvidenceKind.OBSERVED, summary=summary,
                                path=file_path, outcome="success")
