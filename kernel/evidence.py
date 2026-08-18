"""协议 1：证据 EvidenceArtifact。

一切结论的地基。红线：推断（inferred）不许当观测（observed）。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class EvidenceKind(str, Enum):
    OBSERVED = "observed"   # 真跑真看到
    INFERRED = "inferred"   # 从源码/日志推出来的


@dataclass
class EvidenceArtifact:
    capability: str
    source: str
    kind: EvidenceKind
    summary: str
    path: Optional[str] = None
    for_hypothesis: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            self.kind = EvidenceKind(self.kind)
        if not self.id:
            seed = f"{self.capability}|{self.source}|{self.summary}|{self.created_at}"
            self.id = "ev-" + hashlib.sha1(seed.encode()).hexdigest()[:8]

    def is_observed(self) -> bool:
        return self.kind == EvidenceKind.OBSERVED

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d
