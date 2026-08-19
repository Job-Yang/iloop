"""协议 1：证据 EvidenceArtifact。

一切结论的地基。红线：推断（inferred）不许当观测（observed）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Dict, Optional
from pathlib import Path


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
    outcome: str = "unknown"  # success | failure | neutral | unknown
    metadata: Dict[str, object] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.kind, str):
            self.kind = EvidenceKind(self.kind)
        if self.kind == EvidenceKind.OBSERVED and self.path and "artifact_sha256" not in self.metadata:
            digest = self._artifact_digest(Path(self.path))
            if digest:
                self.metadata["artifact_sha256"] = digest
        if not self.id:
            seed = f"{self.capability}|{self.source}|{self.summary}|{self.created_at}"
            self.id = "ev-" + hashlib.sha1(seed.encode()).hexdigest()[:8]

    def is_observed(self) -> bool:
        return self.kind == EvidenceKind.OBSERVED

    def supports_success(
        self,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
        expected_bindings: Optional[Dict[str, object]] = None,
    ) -> bool:
        """Observed is provenance, not verdict. Only successful observations support completion."""
        if not self.is_observed() or self.outcome != "success":
            return False
        for key, expected in (expected_bindings or {}).items():
            if expected not in (None, "") and self.metadata.get(key) != expected:
                return False
        if not self.path:
            return False
        path = Path(self.path)
        expected = str(self.metadata.get("artifact_sha256", ""))
        if not expected or self._artifact_digest(path) != expected:
            return False
        if self.metadata.get("trusted_producer") is True:
            receipt_path = Path(str(self.metadata.get("producer_receipt_path", "")))
            receipt_hash = str(self.metadata.get("producer_receipt_sha256", ""))
            if (
                not receipt_hash
                or not receipt_path.is_file()
                or hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt_hash
            ):
                return False
            try:
                row = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return False
            if (
                row.get("producer") != "iloop-runtime"
                or self.metadata.get("human_confirmed") is True
                or verify_attestation is None
                or not verify_attestation(receipt_path, row)
            ):
                return False
        else:
            receipt_path = Path(str(self.metadata.get("attestation_path", "")))
            receipt_hash = str(self.metadata.get("attestation_sha256", ""))
            if (
                not receipt_hash
                or not receipt_path.is_file()
                or hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt_hash
            ):
                return False
            try:
                row = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return False
            if verify_attestation is None or not verify_attestation(receipt_path, row):
                return False
            if float(row.get("expires_at", 0)) <= time.time():
                return False
            if self.metadata.get("human_confirmed") is True and (
                row.get("kind") != "user_confirmation"
                or row.get("user_id") != self.metadata.get("user_id")
            ):
                return False
        expected_receipt = {
            "capability": self.capability,
            "source": self.source,
            "outcome": self.outcome,
            "summary": self.summary,
            "path": self.path,
            "artifact_sha256": expected,
            "task_id": self.metadata.get("task_id", ""),
            "run_id": self.metadata.get("run_id", ""),
            "source_run_id": self.metadata.get("source_run_id", ""),
            "flow_id": self.metadata.get("flow_id", ""),
            "flow_run_id": self.metadata.get("flow_run_id", ""),
            "device": self.metadata.get("device", ""),
            "device_id": self.metadata.get("device_id", ""),
            "subjects": self.metadata.get("subjects", []),
            "gates": self.metadata.get("gates", []),
        }
        return all(
            row.get(key, "" if key == "source_run_id" else None) == value
            for key, value in expected_receipt.items()
        )

    def supports_gate(
        self,
        gate: str,
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
        expected_bindings: Optional[Dict[str, object]] = None,
    ) -> bool:
        if not self.supports_success(verify_attestation, expected_bindings):
            return False
        return gate in self.metadata.get("gates", [])

    @staticmethod
    def _artifact_digest(path: Path) -> str:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_dir():
            rows = []
            for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
                rows.append({
                    "path": str(item.relative_to(path)),
                    "sha256": hashlib.sha256(item.read_bytes()).hexdigest(),
                })
            if rows:
                return hashlib.sha256(
                    json.dumps(rows, sort_keys=True).encode("utf-8")
                ).hexdigest()
        return ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d
