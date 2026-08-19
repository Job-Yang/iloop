"""Project-scoped durable context, constitution, and blockers."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable, Iterable, Optional


class ProjectMemory:
    def __init__(
        self,
        data_dir: str | Path,
        *,
        project_root: str | Path = "",
    ) -> None:
        self.root = Path(data_dir)
        self.project_root = (
            Path(project_root).expanduser().resolve()
            if project_root
            else None
        )
        for name in (
            "inputs", "business_context", "constitution", "blockers",
            "records", "analysis", "reports", "lessons", "acceptance",
        ):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def context_manifest(self) -> dict:
        inputs = self.root / "inputs"
        files = []
        for path in sorted(p for p in inputs.rglob("*") if p.is_file()):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({
                "path": str(path.relative_to(inputs)),
                "size": path.stat().st_size,
                "sha256": digest,
            })
        manifest = {
            "generated_at": time.time(),
            "files": files,
            "critical_entries": [],
            "runtime_rules": [],
        }
        target = self.root / "business_context" / "manifest.json"
        target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    @property
    def constitution_path(self) -> Path:
        return self.root / "constitution" / "rules.json"

    def constitution(self) -> list[dict]:
        if not self.constitution_path.exists():
            return []
        return json.loads(self.constitution_path.read_text(encoding="utf-8"))

    def add_constitution(
        self,
        rule: str,
        *,
        source: str,
        evidence_path: str = "",
        verify_attestation: Optional[Callable[[Path, dict], bool]] = None,
    ) -> dict:
        if source not in {"user-confirmed", "project-file", "machine-verified"}:
            raise ValueError("constitution source must be user-confirmed, project-file, or machine-verified")
        path = Path(evidence_path)
        if not rule.strip():
            raise ValueError("constitution rule cannot be empty")
        if not path.is_file():
            raise ValueError("constitution fact requires a durable evidence file")
        if source == "project-file":
            resolved = path.expanduser().resolve()
            if (
                self.project_root is None
                or resolved != self.project_root
                and self.project_root not in resolved.parents
            ):
                raise ValueError("project-file evidence must stay below project_root")
            try:
                source_text = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ValueError("project-file evidence must be readable text") from error
            if rule.strip() not in source_text:
                raise ValueError("constitution rule is not present in project-file evidence")
        else:
            try:
                attestation = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError("constitution attestation must be JSON") from error
            if verify_attestation is None or not verify_attestation(path, attestation):
                raise ValueError("constitution fact requires trusted host attestation")
            if (
                attestation.get("rule") != rule
                or attestation.get("source") != source
                or float(attestation.get("expires_at", 0)) <= time.time()
            ):
                raise ValueError("constitution attestation subject mismatch or expired")
        rules = self.constitution()
        row = {"id": f"rule-{len(rules)+1:03d}", "rule": rule, "source": source,
               "created_at": time.time(), "evidence_path": str(path),
               "evidence_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        rules.append(row)
        self.constitution_path.write_text(
            json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return row

    def emit_blocker(self, task_id: str, *, reason: str, evidence: Iterable[str],
                     options: Iterable[str], recommendation: str) -> Path:
        evidence = [item for item in evidence if item]
        options = [item for item in options if item]
        if not reason or not evidence or not options or not recommendation:
            raise ValueError("blocker requires reason, evidence, options, and recommendation")
        row = {
            "task_id": task_id,
            "reason": reason,
            "evidence": evidence,
            "options": options,
            "recommendation": recommendation,
            "created_at": time.time(),
        }
        path = self.root / "blockers" / f"{task_id}-{int(time.time())}.json"
        path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
